"""
SQLite data layer for Nexov Clinic.

Everything the voice agent and the dashboard know about doctors, slots and
appointments lives in one small SQLite file (``server/nexov.db``). The schema is
deliberately tiny:

    doctors       who works here and what they treat
    slots         30-minute openings in each doctor's diary (is_booked flag)
    appointments  a patient (identified by name + phone) holding a slot (status: confirmed / cancelled)

Phone numbers are NEVER stored. We keep only a keyed hash (HMAC-SHA256, to match a
returning caller) and the last four digits (to show "•••••3210"). A copy of the
database file therefore leaks no usable phone numbers. The HMAC key lives outside
the database (PHONE_HASH_KEY env var, or an auto-created server/.phone_key file).

On boot we make sure a rolling 7-day rota exists starting today. Existing
appointments are NEVER deleted by a restart or by the date rolling over: only
missing days are added. A handful of newly created slots are pre-booked by
"walk-in patients" to make the dashboard look like a real clinic on a real day.
A full wipe only happens through seed() (tests, /api/reset) or RESET_DB=1.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import sqlite3
import string
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import zoneinfo  
from zoneinfo import ZoneInfo

DB_PATH = Path(os.getenv("NEXOV_DB_PATH", Path(__file__).parent / "nexov.db"))
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TZ", "Asia/Kolkata"))
SLOT_MINUTES = 30
ROTA_DAYS = 7

# ---------------------------------------------------------------------------
# Mock roster. weekdays: 0 = Monday ... 6 = Sunday.
# ---------------------------------------------------------------------------
DOCTORS = [
    {
        "id": "rao",
        "name": "Dr. Ananya Rao",
        "specialty": "General Physician",
        "keywords": "general physician gp fever cold cough flu checkup family medicine",
        "room": "Room 1",
        "weekdays": [0, 1, 2, 3, 4, 5],
        "hours": [("09:00", "13:00"), ("16:00", "18:00")],
    },
    {
        "id": "mehta",
        "name": "Dr. Kabir Mehta",
        "specialty": "Cardiologist",
        "keywords": "cardiologist cardiology heart chest pain blood pressure bp",
        "room": "Room 4",
        "weekdays": [0, 2, 4],
        "hours": [("10:00", "14:00")],
    },
    {
        "id": "iyer",
        "name": "Dr. Meera Iyer",
        "specialty": "Dermatologist",
        "keywords": "dermatologist dermatology skin acne rash hair allergy",
        "room": "Room 2",
        "weekdays": [1, 3, 5],
        "hours": [("11:00", "15:00")],
    },
    {
        "id": "nair",
        "name": "Dr. Arjun Nair",
        "specialty": "Orthopaedic Surgeon",
        "keywords": "orthopaedic orthopedic ortho bone bones joint knee back spine fracture",
        "room": "Room 5",
        "weekdays": [0, 1, 3, 4],
        "hours": [("14:00", "18:00")],
    },
    {
        "id": "khan",
        "name": "Dr. Sara Khan",
        "specialty": "Paediatrician",
        "keywords": "paediatrician pediatrician paediatrics pediatrics child children kid kids baby infant",
        "room": "Room 3",
        "weekdays": [0, 1, 2, 3, 4, 5],
        "hours": [("09:30", "12:30")],
    },
    {
        "id": "sethi",
        "name": "Dr. Vikram Sethi",
        "specialty": "ENT Specialist",
        "keywords": "ent ear nose throat sinus hearing tonsils",
        "room": "Room 6",
        "weekdays": [1, 2, 4, 5],
        "hours": [("15:00", "19:00")],
    },
]

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now_local() -> datetime:
    return datetime.now(CLINIC_TZ)


def today_local() -> date:
    return now_local().date()


_KEY_FILE = Path(__file__).parent / ".phone_key"
_key_cache: bytes | None = None


def _hash_key() -> bytes:
    """Secret used to hash phone numbers. Env var wins; otherwise create and reuse a key file."""
    global _key_cache
    if _key_cache is None:
        env = os.getenv("PHONE_HASH_KEY")
        if env:
            _key_cache = env.encode()
        else:
            path = Path(os.getenv("PHONE_KEY_FILE", _KEY_FILE))
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_hex(32))
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
            _key_cache = path.read_text().strip().encode()
    return _key_cache


def phone_hash(digits: str) -> str:
    """Keyed hash of a normalised phone number (digits only)."""
    return hmac.new(_hash_key(), digits.encode(), hashlib.sha256).hexdigest()


def connect() -> sqlite3.Connection:
    """Return the process-wide connection, creating + seeding the DB on first use."""
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA foreign_keys = ON")
            _create_schema(_conn)
            if _needs_seed(_conn):
                seed(_conn)
            else:
                ensure_rota(_conn)
        return _conn


def _create_schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS doctors (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            specialty  TEXT NOT NULL,
            keywords   TEXT NOT NULL,
            room       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id  TEXT NOT NULL REFERENCES doctors(id),
            day        TEXT NOT NULL,          -- YYYY-MM-DD (clinic local time)
            start      TEXT NOT NULL,          -- HH:MM
            is_booked  INTEGER NOT NULL DEFAULT 0,
            UNIQUE (doctor_id, day, start)
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ref           TEXT NOT NULL UNIQUE, -- short human code, e.g. NX-7K2P
            patient_name  TEXT NOT NULL,        -- matched case-insensitively
            phone_hash    TEXT,                 -- HMAC-SHA256 of the digits; the number itself is never stored
            phone_last4   TEXT,                 -- for display only
            reason        TEXT,
            doctor_id     TEXT NOT NULL REFERENCES doctors(id),
            slot_id       INTEGER NOT NULL REFERENCES slots(id),
            status        TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled
            source        TEXT NOT NULL DEFAULT 'voice',      -- voice | walk-in
            created_at    TEXT NOT NULL,
            cancelled_at  TEXT
        );
        """
    )
    _migrate(c)
    c.execute("CREATE INDEX IF NOT EXISTS idx_appt_name ON appointments(LOWER(patient_name), status)")
    c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """Upgrade a database created when callers were identified by phone number."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
    for col in ("phone_hash", "phone_last4"):
        if col not in cols:
            c.execute(f"ALTER TABLE appointments ADD COLUMN {col} TEXT")
    if "phone" in cols:
        # Legacy raw numbers: convert to hash + last4, then drop the plaintext column.
        for r in c.execute("SELECT id, phone FROM appointments WHERE phone IS NOT NULL").fetchall():
            c.execute(
                "UPDATE appointments SET phone_hash = ?, phone_last4 = ? WHERE id = ?",
                (phone_hash(r["phone"]), r["phone"][-4:], r["id"]),
            )
        c.execute("DROP INDEX IF EXISTS idx_appt_phone")
        c.execute("UPDATE appointments SET patient_name = 'Patient' WHERE patient_name IS NULL OR TRIM(patient_name) = ''")
        c.execute("ALTER TABLE appointments DROP COLUMN phone")
        c.commit()
        # DROP COLUMN leaves the old plaintext in free pages of the file; VACUUM rewrites it away.
        c.execute("VACUUM")


def _needs_seed(c: sqlite3.Connection) -> bool:
    return os.getenv("RESET_DB", "").lower() in ("1", "true", "yes")


WALK_IN_NAMES = [
    "Rohan Kapoor", "Priya Nair", "Imran Sheikh", "Neha Verma", "Suresh Pillai", "Anjali Menon",
    "Farhan Ali", "Divya Reddy", "Karthik Subramanian", "Pooja Desai", "Vivek Joshi", "Sana Qureshi",
    "Manoj Tiwari", "Ritu Bansal", "Aditya Rao", "Lakshmi Iyer", "Harpreet Singh", "Meghna Das",
]


def ensure_rota(c: sqlite3.Connection) -> int:
    """Add any missing doctors/slots for today..today+ROTA_DAYS. Never deletes anything.

    Returns the number of slots created. Newly created slots are randomly pre-booked
    by walk-in patients (demo realism); slots that already exist are left untouched.
    """
    created: list[sqlite3.Row] = []
    with _lock:
        for d in DOCTORS:
            c.execute(
                """INSERT INTO doctors (id, name, specialty, keywords, room) VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, specialty=excluded.specialty,
                   keywords=excluded.keywords, room=excluded.room""",
                (d["id"], d["name"], d["specialty"], d["keywords"], d["room"]),
            )
            for offset in range(ROTA_DAYS):
                day = today_local() + timedelta(days=offset)
                if day.weekday() not in d["weekdays"]:
                    continue
                for start_s, end_s in d["hours"]:
                    t = datetime.combine(day, time.fromisoformat(start_s))
                    end = datetime.combine(day, time.fromisoformat(end_s))
                    while t < end:
                        cur = c.execute(
                            "INSERT OR IGNORE INTO slots (doctor_id, day, start) VALUES (?,?,?)",
                            (d["id"], day.isoformat(), t.strftime("%H:%M")),
                        )
                        if cur.rowcount == 1:
                            created.append((cur.lastrowid, d["id"]))
                        t += timedelta(minutes=SLOT_MINUTES)

        # Pre-book ~16% of the NEW slots for "walk-in" patients so the board looks alive.
        rng = random.Random(today_local().toordinal())
        for slot_id, doctor_id in created:
            if rng.random() < 0.16:
                fake = "9" + "".join(rng.choice(string.digits) for _ in range(9))
                c.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (slot_id,))
                c.execute(
                    """INSERT INTO appointments
                       (ref, patient_name, phone_hash, phone_last4, doctor_id, slot_id, status, source, created_at)
                       VALUES (?,?,?,?,?,?, 'confirmed', 'walk-in', ?)""",
                    (_new_ref(c), rng.choice(WALK_IN_NAMES), phone_hash(fake), fake[-4:], doctor_id, slot_id, now_local().isoformat()),
                )
        c.commit()
    return len(created)


def seed(c: sqlite3.Connection) -> None:
    """DESTRUCTIVE: wipe everything and rebuild a fresh rota (tests, /api/reset, RESET_DB=1)."""
    with _lock:
        c.executescript("DELETE FROM appointments; DELETE FROM slots; DELETE FROM doctors;")
        ensure_rota(c)


def _new_ref(c: sqlite3.Connection) -> str:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L — easy to read aloud
    while True:
        ref = "NX-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if not c.execute("SELECT 1 FROM appointments WHERE ref = ?", (ref,)).fetchone():
            return ref


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------
def list_doctors() -> list[dict]:
    c = connect()
    with _lock:
        return [dict(r) for r in c.execute("SELECT * FROM doctors ORDER BY name")]


def get_doctor(doctor_id: str) -> dict | None:
    c = connect()
    with _lock:
        r = c.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
        return dict(r) if r else None


def list_slots(doctor_id: str | None = None, day: str | None = None, include_past: bool = False) -> list[dict]:
    """All slots (booked and free), optionally filtered. Past slots today are hidden."""
    c = connect()
    q = "SELECT * FROM slots WHERE 1=1"
    args: list = []
    if doctor_id:
        q += " AND doctor_id = ?"
        args.append(doctor_id)
    if day:
        q += " AND day = ?"
        args.append(day)
    q += " ORDER BY day, start"
    with _lock:
        rows = [dict(r) for r in c.execute(q, args)]
    if include_past:
        return rows
    now = now_local()
    today, hhmm = now.date().isoformat(), now.strftime("%H:%M")
    return [r for r in rows if r["day"] > today or (r["day"] == today and r["start"] > hhmm)]


def find_slot(doctor_id: str, day: str, start: str) -> dict | None:
    c = connect()
    with _lock:
        r = c.execute(
            "SELECT * FROM slots WHERE doctor_id = ? AND day = ? AND start = ?",
            (doctor_id, day, start),
        ).fetchone()
        return dict(r) if r else None


APPT_SELECT = """
    SELECT a.*, s.day, s.start, d.name AS doctor_name, d.specialty, d.room
    FROM appointments a
    JOIN slots s   ON s.id = a.slot_id
    JOIN doctors d ON d.id = a.doctor_id
"""


def list_appointments(patient_name: str | None = None, phone_digits: str | None = None, status: str | None = None) -> list[dict]:
    c = connect()
    q = APPT_SELECT + " WHERE 1=1"
    args: list = []
    if patient_name:
        q += " AND LOWER(a.patient_name) = LOWER(?)"
        args.append(patient_name)
    if phone_digits:
        q += " AND a.phone_hash = ?"
        args.append(phone_hash(phone_digits))
    if status:
        q += " AND a.status = ?"
        args.append(status)
    q += " ORDER BY s.day, s.start"
    with _lock:
        return [dict(r) for r in c.execute(q, args)]


def get_appointment(appt_id: int) -> dict | None:
    c = connect()
    with _lock:
        r = c.execute(APPT_SELECT + " WHERE a.id = ?", (appt_id,)).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------------------
# Write helpers (each one is a single atomic transaction)
# ---------------------------------------------------------------------------
@dataclass
class BookingError(Exception):
    code: str
    message: str


def book(patient_name: str, phone_digits: str, slot_id: int, reason: str | None) -> dict:
    c = connect()
    with _lock:
        slot = c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if slot is None:
            raise BookingError("no_such_slot", "That slot does not exist.")
        # Guard against a double-booking race: flip the flag only if still free.
        cur = c.execute("UPDATE slots SET is_booked = 1 WHERE id = ? AND is_booked = 0", (slot_id,))
        if cur.rowcount == 0:
            c.rollback()
            raise BookingError("slot_taken", "That slot was just taken.")
        clash = c.execute(
            """SELECT 1 FROM appointments a JOIN slots s ON s.id = a.slot_id
               WHERE a.phone_hash = ? AND a.status = 'confirmed' AND s.day = ? AND s.start = ?""",
            (phone_hash(phone_digits), slot["day"], slot["start"]),
        ).fetchone()
        if clash:
            c.rollback()
            raise BookingError("caller_busy", "This patient already has an appointment at that exact time.")
        ref = _new_ref(c)
        cur = c.execute(
            """INSERT INTO appointments
               (ref, patient_name, phone_hash, phone_last4, reason, doctor_id, slot_id, status, source, created_at)
               VALUES (?,?,?,?,?,?,?, 'confirmed', 'voice', ?)""",
            (ref, patient_name, phone_hash(phone_digits), phone_digits[-4:], reason, slot["doctor_id"], slot_id, now_local().isoformat()),
        )
        c.commit()
        return get_appointment(cur.lastrowid)  # type: ignore[arg-type]


def cancel(appt_id: int) -> dict:
    c = connect()
    with _lock:
        appt = c.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        if appt is None or appt["status"] != "confirmed":
            raise BookingError("not_active", "That appointment is not active.")
        c.execute(
            "UPDATE appointments SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
            (now_local().isoformat(), appt_id),
        )
        c.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (appt["slot_id"],))  # slot reopens
        c.commit()
        return get_appointment(appt_id)  # type: ignore[return-value]

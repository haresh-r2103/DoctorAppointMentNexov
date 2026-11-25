"""Business-logic tests for the agent's tools. No audio, no LLM, no network."""

import pytest

import db
import tools


@pytest.fixture(autouse=True)
def fresh_db():
    db.seed(db.connect())


def first_free(doctor_id):
    return next(s for s in db.list_slots(doctor_id) if not s["is_booked"])


def test_phone_normalisation():
    assert tools.normalize_phone("+91 98765-43210") == "9876543210"
    assert tools.normalize_phone("098765 43210") == "9876543210"
    assert tools.normalize_phone("12") is None
    assert tools.spoken_phone("9876543210") == "9 8 7 6 5, 4 3 2 1 0"
    assert tools.mask_phone("3210") == "•••••3210"


def test_name_normalisation():
    assert tools.normalize_name("  asha   KAPOOR ") == "Asha Kapoor"
    assert tools.normalize_name("Dr. Rohan mehta") == "Rohan Mehta"
    assert tools.normalize_name("a") is None
    assert tools.normalize_name("12345") is None
    assert tools.mask_name("Asha Kapoor") == "Asha K."


def test_time_parsing():
    assert tools.parse_time("10:30 am") == "10:30"
    assert tools.parse_time("2 pm") == "14:00"
    assert tools.parse_time("14:30") == "14:30"
    assert tools.parse_time("3") == "15:00"  # clinic hours heuristic
    assert tools.parse_time("banana") is None


def test_doctor_matching_handles_speech():
    assert tools.match_doctor("Dr Mehta")["id"] == "mehta"
    assert tools.match_doctor("doctor meera")["id"] == "iyer"
    assert tools.match_doctor("skin doctor")["id"] == "iyer"
    assert tools.match_doctor("heart specialist")["id"] == "mehta"
    assert tools.match_doctor("Dr. Mehra")["id"] == "mehta"  # STT typo
    assert tools.match_doctor("xyzzy") is None


def test_availability_all_and_filtered():
    res = tools.check_doctor_availability()
    assert res["ok"] and len(res["results"]) == len(db.DOCTORS)
    res = tools.check_doctor_availability(doctor="dermatologist")
    assert [r["doctor"] for r in res["results"]] == ["Dr. Meera Iyer"]
    assert tools.check_doctor_availability(doctor="dr nobody")["error"] == "unknown_doctor"


def test_booking_requires_confirmed_name():
    s = first_free("rao")
    res = tools.book_appointment("asha kapoor", False, "9876543210", False, "Dr. Ananya Rao", s["day"], s["start"])
    assert res["error"] == "identity_not_confirmed"
    assert "Asha Kapoor" in res["message"] and "9 8 7 6 5" in res["message"]  # both need reading back
    only_phone = tools.book_appointment("asha kapoor", True, "9876543210", False, "Dr. Ananya Rao", s["day"], s["start"])
    assert only_phone["error"] == "identity_not_confirmed" and "Asha Kapoor" not in only_phone["message"]
    assert tools.book_appointment("", True, "9876543210", True, "Dr. Ananya Rao", s["day"], s["start"])["error"] == "invalid_name"
    assert tools.book_appointment("Asha Kapoor", True, "12", True, "Dr. Ananya Rao", s["day"], s["start"])["error"] == "invalid_phone"


def test_book_then_cancel_round_trip():
    s = first_free("rao")
    res = tools.book_appointment("asha kapoor", True, "9876543210", True, "Dr. Ananya Rao", s["day"], s["start"])
    assert res["ok"], res
    ref = res["appointment"]["ref"]
    assert db.find_slot("rao", s["day"], s["start"])["is_booked"] == 1

    # Same slot can't be booked twice.
    again = tools.book_appointment("Rohan Das", True, "9123456789", True, "Dr. Ananya Rao", s["day"], s["start"])
    assert again["error"] == "slot_unavailable"

    mine = tools.find_my_appointments("ASHA kapoor", True, "98765 43210", True)
    assert [a["ref"] for a in mine["appointments"]] == [ref]

    res = tools.cancel_appointment("ASHA kapoor", True, "98765 43210", True)
    assert res["ok"] and res["cancelled"]["ref"] == ref
    assert db.find_slot("rao", s["day"], s["start"])["is_booked"] == 0  # slot reopened
    assert tools.cancel_appointment("ASHA kapoor", True, "98765 43210", True)["error"] == "no_appointment"


def test_cancel_disambiguates_multiple():
    a = first_free("rao")
    b = next(s for s in db.list_slots("khan") if not s["is_booked"] and (s["day"], s["start"]) != (a["day"], a["start"]))
    assert tools.book_appointment("Nina Shah", True, "9000000001", True, "Dr. Ananya Rao", a["day"], a["start"])["ok"]
    assert tools.book_appointment("Nina Shah", True, "9000000001", True, "Dr. Sara Khan", b["day"], b["start"])["ok"]
    res = tools.cancel_appointment("Nina Shah", True, "9000000001", True)
    assert res["error"] == "multiple_appointments" and len(res["appointments"]) == 2
    res = tools.cancel_appointment("Nina Shah", True, "9000000001", True, doctor="Dr. Khan")
    assert res["ok"] and res["cancelled"]["doctor"] == "Dr. Sara Khan"


def test_same_caller_cannot_be_in_two_places():
    a = first_free("rao")
    clash = next((s for s in db.list_slots("khan") if not s["is_booked"] and (s["day"], s["start"]) == (a["day"], a["start"])), None)
    if clash is None:
        pytest.skip("no overlapping free slot in today's seed")
    assert tools.book_appointment("Omar Sheikh", True, "9000000002", True, "Dr. Ananya Rao", a["day"], a["start"])["ok"]
    assert tools.book_appointment("Omar Sheikh", True, "9000000002", True, "Dr. Sara Khan", a["day"], a["start"])["error"] == "caller_busy"


def test_formatting_is_portable():
    # "%-I" / "%-d" raise ValueError on Windows; these must work everywhere.
    assert tools.pretty_time("09:05") == "9:05 AM"
    assert tools.pretty_time("00:30") == "12:30 AM"
    assert tools.pretty_time("14:00") == "2:00 PM"
    assert "," in tools.pretty_day("2030-01-05")


def test_restart_never_wipes_bookings():
    s = first_free("rao")
    ref = tools.book_appointment("Asha Kapoor", True, "9876543210", True, "Dr. Ananya Rao", s["day"], s["start"])["appointment"]["ref"]
    db.ensure_rota(db.connect())  # what a restart (or a new day) does
    assert ref in [a["ref"] for a in db.list_appointments(patient_name="asha kapoor")]


def test_rota_rolls_forward_without_deleting(monkeypatch):
    from datetime import date, timedelta
    before = len(db.list_slots(include_past=True))
    ref = tools.book_appointment("Asha Kapoor", True, "9876543210", True, "Dr. Ananya Rao", **{
        "date": (s := first_free("rao"))["day"], "time": s["start"]})["appointment"]["ref"]
    monkeypatch.setattr(db, "today_local", lambda: date.fromisoformat(s["day"]) + timedelta(days=1))
    added = db.ensure_rota(db.connect())
    assert added > 0 and len(db.list_slots(include_past=True)) >= before
    assert ref in [a["ref"] for a in db.list_appointments()]


def test_old_phone_database_is_migrated():
    c = db.connect()
    c.executescript("""
        DROP INDEX IF EXISTS idx_appt_name;
        ALTER TABLE appointments ADD COLUMN phone TEXT;
        CREATE INDEX idx_appt_phone ON appointments(phone, status);
    """)
    db._migrate(c)
    assert "phone" not in {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}


def test_phone_number_is_never_stored():
    s = first_free("rao")
    res = tools.book_appointment("Asha Kapoor", True, "98765 43210", True, "Dr. Ananya Rao", s["day"], s["start"])
    assert res["ok"], res
    assert res["appointment"]["phone_masked"] == "•••••3210"
    # Nothing the LLM / dashboard sees contains the number...
    assert "9876543210" not in str(res) and "98765" not in str(res)
    # ...and neither does the database file: dump every value in every table.
    c = db.connect()
    dump = ""
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += str([tuple(r) for r in c.execute(f"SELECT * FROM {t}")])
    assert "9876543210" not in dump and "98765 43210" not in dump
    cols = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
    assert "phone" not in cols and {"phone_hash", "phone_last4"} <= cols


def test_name_alone_is_not_enough():
    s = first_free("rao")
    assert tools.book_appointment("Asha Kapoor", True, "9876543210", True, "Dr. Ananya Rao", s["day"], s["start"])["ok"]
    # Right name, wrong number -> can't see or cancel it.
    assert tools.find_my_appointments("Asha Kapoor", True, "9111111111", True)["appointments"] == []
    assert tools.cancel_appointment("Asha Kapoor", True, "9111111111", True)["error"] == "no_appointment"
    # Right number, wrong name -> same.
    assert tools.find_my_appointments("Someone Else", True, "9876543210", True)["appointments"] == []
    # Both right (any formatting of the number) -> found.
    assert len(tools.find_my_appointments("asha kapoor", True, "+91 98765-43210", True)["appointments"]) == 1


def test_logs_redact_identity():
    red = tools._redact({"patient_name": "Asha Kapoor", "phone_number": "98765 43210", "doctor": "x"})
    assert red == {"patient_name": "Asha K.", "phone_number": "•••••3210", "doctor": "x"}


def test_legacy_plaintext_phone_column_is_converted():
    c = db.connect()
    c.execute("ALTER TABLE appointments ADD COLUMN phone TEXT")
    c.execute("UPDATE appointments SET phone = '9876543210' WHERE id = (SELECT MIN(id) FROM appointments)")
    db._migrate(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
    assert "phone" not in cols
    row = c.execute("SELECT phone_hash, phone_last4 FROM appointments ORDER BY id LIMIT 1").fetchone()
    assert row["phone_last4"] == "3210" and row["phone_hash"] == db.phone_hash("9876543210")
    # The plaintext must be gone from the file itself, not just from the schema.
    assert b"9876543210" not in db.DB_PATH.read_bytes()

"""
The agent's tools.

Each tool has three parts:

1. A plain Python function (``check_doctor_availability`` etc.) holding the
   business logic. These take normal arguments and return a JSON-able dict, so
   they are unit-tested without any audio or LLM (see tests/test_tools.py).
2. A ``FunctionSchema`` describing the tool to the LLM: its name, what it does,
   and the JSON-schema of its arguments. Gemini reads this to decide when and
   how to call the tool.
3. A Pipecat handler (built by ``_handler``) that receives the LLM's call,
   runs the function, and hands the result back to the LLM via
   ``params.result_callback``. The LLM then turns that result into speech.

Guardrail: the three patient tools refuse to act unless the LLM passes
``patient_name_confirmed=true`` AND ``phone_number_confirmed=true``, i.e. it has
read both back and the caller said yes. The prompt asks for this; the code enforces it.

Identity: a caller is identified by full name + phone number, both spoken on the
call. The phone number is never stored: db.py keeps only a keyed hash (to match a
returning caller) and the last four digits (for display). Everything this module
hands to the LLM, the logs, the dashboard or the event stream carries only the
masked form ("•••••3210").
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger

import db
import events
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

MAX_DAYS_RETURNED = 3
MAX_SLOTS_PER_DAY = 8
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------------
# Parsing helpers: the LLM is asked for clean values, but voice is messy, so be
# forgiving about what we accept.
# ---------------------------------------------------------------------------
def normalize_name(raw: str | None) -> str | None:
    """'  asha   KAPOOR ' -> 'Asha Kapoor'. None when it can't be a person's name."""
    cleaned = re.sub(r"[^A-Za-z\u00C0-\u024F' .-]", " ", raw or "")
    cleaned = re.sub(r"^(mr|mrs|ms|miss|dr)\.?\s+", "", cleaned.strip(), flags=re.I)
    parts = cleaned.split()
    if not parts or sum(len(p) for p in parts) < 2 or len(" ".join(parts)) > 60:
        return None
    return " ".join(p[:1].upper() + p[1:].lower() if p.islower() or p.isupper() else p for p in parts)


def normalize_phone(raw: str | None) -> str | None:
    """'+91 98765-43210' -> '9876543210'. Returns None when it can't be a phone number."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if 7 <= len(digits) <= 15 else None


def spoken_phone(digits: str) -> str:
    """'9876543210' -> '9 8 7 6 5, 4 3 2 1 0' — how the agent should read it back."""
    if len(digits) == 10:
        return " ".join(digits[:5]) + ", " + " ".join(digits[5:])
    return " ".join(digits)


def mask_phone(last4: str | None) -> str:
    """'3210' -> '•••••3210'. The only form of a phone number that leaves the database layer."""
    return f"•••••{last4}" if last4 else ""


def mask_name(name: str | None) -> str:
    """'Asha Kapoor' -> 'Asha K.' for logs and the public dashboard API."""
    parts = (name or "").split()
    if len(parts) < 2:
        return parts[0] if parts else ""
    return f"{parts[0]} {parts[-1][0]}."


def parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    s = raw.strip().lower()
    today = db.today_local()
    if s in ("today", "now"):
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s in ("day after tomorrow", "overmorrow"):
        return today + timedelta(days=2)
    for i, name in enumerate(WEEKDAYS):
        if name in s:  # "next friday", "friday" -> the next one on or after today
            return today + timedelta(days=(i - today.weekday()) % 7)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_time(raw: str | None) -> str | None:
    """'10:30 am' / '2 pm' / '14:00' / '1430' -> 'HH:MM' (24h)."""
    if not raw:
        return None
    s = raw.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a m|p m)?$", s)
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not ampm and 1 <= h <= 7:  # "at 3" in a clinic almost always means 3 PM
        h += 12
    if not (0 <= h < 24 and 0 <= mins < 60):
        return None
    return f"{h:02d}:{mins:02d}"


def pretty_time(hhmm: str) -> str:
    t = datetime.strptime(hhmm, "%H:%M")  # no "%-I": that flag fails on Windows
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def pretty_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    today = db.today_local()
    label = f"{d.strftime('%A')}, {d.day} {d.strftime('%B')}"
    if d == today:
        return f"today ({label})"
    if d == today + timedelta(days=1):
        return f"tomorrow ({label})"
    return label


def match_doctor(query: str | None) -> dict | None:
    """Fuzzy-match a spoken doctor name or specialty ('Dr Mehta', 'skin doctor')."""
    if not query:
        return None
    q = re.sub(r"\b(dr|doctor|doc)\b\.?", " ", query.lower())
    q = re.sub(r"[^a-z ]", " ", q).strip()
    if not q:
        return None
    best, best_score = None, 0.0
    for d in db.list_doctors():
        name_parts = re.sub(r"^dr\.?\s*", "", d["name"].lower()).split()
        candidates = [" ".join(name_parts), *name_parts, d["specialty"].lower(), *d["keywords"].split()]
        for word in q.split() + [q]:
            for cand in candidates:
                score = difflib.SequenceMatcher(None, word, cand).ratio()
                if score > best_score:
                    best, best_score = d, score
    return best if best_score >= 0.72 else None


def _appt_public(a: dict) -> dict:
    """The shape of an appointment we hand to the LLM and the UI."""
    return {
        "ref": a["ref"],
        "status": a["status"],
        "doctor_id": a["doctor_id"],
        "doctor": a["doctor_name"],
        "specialty": a["specialty"],
        "room": a["room"],
        "date": a["day"],
        "time": a["start"],
        "when_spoken": f"{pretty_day(a['day'])} at {pretty_time(a['start'])}",
        "patient_name": a["patient_name"],
        "phone_masked": mask_phone(a.get("phone_last4")),
    }


def _identity_guard(
    patient_name: str | None,
    name_confirmed: bool | None,
    phone_number: str | None,
    phone_confirmed: bool | None,
) -> tuple[tuple[str, str] | None, dict | None]:
    """Validate and confirm BOTH name and phone. Returns ((name, phone_digits), None) or (None, error)."""
    name = normalize_name(patient_name)
    if not name:
        return None, {
            "ok": False,
            "error": "invalid_name",
            "message": "Ask the caller for their full name (first and last), and to spell it if it's unusual.",
        }
    phone = normalize_phone(phone_number)
    if not phone:
        return None, {
            "ok": False,
            "error": "invalid_phone",
            "message": "That doesn't look like a full phone number. Ask the caller to say it again, digit by digit.",
        }
    pending = []
    if not name_confirmed:
        pending.append(f"the name as '{name}'")
    if not phone_confirmed:
        pending.append(f"the number as '{spoken_phone(phone)}'")
    if pending:
        return None, {
            "ok": False,
            "error": "identity_not_confirmed",
            "message": "Read back " + " and ".join(pending) + ", wait for the caller to say yes, then call again "
            "with patient_name_confirmed=true and phone_number_confirmed=true.",
        }
    return (name, phone), None


# ---------------------------------------------------------------------------
# 1) check_doctor_availability
# ---------------------------------------------------------------------------
def check_doctor_availability(doctor: str | None = None, date: str | None = None) -> dict:
    doctors = db.list_doctors()
    if doctor:
        found = match_doctor(doctor)
        if not found:
            return {
                "ok": False,
                "error": "unknown_doctor",
                "message": f"No doctor matches '{doctor}'.",
                "doctors_available": [f"{d['name']} ({d['specialty']})" for d in doctors],
            }
        doctors = [found]

    day = parse_day(date)
    if date and not day:
        return {"ok": False, "error": "bad_date", "message": f"Couldn't understand the date '{date}'."}

    results = []
    for d in doctors:
        free = [s for s in db.list_slots(d["id"], day.isoformat() if day else None) if not s["is_booked"]]
        by_day: dict[str, list[str]] = {}
        for s in free:
            by_day.setdefault(s["day"], []).append(s["start"])
        days = [
            {
                "date": iso,
                "date_spoken": pretty_day(iso),
                "free_slots": [pretty_time(t) for t in times[:MAX_SLOTS_PER_DAY]],
                "more_slots_not_listed": max(0, len(times) - MAX_SLOTS_PER_DAY),
            }
            for iso, times in list(by_day.items())[:MAX_DAYS_RETURNED]
        ]
        results.append({"doctor_id": d["id"], "doctor": d["name"], "specialty": d["specialty"], "room": d["room"], "availability": days})

    any_free = any(r["availability"] for r in results)
    out: dict[str, Any] = {"ok": True, "date_requested": day.isoformat() if day else None, "results": results}
    if not any_free:
        out["message"] = "No free slots for that request. Offer another day or another doctor."
    return out


# ---------------------------------------------------------------------------
# 2) book_appointment
# ---------------------------------------------------------------------------
def book_appointment(
    patient_name: str,
    patient_name_confirmed: bool,
    phone_number: str,
    phone_number_confirmed: bool,
    doctor: str,
    date: str,
    time: str,
    reason: str | None = None,
) -> dict:
    ident, err = _identity_guard(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed)
    if err:
        return err
    name, phone = ident
    doc = match_doctor(doctor)
    if not doc:
        return {"ok": False, "error": "unknown_doctor", "message": f"No doctor matches '{doctor}'."}
    day, hhmm = parse_day(date), parse_time(time)
    if not day or not hhmm:
        return {"ok": False, "error": "bad_datetime", "message": "Need a clear date and time for the booking."}

    slot = db.find_slot(doc["id"], day.isoformat(), hhmm)
    upcoming = [s for s in db.list_slots(doc["id"], day.isoformat()) if not s["is_booked"]]
    # `upcoming` already excludes booked slots and times that have passed today.
    if not slot or slot["id"] not in {s["id"] for s in upcoming}:
        return {
            "ok": False,
            "error": "slot_unavailable",
            "message": f"{doc['name']} has no free slot at {pretty_time(hhmm)} on {pretty_day(day.isoformat())}.",
            "nearest_free_slots_that_day": [pretty_time(s["start"]) for s in upcoming[:5]],
        }
    try:
        appt = db.book(name, phone, slot["id"], reason)
    except db.BookingError as e:
        return {"ok": False, "error": e.code, "message": e.message}

    public = _appt_public(appt)
    events.publish("appointment.booked", {"appointment": {**appt, "patient_name": mask_name(appt["patient_name"])}})
    logger.info(f"Booked {public['ref']} for {mask_name(name)} with {doc['name']} {public['when_spoken']}")
    return {
        "ok": True,
        "appointment": public,
        "say": f"Confirmed with {public['doctor']} {public['when_spoken']}, {public['room']}. "
        f"Reference {' '.join(public['ref'].replace('-', ''))}.",
    }


# ---------------------------------------------------------------------------
# 3) cancel_appointment
# ---------------------------------------------------------------------------
def cancel_appointment(
    patient_name: str,
    patient_name_confirmed: bool,
    phone_number: str,
    phone_number_confirmed: bool,
    appointment_ref: str | None = None,
    doctor: str | None = None,
    date: str | None = None,
) -> dict:
    ident, err = _identity_guard(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed)
    if err:
        return err
    name, phone = ident

    active = db.list_appointments(patient_name=name, phone_digits=phone, status="confirmed")
    if not active:
        return {
            "ok": False,
            "error": "no_appointment",
            "message": f"No active appointment matches the name {name} with that phone number.",
        }

    # Narrow down by whatever the caller gave us.
    matches = active
    if appointment_ref:
        ref = re.sub(r"[^A-Z0-9]", "", appointment_ref.upper()).removeprefix("NX")
        matches = [a for a in matches if a["ref"].replace("NX-", "") == ref] or matches
    if doctor and (doc := match_doctor(doctor)):
        matches = [a for a in matches if a["doctor_id"] == doc["id"]] or matches
    if date and (day := parse_day(date)):
        matches = [a for a in matches if a["day"] == day.isoformat()] or matches

    if len(matches) > 1:
        return {
            "ok": False,
            "error": "multiple_appointments",
            "message": "This number has more than one appointment. Ask which one to cancel, "
            "then call again with the doctor or date.",
            "appointments": [_appt_public(a) for a in matches],
        }

    appt = db.cancel(matches[0]["id"])
    public = _appt_public(appt)
    events.publish("appointment.cancelled", {"appointment": {**appt, "patient_name": mask_name(appt["patient_name"])}})
    logger.info(f"Cancelled {public['ref']} for {mask_name(name)}")
    return {"ok": True, "cancelled": public, "say": f"Cancelled your {public['when_spoken']} visit with {public['doctor']}."}


# ---------------------------------------------------------------------------
# 4) (extra) find_my_appointments — "when is my appointment?"
# ---------------------------------------------------------------------------
def find_my_appointments(
    patient_name: str, patient_name_confirmed: bool, phone_number: str, phone_number_confirmed: bool
) -> dict:
    ident, err = _identity_guard(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed)
    if err:
        return err
    name, phone = ident
    active = db.list_appointments(patient_name=name, phone_digits=phone, status="confirmed")
    return {"ok": True, "patient_name": name, "appointments": [_appt_public(a) for a in active]}


# ---------------------------------------------------------------------------
# Schemas the LLM sees
# ---------------------------------------------------------------------------
_PATIENT = {
    "patient_name": {
        "type": "string",
        "description": "The caller's full name (first and last) exactly as they said it, e.g. 'Asha Kapoor'.",
    },
    "patient_name_confirmed": {
        "type": "boolean",
        "description": "True only after you read the name back to the caller and they said it is correct.",
    },
    "phone_number": {
        "type": "string",
        "description": "The caller's phone number exactly as they spoke it, digits only, e.g. '9876543210'.",
    },
    "phone_number_confirmed": {
        "type": "boolean",
        "description": "True only after you read the number back to the caller and they said it is correct.",
    },
}

SCHEMAS = [
    FunctionSchema(
        name="check_doctor_availability",
        description="Look up free appointment slots. Use it before offering times. "
        "Filter by a doctor's name or a specialty, and/or a date.",
        properties={
            "doctor": {
                "type": "string",
                "description": "Doctor's name or a specialty, e.g. 'Dr. Mehta' or 'dermatologist'. Omit for all doctors.",
            },
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD. Omit to get the next days with availability.",
            },
        },
        required=[],
    ),
    FunctionSchema(
        name="book_appointment",
        description="Book a free slot for the caller. Only call after the caller picked a slot "
        "and confirmed their read-back name and phone number.",
        properties={
            **_PATIENT,
            "doctor": {"type": "string", "description": "Doctor's full name, e.g. 'Dr. Kabir Mehta'."},
            "date": {"type": "string", "description": "Date in YYYY-MM-DD."},
            "time": {"type": "string", "description": "Start time in 24h HH:MM, e.g. '14:30'."},
            "reason": {"type": "string", "description": "Short reason for the visit if mentioned."},
        },
        required=["patient_name", "patient_name_confirmed", "phone_number", "phone_number_confirmed", "doctor", "date", "time"],
    ),
    FunctionSchema(
        name="cancel_appointment",
        description="Cancel the caller's existing appointment, found by their confirmed name and phone number. "
        "If they have several, pass the doctor or date to pick one.",
        properties={
            **_PATIENT,
            "appointment_ref": {"type": "string", "description": "Booking reference like 'NX-7K2P', if known."},
            "doctor": {"type": "string", "description": "Doctor of the appointment to cancel, if known."},
            "date": {"type": "string", "description": "Date (YYYY-MM-DD) of the appointment to cancel, if known."},
        },
        required=["patient_name", "patient_name_confirmed", "phone_number", "phone_number_confirmed"],
    ),
    FunctionSchema(
        name="find_my_appointments",
        description="List the caller's upcoming appointments by their confirmed name and phone number.",
        properties=_PATIENT,
        required=["patient_name", "patient_name_confirmed", "phone_number", "phone_number_confirmed"],
    ),
]

TOOLS = ToolsSchema(standard_tools=SCHEMAS)

IMPLEMENTATIONS = {
    "check_doctor_availability": check_doctor_availability,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
    "find_my_appointments": find_my_appointments,
}


def _redact(args: dict) -> dict:
    """Keep names and phone numbers out of the logs."""
    out = {}
    for k, v in args.items():
        if k == "patient_name" and isinstance(v, str):
            v = mask_name(v)
        elif k == "phone_number":
            v = mask_phone((normalize_phone(str(v)) or "")[-4:])
        out[k] = v
    return out


def _handler(fn):
    """Adapt a plain function into a Pipecat function-call handler."""

    async def handler(params: FunctionCallParams):
        args = dict(params.arguments or {})
        logger.info(f"tool → {params.function_name}({_redact(args)})")
        try:
            result = fn(**args)
        except TypeError as e:  # LLM sent an unexpected/missing argument
            result = {"ok": False, "error": "bad_arguments", "message": str(e)}
        except Exception as e:  # never let a tool crash the call
            logger.exception(e)
            result = {"ok": False, "error": "internal", "message": "Something went wrong on our side."}
        logger.info(f"tool ← {params.function_name}: ok={result.get('ok')} error={result.get('error')}")
        await params.result_callback(result)

    return handler


def register_tools(llm) -> None:
    for name, fn in IMPLEMENTATIONS.items():
        llm.register_function(name, _handler(fn))

"""Bounded, read-only acceptance checks against existing AI-created bookings.

This does not create a test patient or appointment. Sensitive fields stay in the
server process; callers receive only execution/verification flags and counts.
"""
from __future__ import annotations

import asyncio
from datetime import date as date_cls, time as time_cls
import json
from typing import Any

import crm
import state

_MAX_SESSION_ROWS = 200
_MAX_BOOKING_READBACKS = 3
_READBACK_TIMEOUT_SECONDS = 20.0


def _text(value: Any, *, limit: int = 256) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    return text if 0 < len(text) <= limit else ""


def _candidate(session: dict[str, Any], chat_id: Any) -> dict[str, Any] | None:
    if session.get("booking_confirmed") is not True:
        return None
    # These fields are written by the AI booking tool. A CRM lookup alone or
    # an operator's existing appointment is not evidence of an AI-created row.
    if session.get("created_by_ai") is not True and not _text(session.get("crm_booking_id")):
        return None
    appointment = session.get("appointment")
    appointment = appointment if isinstance(appointment, dict) else {}
    slot = session.get("selected_slot")
    slot = slot if isinstance(slot, dict) else {}
    appointment_id = _text(
        session.get("crm_booking_id") or session.get("appointment_id")
        or appointment.get("appointmentId") or appointment.get("id")
        or appointment.get("bookingId"), limit=200,
    )
    phone = crm.normalize_phone(_text(session.get("phone") or session.get("phone_normalized") or chat_id))
    date = _text(session.get("appointment_date") or session.get("selected_date") or slot.get("date") or appointment.get("date"))
    time_start = _text(session.get("appointment_time") or session.get("selected_time") or slot.get("time_start") or slot.get("timeStart") or appointment.get("timeStart"))
    doctor_login = _text(session.get("appointment_doctor_login") or session.get("selected_doctor_login") or slot.get("doctor_login") or slot.get("doctorLogin"), limit=128)
    doctor_name = _text(session.get("appointment_doctor_name") or session.get("selected_doctor_name") or slot.get("doctor_name") or slot.get("doctorName"))
    if not all((appointment_id, phone, date, time_start, doctor_login)):
        return None
    try:
        if date_cls.fromisoformat(date).isoformat() != date or len(time_start) != 5:
            return None
        time_cls.fromisoformat(time_start)
    except ValueError:
        return None
    return {
        "response": {"appointmentId": appointment_id}, "phone": phone,
        "doctor_login": doctor_login, "doctor_name": doctor_name or None,
        "date": date, "time_start": time_start,
    }


def _load_candidates() -> list[dict[str, Any]]:
    with state._connect() as conn:
        rows = conn.execute(
            "SELECT chat_id,data_json FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (_MAX_SESSION_ROWS,),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        try:
            session = json.loads(row["data_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(session, dict):
            continue
        candidate = _candidate(session, row["chat_id"])
        if candidate is None:
            continue
        identity = str(candidate["response"]["appointmentId"])
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)
        if len(candidates) >= _MAX_BOOKING_READBACKS:
            break
    return candidates


async def verify_existing_ai_booking() -> dict[str, Any]:
    """Verify a previously created AI booking with no writes or patient messages.

    A successful result proves a persisted readback of an existing row. It does
    not prove that a new end-to-end CREATE was executed in a test environment.
    """
    result: dict[str, Any] = {
        "executed": False, "verified": False, "attempted_records": 0,
        "reason": "no_confirmed_ai_booking_available",
    }
    try:
        candidates = _load_candidates()
    except Exception:
        result["reason"] = "session_read_unavailable"
        return result
    if not candidates:
        return result

    async def _readback() -> None:
        errors = 0
        for candidate in candidates:
            result["attempted_records"] += 1
            result["executed"] = True
            try:
                verification = await crm.verify_saved_booking(**candidate)
                if isinstance(verification, dict) and verification.get("verified") is True:
                    result["verified"] = True
                    result["reason"] = "existing_ai_booking_readback_verified"
                    return
            except Exception:
                # Exception strings may contain a request URL or patient data.
                errors += 1
        result["reason"] = "booking_readback_unavailable" if errors == len(candidates) else "booking_readback_unverified"

    try:
        await asyncio.wait_for(_readback(), timeout=_READBACK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        result["reason"] = "readback_timeout"
    return result

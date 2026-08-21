from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm
import dialog
import state


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def appointment() -> dict[str, Any]:
    return {
        "id": 777,
        "date": "2099-01-02",
        "timeStart": "09:20",
        "doctorLogin": "doctor1",
        "doctorName": "Тестовый врач",
        "status": "confirmed",
    }


def seed_booked(chat_id: str, *, language: str = "ru") -> dict[str, Any]:
    state.init_db()
    state.reset_session(chat_id)
    s = state.get_session(chat_id)
    appt = appointment()
    s.update(
        {
            "chat_id": chat_id,
            "phone": "77011234567",
            "language": language,
            "created_by_ai": True,
            "booking_confirmed": True,
            "booking_confirmed_at": datetime.now(timezone.utc).isoformat(),
            "post_booking_support_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "post_booking_support_active": True,
            "is_booked_client": True,
            "step": "booked",
            "appointment_id": appt["id"],
            "appointment_date": appt["date"],
            "appointment_time": appt["timeStart"],
            "appointment_doctor_login": appt["doctorLogin"],
            "appointment_doctor_name": appt["doctorName"],
            "nearest_active_appointment": dict(appt),
            "crm_patient_state": "ACTIVE_BOOKING",
            "ai_lead_started": True,
            "first_touch_info_sent": True,
        }
    )
    state.save_session(chat_id, s)
    return s


def patch_crm(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"lookup": [], "slots": [], "reschedule": [], "cancel": [], "book": []}
    appt = appointment()

    async def fake_lookup(phone: str) -> dict[str, Any]:
        calls["lookup"].append(phone)
        return {
            "ok": True,
            "found": True,
            "isNew": False,
            "hasActiveAppointment": True,
            "appointment": dict(appt),
            "appointments": [dict(appt)],
        }

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "ok": True,
            "date": date,
            "availability": [
                {
                    "doctorLogin": doctor_login or "doctor1",
                    "doctorName": "Тестовый врач",
                    "date": date,
                    "availableSlots": ["14:00", "15:20"],
                }
            ],
        }

    async def fake_reschedule(**kwargs: Any) -> dict[str, Any]:
        calls["reschedule"].append(kwargs)
        return {
            "ok": True,
            "rescheduled": True,
            "appointmentId": kwargs.get("appointment_id"),
            "date": kwargs.get("new_date"),
            "timeStart": kwargs.get("new_time_start"),
        }

    async def forbidden_cancel(**kwargs: Any) -> dict[str, Any]:
        calls["cancel"].append(kwargs)
        raise AssertionError("reschedule flow must never cancel the original appointment")

    async def forbidden_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        raise AssertionError("reschedule flow must never create a second booking")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(crm, "reschedule_appointment", fake_reschedule)
    monkeypatch.setattr(crm, "cancel_appointment", forbidden_cancel)
    monkeypatch.setattr(crm, "book_appointment", forbidden_book)
    return calls


def test_reschedule_request_only_enters_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    session = seed_booked("reschedule-enter")

    answer = run(dialog._post_booking_support_answer("reschedule-enter", session["phone"], session, "хочу перенести запись"))

    assert session["reschedule_mode"] is True
    assert session["reschedule_step"] == "date"
    assert session["original_appointment"]["id"] == 777
    assert calls["slots"] == []
    assert calls["reschedule"] == []
    assert calls["cancel"] == []
    assert calls["book"] == []
    assert "день" in answer.lower() or "дат" in answer.lower()


def test_reschedule_date_uses_same_doctor_and_only_crm_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    session = seed_booked("reschedule-date")
    session.update({"reschedule_mode": True, "reschedule_step": "date", "original_appointment": appointment()})
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2099-01-03")

    answer = run(dialog._post_booking_support_answer("reschedule-date", session["phone"], session, "завтра"))

    assert calls["slots"] == [{"date": "2099-01-03", "doctor_login": "doctor1"}]
    assert session["reschedule_step"] == "time"
    assert [dialog._slot_time(s) for s in session["reschedule_slots"]] == ["14:00", "15:20"]
    assert "14:00" in answer and "15:20" in answer
    assert calls["reschedule"] == []
    assert calls["cancel"] == []
    assert calls["book"] == []


def test_reschedule_time_calls_only_existing_reschedule_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    session = seed_booked("reschedule-time")
    session.update(
        {
            "reschedule_mode": True,
            "reschedule_step": "time",
            "original_appointment": appointment(),
            "reschedule_date": "2099-01-03",
            "reschedule_slots": [
                {"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": "2099-01-03", "timeStart": "14:00", "time": "14:00"},
                {"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": "2099-01-03", "timeStart": "15:20", "time": "15:20"},
            ],
        }
    )

    answer = run(dialog._post_booking_support_answer("reschedule-time", session["phone"], session, "2 вариант"))

    assert len(calls["reschedule"]) == 1
    payload = calls["reschedule"][0]
    assert payload["phone"] == "77011234567"
    assert payload["appointment_id"] == 777
    assert payload["new_date"] == "2099-01-03"
    assert payload["new_time_start"] == "15:20"
    assert calls["cancel"] == []
    assert calls["book"] == []
    assert session["appointment_id"] == 777
    assert session["appointment_date"] == "2099-01-03"
    assert session["appointment_time"] == "15:20"
    assert session["booking_confirmed"] is True
    assert session["step"] == "booked"
    assert session.get("reschedule_mode") is not True
    assert session["rescheduled"] is True
    assert "перенес" in answer.lower() or "ауыстыр" in answer.lower()


def test_unoffered_time_never_reschedules(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    session = seed_booked("reschedule-unoffered")
    session.update(
        {
            "reschedule_mode": True,
            "reschedule_step": "time",
            "original_appointment": appointment(),
            "reschedule_slots": [
                {"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": "2099-01-03", "timeStart": "14:00", "time": "14:00"},
            ],
        }
    )

    answer = run(dialog._post_booking_support_answer("reschedule-unoffered", session["phone"], session, "18:40"))

    assert calls["reschedule"] == []
    assert calls["cancel"] == []
    assert calls["book"] == []
    assert session["reschedule_mode"] is True
    assert session["reschedule_step"] == "time"
    assert "14:00" in answer


def test_reschedule_crm_failure_fails_closed_and_preserves_original(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    session = seed_booked("reschedule-fail")
    original = appointment()
    session.update(
        {
            "reschedule_mode": True,
            "reschedule_step": "time",
            "original_appointment": dict(original),
            "reschedule_slots": [
                {"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": "2099-01-03", "timeStart": "14:00", "time": "14:00"},
            ],
        }
    )

    async def fail_reschedule(**kwargs: Any) -> dict[str, Any]:
        calls["reschedule"].append(kwargs)
        raise crm.CRMError("reschedule unavailable")

    monkeypatch.setattr(crm, "reschedule_appointment", fail_reschedule)

    answer = run(dialog._post_booking_support_answer("reschedule-fail", session["phone"], session, "14:00"))

    assert len(calls["reschedule"]) == 1
    assert calls["cancel"] == []
    assert calls["book"] == []
    assert session["appointment_id"] == original["id"]
    assert session["appointment_date"] == original["date"]
    assert session["appointment_time"] == original["timeStart"]
    assert session["manual_takeover"] is True
    assert session["escalated"] is True
    assert session["booking_confirmed"] is True
    assert "администратор" in answer.lower() or "әкімші" in answer.lower()


def test_handle_message_routes_plain_next_message_back_into_reschedule_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_crm(monkeypatch)
    chat_id = "reschedule-routing"
    seed_booked(chat_id)
    s = state.get_session(chat_id)
    s.update({"reschedule_mode": True, "reschedule_step": "date", "original_appointment": appointment()})
    state.save_session(chat_id, s)
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2099-01-03")

    answer = run(dialog.handle_message(chat_id, "77011234567", "завтра"))
    saved = state.get_session(chat_id)

    assert calls["slots"] == [{"date": "2099-01-03", "doctor_login": "doctor1"}]
    assert saved["reschedule_mode"] is True
    assert saved["reschedule_step"] == "time"
    assert "14:00" in answer
    assert "вы уже записаны" not in answer.lower()


def test_reschedule_prompts_follow_kazakh_dialog_language(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_crm(monkeypatch)
    session = seed_booked("reschedule-kk", language="kk")

    answer = run(dialog._post_booking_support_answer("reschedule-kk", session["phone"], session, "жазбаны ауыстырғым келеді"))

    assert session["reschedule_mode"] is True
    assert session["reschedule_step"] == "date"
    assert "күн" in answer.lower() or "уақыт" in answer.lower()

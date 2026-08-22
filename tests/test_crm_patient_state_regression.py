from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm
import dialog
import state


def run(coro):
    return asyncio.run(coro)


def reset(chat_id: str, data: dict[str, Any] | None = None) -> None:
    state.init_db()
    state.save_session(chat_id, data or {})


RETURNING_RESPONSE = {
    "ok": True,
    "status_code": 200,
    "found": True,
    "isNew": False,
    "patient": {"name": "Бекзат Сакулбасов"},
    "lead": {"id": 40885, "status": "НЕ_ПРИШЕЛ", "complaint": "Правая коленный сустав", "request": "Колени"},
    "lastAppointment": {"id": 3685, "date": "2026-07-06", "timeStart": "09:20", "doctorName": "Кумарова Айман Адылбековна", "status": "НЕ_ПРИШЁЛ"},
    "hasActiveAppointment": False,
    "appointments": [],
    "appointment": None,
}


async def returning_lookup(phone: str) -> dict[str, Any]:
    return dict(RETURNING_RESPONSE)


async def new_lookup(phone: str) -> dict[str, Any]:
    return {"ok": True, "status_code": 200, "found": False, "isNew": True, "patient": None, "lead": None, "lastAppointment": None, "appointment": None, "appointments": [], "hasActiveAppointment": False}


async def active_lookup(phone: str) -> dict[str, Any]:
    return {"ok": True, "status_code": 200, "found": True, "isNew": False, "hasActiveAppointment": True, "appointment": {"id": 99, "date": "2099-01-02", "timeStart": "12:30", "doctorName": "Тестовый врач", "status": "confirmed"}, "appointments": []}


async def failing_lookup(phone: str) -> dict[str, Any]:
    raise RuntimeError("crm down")


def assert_silent_old_lead(chat_id: str, answer: str, *, reason: str = "old_lead_from_crm") -> dict[str, Any]:
    session = state.get_session(chat_id)
    assert answer == ""
    assert session["silent_old_lead"] is True
    assert session["no_reply_reason"] == reason
    assert session["first_touch_allowed"] is False
    assert session["guard_decision"]["should_send_wazzup"] is False
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer
    assert "Активной записи сейчас не вижу" not in answer
    return session


def test_1_new_patient_allows_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_lookup)
    chat_id = "crm_state_new"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000003", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_patient_state"] == "NEW_PATIENT"
    assert session["first_touch_allowed"] is True
    assert answer
    assert "что Вас беспокоит" in answer
    assert answer != dialog.FIRST_TOUCH_CLINIC_INFO_RU


def test_2_returning_patient_greeting_is_silent(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_hello"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000000", "Здравствуйте"))
    session = assert_silent_old_lead(chat_id, answer)
    assert session["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    assert session["first_touch_blocked_reason"] == "returning_patient_old_lead"


def test_3_active_booking_is_silent(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup)
    chat_id = "crm_state_active"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000004", "Здравствуйте"))
    session = assert_silent_old_lead(chat_id, answer, reason="active_booking_old_lead")
    assert session["crm_patient_state"] == "ACTIVE_BOOKING"


def test_4_returning_patient_wants_booking_is_silent(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_booking"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000002", "Хочу записаться"))
    assert_silent_old_lead(chat_id, answer)


def test_5_returning_patient_address_is_silent(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_address"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000005", "Адрес"))
    assert_silent_old_lead(chat_id, answer)


def test_6_crm_lookup_failed_fails_closed(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", failing_lookup)
    chat_id = "crm_state_failed"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000006", "Здравствуйте"))
    session = state.get_session(chat_id)
    assert answer == ""
    assert session["manual_takeover"] is True
    assert session["no_reply_reason"] == "crm_lookup_failed"
    assert session["first_touch_allowed"] is False
    assert session["guard_decision"]["should_send_wazzup"] is False


def test_7_ai_created_booking_support_within_one_hour(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup)
    chat_id = "crm_state_ai_booking_support"
    reset(chat_id, {"chat_id": chat_id, "created_by_ai": True, "booking_confirmed": True, "booking_confirmed_at": datetime.now(timezone.utc).isoformat(), "post_booking_support_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "appointment_id": 99, "appointment_date": "2099-01-02", "appointment_time": "12:30", "step": "booked"})

    answer = run(dialog.handle_message(chat_id, "77000000007", "адрес"))
    session = state.get_session(chat_id)
    assert session["post_booking_support_active"] is True
    assert "Кабанбай" in answer


def test_8_non_ai_crm_appointment_is_silent(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup)
    chat_id = "crm_state_non_ai_booking"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000008", "адрес"))
    assert_silent_old_lead(chat_id, answer, reason="active_booking_old_lead")

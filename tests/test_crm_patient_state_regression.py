from __future__ import annotations

import asyncio
import sys
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


def reset(chat_id: str) -> None:
    state.init_db()
    state.save_session(chat_id, {})


RETURNING_RESPONSE = {
    "ok": True,
    "status_code": 200,
    "found": True,
    "isNew": False,
    "patient": {"name": "Бекзат Сакулбасов"},
    "lead": {
        "id": 40885,
        "status": "НЕ_ПРИШЕЛ",
        "complaint": "Правая коленный сустав",
        "request": "Колени",
    },
    "lastAppointment": {
        "id": 3685,
        "date": "2026-07-06",
        "timeStart": "09:20",
        "doctorName": "Кумарова Айман Адылбековна",
        "status": "НЕ_ПРИШЁЛ",
    },
    "hasActiveAppointment": False,
    "appointments": [],
    "appointment": None,
}


async def returning_lookup(phone: str) -> dict[str, Any]:
    return dict(RETURNING_RESPONSE)


async def new_lookup(phone: str) -> dict[str, Any]:
    return {"ok": True, "status_code": 200, "found": False, "isNew": True, "patient": None, "lead": None, "appointment": None, "appointments": [], "hasActiveAppointment": False}


async def active_lookup(phone: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status_code": 200,
        "hasActiveAppointment": True,
        "appointment": {"id": 99, "date": "2099-01-02", "timeStart": "12:30", "doctorName": "Тестовый врач", "status": "confirmed"},
        "appointments": [],
    }


def test_returning_patient_greeting_no_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_hello"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000000", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    assert session["first_touch_allowed"] is False
    assert session["first_touch_blocked_reason"] == "returning_patient_in_crm"
    assert "Вы уже обращались" in answer
    assert "Активной записи сейчас не вижу" in answer
    assert "подберу ближайшее свободное время" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer
    assert answer.strip()


def test_returning_patient_booking_status_question(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_status"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000001", "я записан?"))

    assert "Активной записи сейчас не вижу" in answer
    assert "Могу подобрать" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer


def test_returning_patient_yes_continues_booking_with_crm_complaint(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lookup)
    chat_id = "crm_state_returning_yes"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000002", "да"))
    session = state.get_session(chat_id)

    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer
    assert session["complaint"] == "Колени"
    assert session["step"] in {"age", "contraindications"}
    assert "сколько Вам лет" in answer or "противопоказ" in answer.lower()


def test_new_patient_allows_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_lookup)
    chat_id = "crm_state_new"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000003", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_patient_state"] == "NEW_PATIENT"
    assert session["first_touch_allowed"] is True
    assert answer == dialog.FIRST_TOUCH_CLINIC_INFO_RU


def test_active_booking_blocks_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup)
    chat_id = "crm_state_active"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77000000004", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_patient_state"] == "ACTIVE_BOOKING"
    assert "Вы уже записаны" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import crm
import dialog
import state

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session["ai_lead_started"] = True
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


def active_lookup(*, appointment_id: int = 77):
    async def _lookup(phone: str) -> dict[str, Any]:
        return {
            "ok": True,
            "hasActiveAppointment": True,
            "appointments": [
                {
                    "id": appointment_id,
                    "appointmentId": appointment_id,
                    "date": "2099-01-01",
                    "timeStart": "10:00",
                    "doctorName": "Тестовый врач",
                    "doctorLogin": "doctor1",
                    "status": "confirmed",
                }
            ],
            "appointment": {
                "id": appointment_id,
                "appointmentId": appointment_id,
                "date": "2099-01-01",
                "timeStart": "10:00",
                "doctorName": "Тестовый врач",
                "doctorLogin": "doctor1",
                "status": "confirmed",
            },
            "lastAppointment": {
                "id": appointment_id,
                "appointmentId": appointment_id,
                "date": "2099-01-01",
                "timeStart": "10:00",
                "doctorName": "Тестовый врач",
                "doctorLogin": "doctor1",
                "status": "confirmed",
            },
        }

    return _lookup


def no_active_lookup():
    async def _lookup(phone: str) -> dict[str, Any]:
        return {"ok": True, "hasActiveAppointment": False, "appointments": []}

    return _lookup


def test_active_appointment_lookup_prevents_new_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup())
    chat_id = "crm_sst_active"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77008984505", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["active_appointment_found"] is True
    assert session["crm_patient_state"] == "ACTIVE_BOOKING"
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer


def test_no_active_appointment_allows_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", no_active_lookup())
    chat_id = "crm_sst_new"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77008984505", "Здравствуйте, хочу записаться"))

    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU in answer


def test_cancel_active_appointment_uses_crm_source(monkeypatch: Any) -> None:
    cancelled: list[dict[str, Any]] = []

    async def fake_cancel(**kwargs: Any) -> dict[str, Any]:
        cancelled.append(kwargs)
        return {"ok": True, "success": True, "cancelled": True}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup(appointment_id=91))
    monkeypatch.setattr(crm, "cancel_appointment", fake_cancel)
    chat_id = "crm_sst_cancel"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77008984505", "отмените запись"))
    session = state.get_session(chat_id)

    assert cancelled
    assert cancelled[0]["appointment_id"] == 91
    assert "Запись отменена" in answer
    assert session["booking_confirmed"] is False
    assert session["cancellation_confirmed"] is True


def test_reschedule_active_appointment_asks_for_new_datetime(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup())
    chat_id = "crm_sst_reschedule"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77008984505", "перенесите запись"))
    session = state.get_session(chat_id)

    assert session["reschedule_mode"] is True
    assert session["original_appointment"]
    # Safe staged flow: first collect the new date, then show live CRM slots and collect time.
    assert "На какой день" in answer
    assert "время" not in answer.lower() or "На какой день и время" not in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer

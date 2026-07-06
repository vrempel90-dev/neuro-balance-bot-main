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


def active_lookup(*, appointment_id: int = 77):
    async def fake(phone: str) -> dict[str, Any]:
        return {
            "ok": True,
            "status_code": 200,
            "phone_variants": crm.phone_lookup_variants(phone),
            "appointments": [
                {
                    "id": appointment_id,
                    "bookingId": f"b-{appointment_id}",
                    "patientId": 5,
                    "date": "2099-01-02",
                    "timeStart": "12:30",
                    "doctorName": "Тестовый врач",
                    "doctorLogin": "doctor1",
                    "status": "booked",
                }
            ],
            "appointment": {
                "id": appointment_id,
                "bookingId": f"b-{appointment_id}",
                "patientId": 5,
                "date": "2099-01-02",
                "timeStart": "12:30",
                "doctorName": "Тестовый врач",
                "doctorLogin": "doctor1",
                "status": "booked",
            },
            "appointments_cache": [],
            "active_count": 1,
        }

    return fake


def no_active_lookup(phone: str):
    async def fake(_: str) -> dict[str, Any]:
        return {"ok": True, "status_code": 200, "appointments": [], "appointment": None, "active_count": 0}

    return fake(phone)


def test_active_appointment_blocks_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", active_lookup())
    chat_id = "crm_sst_active"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "+7 700 898 45 05", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_lookup_called"] is True
    assert session["active_appointment_found"] is True
    assert session["is_booked_client"] is True
    assert "Вы уже записаны" in answer
    assert "12:30" in answer
    assert "Тестовый врач" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer
    assert "что именно Вас беспокоит" not in answer
    assert "сколько Вам лет" not in answer
    assert "противопоказ" not in answer.lower()


def test_no_active_appointment_allows_first_touch(monkeypatch: Any) -> None:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", no_active_lookup)
    chat_id = "crm_sst_no_active"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "87008984505", "хочу записаться"))
    session = state.get_session(chat_id)

    assert session["crm_lookup_called"] is True
    assert session["active_appointment_found"] is False
    assert session["first_touch_allowed"] is True
    assert answer == dialog.FIRST_TOUCH_CLINIC_INFO_RU


def test_lookup_failure_fails_closed(monkeypatch: Any) -> None:
    async def broken(phone: str) -> dict[str, Any]:
        raise crm.CRMError("down")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", broken)
    chat_id = "crm_sst_failure"
    reset(chat_id)

    answer = run(dialog.handle_message(chat_id, "77008984505", "Здравствуйте"))
    session = state.get_session(chat_id)

    assert session["crm_lookup_called"] is True
    assert session["first_touch_allowed"] is False
    assert session["first_touch_blocked_reason"] == "crm_lookup_failed"
    assert session["manual_takeover"] is True
    assert "уточню Вашу запись у администратора" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer


def test_cancel_active_appointment_uses_crm(monkeypatch: Any) -> None:
    cancelled: list[Any] = []

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
    assert "На какой день и время" in answer
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in answer

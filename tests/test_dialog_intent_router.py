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
import state
from dialog import handle_message


state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def setup_crm(monkeypatch: Any, *, lookup_error: bool = False, cancel_error: bool = False) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "lookup": [],
        "cancel": [],
        "slots": [],
        "book": [],
    }

    async def fake_patient_lookup(phone: str) -> dict[str, Any]:
        calls["lookup"].append(phone)
        if lookup_error:
            raise crm.CRMError("CRM unavailable")
        return {
            "hasActiveAppointment": True,
            "lastAppointment": {
                "appointmentId": 777,
                "date": "2099-01-01",
                "timeStart": "18:00",
                "doctorName": "Тестовый врач",
            },
        }

    async def fake_cancel_appointment(**kwargs: Any) -> dict[str, Any]:
        calls["cancel"].append(kwargs)
        if cancel_error:
            raise crm.CRMError("CRM unavailable")
        return {"ok": True, "cancelled": True, "appointmentId": kwargs.get("appointment_id")}

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": "doctor1",
                    "doctorName": "Тестовый врач",
                    "availableSlots": ["18:00"],
                }
            ]
        }

    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        return {
            "ok": True,
            "appointmentId": 999,
            "date": kwargs.get("date"),
            "timeStart": kwargs.get("time_start"),
            "doctorName": kwargs.get("doctor_name") or "Тестовый врач",
        }

    monkeypatch.setattr(crm, "patient_lookup", fake_patient_lookup)
    monkeypatch.setattr(crm, "cancel_appointment", fake_cancel_appointment)
    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    return calls


def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    if preset:
        session = state.get_session(chat_id)
        session.update(preset)
        state.save_session(chat_id, session)


def answer(chat_id: str, text: str) -> str:
    return run(handle_message(chat_id, "77011234567", text))


def test_standalone_thanks_ok_do_not_start_questionnaire(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["спасибо", "рахмет", "ок", "спосибо", "ракмет"]:
        chat_id = f"thanks_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert result == ""
        assert session["step"] == "start"

    assert calls["lookup"] == []
    assert calls["slots"] == []
    assert calls["book"] == []


def test_visit_confirmation_is_not_new_booking(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Буду", "приду", "буду в 18.00"]:
        chat_id = f"confirm_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "будем ждать" in result
        assert session["status"] == "visit_confirmed"
        assert session["step"] == "done"

    assert calls["lookup"] == []
    assert calls["slots"] == []
    assert calls["book"] == []


def test_existing_appointment_lookup_wins_over_booking_flow(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Я уже записан", "напомните время", "напомните время, хочу записаться"]:
        chat_id = f"lookup_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        assert "Вы уже записаны" in result
        assert "18:00" in result

    assert len(calls["lookup"]) == 3
    assert calls["slots"] == []
    assert calls["book"] == []


def test_cancel_and_negative_visit_use_crm_not_confirmation(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Отмените", "не приду", "ни приду", "атмените запись"]:
        chat_id = f"cancel_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "отменили" in result
        assert session["status"] == "cancelled"
        assert "будем ждать" not in result

    assert len(calls["lookup"]) == 4
    assert len(calls["cancel"]) == 4
    assert calls["slots"] == []
    assert calls["book"] == []


def test_mri_ct_images_do_not_start_questionnaire_and_viktor_is_not_ct(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Нужно ли МРТ?", "КТ надо?", "У меня есть снимки"]:
        chat_id = f"mri_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "заранее делать не обязательно" in result
        assert session["step"] == "start"

    reset("viktor_start")
    result = answer("viktor_start", "Виктор")
    assert "МРТ" not in result
    assert "КТ" not in result
    assert "чем можем помочь" in result

    assert calls["slots"] == []
    assert calls["book"] == []


def test_when_waiting_for_name_accepts_name_and_books(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    reset(
        "name_viktor",
        {
            "step": "name",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 35,
            "contraindications_ok": True,
            "selected_slot": {
                "doctor_login": "doctor1",
                "doctor_name": "Тестовый врач",
                "date": "2099-01-01",
                "time": "18:00",
            },
        },
    )

    result = answer("name_viktor", "Виктор")
    session = state.get_session("name_viktor")
    assert result
    assert session["patient_name"] == "Виктор"
    assert session["step"] == "done"
    assert session["status"] == "booked"
    assert len(calls["book"]) == 1


def test_uncertain_message_clarifies_instead_of_booking(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    reset("uncertain")

    result = answer("uncertain", "непонятно")
    session = state.get_session("uncertain")
    assert "чем можем помочь" in result
    assert session["step"] == "start"
    assert calls["slots"] == []
    assert calls["book"] == []


def test_crm_lookup_and_cancel_fallbacks_do_not_go_silent(monkeypatch: Any) -> None:
    setup_crm(monkeypatch, lookup_error=True)
    reset("lookup_fallback")
    result = answer("lookup_fallback", "напомните время")
    session = state.get_session("lookup_fallback")
    assert result
    assert "администратор" in result
    assert session["step"] == "escalated"

    setup_crm(monkeypatch, lookup_error=True, cancel_error=True)
    reset("cancel_fallback")
    result = answer("cancel_fallback", "не приду")
    session = state.get_session("cancel_fallback")
    assert result
    assert "администратор" in result
    assert session["step"] == "escalated"


def test_language_lock_keeps_ru_and_kk(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("lang_ru", {"language": "ru", "language_locked": True, "step": "date"})
    assert answer("lang_ru", "рахмет") == ""
    assert state.get_session("lang_ru")["language"] == "ru"

    reset("lang_kk", {"language": "kk", "language_locked": True, "step": "date"})
    assert answer("lang_kk", "ок") == ""
    assert state.get_session("lang_kk")["language"] == "kk"

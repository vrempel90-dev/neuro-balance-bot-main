from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_tools
import crm
import dialog


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def slot(time: str = "09:20") -> dict[str, Any]:
    return {
        "doctorLogin": "doctor1",
        "doctorName": "Тестовый врач",
        "date": "2099-01-02",
        "timeStart": time,
        "doctor_login": "doctor1",
        "doctor_name": "Тестовый врач",
        "time": time,
    }


def ready_session(*, language: str = "ru") -> dict[str, Any]:
    selected = slot()
    session: dict[str, Any] = {
        "chat_id": "booking-race",
        "phone": "77011234567",
        "language": language,
        "step": "name",
        "complaint": "болит спина",
        "age": 32,
        "contraindications_ok": True,
        "contraindications_verdict": bot_tools.CONTRA_PROCEED,
        "patient_name": "Дана",
        "preferred_date": "2099-01-02",
        "selected_date": "2099-01-02",
        "selected_time": "09:20",
        "selected_doctor_login": "doctor1",
        "selected_doctor_name": "Тестовый врач",
        "selected_slot": dict(selected),
        "last_slots": [dict(selected)],
    }
    bot_tools.check_complaint(session, "болит спина", is_in_profile=True)
    return session


def response_error(status: int = 500, message: str = "boom") -> crm.CRMResponseError:
    response = httpx.Response(
        status,
        text=message,
        request=httpx.Request("POST", "https://crm.test/api/bot/book"),
    )
    return crm.CRMResponseError("book", response, {"message": message})


def test_generic_crm_failure_never_claims_booking_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ready_session()

    async def fail_book(**kwargs: Any) -> dict[str, Any]:
        raise response_error(500, "database temporarily unavailable")

    async def still_available(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {
            "date": date,
            "availability": [{
                "doctorLogin": doctor_login or "doctor1",
                "doctorName": "Тестовый врач",
                "date": date,
                "availableSlots": ["09:20", "14:00"],
            }],
        }

    monkeypatch.setattr(crm, "book_appointment", fail_book)
    monkeypatch.setattr(crm, "check_slots", still_available)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)

    answer = run(dialog._book("booking-generic-failure", session, session["phone"]))

    assert session["booking_confirmed"] is False
    assert session["manual_takeover"] is True
    assert session["escalated"] is True
    assert "подтверждена" not in answer.lower()
    assert "не удалось" in answer.lower() or "администратор" in answer.lower()


def test_slot_taken_during_booking_refreshes_options_instead_of_false_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ready_session()
    check_calls: list[dict[str, Any]] = []

    async def conflict_book(**kwargs: Any) -> dict[str, Any]:
        raise response_error(500, "slot already occupied")

    async def refreshed(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        check_calls.append({"date": date, "doctor_login": doctor_login})
        return {
            "date": date,
            "availability": [{
                "doctorLogin": doctor_login or "doctor1",
                "doctorName": "Тестовый врач",
                "date": date,
                "availableSlots": ["14:00", "15:20"],
            }],
        }

    monkeypatch.setattr(crm, "book_appointment", conflict_book)
    monkeypatch.setattr(crm, "check_slots", refreshed)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)

    answer = run(dialog._book("booking-race-recover", session, session["phone"]))

    assert check_calls == [{"date": "2099-01-02", "doctor_login": "doctor1"}]
    assert session["booking_confirmed"] is False
    assert session["manual_takeover"] is not True
    assert session["escalated"] is not True
    assert session["step"] == "time"
    assert session.get("selected_slot") is None
    assert session.get("selected_time") in ("", None)
    assert [dialog._slot_time(s) for s in session["last_slots"]] == ["14:00", "15:20"]
    assert "подтверждена" not in answer.lower()
    assert "14:00" in answer and "15:20" in answer


def test_slot_taken_with_no_other_slots_returns_to_date_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ready_session()

    async def conflict_book(**kwargs: Any) -> dict[str, Any]:
        raise response_error(500, "slot already occupied")

    async def no_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"date": date, "availability": []}

    monkeypatch.setattr(crm, "book_appointment", conflict_book)
    monkeypatch.setattr(crm, "check_slots", no_slots)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)

    answer = run(dialog._book("booking-race-no-options", session, session["phone"]))

    assert session["booking_confirmed"] is False
    assert session["step"] == "date"
    assert session.get("selected_slot") is None
    assert "подтверждена" not in answer.lower()
    assert "друг" in answer.lower() or "день" in answer.lower()


def test_auth_failure_does_not_masquerade_as_slot_race(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ready_session()
    check_calls: list[Any] = []

    async def auth_fail(**kwargs: Any) -> dict[str, Any]:
        raise response_error(401, "Unauthorized")

    async def should_not_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
        check_calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(crm, "book_appointment", auth_fail)
    monkeypatch.setattr(crm, "check_slots", should_not_check)

    answer = run(dialog._book("booking-auth-failure", session, session["phone"]))

    assert check_calls == []
    assert session["manual_takeover"] is True
    assert session["escalated"] is True
    assert session["booking_confirmed"] is False
    assert "подтверждена" not in answer.lower()


def test_honest_failure_message_is_localized_to_kazakh(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ready_session(language="kk")

    async def fail_book(**kwargs: Any) -> dict[str, Any]:
        raise response_error(503, "service unavailable")

    async def still_available(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {
            "date": date,
            "availability": [{
                "doctorLogin": doctor_login or "doctor1",
                "doctorName": "Тестовый врач",
                "date": date,
                "availableSlots": ["09:20"],
            }],
        }

    monkeypatch.setattr(crm, "book_appointment", fail_book)
    monkeypatch.setattr(crm, "check_slots", still_available)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)

    answer = run(dialog._book("booking-failure-kk", session, session["phone"]))

    assert session["booking_confirmed"] is False
    assert "расталды" not in answer.lower()
    assert "әкімші" in answer.lower() or "растай" in answer.lower()

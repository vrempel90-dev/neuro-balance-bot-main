from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import crm
import dialog
import state

state.init_db()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _chat_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    if preset:
        state.save_session(chat_id, dict(preset))


def _answer(chat_id: str, text: str) -> str:
    return _run(dialog.handle_message(chat_id, "77011234567", text))


def test_explicit_new_lead_starts_ai_flow() -> None:
    chat_id = _chat_id("new-lead")
    _reset(chat_id)

    answer = _answer(chat_id, "Здравствуйте хочу записаться")
    session = state.get_session(chat_id)

    assert answer
    assert session["ai_lead_started"] is True
    assert not session.get("no_reply_reason")


def test_profile_complaint_starts_ai_flow_and_asks_age() -> None:
    chat_id = _chat_id("profile")
    _reset(chat_id)

    answer = _answer(chat_id, "Спина болит")
    session = state.get_session(chat_id)

    assert answer
    assert session["ai_lead_started"] is True
    assert session["step"] == "age"


def test_old_chat_is_silent() -> None:
    chat_id = _chat_id("old")
    _reset(chat_id, {"old_chat": True})

    assert _answer(chat_id, "Здравствуйте") == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "old_chat_ai_disabled"


def test_booked_session_is_always_silent() -> None:
    inputs = (
        "Здравствуйте. Подтверждаю",
        "А разве я не записан уже на 10 часов?",
        "Грыжи и протрузия",
    )
    for text in inputs:
        chat_id = _chat_id("booked")
        _reset(chat_id, {
            "step": "booked",
            "appointment_time": "10:00",
            "appointment_status": "booked",
        })

        answer = _answer(chat_id, text)
        session = state.get_session(chat_id)

        assert answer == ""
        assert session["step"] == "booked"
        assert session["no_reply_reason"] == "booked_session_ai_disabled"


def test_manual_takeover_is_silent_even_for_profile_complaint() -> None:
    chat_id = _chat_id("manual")
    _reset(chat_id, {"manual_takeover": True})

    assert _answer(chat_id, "Грыжа и протрузия") == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "manual_takeover"


def test_short_reply_does_not_start_lead() -> None:
    chat_id = _chat_id("short")
    _reset(chat_id, {"some_existing_context": True})

    assert _answer(chat_id, "да") == ""
    session = state.get_session(chat_id)
    assert session["no_reply_reason"] == "not_new_lead"
    assert session.get("ai_muted") is not True


def test_active_city_reply_continues_and_asks_astana_visit() -> None:
    chat_id = _chat_id("active-city")
    _reset(chat_id, {
        "step": "complaint",
        "ai_lead_started": True,
        "last_bot_answer": "Вы из какого города обращаетесь?",
    })

    answer = _answer(chat_id, "Караганда")
    session = state.get_session(chat_id)

    assert answer
    assert "планируете приехать в Астану" in answer
    assert session.get("ai_muted") is not True


def test_active_greeting_reply_reasks_complaint_without_mute() -> None:
    chat_id = _chat_id("active-greeting")
    _reset(chat_id, {
        "step": "complaint",
        "ai_lead_started": True,
        "last_bot_answer": "Подскажите, пожалуйста, что Вас беспокоит?",
    })

    answer = _answer(chat_id, "Доброе утро")
    session = state.get_session(chat_id)

    assert answer
    assert "что Вас беспокоит" in answer
    assert session.get("ai_muted") is not True


def test_active_ai_lead_continues() -> None:
    chat_id = _chat_id("active")
    _reset(chat_id, {
        "ai_lead_started": True,
        "step": "age",
        "complaint": "Спина болит",
    })

    answer = _answer(chat_id, "34")
    session = state.get_session(chat_id)

    assert answer
    assert session["step"] == "contraindications"


def test_active_clear_contraindications_continue_to_date() -> None:
    chat_id = _chat_id("active-contra")
    _reset(chat_id, {
        "step": "contraindications",
        "ai_lead_started": True,
    })

    answer = _answer(chat_id, "Все чисто")
    session = state.get_session(chat_id)

    assert "На какой день" in answer
    assert session["step"] == "date"


def test_active_date_reply_is_not_muted() -> None:
    chat_id = _chat_id("active-date")
    _reset(chat_id, {
        "step": "date",
        "ai_lead_started": True,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
    })

    answer = _answer(chat_id, "завтра")
    session = state.get_session(chat_id)

    assert answer
    assert session["step"] in ("time", "date", "escalated")
    assert session.get("ai_muted") is not True


def test_successful_booking_mutes_future_messages(monkeypatch: Any) -> None:
    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "appointmentId": 123,
            "date": kwargs["date"],
            "timeStart": kwargs["time_start"],
        }

    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    chat_id = _chat_id("booking")
    session = {
        "ai_lead_started": True,
        "step": "name",
        "phone": "77011234567",
        "complaint": "Спина болит",
        "complaint_gate": "COMPLAINT_OK",
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "selected_slot": {
            "date": "2099-01-01",
            "time": "10:00",
            "doctor_login": "doctor",
        },
    }
    _reset(chat_id, session)

    assert _answer(chat_id, "Иван") 
    booked = state.get_session(chat_id)
    assert booked["step"] == "booked"
    assert booked["appointment_status"] == "booked"
    assert booked["ai_muted"] is True
    assert _answer(chat_id, "спасибо") == ""


def test_refund_and_claim_messages_are_handed_to_admin() -> None:
    messages = (
        "Моя мама у вас проходила лечение на рассрочку. Писала заявление на отмену рассрочки. Когда будет отмена? Решите вопрос",
        "Когда вернут деньги за лечение?",
        "Хочу отменить рассрочку Kaspi Red",
        "Написала претензию, когда ответите?",
    )
    for text in messages:
        chat_id = _chat_id("refund")
        _reset(chat_id)

        answer = _answer(chat_id, text)
        session = state.get_session(chat_id)

        assert "передам ответственному администратору" in answer
        assert session["no_reply_reason"] == "refund_claim_admin_required"
        assert session["manual_takeover"] is True
        assert session["ai_muted"] is True
        assert session.get("step") != "age"
        assert "будем ждать" not in answer.lower()
        assert "что вас беспокоит" not in answer.lower()
        assert "сколько вам лет" not in answer.lower()


def test_normal_lead_is_not_refund_issue() -> None:
    assert dialog._is_refund_or_claim_issue("Здравствуйте хочу записаться") is False
    assert dialog._is_new_lead_text("Здравствуйте хочу записаться") is True

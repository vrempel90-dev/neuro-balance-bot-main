from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import main
import state
from dialog import CONTRAINDICATIONS_MESSAGE_RU, FIRST_TOUCH_CLINIC_INFO_RU, handle_message


def setup_function():
    state.init_db()


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("user", ["хочу записаться", "Сәлеметсіз", "+"])
def test_new_chat_first_touch_starts_gpt_led_funnel(user: str) -> None:
    chat_id = f"locked_first_{abs(hash(user))}"
    answer = run(handle_message(chat_id, "77010000000", user))
    session = state.get_session(chat_id)

    assert answer
    assert session["first_touch_info_sent"] is True
    assert session["ai_lead_started"] is True
    assert session["step"] in {"start", "complaint", "age"}
    assert session["answer_source"] in {
        "openai_dialog_brain",
        "python_fallback:first_touch",
        "pending:gpt_first_touch",
    }
    assert "чем можем помочь" not in answer
    assert "Не беспокоит" not in answer
    assert answer != FIRST_TOUCH_CLINIC_INFO_RU


def test_age_to_contraindications_uses_full_locked_template() -> None:
    chat_id = "locked_contra_after_age"
    session = state.get_session(chat_id)
    session.update({"step": "age", "complaint": "болит спина", "ai_lead_started": True, "first_touch_info_sent": True})
    state.save_session(chat_id, session)

    answer = run(handle_message(chat_id, "77010000000", "52"))

    assert CONTRAINDICATIONS_MESSAGE_RU in answer
    for term in ["кардиостимулятор", "беременность", "онкология", "металл в зоне лечения", "эпилепсия", "коляски, костыли"]:
        assert term in answer
    assert "Есть ли у Вас какие-нибудь противопоказания?" not in answer
    for forbidden in ["диабет", "тромбоз", "давление", "инсульт", "инфаркт", "температура"]:
        assert forbidden not in answer.lower()


def test_contraindications_details_question_repeats_approved_list_without_escalation() -> None:
    chat_id = "locked_contra_details"
    session = state.get_session(chat_id)
    session.update({"step": "contraindications", "complaint": "спина", "age": 52, "ai_lead_started": True, "first_touch_info_sent": True})
    state.save_session(chat_id, session)

    answer = run(handle_message(chat_id, "77010000000", "На что?"))
    session = state.get_session(chat_id)

    assert "Уточняю по противопоказаниям из списка выше" in answer
    assert "кардиостимулятор" in answer
    assert "ограниченная подвижность" in answer
    assert session.get("manual_takeover") is not True
    assert session.get("escalated") is not True


def test_wazzup_duplicate_first_touch_not_sent(monkeypatch) -> None:
    chat_id = "locked_wazzup_dedupe"
    session = state.get_session(chat_id)
    session["last_sent_answer"] = FIRST_TOUCH_CLINIC_INFO_RU
    state.save_session(chat_id, session)
    called = []

    async def fake_send_text(**kwargs):
        called.append(kwargs)
        return {"status_code": 200}

    monkeypatch.setattr(main, "send_text", fake_send_text)
    run(main._send_answer_parts(chat_id=chat_id, answer=FIRST_TOUCH_CLINIC_INFO_RU, chat_type="whatsapp", channel_id="ch", phone="77010000000"))

    assert called == []
    assert state.get_session(chat_id).get("wazzup_send_called") is False


def test_locked_first_touch_bypasses_humanizer_and_repairs_shortened_answer(monkeypatch) -> None:
    chat_id = "locked_humanizer_repair"
    session = state.get_session(chat_id)
    session.update({"step": "complaint", "answer_source": "locked_template:first_touch", "skip_humanize": True})
    state.save_session(chat_id, session)

    async def should_not_call(*args, **kwargs):
        raise AssertionError("humanizer must not be called for locked templates")

    monkeypatch.setattr(main, "humanize_reply_with_openai", should_not_call)
    answer = run(main._maybe_humanize_answer(chat_id, "+", "Здравствуйте ☺️ Хорошо, расскажу подробнее..."))
    assert answer == "Здравствуйте ☺️ Хорошо, расскажу подробнее..."

    from dialog import _finalize
    repaired = _finalize(chat_id, state.get_session(chat_id), answer)
    assert repaired == FIRST_TOUCH_CLINIC_INFO_RU

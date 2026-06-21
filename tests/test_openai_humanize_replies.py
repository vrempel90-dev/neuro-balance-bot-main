from __future__ import annotations

import asyncio
import os
from typing import Any

os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

import ai
import main
import state

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def reset(chat_id: str, session: dict[str, Any]) -> None:
    state.reset_session(chat_id)
    state.save_session(chat_id, session)


async def fake_humanized(*, base_answer: str, user_text: str, session: dict[str, Any], recent_history: list[dict[str, str]] | None = None):
    return (
        "Поняла Вас, спина беспокоит 🌿 Подскажите, пожалуйста, сколько Вам лет?",
        {
            "openai_used": True,
            "openai_model": "test-model",
            "openai_skip_reason": "",
            "openai_guard_failed": False,
            "base_answer_preview": base_answer[:160],
            "final_answer_preview": "Поняла Вас, спина беспокоит 🌿 Подскажите, пожалуйста, сколько Вам лет?",
        },
    )


def test_openai_humanize_on_new_lead(monkeypatch: Any) -> None:
    chat_id = "humanize_new_lead"
    base = "Поняла, спина беспокоит. Это наш профиль 🌿 Подскажите, пожалуйста, сколько Вам лет?"
    reset(chat_id, {"ai_lead_started": True, "step": "age", "gate_reason": "active_ai_lead"})
    monkeypatch.setattr(main, "humanize_reply_with_openai", fake_humanized)

    answer = run(main._maybe_humanize_answer(chat_id, "Спина болит", base))
    session = state.get_session(chat_id)

    assert session["openai_used"] is True
    assert "сколько Вам лет" in answer
    assert answer != base


def test_guard_blocks_premature_name() -> None:
    base = "Подскажите, пожалуйста, сколько Вам лет?"
    humanized = "Как Вас зовут?"

    assert ai._humanize_guard_ok(base, humanized) is False


def test_no_openai_for_booked() -> None:
    chat_id = "humanize_booked"
    reset(chat_id, {"step": "booked", "ai_muted": True, "ai_lead_started": True})

    answer = run(main._maybe_humanize_answer(chat_id, "Спасибо", ""))
    session = state.get_session(chat_id)

    assert answer == ""
    assert session["openai_used"] is False
    assert session["openai_skip_reason"] in ("empty_answer", "booked_or_muted")


def test_no_openai_for_refund_claim(monkeypatch: Any) -> None:
    chat_id = "humanize_refund"
    reset(chat_id, {"step": "start", "gate_reason": "refund_claim_admin_required", "ai_lead_started": True})
    called = False

    async def should_not_call(**kwargs: Any):
        nonlocal called
        called = True
        return kwargs["base_answer"], {"openai_used": True}

    monkeypatch.setattr(main, "humanize_reply_with_openai", should_not_call)
    base = "Понимаю Вас. Вопрос по возврату передам администратору 🌿"

    answer = run(main._maybe_humanize_answer(chat_id, "хочу возврат", base))
    session = state.get_session(chat_id)

    assert answer == base
    assert called is False
    assert session["openai_used"] is False
    assert session["openai_skip_reason"] == "refund_or_claim"


def test_contraindication_checklist_preserved() -> None:
    base = "Противопоказаний нет? Кардиостимулятор, онкология, беременность, острые инфекции."
    humanized = "Кардиостимулятор, онкология, беременность, острые инфекции отсутствуют?"

    assert ai._humanize_guard_ok(base, humanized) is False


def test_config_missing(monkeypatch: Any) -> None:
    monkeypatch.setattr(ai, "AsyncOpenAI", None)
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "")

    base = "Подскажите, пожалуйста, сколько Вам лет?"
    answer, debug = run(ai.humanize_reply_with_openai(base_answer=base, user_text="Спина", session={"step": "age", "ai_lead_started": True}))

    assert answer == base
    assert debug["openai_used"] is False
    assert debug["openai_skip_reason"] == "config_missing"


def test_debug_contains_openai_fields() -> None:
    debug = main._dialog_debug(
        {
            "openai_used": False,
            "openai_model": "gpt-4o-mini",
            "openai_skip_reason": "config_missing",
            "openai_guard_failed": False,
            "base_answer_preview": "base",
            "final_answer_preview": "final",
        },
        "final",
    )

    assert "openai_used" in debug
    assert "openai_model" in debug
    assert "openai_skip_reason" in debug
    assert "openai_guard_failed" in debug
    assert debug["base_answer_preview"] == "base"
    assert debug["final_answer_preview"] == "final"

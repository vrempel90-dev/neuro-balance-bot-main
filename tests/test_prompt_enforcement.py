from __future__ import annotations

import asyncio
import json
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

import ai
import dialog
import state

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_rendered_prompt_is_loaded_into_dialog_brain(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeChoice:
        message = type("Msg", (), {"content": json.dumps({
            "intent": "complaint",
            "patient_meaning": "болит спина",
            "reply": "Понимаю. Сколько Вам лет?",
            "next_step": "age",
            "extracted": {"complaint": "спина", "language": "ru"},
            "needs_python_tool": "none",
            "safety": {"hard_stop": False, "reason": "", "unsafe_medical_claim": False, "tries_to_book_without_rules": False},
        }, ensure_ascii=False)})()

    class FakeCompletions:
        async def create(self, **kwargs: Any):
            captured.update(kwargs)
            return type("Resp", (), {"choices": [FakeChoice()]})()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai.get_settings(), "ai_brain_model", "gpt-5.4-mini")
    monkeypatch.setattr(ai.get_settings(), "ai_brain_temperature", 0.2)
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient())
    decision, debug = run(ai.run_openai_dialog_brain(user_text="спина", session={"step": "complaint"}))
    system_prompt = captured["messages"][0]["content"]
    assert "SYSTEM_PROMPT" in system_prompt or "PROJECT OVERRIDES" in system_prompt
    assert "20:00–08:00" in system_prompt
    assert decision["action"] == "ask_age"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["temperature"] == 0.2
    assert debug["openai_brain_model"] == "gpt-5.4-mini"
    assert debug["openai_brain_temperature"] == 0.2
    assert debug["openai_brain_used"] is True


def test_schema_rejects_extra_fields() -> None:
    raw = {
        "intent": "faq",
        "patient_meaning": "x",
        "reply": "ок",
        "next_step": "keep_current",
        "extracted": {"language": "ru", "made_up": "bad"},
        "needs_python_tool": "none",
        "safety": {"hard_stop": False, "reason": "", "unsafe_medical_claim": False, "tries_to_book_without_rules": False},
    }
    decision, reason = ai._normalize_dialog_brain_decision(raw)
    assert decision == {}
    assert reason == "schema_extra_extracted"


def test_python_blocks_llm_booking_and_name_before_slot() -> None:
    ok, reason = dialog.validate_openai_dialog_decision(
        {"action": "ask_name", "next_step": "name", "reply": "Как Вас зовут?", "extracted": {}, "needs_python_tool": "none"},
        {"step": "date", "ai_lead_started": True},
        "завтра",
    )
    assert ok is False
    assert reason == "ask_name_without_slot"

    ok, reason = dialog.validate_openai_dialog_decision(
        {"action": "ask_name", "next_step": "booked", "reply": "Записал", "extracted": {}, "needs_python_tool": "book_appointment"},
        {"step": "name", "selected_slot": {"time": "10:00"}},
        "Алия",
    )
    assert ok is False
    assert reason == "llm_attempted_booking"


def test_final_fact_validator_blocks_hallucinated_slots_and_prices() -> None:
    session = {"step": "date", "crm_availability_empty": True, "last_slots": [], "language": "ru"}
    answer = dialog._validate_final_fact_answer("guard_fact", session, "Есть свободные слоты 10:00 и 11:00")
    assert "свобод" in answer.lower() and "день" in answer.lower()

    session = {"step": "complaint", "language": "ru"}
    answer = dialog._validate_final_fact_answer("guard_price", session, "Курс стоит 120000 тг")
    assert "Первичный приём — 5 000 тг" in answer

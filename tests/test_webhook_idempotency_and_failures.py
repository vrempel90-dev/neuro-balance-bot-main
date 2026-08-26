"""Регрессии: повторный вебхук, гонка дубликатов, сбои OpenAI.

Находка аудита: is_processed_message и mark_processed_message разделены
всем пайплайном (CRM, OpenAI, отправка). Два одновременных вебхука с одним
и тем же message_key успевали пройти проверку оба — пациент получал два
ответа, а запись могла уйти в CRM дважды.

Плюс проверяем, что падение OpenAI не ломает состояние диалога и не
выполняет side-effect без валидного решения.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ai  # noqa: E402
import crm  # noqa: E402
import main  # noqa: E402
import state  # noqa: E402

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ------------------------------------------------------ атомарный захват


def test_claim_message_is_atomic() -> None:
    key = "claim-atomic-1"
    state.release_message(key)
    assert state.claim_message(key, "chat") is True
    assert state.claim_message(key, "chat") is False, "второй захват того же ключа прошёл"


def test_release_message_gives_a_second_chance() -> None:
    key = "claim-release-1"
    state.release_message(key)
    assert state.claim_message(key, "chat") is True
    state.release_message(key)
    assert state.claim_message(key, "chat") is True


def test_empty_key_never_blocks_processing() -> None:
    """Сообщение без ключа блокировать нельзя — иначе потеряем ответ."""
    assert state.claim_message("", "chat") is True
    assert state.claim_message("", "chat") is True


def test_claimed_key_is_reported_as_processed() -> None:
    key = "claim-processed-1"
    state.release_message(key)
    state.claim_message(key, "chat")
    assert state.is_processed_message(key) is True


# ---------------------------------------------- дубликат вебхука в пайплайне


@pytest.fixture
def offline_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_lookup(phone: str) -> dict[str, Any]:
        return {
            "ok": True, "status_code": 200, "phone_raw": phone,
            "phone_normalized": phone, "phone_variants": [phone],
            "endpoint_url": "test", "query_params": {},
            "appointments_raw_count": 0, "appointments": [], "appointment": None,
            "appointments_cache": [], "active_count": 0, "raw": {"found": False},
        }

    async def fake_patient_lookup(phone: str) -> dict[str, Any]:
        return {"found": False}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(crm, "patient_lookup", fake_patient_lookup)
    monkeypatch.setattr(main, "is_bot_work_time", lambda *a, **k: True)


def make_message(chat_id: str, key: str, text: str = "Болит спина") -> dict[str, Any]:
    return {
        "chat_id": chat_id, "phone": "77011234567", "text": text,
        "message_key": key, "kind": "text", "source": "wazzup",
        "direction": "incoming",
    }


def test_same_webhook_delivered_twice_answers_only_once(offline_crm: None) -> None:
    chat_id = "dup-webhook-1"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead", "step": "complaint"})
    state.save_session(chat_id, session)
    state.release_message("dup-key-1")

    first = run(main._build_answer_for_message(make_message(chat_id, "dup-key-1")))
    second = run(main._build_answer_for_message(make_message(chat_id, "dup-key-1")))

    assert first.strip(), "первый вебхук остался без ответа"
    assert second == "", "повторный вебхук получил второй ответ"


def test_duplicate_webhook_does_not_advance_the_step(offline_crm: None) -> None:
    chat_id = "dup-webhook-2"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead", "step": "complaint"})
    state.save_session(chat_id, session)
    state.release_message("dup-key-2")

    run(main._build_answer_for_message(make_message(chat_id, "dup-key-2")))
    step_after_first = state.get_session(chat_id).get("step")
    run(main._build_answer_for_message(make_message(chat_id, "dup-key-2")))
    assert state.get_session(chat_id).get("step") == step_after_first


def test_different_keys_are_both_processed(offline_crm: None) -> None:
    chat_id = "dup-webhook-3"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead", "step": "complaint"})
    state.save_session(chat_id, session)
    for key in ("dup-key-3a", "dup-key-3b"):
        state.release_message(key)

    first = run(main._build_answer_for_message(make_message(chat_id, "dup-key-3a", "Болит спина")))
    run(main._build_answer_for_message(make_message(chat_id, "dup-key-3b", "Мне 42")))
    assert first.strip()
    # Оба ключа реально обработаны. Раньше здесь проверялся текст второго
    # ответа, но это была проверка старой воронки, которая отвечала на любое
    # сообщение сама: теперь текст пишет агент, а без него ход остаётся без
    # ответа (администратор уже уведомлён на первом ходе).
    assert state.is_processed_message("dup-key-3a") is True
    assert state.is_processed_message("dup-key-3b") is True


# ------------------------------------------------------- сбои OpenAI


def test_openai_timeout_returns_safe_fallback_decision() -> None:
    """Падение OpenAI не должно ронять диалог и не даёт side-effect."""
    decision, debug = run(ai.run_openai_dialog_brain(user_text="Болит спина", session={"step": "complaint"}))
    assert decision["action"] == "fallback_rule_based"
    assert decision["needs_python_tool"] == "none"
    assert debug["openai_brain_fallback_used"] is True


def test_invalid_json_from_model_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)

    class FakeCompletions:
        async def create(self, **kwargs: Any) -> Any:
            msg = type("Msg", (), {"content": "это не json"})()
            return type("Resp", (), {"choices": [type("C", (), {"message": msg})()]})()

    monkeypatch.setattr(ai, "_openai_client", lambda key: type("Cl", (), {
        "chat": type("Chat", (), {"completions": FakeCompletions()})()
    })())
    decision, _ = run(ai.run_openai_dialog_brain(user_text="x", session={"step": "complaint"}))
    assert decision["action"] == "fallback_rule_based"
    assert decision["needs_python_tool"] == "none"


@pytest.mark.parametrize(
    "raw, expected_error",
    [
        ({"intent": "complaint", "reply": "ок", "next_step": "age", "extracted": {"whatever": 1},
          "needs_python_tool": "none", "safety": {}}, "schema_extra_extracted"),
        ({"intent": "не_существует", "reply": "ок", "next_step": "age", "extracted": {},
          "needs_python_tool": "none", "safety": {}}, "invalid_intent"),
        ({"intent": "complaint", "reply": "ок", "next_step": "куда-то", "extracted": {},
          "needs_python_tool": "none", "safety": {}}, "invalid_next_step"),
        ({"intent": "complaint", "reply": "ок", "next_step": "age", "extracted": {},
          "needs_python_tool": "выдуманный_тул", "safety": {}}, "invalid_tool"),
        ("совсем не объект", "not_object"),
    ],
)
def test_malformed_decisions_are_rejected_fail_closed(raw: Any, expected_error: str) -> None:
    decision, error = ai._normalize_dialog_brain_decision(raw)
    assert error == expected_error
    assert decision == {}

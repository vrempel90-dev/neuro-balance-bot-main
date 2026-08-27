"""Два сообщения подряд в одном чате: пациент не должен получить дубль."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import ai
import crm
import main
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text

state.init_db()

PHONE = "77011234567"


@pytest.fixture(autouse=True)
def _openai_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    monkeypatch.setenv("MESSAGE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


def _message(chat_id: str, key: str, text: str) -> dict[str, Any]:
    return {
        "chat_id": chat_id, "phone": PHONE, "text": text, "kind": "text",
        "message_id": key, "message_key": key, "chat_type": "whatsapp",
        "channel_id": None, "is_incoming": True, "direction": "inbound",
        "timestamp": "2026-08-26T21:00:00.000Z", "source": "wazzup",
    }


def test_two_messages_at_once_do_not_produce_a_duplicate_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прод 26.08.2026: «Шейный позвонок» + «Протрузия» → два одинаковых ответа."""
    sent: list[str] = []

    async def fake_lookup(phone: str) -> dict[str, Any]:
        return {"ok": True, "found": True, "isNew": True, "patient": None,
                "lead": {"id": 1, "status": "НОВАЯ"}, "lastAppointment": None,
                "hasActiveAppointment": False, "appointment": None, "appointments": []}

    async def fake_send_text(**kwargs: Any) -> dict[str, Any]:
        sent.append(str(kwargs.get("text") or ""))
        return {"ok": True, "status_code": 201}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(main, "send_text", fake_send_text)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    # Модель отвечает одинаково на оба хода — ровно как в проде, и не
    # мгновенно: реальный вызов OpenAI занимает секунды, и именно в этом окне
    # второй ход успевал прочитать сессию до ответа первого.
    client = FakeOpenAIClient([
        assistant_text("Поняла. Сколько вам полных лет?"),
        assistant_text("Поняла. Сколько вам полных лет?"),
    ])
    _create = client.completions.create

    async def slow_create(**kwargs: Any):
        await asyncio.sleep(0.05)
        return await _create(**kwargs)

    client.completions.create = slow_create
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    chat_id = "race_chat"
    state.reset_session(chat_id)
    for key in ("race-a", "race-b"):
        state.release_message(key)

    async def scenario() -> None:
        await asyncio.gather(
            main._debounced_process_and_send(_message(chat_id, "race-a", "Шейный позвонок")),
            main._debounced_process_and_send(_message(chat_id, "race-b", "Протрузия")),
        )

    asyncio.run(scenario())

    assert sent, "хотя бы один ответ пациент получить обязан"
    assert len(sent) == len(set(sent)), f"пациенту ушёл дубль: {sent}"
    assert len(sent) == 1, f"на два сообщения подряд ушло {len(sent)} ответов: {sent}"

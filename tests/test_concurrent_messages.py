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
import httpx

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


def test_production_webhook_serializes_distinct_messages_per_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real POST webhook path must serialize different messages in one chat."""
    monkeypatch.setenv("BOT_AUTO_REPLY_ENABLED", "true")
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", "test-channel")
    get_settings.cache_clear()
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)

    active = 0
    max_active = 0
    sent: list[str] = []

    async def handler(message: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"answer": f"Ответ: {message['text']}", "should_send_wazzup": True}

    async def sender(**kwargs: Any) -> dict[str, Any]:
        sent.append(str(kwargs.get("text") or ""))
        await asyncio.sleep(0.01)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)

    chat_id = "serialized-webhook-chat"
    for key in ("serialized-1", "serialized-2"):
        state.release_message(key)

    def payload(mid: str, text: str) -> dict[str, Any]:
        return {
            "chatId": chat_id,
            "phone": PHONE,
            "text": text,
            "messageId": mid,
            "dateTime": "2026-09-21T21:00:00+05:00",
            "status": "inbound",
            "isEcho": False,
            "channelId": "test-channel",
            "chatType": "whatsapp",
            "type": "text",
        }

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first, second = await asyncio.gather(
                client.post("/webhook/wazzup", json=payload("serialized-1", "Первое")),
                client.post("/webhook/wazzup", json=payload("serialized-2", "Второе")),
            )
            assert first.status_code == 200
            assert second.status_code == 200

    asyncio.run(scenario())

    assert max_active == 1, "two distinct Wazzup turns for one chat ran concurrently"
    assert len(sent) == 2

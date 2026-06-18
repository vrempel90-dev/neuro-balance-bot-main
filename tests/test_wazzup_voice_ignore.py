from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main
import state
from config import get_settings


class _DoneTask:
    def add_done_callback(self, _callback):
        return None


def _run_ready_coroutine(coro):
    try:
        coro.send(None)
    except StopIteration:
        return _DoneTask()
    finally:
        coro.close()
    return _DoneTask()


def _payload(**message_overrides):
    message = {
        "id": "msg-1",
        "chatId": "77011234567",
        "chatType": "whatsapp",
        "channelId": "channel-1",
        "fromMe": False,
    }
    message.update(message_overrides)
    return {"messages": [message]}


def _prepare(monkeypatch, tmp_path):
    settings = get_settings()
    settings.sqlite_path = str(tmp_path / "test.sqlite3")
    settings.webhook_secret = ""
    settings.message_debounce_seconds = 0
    state.init_db()

    sent = []
    handled = []

    async def fake_send_text(**kwargs):
        sent.append(kwargs)
        return {"ok": True}

    async def fake_handle_message(**kwargs):
        handled.append(kwargs)
        return "ok"

    monkeypatch.setattr(main, "send_text", fake_send_text)
    monkeypatch.setattr(main, "handle_message", fake_handle_message)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    return sent, handled


def test_wazzup_audio_type_is_ignored(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event_type, payload: events.append(event_type))
    sent, handled = _prepare(monkeypatch, tmp_path)
    response = TestClient(main.app).post("/webhook/wazzup", json=_payload(type="audio"))

    assert response.status_code == 200
    assert sent == []
    assert handled == []
    assert "ignored_voice_message" in events


def test_wazzup_voice_type_is_ignored(monkeypatch, tmp_path):
    sent, handled = _prepare(monkeypatch, tmp_path)
    response = TestClient(main.app).post("/webhook/wazzup", json=_payload(type="voice"))

    assert response.status_code == 200
    assert sent == []
    assert handled == []


def test_wazzup_audio_mime_type_is_ignored(monkeypatch, tmp_path):
    sent, handled = _prepare(monkeypatch, tmp_path)
    response = TestClient(main.app).post("/webhook/wazzup", json=_payload(mimeType="audio/ogg"))

    assert response.status_code == 200
    assert sent == []
    assert handled == []


def test_wazzup_audio_filename_is_ignored(monkeypatch, tmp_path):
    sent, handled = _prepare(monkeypatch, tmp_path)
    response = TestClient(main.app).post("/webhook/wazzup", json=_payload(fileName="voice.ogg"))

    assert response.status_code == 200
    assert sent == []
    assert handled == []


def test_wazzup_text_payload_is_processed_as_before(monkeypatch, tmp_path):
    sent, handled = _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(asyncio, "create_task", _run_ready_coroutine)

    response = TestClient(main.app).post("/webhook/wazzup", json=_payload(text="Здравствуйте"))

    assert response.status_code == 200
    assert len(handled) == 1
    assert handled[0]["user_text"] == "Здравствуйте"
    assert len(sent) == 1

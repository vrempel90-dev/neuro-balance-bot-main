from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("BOT_AUTO_REPLY_ENABLED", "true")
os.environ.setdefault("WAZZUP_API_KEY", "test-key")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main
import state
from config import get_settings


def setup_function():
    get_settings.cache_clear()
    state.init_db()


def _payload(**overrides):
    data = {
        "chatId": "77010000001",
        "phone": "77010000001",
        "text": "Здравствуйте, хочу записаться. Спина болит",
        "messageId": "msg-1",
        "timestamp": "2026-07-07T20:00:00+05:00",
        "direction": "incoming",
        "channelId": "chan-1",
    }
    data.update(overrides)
    return data


async def _fake_handler(message):
    assert message["source"] == "wazzup"
    assert message["force"] is False
    return "Здравствуйте! Сколько Вам лет?"


async def _fake_sender(**kwargs):
    return {"ok": True, "status_code": 200, "echo": kwargs}


def test_post_wazzup_webhook_incoming_new_lead_calls_handler_and_sends(monkeypatch):
    calls = {"handler": [], "sender": []}

    async def handler(message):
        calls["handler"].append(message)
        return "Здравствуйте! Сколько Вам лет?"

    async def sender(**kwargs):
        calls["sender"].append(kwargs)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    monkeypatch.setattr(main, "is_bot_work_time", lambda *a, **k: True)
    client = TestClient(main.app)

    response = client.post("/wazzup/webhook", json=_payload())

    assert response.status_code == 200
    assert response.json()["answer"]
    assert calls["handler"][0]["source"] == "wazzup"
    assert calls["handler"][0]["force"] is False
    assert calls["sender"]


def test_post_webhook_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", _fake_sender)
    client = TestClient(main.app)

    response = client.post("/webhook", json=_payload(messageId="msg-alias-1"))

    assert response.status_code == 200
    assert calls and calls[0]["chat_id"] == "77010000001"


def test_post_api_wazzup_webhook_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", _fake_sender)
    client = TestClient(main.app)

    response = client.post("/api/wazzup/webhook", json=_payload(messageId="msg-alias-2"))

    assert response.status_code == 200
    assert calls and calls[0]["source"] == "wazzup"


def test_outgoing_from_me_payload_is_silent(monkeypatch):
    handler_calls = []
    sender_calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: handler_calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", lambda **kwargs: sender_calls.append(kwargs) or _fake_sender(**kwargs))
    client = TestClient(main.app)

    response = client.post("/wazzup/webhook", json=_payload(fromMe=True, direction="outgoing"))

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == ""
    assert body["should_send_wazzup"] is False
    assert body["no_reply_reason"] == "non_incoming_message"
    assert handler_calls == []
    assert sender_calls == []


def test_invalid_json_is_ignored():
    client = TestClient(main.app)

    response = client.post("/wazzup/webhook", data="{not-json", headers={"content-type": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True, "reason": "invalid_json"}



def test_post_webhook_wazzup_empty_body_is_ignored(monkeypatch):
    handler_calls = []
    sender_calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: handler_calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", lambda **kwargs: sender_calls.append(kwargs) or _fake_sender(**kwargs))
    client = TestClient(main.app)

    response = client.post("/webhook/wazzup", content=b"")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True, "reason": "empty_body"}
    assert handler_calls == []
    assert sender_calls == []


def test_post_webhook_wazzup_invalid_json_is_ignored(monkeypatch):
    handler_calls = []
    sender_calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: handler_calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", lambda **kwargs: sender_calls.append(kwargs) or _fake_sender(**kwargs))
    client = TestClient(main.app)

    response = client.post("/webhook/wazzup", data="{not-json", headers={"content-type": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True, "reason": "invalid_json"}
    assert handler_calls == []
    assert sender_calls == []


def test_post_webhook_wazzup_valid_json_calls_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: calls.append(message) or _fake_handler(message))
    monkeypatch.setattr(main, "send_wazzup_message", _fake_sender)
    client = TestClient(main.app)

    response = client.post("/webhook/wazzup", json=_payload(messageId="msg-webhook-wazzup"))

    assert response.status_code == 200
    assert calls and calls[0]["source"] == "wazzup"


def test_get_wazzup_webhook_health():
    client = TestClient(main.app)

    response = client.get("/wazzup/webhook")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_get_webhook_wazzup_health():
    client = TestClient(main.app)

    response = client.get("/webhook/wazzup")

    assert response.status_code == 200
    assert response.json()["ok"] is True

def test_health_contains_wazzup_webhook_paths():
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["wazzup_webhook_paths"] == ["/wazzup/webhook", "/webhook", "/api/wazzup/webhook", "/webhook/wazzup"]


def test_debug_wazzup_config_works():
    client = TestClient(main.app)

    response = client.get("/debug/wazzup/config")

    assert response.status_code == 200
    body = response.json()
    assert body["wazzup_api_key_configured"] is True
    assert body["wazzup_channel_id_configured"] is True
    assert body["available_webhook_paths"] == ["/wazzup/webhook", "/webhook", "/api/wazzup/webhook", "/webhook/wazzup"]

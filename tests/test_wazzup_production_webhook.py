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



def test_post_webhook_wazzup_logs_bot_decision_after_received(monkeypatch):
    async def handler(message):
        return {"answer": "Здравствуйте!", "should_send_wazzup": False}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", _fake_sender)
    client = TestClient(main.app)

    response = client.post("/webhook/wazzup", json=_payload(
        phone="77712841932",
        chatId="77712841932",
        text="Здравствуйте",
        messageId="msg-decision-after-received",
        isEcho=False,
        status="inbound",
        type="text",
        channelId="7e4fc1db-0061-481c-9e67-309455af8aeb",
    ))

    assert response.status_code == 200
    events = state.recent_events_for_phone("77712841932", limit=20)
    names = [event["event"] for event in events]
    assert "wazzup_webhook_received" in names
    assert "bot_decision" in names
    assert names.index("wazzup_webhook_received") < names.index("bot_decision")

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
    assert body["no_reply_reason"] == "echo_or_outgoing_message"
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



def test_health_exposes_current_railway_wazzup_webhook_url():
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["current_public_base_url"] == "https://neuro-balance-bot-main-production.up.railway.app"
    assert body["current_wazzup_webhook_url"] == "https://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup"
    assert "https://neuro-balance-bot-final-production.up.railway.app/webhook/wazzup" in body["deprecated_wazzup_webhook_urls"]


def test_wazzup_webhook_health_exposes_url_warning():
    client = TestClient(main.app)

    response = client.get("/webhook/wazzup")

    assert response.status_code == 200
    body = response.json()
    assert body["current_wazzup_webhook_url"] == "https://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup"
    assert body["deprecated_wazzup_webhook_urls"] == ["https://neuro-balance-bot-final-production.up.railway.app/webhook/wazzup"]
    assert "deprecated" in body["webhook_url_warning"]

def test_debug_wazzup_config_works():
    client = TestClient(main.app)

    response = client.get("/debug/wazzup/config")

    assert response.status_code == 200
    body = response.json()
    assert body["wazzup_api_key_configured"] is True
    assert body["wazzup_channel_id_configured"] is True
    assert body["available_webhook_paths"] == ["/wazzup/webhook", "/webhook", "/api/wazzup/webhook", "/webhook/wazzup"]


def _messages_payload(message):
    return {"messages": [message]}


def _real_inbound_message(**overrides):
    data = {
        "messageId": "msg1",
        "dateTime": "2026-07-07T18:39:00.002Z",
        "channelId": "ch1",
        "chatType": "whatsapp",
        "chatId": "77782964389",
        "type": "text",
        "isEcho": False,
        "contact": {"phone": "77782964389"},
        "text": "Здравствуйте",
        "status": "inbound",
    }
    data.update(overrides)
    return data


def test_real_wazzup_inbound_text_is_processed_not_blocked(monkeypatch):
    calls = {"handler": [], "sender": []}

    async def handler(message):
        calls["handler"].append(message)
        return {"answer": "Здравствуйте!", "should_send_wazzup": True}

    async def sender(**kwargs):
        calls["sender"].append(kwargs)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    response = TestClient(main.app).post("/webhook/wazzup", json=_messages_payload(_real_inbound_message()))

    assert response.status_code == 200
    body = response.json()
    assert body["processed_count"] == 1
    assert body["ignored_count"] == 0
    assert body["results"][0]["no_reply_reason"] != "not_client_incoming_message"
    assert calls["handler"]
    parsed = calls["handler"][0]
    assert parsed["is_incoming"] is True
    assert parsed["message_type"] == "text"
    assert parsed["direction"] == "inbound"
    assert parsed["status"] == "inbound"
    assert parsed["is_echo"] is False
    assert parsed["from_me"] is False
    assert parsed["phone"] == "77782964389"
    assert calls["sender"]


def test_real_wazzup_inbound_ad_text_type_text_is_processed(monkeypatch):
    calls = []
    ad_text = "Хочу записаться на консультацию по АКЦИИ и получить пробную процедуру в подарок https://www.instagram.com/p/..."

    async def handler(message):
        calls.append(message)
        return "Оставьте, пожалуйста, номер телефона"

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", _fake_sender)
    response = TestClient(main.app).post("/webhook/wazzup", json=_messages_payload(_real_inbound_message(messageId="msg-ad", text=ad_text)))

    assert response.status_code == 200
    body = response.json()
    assert body["processed_count"] == 1
    assert body["ignored_count"] == 0
    assert calls and calls[0]["is_incoming"] is True
    assert calls[0]["message_type"] == "text"


def test_wazzup_outbound_echo_is_ignored_without_handler_or_crm(monkeypatch):
    handler_calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: handler_calls.append(message) or _fake_handler(message))
    response = TestClient(main.app).post("/webhook/wazzup", json=_messages_payload(_real_inbound_message(isEcho=True, status="outbound")))

    assert response.status_code == 200
    body = response.json()
    assert body["processed_count"] == 0
    assert body["ignored_count"] == 1
    assert body["results"][0]["no_reply_reason"] == "echo_or_outgoing_message"
    assert handler_calls == []


def test_wazzup_from_me_true_is_ignored_without_handler(monkeypatch):
    handler_calls = []
    monkeypatch.setattr(main, "handle_incoming_message", lambda message: handler_calls.append(message) or _fake_handler(message))
    response = TestClient(main.app).post("/webhook/wazzup", json=_messages_payload(_real_inbound_message(fromMe=True)))

    assert response.status_code == 200
    body = response.json()
    assert body["processed_count"] == 0
    assert body["ignored_count"] == 1
    assert body["results"][0]["no_reply_reason"] == "echo_or_outgoing_message"
    assert handler_calls == []


def test_allowed_channel_ids_accepts_secondary_channel_and_sends_on_payload_channel(monkeypatch):
    calls = []
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", "7e4fc1db-0061-481c-9e67-309455af8aeb")
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", "7e4fc1db-0061-481c-9e67-309455af8aeb,15e83f65-253b-4c16-a894-effd547d6b76")
    get_settings.cache_clear()

    async def handler(message):
        return {"answer": "Здравствуйте!", "should_send_wazzup": True}

    async def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    monkeypatch.setattr(main, "is_bot_work_time", lambda *a, **k: True)
    client = TestClient(main.app)
    secondary = "15e83f65-253b-4c16-a894-effd547d6b76"

    response = client.post("/wazzup/webhook", json=_payload(channelId=secondary, messageId="msg-secondary-channel"))

    assert response.status_code == 200
    assert response.json()["should_send_wazzup"] is True
    assert calls[0]["channel_id"] == secondary
    events = state.recent_events_for_phone("77010000001", limit=20)
    decisions = [e for e in events if e["event"] == "bot_decision"]
    assert decisions[-1]["payload"]["channel_id_match"] is True


def test_empty_text_logs_crm_lookup_skipped_not_result(monkeypatch):
    monkeypatch.setattr(main, "handle_incoming_message", _fake_handler)
    client = TestClient(main.app)

    response = client.post("/wazzup/webhook", json=_payload(chatId="77010000999", phone="77010000999", text="", type="text", messageId="msg-empty-text"))

    assert response.status_code == 200
    events = state.recent_events_for_phone("77010000999", limit=20)
    names = [event["event"] for event in events]
    assert "crm_lookup_skipped" in names
    assert "crm_lookup_result" not in names

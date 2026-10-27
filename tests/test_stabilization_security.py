"""Security and outbound-delivery regressions; no external network calls."""
import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import state
from config import get_settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "security.sqlite3"))
    monkeypatch.setenv("OPENAI_DEBUG_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("DURABLE_WEBHOOK_ENABLED", "false")
    get_settings.cache_clear()
    state.init_db()


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/debug/time", None),
    ("GET", "/debug/last-production-events", None),
    ("GET", "/debug/last-events?phone=synthetic", None),
    ("GET", "/debug/crm/lookup?phone=synthetic", None),
    ("GET", "/debug/crm-check", None),
    ("POST", "/debug/crm/lookup", {"phone": "synthetic"}),
    ("POST", "/debug/chat", {"text": "test", "force": True}),
    ("POST", "/debug/reset", {"chat_id": "synthetic"}),
    ("POST", "/debug/replay-last-wazzup?dry_run=false", {}),
    ("POST", "/debug/wazzup/incoming-test", {}),
    ("POST", "/debug/voice", {}),
])
def test_private_routes_require_admin_before_side_effects(method, path, body, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized request reached patient pipeline")
    monkeypatch.setattr(main, "handle_message", forbidden)
    response = TestClient(main.app).request(method, path, json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["cache-control"] == "no-store"


def test_missing_admin_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("OPENAI_DEBUG_ADMIN_TOKEN")
    assert TestClient(main.app).get("/debug/time").status_code == 503


def test_valid_admin_can_inspect_time_without_cache():
    response = TestClient(main.app).get("/debug/time", headers={"Authorization": "bearer test-admin"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", main.WAZZUP_WEBHOOK_PATHS)
def test_configured_webhook_secret_protects_every_alias(path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook")
    get_settings.cache_clear()
    client = TestClient(main.app)
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(path, json={}, headers={"Authorization": "bearer test-webhook"}).status_code == 200


def test_webhook_payload_limit_rejects_before_processing():
    response = TestClient(main.app).post("/webhook/wazzup", content=b"x" * 262145)
    assert response.status_code == 413


def test_scoped_message_keys_do_not_collide_between_clients():
    first = main._normalize_wazzup_message({}, {"chatId": "synthetic-a", "messageId": "same", "text": "hello", "status": "inbound"})
    second = main._normalize_wazzup_message({}, {"chatId": "synthetic-b", "messageId": "same", "text": "hello", "status": "inbound"})
    assert first["message_key"] != second["message_key"]
    assert first["message_key"] == main._normalize_wazzup_message({}, {"chatId": "synthetic-a", "messageId": "same", "text": "hello", "status": "inbound"})["message_key"]


def test_foreign_channel_never_calls_crm_or_ai(monkeypatch):
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", "allowed")
    get_settings.cache_clear()
    async def forbidden(*args, **kwargs):
        raise AssertionError("foreign channel reached CRM/AI")
    monkeypatch.setattr(main, "handle_incoming_message", forbidden)
    response = TestClient(main.app).post("/webhook/wazzup", json={
        "chatId": "synthetic-a", "messageId": "foreign", "text": "hello",
        "channelId": "foreign", "status": "inbound",
    })
    assert response.status_code == 200
    assert response.json()["no_reply_reason"] == "channel_id_mismatch"


def test_uncertain_send_is_attempted_once_and_not_reported_success(monkeypatch):
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", "allowed")
    get_settings.cache_clear()
    sends = []
    async def handler(message):
        return {"answer": "Сколько Вам лет?", "should_send_wazzup": True}
    async def sender(**kwargs):
        sends.append(kwargs)
        raise httpx.ReadTimeout("synthetic provider timeout")
    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    request = SimpleNamespace(url=SimpleNamespace(path="/webhook/wazzup"), method="POST")
    raw = {"chatId": "synthetic-send", "messageId": "uncertain", "channelId": "allowed", "text": "test", "status": "inbound"}
    async def scenario():
        first = await main._process_wazzup_message(request, raw, raw, {"ok": True})
        second = await main._process_wazzup_message(request, raw, raw, {"ok": True})
        return first, second
    first, second = asyncio.run(scenario())
    assert first["ok"] is False and first["no_reply_reason"] == "send_uncertain"
    assert first["should_send_wazzup"] is False
    assert second["delivery_status"] == "uncertain"
    assert len(sends) == 1


def test_uvicorn_access_logs_never_include_patient_query():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %s',
        ("test-client", "GET", "/debug/crm/lookup?phone=synthetic-private-contact", "1.1", 200), None)
    main._WebhookAccessLogFilter().filter(record)
    assert "synthetic-private-contact" not in record.getMessage()


def test_observer_does_not_export_personal_or_medical_free_text():
    import repair_observer
    result = repair_observer._safe_payload(
        chat_id="synthetic-contact", user_text="Synthetic Name, synthetic medical complaint",
        session={"patient_name": "Synthetic Name", "message_id": "synthetic-private-id"},
        answer="Synthetic private answer",
    )
    assert result["telemetry_only"] is True
    for private in ("Synthetic Name", "synthetic medical complaint", "Synthetic private answer", "synthetic-private-id"):
        assert private not in str(result)


def test_staff_outgoing_message_mutes_ai_while_own_echo_does_not(monkeypatch):
    settings = get_settings()
    settings.wazzup_allowed_channel_ids = "allowed"
    request = SimpleNamespace(url=SimpleNamespace(path="/webhook/wazzup"), method="POST")
    human = {"chatId": "synthetic-human", "messageId": "human", "channelId": "allowed",
             "text": "Здравствуйте, это сотрудник клиники", "fromMe": True, "status": "outbound"}
    asyncio.run(main._process_wazzup_message(request, human, human, {"ok": True}))
    assert state.get_session("synthetic-human")["manual_takeover"] is True
    own = dict(human, chatId="synthetic-echo", messageId="own", fromApi=True)
    asyncio.run(main._process_wazzup_message(request, own, own, {"ok": True}))
    assert state.get_session("synthetic-echo")["manual_takeover"] is False


def test_durable_provider_token_protects_all_aliases_after_readback(monkeypatch):
    import production_webhook_auth as auth
    auth._save_pending("synthetic-hook-secret", "https://test.invalid/webhook", "https://test.invalid/webhook?hook_token=synthetic-hook-secret", {})
    auth._set_status("enabled")
    client = TestClient(main.app)
    for path in main.WAZZUP_WEBHOOK_PATHS:
        assert client.post(path, json={}).status_code == 401
        assert client.post(path + "?hook_token=wrong", json={}).status_code == 401
        assert client.post(path + "?hook_token=synthetic-hook-secret", json={}).status_code == 200
    assert client.get("/debug/time?hook_token=synthetic-hook-secret").status_code == 401

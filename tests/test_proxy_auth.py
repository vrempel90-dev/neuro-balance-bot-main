"""Observed CRM forwarding authentication, using synthetic headers only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json

from fastapi.testclient import TestClient
import pytest

import main
import proxy_auth
import state
from config import get_settings


SECRET = "synthetic-crm-forwarding-secret"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "proxy-auth.sqlite3"))
    monkeypatch.setenv("CRM_BOT_SECRET", SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("headers", [
    {"X-Bot-Secret": SECRET}, {"x-bot-secret": SECRET},
    {"Authorization": "Bearer " + SECRET}, {"aUtHoRiZaTiOn": "bEaReR " + SECRET},
])
def test_valid_requires_actual_matching_credential(headers):
    assert proxy_auth.valid(headers) is True


@pytest.mark.parametrize("headers", [
    {}, {"X-Bot-Secret": "wrong"}, {"Authorization": "Bearer wrong"},
    {"Authorization": "Basic " + SECRET}, {"X-Forwarded-For": "127.0.0.1"},
    {"X-Bot-Secret": ""}, {"Authorization": SECRET},
])
def test_unverified_headers_never_count_as_authenticated(headers):
    proxy_auth.observe(headers)
    assert proxy_auth.valid(headers) is False
    result = proxy_auth.enable_if_observed()
    assert result["ok"] is False and not proxy_auth.required()


def test_empty_configured_secret_cannot_authenticate_or_enable(monkeypatch):
    monkeypatch.setenv("CRM_BOT_SECRET", "")
    get_settings.cache_clear()
    assert not proxy_auth.valid({"X-Bot-Secret": ""})
    proxy_auth.observe({"X-Bot-Secret": ""})
    assert not proxy_auth.enable_if_observed()["ok"]


def test_observation_alone_does_not_enable_enforcement_and_contains_no_secret():
    proxy_auth.observe({"X-Bot-Secret": SECRET, "X-Unrelated": "synthetic-private"})
    assert not proxy_auth.required()
    summary = proxy_auth.summary()
    assert summary["authenticated_count"] == 1 and summary["unverified_count"] == 0
    assert summary["current_credential_matches"] is True
    dumped = json.dumps(summary)
    assert SECRET not in dumped and "synthetic-private" not in dumped
    assert "fingerprint" not in dumped


def test_successful_enable_is_durable_and_idempotent():
    proxy_auth.observe({"X-Bot-Secret": SECRET})
    first = proxy_auth.enable_if_observed()
    assert first["ok"] and first["verified"] and first["changed"]
    importlib.reload(proxy_auth)
    state.init_db()
    assert proxy_auth.required()
    second = proxy_auth.enable_if_observed()
    assert second["ok"] and second["verified"] and not second["changed"]


def test_enabled_contract_remains_verifiable_after_observation_ages():
    proxy_auth.observe({"X-Bot-Secret": SECRET})
    assert proxy_auth.enable_if_observed()["ok"]
    timestamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with state._connect() as conn:
        conn.execute("UPDATE crm_forwarding_auth SET last_authenticated_at=?", (timestamp,))
    assert proxy_auth.required()
    assert proxy_auth.summary()["current_credential_matches"] is True
    result = proxy_auth.enable_if_observed()
    assert result["ok"] and result["verified"] and not result["changed"]


@pytest.mark.parametrize("delta_seconds", [-901, 60])
def test_stale_or_future_observation_cannot_activate_auth(delta_seconds):
    proxy_auth.observe({"X-Bot-Secret": SECRET})
    timestamp = (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()
    with state._connect() as conn:
        conn.execute("UPDATE crm_forwarding_auth SET last_authenticated_at=?", (timestamp,))
    result = proxy_auth.enable_if_observed()
    assert not result["ok"] and not proxy_auth.required()
    assert result["reason"] == "recent_current_credential_forwarding_not_observed"


def test_rotated_secret_requires_fresh_proof_and_enforcement_stays_fail_closed(monkeypatch):
    proxy_auth.observe({"X-Bot-Secret": SECRET})
    assert proxy_auth.enable_if_observed()["ok"]
    monkeypatch.setenv("CRM_BOT_SECRET", "synthetic-rotated")
    get_settings.cache_clear()
    assert proxy_auth.required(), "Rotation must never reopen unauthenticated ingress"
    assert proxy_auth.summary()["current_credential_matches"] is False
    assert not proxy_auth.valid({"X-Bot-Secret": SECRET})
    assert not proxy_auth.enable_if_observed()["verified"]
    proxy_auth.observe({"X-Bot-Secret": "synthetic-rotated"})
    assert proxy_auth.enable_if_observed()["verified"]


@pytest.mark.parametrize("path", main.WAZZUP_WEBHOOK_PATHS)
def test_every_webhook_alias_enforces_observed_contract_without_processing_patient(path, monkeypatch):
    proxy_auth.observe({"X-Bot-Secret": SECRET})
    assert proxy_auth.enable_if_observed()["ok"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("Unauthorized webhook reached message processor")

    monkeypatch.setattr(main, "handle_incoming_message", forbidden)
    client = TestClient(main.app)
    assert client.post(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(path, json={}, headers={"X-Bot-Secret": "wrong"}).status_code == 401
    assert client.post(path, json={}, headers={"X-Bot-Secret": SECRET}).status_code == 200
    assert client.post(path, json={}, headers={"Authorization": "Bearer " + SECRET}).status_code == 200

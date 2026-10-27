"""Safely add an unguessable Wazzup ingress token to its existing URI.

The provider configuration is changed only when it already points directly to
this production app. The new token is durable before the single PATCH attempt;
enforcement starts only after provider read-back proves the URI was saved.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from config import get_settings
import state

_LOCK = asyncio.Lock()
_PATHS = {"/wazzup/webhook", "/webhook", "/api/wazzup/webhook", "/webhook/wazzup"}
_DDL = """
CREATE TABLE IF NOT EXISTS webhook_auth_migration (
    id INTEGER PRIMARY KEY CHECK (id=1),
    token TEXT NOT NULL,
    status TEXT NOT NULL,
    original_uri TEXT NOT NULL,
    target_uri TEXT NOT NULL,
    subscriptions_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _stored() -> dict[str, Any] | None:
    with state._connect() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='webhook_auth_migration'").fetchone()
        if not exists:
            return None
        row = conn.execute("SELECT * FROM webhook_auth_migration WHERE id=1").fetchone()
    return dict(row) if row else None


def required_token() -> str:
    """Do not turn a pending/uncertain provider migration into an outage."""
    explicit = str(getattr(get_settings(), "webhook_secret", "") or "").strip()
    if explicit:
        return explicit
    stored = _stored()
    if stored and stored.get("status") == "enabled":
        return str(stored["token"])
    return ""


def _save_pending(token: str, original: str, target: str, subscriptions: Any) -> None:
    with state._connect() as conn:
        conn.execute(_DDL)
        conn.execute(
            "INSERT INTO webhook_auth_migration (id,token,status,original_uri,target_uri,subscriptions_json,updated_at) "
            "VALUES (1,?,'pending',?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "token=excluded.token,status='pending',original_uri=excluded.original_uri,"
            "target_uri=excluded.target_uri,subscriptions_json=excluded.subscriptions_json,updated_at=excluded.updated_at",
            (token, original, target, json.dumps(subscriptions), state.now_iso()),
        )


def _set_status(status: str) -> None:
    if status not in {"attempted", "enabled", "uncertain", "rejected"}:
        raise ValueError("invalid webhook migration state")
    with state._connect() as conn:
        conn.execute("UPDATE webhook_auth_migration SET status=?,updated_at=? WHERE id=1", (status, state.now_iso()))


def _safe_destination(uri: str) -> dict[str, str]:
    try:
        parsed = urlsplit(uri)
        return {"destination_host": str(parsed.hostname or ""), "destination_path": parsed.path}
    except ValueError:
        return {"destination_host": "", "destination_path": ""}


def _owned_destination(uri: str, base: str) -> bool:
    try:
        candidate, expected = urlsplit(uri), urlsplit(base)
        return bool(
            candidate.scheme == expected.scheme == "https"
            and candidate.hostname
            and candidate.hostname.lower() == str(expected.hostname or "").lower()
            and candidate.port == expected.port
            and candidate.username is None and candidate.password is None
            and candidate.path in _PATHS
            and not candidate.fragment
            and not expected.path.rstrip("/")
        )
    except ValueError:
        return False


def _target_uri(uri: str, token: str) -> str:
    parts = urlsplit(uri)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "hook_token"]
    query.append(("hook_token", token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


async def _read(client: httpx.AsyncClient, endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(endpoint, headers=headers)
    response.raise_for_status()
    data = response.json()
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("webhooksUri"), str)
        or "subscriptions" not in data
        or not isinstance(data["subscriptions"], (dict, list))
    ):
        raise ValueError("invalid webhook configuration contract")
    return data


async def secure_existing_webhook() -> dict[str, Any]:
    """Migrate only the existing direct webhook; no repointing and no blind retry."""
    async with _LOCK:
        settings = get_settings()
        api_key = str(getattr(settings, "wazzup_api_key", "") or "").strip()
        if not api_key:
            return {"ok": False, "auth_enabled": bool(required_token()), "reason": "wazzup_not_configured", "changed": False}
        endpoint = str(settings.wazzup_api_url).rstrip("/") + "/webhooks"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0), follow_redirects=False) as client:
            try:
                current = await _read(client, endpoint, headers)
            except Exception as exc:
                return {"ok": False, "auth_enabled": bool(required_token()), "reason": "configuration_read_failed", "error_type": type(exc).__name__, "changed": False}
            uri = current["webhooksUri"]
            destination = _safe_destination(uri)
            if not _owned_destination(uri, str(settings.public_base_url).rstrip("/")):
                return {"ok": False, "auth_enabled": bool(required_token()), "reason": "existing_webhook_is_not_direct_production_destination", "changed": False, **destination}
            stored = _stored()
            explicit = str(getattr(settings, "webhook_secret", "") or "").strip()
            # Recover the exact previously persisted target without generating a
            # new token or submitting a second potentially duplicated PATCH.
            if stored and (not explicit or explicit == stored["token"]):
                if uri == stored["target_uri"] and (stored["status"] == "enabled" or current["subscriptions"] == json.loads(stored["subscriptions_json"])):
                    _set_status("enabled")
                    return {"ok": True, "auth_enabled": True, "verified": True, "changed": False, "reason": "already_secured", **destination}
                if stored["status"] in {"attempted", "uncertain", "rejected", "enabled"}:
                    if stored["status"] not in {"rejected", "enabled"}:
                        _set_status("uncertain")
                    return {"ok": False, "auth_enabled": bool(required_token()), "verified": False, "changed": False, "reason": "prior_migration_requires_reconciliation", **destination}
                token = str(stored["token"])
            else:
                token = explicit or secrets.token_urlsafe(32)
            target = _target_uri(uri, token)
            _save_pending(token, uri, target, current["subscriptions"])
            # Persist the network boundary before sending. A crash/timeout must
            # not make a future invocation repeat a PATCH with unknown outcome.
            _set_status("attempted")
            patch_error: str | None = None
            rejected = False
            try:
                response = await client.patch(endpoint, headers=headers, json={
                    "webhooksUri": target,
                    "subscriptions": current["subscriptions"],
                })
                response.raise_for_status()
            except Exception as exc:
                patch_error = type(exc).__name__
                status = getattr(getattr(exc, "response", None), "status_code", None)
                rejected = status is not None and 400 <= status < 500
            try:
                verified = await _read(client, endpoint, headers)
            except Exception as exc:
                _set_status("uncertain")
                return {"ok": False, "auth_enabled": bool(explicit), "verified": False, "changed": False, "reason": "configuration_write_outcome_uncertain", "error_type": type(exc).__name__, **destination}
            if verified["webhooksUri"] == target and verified["subscriptions"] == current["subscriptions"]:
                _set_status("enabled")
                return {"ok": True, "auth_enabled": True, "verified": True, "changed": True, "reason": "secured_and_verified", **destination}
            _set_status("rejected" if rejected else "uncertain")
            result = {"ok": False, "auth_enabled": bool(explicit), "verified": False, "changed": False, "reason": "configuration_patch_rejected" if rejected else "configuration_write_outcome_uncertain", **destination}
            if patch_error:
                result["error_type"] = patch_error
            return result

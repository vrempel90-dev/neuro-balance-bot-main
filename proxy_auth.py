"""Observe an existing authenticated CRM forwarding contract without secrets.

Enforcement is enabled only after a request actually proved possession of the
configured CRM integration secret. Never infer authentication from a header's
name, source IP or the mere existence of the CRM gateway.
"""
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any

from config import get_settings
import state

_DDL = """
CREATE TABLE IF NOT EXISTS crm_forwarding_auth (
    id INTEGER PRIMARY KEY CHECK(id=1),
    authenticated_count INTEGER NOT NULL DEFAULT 0,
    unverified_count INTEGER NOT NULL DEFAULT 0,
    last_authenticated_at TEXT NOT NULL DEFAULT '',
    credential_fingerprint TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0
)
"""


def valid(headers: Any) -> bool:
    secret = str(getattr(get_settings(), "crm_bot_secret", "") or "")
    if not secret:
        return False
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    candidates = [normalized.get("x-bot-secret", "")]
    authorization = normalized.get("authorization", "").split(None, 1)
    if len(authorization) == 2 and authorization[0].lower() == "bearer":
        candidates.append(authorization[1])
    expected = secret.encode("utf-8")
    return any(hmac.compare_digest(candidate.encode("utf-8"), expected) for candidate in candidates)


def observe(headers: Any) -> None:
    authenticated = valid(headers)
    secret = str(getattr(get_settings(), "crm_bot_secret", "") or "")
    fingerprint = hashlib.sha256(secret.encode()).hexdigest() if authenticated else ""
    with state._connect() as conn:
        conn.execute(_DDL)
        conn.execute("INSERT OR IGNORE INTO crm_forwarding_auth(id) VALUES(1)")
        conn.execute(
            "UPDATE crm_forwarding_auth SET authenticated_count=authenticated_count+?, "
            "unverified_count=unverified_count+?, last_authenticated_at="
            "CASE WHEN ? THEN ? ELSE last_authenticated_at END, credential_fingerprint="
            "CASE WHEN ? THEN ? ELSE credential_fingerprint END WHERE id=1",
            (int(authenticated), int(not authenticated), int(authenticated), state.now_iso(), int(authenticated), fingerprint),
        )


def summary() -> dict[str, Any]:
    empty = {"enabled": False, "authenticated_count": 0, "unverified_count": 0,
             "last_authenticated_at": "", "current_credential_matches": False}
    with state._connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='crm_forwarding_auth'").fetchone():
            return empty
        row = conn.execute("SELECT * FROM crm_forwarding_auth WHERE id=1").fetchone()
    if not row:
        return empty
    secret = str(getattr(get_settings(), "crm_bot_secret", "") or "")
    matches = bool(secret) and hmac.compare_digest(
        str(row["credential_fingerprint"]), hashlib.sha256(secret.encode()).hexdigest())
    return {"enabled": bool(row["enabled"]), "authenticated_count": row["authenticated_count"],
            "unverified_count": row["unverified_count"], "last_authenticated_at": row["last_authenticated_at"],
            "current_credential_matches": matches}


def required() -> bool:
    return bool(summary()["enabled"])


def enable_if_observed() -> dict[str, Any]:
    observation = summary()
    if observation["enabled"] and observation["current_credential_matches"]:
        return {"ok": True, "auth_enabled": True, "verified": True,
                "changed": False, "reason": "observed_crm_forwarding_auth_enforced"}
    if not observation["authenticated_count"]:
        return {"ok": False, "auth_enabled": False, "verified": False,
                "changed": False, "reason": "proxy_forwarding_header_not_observed"}
    if not str(getattr(get_settings(), "crm_bot_secret", "") or ""):
        return {"ok": False, "auth_enabled": False, "verified": False,
                "changed": False, "reason": "crm_integration_secret_unavailable"}
    with state._connect() as conn:
        row = conn.execute("SELECT credential_fingerprint,last_authenticated_at FROM crm_forwarding_auth WHERE id=1").fetchone()
    expected = hashlib.sha256(str(get_settings().crm_bot_secret).encode()).hexdigest()
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_authenticated_at"])).total_seconds()
        current = hmac.compare_digest(row["credential_fingerprint"], expected) and 0 <= age <= 900
    except (ValueError, TypeError):
        current = False
    if not current:
        return {"ok": False, "auth_enabled": required(), "verified": False,
                "changed": False, "reason": "recent_current_credential_forwarding_not_observed"}
    with state._connect() as conn:
        conn.execute("UPDATE crm_forwarding_auth SET enabled=1 WHERE id=1")
    return {"ok": True, "auth_enabled": True, "verified": True,
            "changed": not observation["enabled"], "reason": "observed_crm_forwarding_auth_enforced"}

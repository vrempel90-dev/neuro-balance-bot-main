from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Query, Request, Response
from jwt import PyJWKClient


app = FastAPI(title="Neuro Claude Repair Gateway")

_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_GITHUB_OIDC_JWKS = "https://token.actions.githubusercontent.com/.well-known/jwks"
_GITHUB_OIDC_AUDIENCE = os.getenv("CLAUDE_REPAIR_OIDC_AUDIENCE", "neuro-claude-repair")
_EXPECTED_REPOSITORY = os.getenv(
    "CLAUDE_REPAIR_GITHUB_REPOSITORY",
    "vrempel90-dev/neuro-balance-bot-main",
)
_EXPECTED_WORKFLOW_REF = os.getenv(
    "CLAUDE_REPAIR_WORKFLOW_REF",
    "vrempel90-dev/neuro-balance-bot-main/.github/workflows/claude-real-repair.yml@refs/heads/main",
)
_PRODUCTION_URL = os.getenv(
    "NEURO_PRODUCTION_URL",
    "https://neuro-balance-bot-main-production.up.railway.app",
).rstrip("/")
_ANTHROPIC_API_URL = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com").rstrip("/")
_ALMATY = ZoneInfo("Asia/Almaty")

_ALLOWED_EVENT_TYPES = {
    "incoming_message_received",
    "dialog_start",
    "dialog_result",
    "bot_no_reply",
    "bot_decision",
    "ai_or_template_response_ready",
    "crm_lookup_result",
    "wazzup_send_result",
    "bot_processing_finish",
    "language_guard_violation",
    "openai_brain_guard_failed",
    "openai_skipped",
    "humanize_skipped",
}

_ALLOWED_PAYLOAD_KEYS = {
    "source",
    "kind",
    "text_preview",
    "answer_preview",
    "answer_empty",
    "has_answer",
    "step",
    "current_step",
    "state_before_step",
    "state_after_step",
    "detected_intent",
    "openai_used",
    "openai_brain_used",
    "openai_brain_intent",
    "openai_brain_action",
    "openai_brain_guard_failed",
    "openai_brain_guard_reason",
    "openai_brain_skip_reason",
    "openai_skip_reason",
    "fallback_reason",
    "gate_reason",
    "no_reply_reason",
    "silent_reason",
    "should_send_wazzup",
    "success",
    "skipped",
    "dry_run",
    "status_code",
    "crm_lookup_ok",
    "crm_error",
    "crm_patient_state",
    "active_appointment_found",
    "booking_ready",
    "booking_confirmed",
    "booking_visible_in_crm",
    "last_slots_count",
    "selected_slot",
    "language_guard_result",
    "detected_language",
    "brain_detected_language",
    "brain_preferred_response_language",
    "working_hours_allowed",
    "bot_work_time_now",
    "normal_work_time_now",
    "test_window_now",
    "local_time",
    "timezone",
}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\d)")
_LONG_ID_RE = re.compile(r"(?<!\d)\d{10,18}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _night_window(now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(_ALMATY)
    return local.hour >= 20 or local.hour < 8


def _redact_text(value: str) -> str:
    text = str(value or "")
    text = _PHONE_RE.sub("[phone]", text)
    text = _LONG_ID_RE.sub("[id]", text)
    text = _EMAIL_RE.sub("[email]", text)
    return text[:1000]


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_safe_value(v) for v in value[:10]]
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            key_low = str(key).lower()
            if any(secret in key_low for secret in ("secret", "token", "api_key", "authorization", "password")):
                continue
            if any(personal in key_low for personal in ("phone", "patient_name", "crm_patient_name")):
                continue
            safe[str(key)] = _safe_value(item)
        return safe
    return _redact_text(str(value))


def _conversation_id(chat_id: str) -> str:
    return hashlib.sha256(f"neuro-repair:{chat_id}".encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    return PyJWKClient(_GITHUB_OIDC_JWKS)


def _extract_oidc_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    x_api_key = str(request.headers.get("x-api-key") or "").strip()
    if x_api_key:
        return x_api_key
    raise HTTPException(status_code=401, detail="Missing GitHub OIDC token")


def _verify_github_oidc(request: Request) -> dict[str, Any]:
    token = _extract_oidc_token(request)
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_GITHUB_OIDC_AUDIENCE,
            issuer=_GITHUB_OIDC_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid GitHub OIDC token") from exc
    if str(claims.get("repository") or "") != _EXPECTED_REPOSITORY:
        raise HTTPException(status_code=403, detail="Repository is not allowed")
    if _EXPECTED_WORKFLOW_REF and str(claims.get("workflow_ref") or "") != _EXPECTED_WORKFLOW_REF:
        raise HTTPException(status_code=403, detail="Workflow is not allowed")
    return dict(claims)


def _event_signal(event_type: str, payload: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    if event_type == "dialog_result" and payload.get("answer_empty") is True:
        no_reply = str(payload.get("no_reply_reason") or "")
        if no_reply not in {
            "working_hours_ai_disabled",
            "manual_takeover",
            "old_lead_or_active_booking",
            "old_chat_ai_disabled",
            "channel_id_mismatch",
        }:
            signals.append("unexpected_empty_answer")
    if event_type == "bot_no_reply":
        no_reply = str(payload.get("no_reply_reason") or "")
        if no_reply not in {
            "working_hours_ai_disabled",
            "manual_takeover",
            "old_lead_or_active_booking",
            "old_chat_ai_disabled",
            "channel_id_mismatch",
        }:
            signals.append("unexpected_silence")
    guard_reason = str(payload.get("openai_brain_guard_reason") or "")
    if guard_reason in {
        "asked_name_too_early",
        "slot_hallucination",
        "need_date_after_contraindications_clear",
        "show_slots_multi_entity_guard",
        "unsafe_decision",
    }:
        signals.append(f"brain_guard:{guard_reason}")
    language_result = str(payload.get("language_guard_result") or "")
    if language_result and language_result not in {"ok", "unknown", ""}:
        signals.append(f"language_guard:{language_result}")
    if payload.get("booking_confirmed") is True and payload.get("booking_visible_in_crm") is False:
        signals.append("booking_confirmation_not_visible_in_crm")
    if event_type == "wazzup_send_result" and payload.get("success") is False and not payload.get("skipped") and not payload.get("dry_run"):
        signals.append("wazzup_send_failed")
    return signals


def _sanitize_events(events: list[dict[str, Any]], *, cutoff: datetime) -> list[dict[str, Any]]:
    recent: list[dict[str, Any]] = []
    real_chat_ids: set[str] = set()

    for event in events:
        created_at = str(event.get("created_at") or "")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created.astimezone(timezone.utc) < cutoff:
            continue
        event_type = str(event.get("event_type") or event.get("event") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "incoming_message_received" and str(payload.get("source") or "").lower() == "wazzup":
            real_chat_ids.add(str(event.get("chat_id") or ""))
        recent.append(event)

    out: list[dict[str, Any]] = []
    for event in recent:
        chat_id = str(event.get("chat_id") or "")
        if not chat_id or chat_id not in real_chat_ids:
            continue
        event_type = str(event.get("event_type") or event.get("event") or "")
        if event_type not in _ALLOWED_EVENT_TYPES:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        safe_payload = {
            key: _safe_value(payload.get(key))
            for key in _ALLOWED_PAYLOAD_KEYS
            if key in payload
        }
        signals = _event_signal(event_type, payload)
        out.append(
            {
                "created_at": str(event.get("created_at") or ""),
                "conversation_id": _conversation_id(chat_id),
                "event": event_type,
                "payload": safe_payload,
                "signals": signals,
            }
        )
    return out[-250:]


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "neuro-claude-repair-gateway"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "anthropic_key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "production_url_configured": bool(_PRODUCTION_URL),
        "night_window_now": _night_window(),
        "timezone": "Asia/Almaty",
        "window": "20:00-08:00",
    }


@app.get("/real-events")
async def real_events(request: Request, minutes: int = Query(default=20, ge=5, le=60)) -> dict[str, Any]:
    claims = _verify_github_oidc(request)
    if not _night_window():
        return {
            "ok": True,
            "active": False,
            "reason": "outside_20_08_asia_almaty",
            "events": [],
        }
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.get(f"{_PRODUCTION_URL}/debug/last-production-events", params={"limit": 500})
            response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Cannot read production event stream") from exc
    raw_events = body.get("events") if isinstance(body, dict) and isinstance(body.get("events"), list) else []
    events = _sanitize_events(raw_events, cutoff=cutoff)
    return {
        "ok": True,
        "active": True,
        "source": "real_wazzup_production",
        "repository": str(claims.get("repository") or ""),
        "minutes": minutes,
        "events": events,
        "signal_count": sum(len(event.get("signals") or []) for event in events),
    }


async def _proxy_anthropic(request: Request, upstream_path: str) -> Response:
    _verify_github_oidc(request)
    api_key = str(os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured")
    body = await request.body()
    headers = {
        "x-api-key": api_key,
        "anthropic-version": str(request.headers.get("anthropic-version") or "2023-06-01"),
        "content-type": str(request.headers.get("content-type") or "application/json"),
    }
    anthropic_beta = str(request.headers.get("anthropic-beta") or "").strip()
    if anthropic_beta:
        headers["anthropic-beta"] = anthropic_beta
    url = f"{_ANTHROPIC_API_URL}{upstream_path}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            upstream = await client.request(
                request.method,
                url,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Anthropic upstream unavailable") from exc
    passthrough_headers: dict[str, str] = {}
    for key in ("request-id", "anthropic-ratelimit-requests-remaining", "anthropic-ratelimit-tokens-remaining"):
        value = upstream.headers.get(key)
        if value:
            passthrough_headers[key] = value
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=passthrough_headers,
    )


@app.post("/v1/messages")
async def proxy_messages(request: Request) -> Response:
    return await _proxy_anthropic(request, "/v1/messages")


@app.post("/v1/messages/count_tokens")
async def proxy_count_tokens(request: Request) -> Response:
    return await _proxy_anthropic(request, "/v1/messages/count_tokens")

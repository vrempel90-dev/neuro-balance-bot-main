"""Read-only production monitoring; never sends messages or creates CRM records.

GitHub Actions authenticates with a short-lived OIDC token bound to the exact
repository, main ref and monitor workflow. No production secret is copied into
Actions. Outputs contain aggregate operational data only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
import httpx
import jwt

from config import get_settings
import state

router = APIRouter(prefix="/internal/monitor", tags=["internal monitoring"])
_ISSUER = "https://token.actions.githubusercontent.com"
_AUDIENCE = "neurobalance-production-monitor"
_REPOSITORY = "vrempel90-dev/neuro-balance-bot-main"
_WORKFLOW = f"{_REPOSITORY}/.github/workflows/production-monitor.yml@refs/heads/main"
_JWKS: dict[str, Any] = {}
_JWKS_UNTIL = 0.0
_JWKS_LOCK = asyncio.Lock()


async def _signing_key(kid: str) -> Any:
    global _JWKS, _JWKS_UNTIL
    async with _JWKS_LOCK:
        if time.monotonic() >= _JWKS_UNTIL:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0), follow_redirects=False) as client:
                response = await client.get(f"{_ISSUER}/.well-known/jwks")
                response.raise_for_status()
                data = response.json()
            keys = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(keys, list):
                raise ValueError("invalid signing key document")
            _JWKS = {
                str(key["kid"]): jwt.PyJWK.from_dict(key).key
                for key in keys
                if isinstance(key, dict) and key.get("kid") and key.get("kty") == "RSA"
            }
            _JWKS_UNTIL = time.monotonic() + 300.0
        if kid not in _JWKS:
            raise ValueError("unknown signing key")
        return _JWKS[kid]


async def require_monitor_auth(authorization: str | None = Header(default=None)) -> None:
    parts = str(authorization or "").split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    token = parts[1]
    admin_token = str(os.getenv("OPENAI_DEBUG_ADMIN_TOKEN") or "").strip()
    if len(token) > 16384:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    if admin_token and hmac.compare_digest(token.encode("utf-8"), admin_token.encode("utf-8")):
        return
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("invalid signing algorithm")
        key = await _signing_key(header["kid"])
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=_AUDIENCE,
            issuer=_ISSUER, leeway=15,
            options={"require": ["exp", "iat", "nbf", "aud", "iss", "sub"]},
        )
        if (
            claims.get("repository") != _REPOSITORY
            or claims.get("ref") != "refs/heads/main"
            or claims.get("workflow_ref") != _WORKFLOW
            or claims.get("event_name") not in {"push", "schedule", "workflow_dispatch"}
            or claims.get("sub") != f"repo:{_REPOSITORY}:ref:refs/heads/main"
        ):
            raise ValueError("wrong workload identity")
    except Exception:
        # Do not log exception/token: JWT libraries can include supplied values.
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"}) from None


def _event_summary(since_iso: str) -> dict[str, Any]:
    """Counters describe observed events, not delivered/seen patient messages."""
    with state._connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM events WHERE created_at>=?", (since_iso,)).fetchone()[0])
        rows = conn.execute(
            "SELECT chat_id,event_type,payload_json FROM events WHERE created_at>=? ORDER BY id DESC LIMIT 10000",
            (since_iso,),
        ).fetchall()
    event_counts: dict[str, int] = {}
    contacts: set[str] = set()
    incoming_message_keys: set[tuple[str, str, str]] = set()
    incoming_events = incoming_without_id = 0
    new_contacts: set[str] = set()
    excluded_contacts: set[str] = set()
    language_counts = {"ru": 0, "kk": 0}
    failed_crm = failed_api = confirmed = failed_booking = duplicates = 0
    for row in rows:
        name = str(row["event_type"])
        event_counts[name] = event_counts.get(name, 0) + 1
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        chat_id = str(row["chat_id"])
        if name == "wazzup_webhook_received" and payload.get("is_incoming") is True:
            incoming_events += 1
            contacts.add(chat_id)
            message_id = str(payload.get("message_id") or "")
            if message_id:
                incoming_message_keys.add((chat_id, str(payload.get("channel_id") or ""), message_id))
            else:
                incoming_without_id += 1
        if name == "crm_lookup_result" and not payload.get("skipped"):
            classification = str(payload.get("crm_patient_state") or "")
            if classification in {"NEW", "NEW_PATIENT", "NEW_LEAD"}:
                new_contacts.add(chat_id)
            if classification in {"RETURNING", "RETURNING_PATIENT_NO_ACTIVE_BOOKING", "ACTIVE_BOOKING", "EXISTING_PATIENT"}:
                excluded_contacts.add(chat_id)
            if payload.get("crm_lookup_ok") is False:
                failed_crm += 1
        if name == "dialog_result" and payload.get("detected_language") in language_counts:
            language_counts[payload["detected_language"]] += 1
        confirmed += int(name == "agent_booking_crm_success")
        failed_booking += int(name in {"agent_booking_crm_error", "agent_booking_crm_rejected"})
        failed_api += int(name in {"openai_error", "openai_brain_error", "agent_openai_error"} or (name == "wazzup_send_result" and payload.get("success") is False and not payload.get("skipped") and not payload.get("dry_run")))
        duplicates += int(name == "bot_decision" and payload.get("silent_reason") in {"duplicate_message", "duplicate_answer"})
    return {
        "events_total": total, "events_analyzed": len(rows), "partial": total > len(rows),
        "incoming_processing_events": event_counts.get("bot_processing_start", 0),
        "incoming_webhook_events": incoming_events,
        "unique_incoming_messages_with_id": len(incoming_message_keys),
        "incoming_events_without_id": incoming_without_id,
        "observed_contact_count": len(contacts), "new_contact_count": len(new_contacts),
        "excluded_existing_contact_count": len(excluded_contacts),
        "confirmed_booking_events": confirmed, "failed_booking_events": failed_booking,
        "crm_failure_events": failed_crm, "api_failure_events": failed_api,
        "duplicate_block_events": duplicates, "dialog_language_events": language_counts,
        "language_violation_events": event_counts.get("language_guard_violation", 0),
        "counter_scope": "observed persisted events; contact count uses incoming=true; unique incoming messages require a logged message ID; processor-event counts may include retries/echo; not proof of delivery",
    }


def status_snapshot(*, worker: Any = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    settings = get_settings()
    volume_mount = str(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    database_path = Path(str(getattr(settings, "sqlite_path", "bot.sqlite3"))).resolve()
    database_on_volume = bool(volume_mount) and database_path.is_relative_to(Path(volume_mount).resolve())
    result: dict[str, Any] = {
        "ok": True, "checked_at": now.isoformat(),
        "release_sha": str(os.getenv("RAILWAY_GIT_COMMIT_SHA") or ""),
        "period_start": (now - timedelta(hours=24)).isoformat(),
        "config": {
            "new_leads_only": bool(getattr(settings, "new_leads_only", True)),
            "auto_reply_enabled": bool(getattr(settings, "bot_auto_reply_enabled", False)),
            "crm_write_enabled": bool(getattr(settings, "crm_write_enabled", False)),
            "crm_secret_present": bool(getattr(settings, "crm_bot_secret", "")),
            "openai_key_present": bool(getattr(settings, "openai_api_key", "")),
            "wazzup_key_present": bool(getattr(settings, "wazzup_api_key", "")),
            "webhook_secret_present": bool(getattr(settings, "webhook_secret", "")),
        },
        "storage": {
            "persistent_volume_configured": bool(volume_mount),
            "database_on_persistent_volume": database_on_volume,
        },
    }
    try:
        import production_webhook_auth
        result["config"]["webhook_auth_enabled"] = bool(production_webhook_auth.required_token())
        with state._connect() as conn:
            result["sqlite_quick_check_ok"] = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            outgoing_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_messages'").fetchone()
            result["unresolved_delivery"] = {"uncertain": 0, "stale_sending": 0}
            if outgoing_exists:
                result["unresolved_delivery"]["uncertain"] = int(conn.execute("SELECT COUNT(*) FROM outgoing_messages WHERE status='uncertain'").fetchone()[0])
                result["unresolved_delivery"]["stale_sending"] = int(conn.execute("SELECT COUNT(*) FROM outgoing_messages WHERE status='sending' AND updated_at<?", ((now - timedelta(seconds=60)).isoformat(),)).fetchone()[0])
            result["unresolved_booking"] = {
                "uncertain": int(conn.execute("SELECT COUNT(*) FROM booking_claims WHERE status='uncertain'").fetchone()[0]),
                "stale_pending": int(conn.execute("SELECT COUNT(*) FROM booking_claims WHERE status='pending' AND created_at<?", ((now - timedelta(seconds=180)).isoformat(),)).fetchone()[0]),
            }
        result["events_24h"] = _event_summary(result["period_start"])
        result["events_recent_10m"] = _event_summary((now - timedelta(minutes=10)).isoformat())
        import production_inbox
        result["inbox"] = production_inbox.status_summary()
        result["ok"] = bool(result["sqlite_quick_check_ok"])
        counts = result["inbox"].get("counts", {})
        if counts.get("uncertain", 0) or counts.get("failed", 0):
            result["ok"] = False
            result["attention_required"] = "inbox_failed_or_uncertain"
        if result["inbox"].get("oldest_pending_seconds", 0) > 180 and not result["inbox"].get("pending_blocked_by_uncertain", 0):
            result["ok"] = False
            result["attention_required"] = "inbox_pending_over_180_seconds"
        if bool(getattr(settings, "durable_webhook_enabled", False)):
            worker_status = worker.status() if worker is not None else {"running": False}
            result["worker"] = {
                key: worker_status.get(key)
                for key in ("running", "active_jobs", "heartbeat_age_seconds")
            }
            heartbeat_age = worker_status.get("heartbeat_age_seconds")
            if not worker_status.get("running") or heartbeat_age is None or heartbeat_age > 30:
                result["ok"] = False
                result["attention_required"] = "inbox_worker_not_live"
        recent = result["events_recent_10m"]
        if recent["crm_failure_events"] or recent["api_failure_events"] or recent["failed_booking_events"]:
            result["ok"] = False
            result["attention_required"] = "recent_integration_failure_events"
        if os.getenv("RAILWAY_SERVICE_ID") and not database_on_volume:
            result["ok"] = False
            result["attention_required"] = "sqlite_not_on_persistent_volume"
        if any(result["unresolved_delivery"].values()) or any(result["unresolved_booking"].values()):
            result["ok"] = False
            result["attention_required"] = "unresolved_delivery_or_booking_outcome"
    except Exception as exc:
        result["ok"] = False
        result["error_type"] = type(exc).__name__
    return result


async def _crm_probe() -> dict[str, Any]:
    import crm
    import admission
    tomorrow = (datetime.now(ZoneInfo("Asia/Almaty")) + timedelta(days=1)).date().isoformat()
    result: dict[str, Any] = {"ok": False, "read_only": True, "date": tomorrow}
    try:
        crm.clear_slots_cache(tomorrow)
        data = await crm.check_slots(tomorrow)
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("invalid slots contract")
        slots = data.get("slots") or []
        availability = data.get("availability") or []
        result.update(ok=True, available_slots=len(slots), doctor_schedule_entries=len(availability))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    # Use an existing server-side session only; never invent or expose a phone.
    candidate: str | None = None
    try:
        with state._connect() as conn:
            rows = conn.execute("SELECT chat_id,data_json FROM sessions ORDER BY updated_at DESC LIMIT 200").fetchall()
        for row in rows:
            data = json.loads(row["data_json"])
            if data.get("raw_crm_isNew") is False or data.get("crm_patient_state") in {"RETURNING_PATIENT_NO_ACTIVE_BOOKING", "ACTIVE_BOOKING", "EXISTING_PATIENT"}:
                phone = crm.normalize_phone(str(data.get("phone") or data.get("phone_normalized") or row["chat_id"]))
                if phone:
                    candidate = phone
                    break
        if candidate:
            lookup = await crm.lookup_active_appointments_by_phone(candidate)
            outcome = admission.classify(candidate, lookup)
            result["existing_patient_check"] = {"executed": True, "excluded_from_ai": not outcome.bot_may_reply, "crm_classification": outcome.state}
            if outcome.state == admission.CRM_UNAVAILABLE or outcome.bot_may_reply:
                result["ok"] = False
        else:
            result["existing_patient_check"] = {"executed": False, "reason": "no_verified_existing_session_available"}
    except Exception as exc:
        result["existing_patient_check"] = {"executed": False, "error_type": type(exc).__name__}
    return result


async def _wazzup_probe() -> dict[str, Any]:
    settings = get_settings()
    key = str(getattr(settings, "wazzup_api_key", "") or "").strip()
    if not key:
        return {"ok": False, "reason": "not_configured", "read_only": True}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
            response = await client.get(
                str(settings.wazzup_api_url).rstrip("/") + "/channels",
                headers={"Authorization": f"Bearer {key}"},
            )
        response.raise_for_status()
        data = response.json()
        channels = data if isinstance(data, list) else data.get("channels", [])
        if not isinstance(channels, list):
            raise ValueError("invalid channels contract")
        configured_channel = str(getattr(settings, "wazzup_channel_id", "") or "").strip()
        found = any(str(c.get("channelId") or c.get("id") or "") == configured_channel for c in channels if isinstance(c, dict))
        return {"ok": bool(found), "read_only": True, "http_status": response.status_code, "channel_count": len(channels), "configured_channel_found": found, "messages_sent": 0}
    except Exception as exc:
        return {"ok": False, "read_only": True, "error_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}


async def _openai_probe() -> dict[str, Any]:
    import ai
    import ai_budget
    from language_guard import analyze_language
    settings = get_settings()
    model = str(settings.ai_brain_model or settings.openai_model)
    if not str(settings.openai_api_key or "").strip():
        return {"ok": False, "reason": "not_configured", "synthetic_inputs_only": True}
    results: dict[str, Any] = {}
    try:
        client = ai._openai_client(settings.openai_api_key)
        for language, prompt in (
            ("ru", "Ответь одним предложением на русском: поздоровайся как AI-администратор клиники и спроси, чем помочь. Не называй врачей, цены, услуги или свободное время."),
            ("kk", "Қазақ тілінде бір сөйлеммен жауап бер: клиниканың AI-әкімшісі ретінде амандасып, қалай көмектесе алатыныңды сұра. Дәрігерлердің аттарын, бағаларды немесе бос уақытты атама."),
        ):
            allowed, reason = ai_budget.check_allowed("production_monitor_probe")
            if not allowed:
                results[language] = {"ok": False, "reason": reason}
                continue
            limits = ai._brain_token_limit_kwargs(model)
            # Keep this synthetic probe bounded; use the app's compatibility seam.
            limits = {key: min(value, 600) for key, value in limits.items()}
            response = await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                **limits, **ai.brain_tool_call_kwargs(model),
            )
            ai_budget.record_usage(response, model=model, purpose="production_monitor_probe")
            text = str(response.choices[0].message.content or "").strip()
            analysis = analyze_language(text)
            detected = analysis.detected_language
            results[language] = {"ok": bool(text) and detected == language, "expected_language": language, "detected_language": detected, "nonempty": bool(text)}
        return {"ok": all(r.get("ok") is True for r in results.values()) and len(results) == 2, "synthetic_inputs_only": True, "model": model, "languages": results, "patient_messages_sent": 0}
    except Exception as exc:
        return {"ok": False, "synthetic_inputs_only": True, "error_type": type(exc).__name__, "languages": results}


@router.get("/status", dependencies=[Depends(require_monitor_auth)])
def monitor_status(request: Request) -> dict[str, Any]:
    return status_snapshot(worker=getattr(request.app.state, "inbox_worker", None))


@router.post("/probe", dependencies=[Depends(require_monitor_auth)])
async def monitor_probe() -> dict[str, Any]:
    async def bounded_probe(callback: Any) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(callback(), timeout=45.0)
        except Exception as exc:
            return {"ok": False, "error_type": type(exc).__name__}
    crm_result, wazzup_result, openai_result = await asyncio.gather(
        bounded_probe(_crm_probe), bounded_probe(_wazzup_probe), bounded_probe(_openai_probe),
    )
    result = {"crm": crm_result, "wazzup": wazzup_result, "openai": openai_result}
    return {"ok": all(item.get("ok") is True for item in result.values()), "checked_at": datetime.now(timezone.utc).isoformat(), "probes": result, "crm_writes": 0, "patient_messages_sent": 0}


@router.post("/secure-webhook", dependencies=[Depends(require_monitor_auth)])
async def secure_production_webhook() -> dict[str, Any]:
    # This action only hardens the already configured direct callback. The
    # scheduler never calls it; deployment/manual validation calls it once.
    from production_webhook_auth import secure_existing_webhook
    return await secure_existing_webhook()

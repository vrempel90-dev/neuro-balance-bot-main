"""Read-only production monitoring; never sends messages or creates CRM records.

GitHub Actions authenticates with a short-lived OIDC token bound to the exact
repository, main ref and monitor workflow. No production secret is copied into
Actions. Outputs contain aggregate operational data only.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
import re
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlsplit

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


async def require_monitor_auth(authorization: str | None = Header(default=None)) -> dict[str, str]:
    parts = str(authorization or "").split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    token = parts[1]
    admin_token = str(os.getenv("OPENAI_DEBUG_ADMIN_TOKEN") or "").strip()
    if len(token) > 16384:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    if admin_token and hmac.compare_digest(token.encode("utf-8"), admin_token.encode("utf-8")):
        return {"principal": "admin", "event": "admin_read"}
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
    return {"principal": "github_actions", "event": claims["event_name"]}


_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS production_monitor_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_production_monitor_kind_time
    ON production_monitor_observations(kind,checked_at);
"""


def _record_observation(kind: str, result: dict[str, Any]) -> None:
    if kind not in {"local", "dependencies", "external_schedule", "external_manual"}:
        raise ValueError("invalid monitor observation kind")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("monitor observation exceeds size limit")
    now = datetime.now(timezone.utc)
    with state._connect() as conn:
        conn.executescript(_OBSERVATIONS_DDL)
        conn.execute("INSERT INTO production_monitor_observations(kind,checked_at,result_json) VALUES (?,?,?)", (kind, now.isoformat(), encoded))
        conn.execute("DELETE FROM production_monitor_observations WHERE checked_at<?", ((now - timedelta(days=14)).isoformat(),))


def _external_coverage() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with state._connect() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='production_monitor_observations'").fetchone()
        rows = conn.execute("SELECT checked_at FROM production_monitor_observations WHERE kind='external_schedule' AND checked_at>=? ORDER BY checked_at", ((now - timedelta(hours=24)).isoformat(),)).fetchall() if exists else []
    times = [datetime.fromisoformat(row["checked_at"]) for row in rows]
    gaps = [(second - first).total_seconds() for first, second in zip(times, times[1:])]
    age = max(0.0, (now - times[-1]).total_seconds()) if times else None
    maximum_gap = max(gaps + ([age] if age is not None else []), default=None)
    return {
        "source": "verified GitHub OIDC schedule observations recorded by this service",
        "scheduled_observations_24h": len(times),
        "first_observed_at": times[0].isoformat() if times else None,
        "last_observed_at": times[-1].isoformat() if times else None,
        "last_observation_age_seconds": round(age, 1) if age is not None else None,
        "maximum_observed_gap_seconds": round(maximum_gap, 1) if maximum_gap is not None else None,
        "state": "unobserved" if not times else ("gap_observed" if maximum_gap is not None and maximum_gap > 900 else "recent"),
        "five_minute_cadence_proven": False,
        "limitation": "recorded points do not prove uninterrupted availability; manual and deployment checks are excluded",
    }


class PeriodicMonitor:
    """Read-only in-process checks complement the external host-death watcher.

    This task cannot detect its own process/host stopping. GitHub observations
    and their actual gaps remain a separate, explicitly reported signal.
    """
    def __init__(self, worker_getter: Any, *, local_interval: float = 60.0, dependency_interval: float = 300.0):
        self.worker_getter = worker_getter
        self.local_interval = max(0.05, float(local_interval))
        self.dependency_interval = max(self.local_interval, float(dependency_interval))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_local: dict[str, Any] | None = None
        self._last_dependencies: dict[str, Any] | None = None
        self._local_checked_at: str | None = None
        self._dependencies_checked_at: str | None = None
        self._local_tick = 0.0
        self._last_error_type: str | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="neuro-production-periodic-monitor")
        return self._task

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=max(0.1, timeout))
            except asyncio.TimeoutError:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done() and not self._stop_event.is_set()),
            "local_interval_seconds": self.local_interval,
            "dependency_interval_seconds": self.dependency_interval,
            "local_checked_at": self._local_checked_at,
            "dependencies_checked_at": self._dependencies_checked_at,
            "local_check_age_seconds": round(time.monotonic() - self._local_tick, 1) if self._local_tick else None,
            "local_ok": self._last_local.get("ok") if self._last_local else None,
            "dependency_results": self._last_dependencies,
            "error_type": self._last_error_type,
            "openai_recurring_calls": 0,
            "cannot_detect_own_host_death": True,
        }

    async def _run(self) -> None:
        next_local = next_dependencies = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_local:
                try:
                    self._last_local = status_snapshot(worker=self.worker_getter())
                    self._local_checked_at = datetime.now(timezone.utc).isoformat()
                    self._local_tick = time.monotonic()
                    _record_observation("local", self._last_local)
                    self._last_error_type = None
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                next_local = now + self.local_interval
            if now >= next_dependencies:
                async def bounded(callback: Any) -> dict[str, Any]:
                    try:
                        return await asyncio.wait_for(callback(), timeout=45.0)
                    except Exception as exc:
                        return {"ok": False, "error_type": type(exc).__name__}
                crm_result, wazzup_result = await asyncio.gather(bounded(_crm_probe), bounded(_wazzup_probe))
                self._last_dependencies = {"crm": crm_result, "wazzup": wazzup_result}
                self._dependencies_checked_at = datetime.now(timezone.utc).isoformat()
                try:
                    _record_observation("dependencies", self._last_dependencies)
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                next_dependencies = now + self.dependency_interval
            wait = max(0.01, min(next_local, next_dependencies) - time.monotonic())
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)


def _event_summary(since_iso: str, until_iso: str | None = None) -> dict[str, Any]:
    """Counters describe observed events, not delivered/seen patient messages."""
    where = "created_at>=?" + (" AND created_at<?" if until_iso else "")
    parameters = (since_iso, until_iso) if until_iso else (since_iso,)
    with state._connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM events WHERE " + where, parameters).fetchone()[0])
        rows = conn.execute(
            "SELECT chat_id,event_type,payload_json FROM events WHERE " + where + " ORDER BY id DESC LIMIT 10000",
            parameters,
        ).fetchall()
    event_counts: dict[str, int] = {}
    contacts: set[str] = set()
    incoming_message_keys: set[tuple[str, str, str]] = set()
    incoming_events = incoming_without_id = 0
    new_contacts: set[str] = set()
    excluded_contacts: set[str] = set()
    language_counts = {"ru": 0, "kk": 0}
    failed_crm = failed_api = confirmed = failed_booking = duplicates = inbox_duplicates = ai_failures = 0
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
        if name == "inbox_webhook_accepted":
            inbox_duplicates += max(0, int(payload.get("duplicate_count") or 0))
        ai_failures += int(name in {"openai_error", "openai_brain_error", "agent_openai_error", "language_guard_error", "inbox_processing_failed", "inbox_task_failed", "inbox_poll_failed"})
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
        "duplicate_block_events": duplicates, "durable_inbox_duplicate_events": inbox_duplicates,
        "ai_or_processing_failure_events": ai_failures, "dialog_language_events": language_counts,
        "language_violation_events": event_counts.get("language_guard_violation", 0),
        "counter_scope": "observed persisted events; contact count uses incoming=true; unique incoming messages require a logged message ID; processor-event counts may include retries/echo; not proof of delivery",
    }


def _completed_daily_report(now: datetime) -> dict[str, Any]:
    """The completed clinic-local 09:00–09:00 interval, upper bound exclusive."""
    local = now.astimezone(ZoneInfo("Asia/Almaty"))
    end = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    counters = _event_summary(start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
    return {
        "timezone": "Asia/Almaty", "start_inclusive": start.isoformat(), "end_exclusive": end.isoformat(),
        "counters": counters,
        "dialog_analysis": {"source": "persisted technical flags only; no free text or medical content", "language_violation_events": counters["language_violation_events"]},
        "delivery_verified": False,
        "limitations": [
            "Observed persisted event counters do not prove end-to-end delivery or exact patient totals.",
            "A contact may be classified new then existing in the same window; categories are not disjoint.",
            "Historical events without IDs or telemetry cannot be reconstructed.",
            "This endpoint computes a report; it does not schedule or deliver the existing ChatGPT automation.",
        ],
    }


def status_snapshot(*, worker: Any = None, periodic_monitor: Any = None) -> dict[str, Any]:
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
        import proxy_auth
        proxy_summary = proxy_auth.summary()
        proxy_enforced = bool(proxy_auth.required() and proxy_summary.get("current_credential_matches"))
        result["config"]["webhook_auth_enabled"] = bool(production_webhook_auth.required_token()) or proxy_enforced
        result["config"]["crm_proxy_auth_enabled"] = proxy_enforced
        result["proxy_auth"] = proxy_summary
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
        result["completed_daily_report"] = _completed_daily_report(now)
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
    # Runtime liveness and authenticated patient ingress are separate checks.
    # A healthy queue/channel must never turn unsecured ingress into a PASS.
    result["operational_ok"] = result["ok"]
    ingress_auth = result["config"].get("webhook_auth_enabled") is True
    result["security"] = {
        "ok": ingress_auth,
        "ingress_authentication": "enforced" if ingress_auth else "not_configured_or_unverified_proxy",
        "provider_route_checked": False,
    }
    if not ingress_auth:
        result["ok"] = False
        if "attention_required" not in result:
            result["attention_required"] = "ingress_authentication_not_configured"
        result["security_attention_required"] = "ingress_authentication_not_configured"
    try:
        result["external_monitoring"] = _external_coverage()
        if result["external_monitoring"]["state"] != "recent":
            result["monitoring_attention_required"] = "external_scheduled_checks_unobserved_or_gapped"
        if periodic_monitor is not None:
            result["periodic_monitor"] = periodic_monitor.status()
            periodic = result["periodic_monitor"]
            if not periodic["running"]:
                result["ok"] = False
                result["monitoring_attention_required"] = "internal_periodic_monitor_not_running"
            elif not periodic.get("local_checked_at") or not periodic.get("dependencies_checked_at"):
                result["ok"] = False
                result["monitoring_attention_required"] = "internal_checks_not_completed"
            elif periodic.get("local_check_age_seconds") is not None and periodic["local_check_age_seconds"] > max(120, periodic.get("local_interval_seconds", 60) * 2):
                result["ok"] = False
                result["monitoring_attention_required"] = "internal_local_check_stale"
            dependencies = periodic.get("dependency_results")
            if isinstance(dependencies, dict) and any(item.get("ok") is False for item in dependencies.values() if isinstance(item, dict)):
                result["ok"] = False
                result["monitoring_attention_required"] = "internal_dependency_check_failed"
            if isinstance(dependencies, dict):
                routing = (dependencies.get("wazzup") or {}).get("webhook_routing")
                if isinstance(routing, dict):
                    result["security"]["provider_route_checked"] = routing.get("checked") is True
                    result["security"]["provider_route_mode"] = routing.get("mode") or "unknown"
                    if routing.get("ingress_auth_configuration_verified") is not True:
                        result["security"]["ok"] = False
                        result["ok"] = False
                        result["security_attention_required"] = "provider_ingress_route_unverified"
            checked_at = periodic.get("dependencies_checked_at")
            if checked_at and (now - datetime.fromisoformat(checked_at)).total_seconds() > periodic.get("dependency_interval_seconds", 300) + 90:
                result["ok"] = False
                result["monitoring_attention_required"] = "internal_dependency_check_stale"
    except Exception as exc:
        result["ok"] = False
        result["monitoring_error_type"] = type(exc).__name__
    return result


async def _crm_probe(*, include_new_client: bool = False) -> dict[str, Any]:
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
        result["ok"] = False
        result["existing_patient_check"] = {"executed": False, "error_type": type(exc).__name__}
    try:
        from production_acceptance import verify_existing_ai_booking
        result["existing_booking_readback"] = await verify_existing_ai_booking()
        if result["existing_booking_readback"].get("executed") is True and result["existing_booking_readback"].get("verified") is not True:
            result["ok"] = False
    except Exception as exc:
        result["existing_booking_readback"] = {"executed": False, "verified": False, "reason": "readback_check_unavailable", "error_type": type(exc).__name__}
    result["new_client_check"] = {"executed": False, "reason": "manual_probe_required"}
    if include_new_client:
        candidate = None
        try:
            with state._connect() as conn:
                rows = conn.execute("SELECT chat_id,data_json FROM sessions ORDER BY updated_at DESC LIMIT 200").fetchall()
            for row in rows:
                data = json.loads(row["data_json"])
                if data.get("raw_crm_isNew") is True or data.get("crm_patient_state") in {"NEW", "NEW_PATIENT", "NEW_LEAD"}:
                    phone = crm.normalize_phone(str(data.get("phone") or data.get("phone_normalized") or row["chat_id"]))
                    if phone:
                        candidate = phone
                        break
            if not candidate:
                result["new_client_check"] = {"executed": False, "reason": "no_previously_new_session_available"}
            else:
                lookup = await asyncio.wait_for(crm.lookup_active_appointments_by_phone(candidate), timeout=12.0)
                outcome = admission.classify(candidate, lookup)
                result["new_client_check"] = {
                    "executed": True, "crm_classification": outcome.state,
                    "currently_new": bool(outcome.bot_may_reply),
                    "bot_may_reply": bool(outcome.bot_may_reply),
                    "read_only": True,
                }
                if outcome.state == admission.CRM_UNAVAILABLE:
                    result["ok"] = False
        except Exception as exc:
            result["ok"] = False
            result["new_client_check"] = {"executed": False, "reason": "lookup_not_completed", "error_type": type(exc).__name__}
        async def metadata(label: str, callback: Any) -> tuple[str, dict[str, Any]]:
            try:
                data = await asyncio.wait_for(callback(force=True), timeout=10.0)
                if not isinstance(data, dict) or data.get("ok") is not True:
                    raise ValueError("invalid_metadata_contract")
                values = data.get("doctors" if label == "doctors" else "treatable")
                if not isinstance(values, list):
                    raise ValueError("invalid_metadata_list")
                summary = {"executed": True, "ok": True, "read_only": True, "count": len(values)}
                if label == "doctors":
                    summary["confirmed_provider_descriptors"] = [
                        {key: str(item[key])[:160] for key in ("name", "fullName", "doctorName", "specialization", "specialty") if item.get(key)}
                        for item in values[:5] if isinstance(item, dict)
                    ]
                return label, summary
            except Exception as exc:
                return label, {"executed": False, "ok": False, "read_only": True, "error_type": type(exc).__name__}
        result["directory_checks"] = dict(await asyncio.gather(metadata("doctors", crm.get_doctors), metadata("services", crm.get_services)))
        if any(item.get("ok") is not True for item in result["directory_checks"].values()):
            result["ok"] = False
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
            result = {"ok": False, "channel_api_ok": bool(found), "read_only": True, "http_status": response.status_code, "channel_count": len(channels), "configured_channel_found": found, "messages_sent": 0}
            try:
                from production_webhook_auth import _owned_destination, _safe_destination, required_token
                import proxy_auth
                callback = await client.get(
                    str(settings.wazzup_api_url).rstrip("/") + "/webhooks",
                    headers={"Authorization": f"Bearer {key}"},
                )
                callback.raise_for_status()
                configuration = callback.json()
                uri = configuration.get("webhooksUri") if isinstance(configuration, dict) else None
                if not isinstance(uri, str) or not uri:
                    raise ValueError("invalid webhook configuration contract")
                direct = _owned_destination(uri, str(getattr(settings, "public_base_url", "") or ""))
                required = required_token()
                auth_enabled = bool(required)
                tokens = parse_qs(urlsplit(uri).query).get("hook_token", [])
                token_matches = bool(len(tokens) == 1 and required and hmac.compare_digest(tokens[0].encode("utf-8"), required.encode("utf-8")))
                callback_origin = urlsplit(uri)
                crm_origin = urlsplit(str(getattr(settings, "crm_base_url", "") or ""))
                configured_crm_proxy = bool(
                    callback_origin.scheme == crm_origin.scheme == "https"
                    and callback_origin.hostname
                    and callback_origin.hostname.lower() == str(crm_origin.hostname or "").lower()
                    and callback_origin.port == crm_origin.port
                    and callback_origin.path == "/api/webhooks/wazzup"
                    and not callback_origin.username and not callback_origin.password
                    and not callback_origin.fragment
                )
                proxy_enforced = bool(configured_crm_proxy and getattr(settings, "crm_bot_secret", "") and proxy_auth.required() and proxy_auth.summary().get("current_credential_matches"))
                result["webhook_routing"] = {
                    "checked": True,
                    "mode": "direct_production" if direct else ("configured_crm_proxy" if configured_crm_proxy else "external_proxy"),
                    "direct_ingress_auth_enabled": auth_enabled,
                    "crm_proxy_auth_enabled": proxy_enforced,
                    "ingress_auth_configuration_verified": bool((direct and token_matches) or proxy_enforced),
                    "end_to_end_ingress_verified": False,
                    "end_to_end_message_flow_tested": False,
                    **_safe_destination(uri),
                }
                if proxy_enforced:
                    result["ok"] = bool(found)
                    result["reason"] = "observed_crm_proxy_auth_enforced"
                elif not direct:
                    result["reason"] = "external_webhook_proxy_authentication_unverified"
                    result["blocked"] = True
                elif not auth_enabled:
                    result["reason"] = "direct_ingress_authentication_not_configured"
                    result["blocked"] = True
                elif not token_matches:
                    result["reason"] = "configured_callback_token_does_not_match_ingress_guard"
                    result["blocked"] = True
                else:
                    result["ok"] = bool(found)
                return result
            except Exception as exc:
                result["reason"] = "webhook_route_verification_failed"
                result["error_type"] = type(exc).__name__
                result["webhook_routing"] = {"checked": False, "end_to_end_ingress_verified": False}
                return result
    except Exception as exc:
        return {"ok": False, "read_only": True, "error_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}


async def _openai_probe() -> dict[str, Any]:
    """Use the actual application prompt/parser with synthetic RU/KK histories.

    No conversation state or patient event is written. This checks the model
    seam, not CRM admission, final Python guards or a real delivery channel.
    """
    import ai
    import ai_budget
    from language_guard import analyze_language
    settings = get_settings()
    model = str(settings.ai_brain_model or settings.openai_model)
    if not str(settings.openai_api_key or "").strip():
        return {"ok": False, "reason": "not_configured", "synthetic_inputs_only": True}
    results: dict[str, Any] = {}
    scenarios: dict[str, Any] = {}
    try:
        client = ai._openai_client(settings.openai_api_key)
        limits = {key: min(value, 1600) for key, value in ai._brain_token_limit_kwargs(model).items()}

        async def turn(name: str, language: str, text: str, session: dict[str, Any], history: list[dict[str, str]], *, expected_age: int | None = None) -> str:
            allowed, reason = ai_budget.check_allowed("production_monitor_probe")
            if not allowed:
                scenarios[name] = {"ok": False, "executed": False, "reason": reason}
                return ""
            context = ai.build_dialog_context(user_text=text, session=session, recent_history=history, available_slots=[])
            response = await asyncio.wait_for(client.chat.completions.create(
                model=model, temperature=float(getattr(settings, "ai_brain_temperature", 0.2)),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": ai._full_dialog_brain_system_prompt()},
                    {"role": "user", "content": json.dumps({"dialog_context": context}, ensure_ascii=False)},
                ],
                **limits, **ai.brain_tool_call_kwargs(model),
            ), timeout=20.0)
            ai_budget.record_usage(response, model=model, purpose="production_monitor_probe")
            raw_text = str(response.choices[0].message.content or "").strip()
            try:
                decision, invalid = ai._normalize_dialog_brain_decision(json.loads(raw_text))
            except (ValueError, TypeError):
                decision, invalid = {}, "invalid_json"
            reply = str(decision.get("reply") or "").strip()
            detected = analyze_language(reply).detected_language
            tool = decision.get("needs_python_tool")
            action = decision.get("action")
            role_consistent = not bool(re.search(
                r"(?:подключу|подключаю|позову|передам|переключу)[^.\n]{0,55}(?:администратор|оператор|менеджер)|"
                r"(?:әкімшіге|операторға|менеджерге)[^.\n]{0,45}(?:қосам|қосып|жіберем|берем)", reply, re.IGNORECASE))
            age_ok = expected_age is None or decision.get("extracted", {}).get("age") == expected_age
            # An incomplete synthetic intake must neither offer invented slots
            # nor create/confirm a booking or invent a human handoff.
            safe_flow = tool in {"none", None} and action not in {"handoff_admin", "show_slots", "select_slot", "ask_name", "no_reply", "fallback_rule_based"}
            safety = decision.get("safety") or {}
            safe = not safety.get("unsafe_medical_claim") and not safety.get("tries_to_book_without_rules")
            result = {
                "executed": True, "ok": bool(reply) and not invalid and detected == language and role_consistent and age_ok and safe_flow and safe,
                "schema_valid": not bool(invalid), "nonempty": bool(reply),
                "expected_language": language, "detected_language": detected,
                "role_consistent": role_consistent, "provided_age_extracted": age_ok,
                "no_unverified_side_effect_requested": safe_flow,
                "safety_flags_clear": safe,
                # Only fixed synthetic input/history reaches this model call;
                # never expose a real patient session or raw model JSON.
                "synthetic_reply": reply[:500],
            }
            if invalid:
                result["reason"] = invalid
            scenarios[name] = result
            return reply

        async def dialogue(first: str, second: str) -> None:
            initial = "Здравствуйте, у меня болит поясница, хочу записаться." if first == "ru" else "Сәлеметсіз бе, белім ауырады, дәрігерге жазылғым келеді."
            reply = await turn(first, first, initial, {"step": "complaint", "language": first}, [])
            results[first] = scenarios[first]
            followup = "Мне 34 года. Продолжим на русском языке." if second == "ru" else "Мен 34 жастамын. Әрі қарай қазақша сөйлесейік."
            history = [{"role": "user", "content": initial}, {"role": "assistant", "content": reply}]
            await turn(first + "_to_" + second, second, followup,
                       {"step": "age", "language": first, "language_locked": True, "complaint": "synthetic lower back pain"}, history, expected_age=34)

        await asyncio.gather(dialogue("ru", "kk"), dialogue("kk", "ru"))
        return {
            "ok": len(scenarios) == 4 and all(r.get("ok") is True for r in scenarios.values()),
            "synthetic_inputs_only": True, "model": model, "languages": results, "scenarios": scenarios,
            "scope": "actual_application_system_prompt_and_parser_with_two_synthetic_two_turn_dialogues",
            "native_language_quality_verified": False,
            "final_python_guards_executed": False,
            "patient_messages_sent": 0, "crm_writes": 0,
        }
    except Exception as exc:
        return {"ok": False, "synthetic_inputs_only": True, "error_type": type(exc).__name__, "languages": results, "scenarios": scenarios}


@router.get("/status")
def monitor_status(request: Request, identity: dict[str, str] = Depends(require_monitor_auth)) -> dict[str, Any]:
    if identity["principal"] == "github_actions":
        _record_observation("external_schedule" if identity["event"] == "schedule" else "external_manual", {"event": identity["event"]})
    return status_snapshot(worker=getattr(request.app.state, "inbox_worker", None), periodic_monitor=getattr(request.app.state, "production_monitor", None))


@router.post("/probe")
async def monitor_probe(identity: dict[str, str] = Depends(require_monitor_auth)) -> dict[str, Any]:
    if identity.get("event") == "schedule":
        raise HTTPException(403, "Synthetic AI probes are deployment/manual only")
    async def bounded_probe(callback: Any) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(callback(), timeout=45.0)
        except Exception as exc:
            return {"ok": False, "error_type": type(exc).__name__}
    crm_result, wazzup_result, openai_result = await asyncio.gather(
        bounded_probe(lambda: _crm_probe(include_new_client=True)), bounded_probe(_wazzup_probe), bounded_probe(_openai_probe),
    )
    result = {"crm": crm_result, "wazzup": wazzup_result, "openai": openai_result}
    return {"ok": all(item.get("ok") is True for item in result.values()), "checked_at": datetime.now(timezone.utc).isoformat(), "probes": result, "crm_writes": 0, "patient_messages_sent": 0}


@router.post("/voice-probe")
async def monitor_voice_probe(identity: dict[str, str] = Depends(require_monitor_auth)) -> dict[str, Any]:
    if identity.get("event") == "schedule":
        raise HTTPException(403, "Synthetic voice probes are deployment/manual only")
    from production_voice_probe import synthetic_voice_probe
    try:
        result = await asyncio.wait_for(synthetic_voice_probe(), timeout=65.0)
    except Exception as exc:
        result = {"executed": None, "ok": False, "outcome_unknown": True, "reason": "synthetic_voice_probe_failed", "error_type": type(exc).__name__}
    return {"ok": result.get("ok") is True, "checked_at": datetime.now(timezone.utc).isoformat(), "voice": result, "crm_writes": 0, "patient_messages_sent": 0}


@router.post("/secure-webhook")
async def secure_production_webhook(identity: dict[str, str] = Depends(require_monitor_auth)) -> dict[str, Any]:
    # This action only hardens the already configured direct callback. The
    # scheduler never calls it; deployment/manual validation calls it once.
    if identity.get("event") == "schedule":
        raise HTTPException(403, "Webhook changes are deployment/manual only")
    from production_webhook_auth import secure_existing_webhook
    return await secure_existing_webhook()

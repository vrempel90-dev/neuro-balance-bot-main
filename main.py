from __future__ import annotations

import asyncio
import json
import logging
import uuid
import mimetypes
import re
from typing import Any
from types import SimpleNamespace
from datetime import datetime, timezone

import httpx

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File, Query

import state
import crm
from config import get_settings
from dialog import FIRST_TOUCH_CLINIC_INFO_RU, handle_message
from guards import GuardDecision, should_auto_reply
from ai import humanize_reply_with_openai
from schedule import astana_now, is_bot_work_time, is_test_window_time, next_work_bounds, time_gate_status
from voice import transcribe_wazzup_voice, transcribe_bytes, transcribe_upload, voice_text_for_bot
from wazzup import extract_incoming_messages, is_audio_message_payload, send_text

# Public seam for production webhook tests/patches.
async def send_wazzup_message(**kwargs: Any) -> dict[str, Any]:
    return await send_text(**kwargs)

WAZZUP_WEBHOOK_PATHS = [
    "/wazzup/webhook",
    "/webhook",
    "/api/wazzup/webhook",
    "/webhook/wazzup",
]
WAZZUP_PRIMARY_WEBHOOK_PATH = "/webhook/wazzup"


def _public_base_url() -> str:
    return str(getattr(get_settings(), "public_base_url", "") or "").strip().rstrip("/")


def _deprecated_public_base_urls() -> list[str]:
    return [
        url.strip().rstrip("/")
        for url in str(getattr(get_settings(), "deprecated_public_base_urls", "") or "").split(",")
        if url.strip()
    ]


def _wazzup_webhook_urls() -> dict[str, Any]:
    base_url = _public_base_url()
    current_url = f"{base_url}{WAZZUP_PRIMARY_WEBHOOK_PATH}" if base_url else WAZZUP_PRIMARY_WEBHOOK_PATH
    deprecated_urls = [f"{url}{WAZZUP_PRIMARY_WEBHOOK_PATH}" for url in _deprecated_public_base_urls()]
    return {
        "current_public_base_url": base_url,
        "primary_wazzup_webhook_path": WAZZUP_PRIMARY_WEBHOOK_PATH,
        "current_wazzup_webhook_url": current_url,
        "deprecated_wazzup_webhook_urls": deprecated_urls,
        "webhook_url_warning": "CRM/Wazzup must use current_wazzup_webhook_url; deprecated URLs belong to removed Railway apps and may return Railway fallback 404.",
    }

try:
    from strict_prompt_guard import enforce_prompt_only
except Exception:
    def enforce_prompt_only(answer: str, session: dict[str, Any] | None = None) -> str:
        return answer or ""

try:
    from language_guard import enforce_response_language
except Exception:
    def enforce_response_language(answer: str, expected: str) -> tuple[str, str]:
        return answer or "", ""


app = FastAPI(title="Neuro Balance Hybrid WhatsApp Booking Bot")


class _WebhookAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "POST /webhook/wazzup" in message or "POST /wazzup/webhook" in message or "POST /api/wazzup/webhook" in message or "POST /webhook " in message:
            return bool(state.log_window_status()["log_window_allowed"])
        return True


logging.getLogger("uvicorn.access").addFilter(_WebhookAccessLogFilter())

_LAST_WAZZUP_WEBHOOK_PAYLOAD: dict[str, Any] = {}
_LAST_WAZZUP_PARSE_META: dict[str, Any] = {}


def _preview(value: Any, limit: int = 120) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


def _crm_debug_payload(phone: str, lookup: dict[str, Any] | None = None, error: Exception | None = None) -> dict[str, Any]:
    lookup = lookup or {}
    appt = lookup.get("appointment") if isinstance(lookup, dict) else None
    appointments = lookup.get("appointments") if isinstance(lookup.get("appointments"), list) else []
    status_code = lookup.get("status_code") if isinstance(lookup, dict) else getattr(error, "status_code", None)
    endpoint_url = lookup.get("endpoint_url") if isinstance(lookup, dict) else getattr(error, "endpoint_url", "")
    return {
        "phone_raw": phone,
        "phone_normalized": crm.normalize_phone(phone),
        "phone_lookup_variants": crm.phone_lookup_variants(phone),
        "crm_lookup_called": True,
        "crm_endpoint_url": endpoint_url or "",
        "crm_status_code": status_code,
        "crm_lookup_error": str(error)[:500] if error else "",
        "crm_raw_response": (lookup.get("raw") if isinstance(lookup, dict) else {}) or {},
        "active_appointment_found": bool(appt),
        "active_appointment_count": int((lookup.get("active_count") if isinstance(lookup, dict) else 0) or len(appointments) or (1 if appt else 0)),
        "appointments": appointments,
        "nearest_active_appointment": appt,
        "appointments_raw_count": int((lookup.get("appointments_raw_count") if isinstance(lookup, dict) else 0) or len(appointments)),
        "crm_query_params": (lookup.get("query_params") if isinstance(lookup, dict) else getattr(error, "query_params", {})) or {},
    }


async def _run_debug_crm_lookup(phone: str) -> dict[str, Any]:
    phone = str(phone or "").strip()
    try:
        lookup = await crm.lookup_active_appointments_by_phone(phone)
        payload = _crm_debug_payload(phone, lookup=lookup)
        state.log_event("debug_crm_lookup", "crm_lookup_success", {k: v for k, v in payload.items() if k != "crm_raw_response"})
        return payload
    except Exception as exc:
        payload = _crm_debug_payload(phone, error=exc)
        state.log_event("debug_crm_lookup", "crm_lookup_failed", payload)
        return payload


def _dialog_debug(session: dict[str, Any], answer: str = "") -> dict[str, Any]:
    decision = session.get("guard_decision") if isinstance(session.get("guard_decision"), dict) else {}
    return {
        "source": session.get("source") or "",
        "message_id": session.get("message_id") or "",
        "NEW_LEADS_ONLY": bool(getattr(get_settings(), "new_leads_only", True)),
        "timezone": getattr(get_settings(), "bot_timezone", "Asia/Almaty"),
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "local_time": session.get("local_time") or astana_now().isoformat(),
        "bot_work_start": getattr(get_settings(), "bot_work_start", "20:00"),
        "bot_work_end": getattr(get_settings(), "bot_work_end", "08:00"),
        "bot_test_window_enabled": bool(getattr(get_settings(), "bot_test_window_enabled", False)),
        "bot_test_window_start": getattr(get_settings(), "bot_test_window_start", "14:00"),
        "bot_test_window_end": getattr(get_settings(), "bot_test_window_end", "14:30"),
        "bot_test_window_date": getattr(get_settings(), "bot_test_window_date", "2026-07-08"),
        "bot_test_window_now": is_test_window_time(),
        "bot_auto_reply_enabled": bool(getattr(get_settings(), "bot_auto_reply_enabled", True)),
        "new_leads_only": bool(getattr(get_settings(), "new_leads_only", True)),
        "bot_work_time_now": is_bot_work_time(),
        "working_hours_allowed": bool(session.get("working_hours_allowed", is_bot_work_time())),
        "working_hours_bypassed_by_force": bool(session.get("working_hours_bypassed_by_force")),
        "openai_used": bool(session.get("openai_used")),
        "openai_model": session.get("openai_model") or "",
        "openai_skip_reason": session.get("openai_skip_reason") or "",
        "openai_guard_failed": bool(session.get("openai_guard_failed")),
        "openai_brain_used": bool(session.get("openai_brain_used")),
        "openai_brain_intent": session.get("openai_brain_intent") or "",
        "openai_brain_action": session.get("openai_brain_action") or "",
        "openai_brain_needs_python_tool": session.get("openai_brain_needs_python_tool") or "",
        "openai_brain_extracted": session.get("openai_brain_extracted") or {},
        "openai_brain_guard_failed": bool(session.get("openai_brain_guard_failed")),
        "openai_brain_guard_reason": session.get("openai_brain_guard_reason") or "",
        "openai_brain_skip_reason": session.get("openai_brain_skip_reason") or "",
        "openai_brain_fallback_used": bool(session.get("openai_brain_fallback_used")),
        "openai_brain_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
        "openai_brain_temperature": session.get("openai_brain_temperature") if session.get("openai_brain_temperature") is not None else getattr(get_settings(), "ai_brain_temperature", None),
        "openai_error_type": session.get("openai_error_type") or "",
        "openai_error_message_preview": session.get("openai_error_message_preview") or "",
        "openai_config_missing_detail": session.get("openai_config_missing_detail") or {},
        "openai_missing_keys": session.get("openai_missing_keys") or [],
        "openai_disabled_flags": session.get("openai_disabled_flags") or [],
        "humanize_skipped_because_brain_valid": bool(session.get("humanize_skipped_because_brain_valid")),
        "humanize_fallback_used": bool(session.get("humanize_fallback_used")),
        "llm_blocked": bool(session.get("llm_blocked")),
        "llm_repaired": bool(session.get("llm_repaired")),
        "repair_reason": session.get("repair_reason") or "",
        "repaired_step": session.get("repaired_step") or "",
        "base_answer_preview": session.get("base_answer_preview") or _preview(answer, 160),
        "final_answer_preview": session.get("final_answer_preview") or _preview(answer, 160),
        "gate_reason": session.get("gate_reason") or "",
        "no_reply_reason": session.get("no_reply_reason") or "",
        "silent_old_lead": bool(session.get("silent_old_lead")),
        "old_lead_reason": session.get("old_lead_reason") or "",
        "crm_called": bool(session.get("crm_called")),
        "wazzup_send_called": bool(session.get("wazzup_send_called")),
        "should_send_wazzup": bool(decision.get("should_send_wazzup", False)),
        "answer_empty": not bool(answer),
        "step": session.get("step") or session.get("current_step") or "",
        "ai_muted": bool(session.get("ai_muted")),
        "manual_takeover": bool(session.get("manual_takeover") or session.get("manual_admin_intervention")),
        "ai_lead_started": bool(session.get("ai_lead_started")),
        "working_hours_bypassed_by_force": bool(session.get("working_hours_bypassed_by_force")),
        "state_before_step": session.get("state_before_step") or "",
        "state_after_step": session.get("state_after_step") or session.get("step") or "",
        "brain_allowed": bool(session.get("brain_allowed")),
        "brain_skip_reason": session.get("brain_skip_reason") or session.get("openai_brain_skip_reason") or "",
        "fallback_reason": session.get("fallback_reason") or "",
        "state_repaired": bool(session.get("state_repaired")),
        "state_repair_reason": session.get("state_repair_reason") or "",
        "state_before": session.get("state_before_step") or "",
        "state_after": session.get("state_after_step") or session.get("step") or "",
        "next_step_reason": session.get("state_repair_reason") or session.get("fallback_reason") or session.get("gate_reason") or "",
        "entity_extraction": session.get("openai_brain_extracted") or {},
        "faq_type": session.get("last_answered_faq_type") or "",
        "crm_result": session.get("crm_result") or session.get("appointment_status") or "",
        "booking_ready": bool(session.get("booking_ready")),
        "booking_confirmed": bool(session.get("booking_confirmed")),
        "final_repair_reason": session.get("repair_reason") or session.get("fallback_reason") or "",
        "answer_source": session.get("answer_source") or ("openai" if session.get("openai_used") else "python"),
        "last_slots_count": len(session.get("last_slots") or []),
        "selected_slot": session.get("selected_slot") or {},
        "selected_doctor_login": session.get("selected_doctor_login") or "",
        "selected_doctor_name": session.get("selected_doctor_name") or "",
        "crm_lookup_called": bool(session.get("crm_lookup_called")),
        "crm_lookup_status_code": session.get("crm_lookup_status_code"),
        "crm_lookup_scope": session.get("crm_lookup_scope") or "",
        "crm_endpoint_url": session.get("crm_endpoint_url") or "",
        "crm_lookup_result": session.get("crm_lookup_result") or {},
        "crm_lookup_error": session.get("crm_lookup_error") or "",
        "crm_lookup_phone_variants": session.get("crm_lookup_phone_variants") or session.get("phone_lookup_variants") or [],
        "phone_raw": session.get("phone_raw") or "",
        "phone_normalized": session.get("phone_normalized") or "",
        "phone_lookup_variants": session.get("phone_lookup_variants") or [],
        "active_appointment_found": bool(session.get("active_appointment_found")),
        "active_appointment_count": int(session.get("active_appointment_count") or 0),
        "appointments_raw_count": int(session.get("appointments_raw_count") or 0),
        "nearest_active_appointment": session.get("nearest_active_appointment"),
        "crm_patient_found": bool(session.get("crm_patient_found")),
        "crm_patient_is_new": bool(session.get("crm_patient_is_new")),
        "crm_patient_state": session.get("crm_patient_state") or "",
        "crm_patient_name": session.get("crm_patient_name") or "",
        "crm_lead_id": session.get("crm_lead_id") or "",
        "crm_lead_status": session.get("crm_lead_status") or "",
        "crm_lead_complaint": session.get("crm_lead_complaint") or "",
        "crm_lead_request": session.get("crm_lead_request") or "",
        "crm_last_appointment_id": session.get("crm_last_appointment_id") or "",
        "crm_last_appointment_date": session.get("crm_last_appointment_date") or "",
        "crm_last_appointment_time": session.get("crm_last_appointment_time") or "",
        "crm_last_appointment_status": session.get("crm_last_appointment_status") or "",
        "appointment_id": session.get("appointment_id") or "",
        "crm_booking_id": session.get("crm_booking_id") or "",
        "appointment_date": session.get("appointment_date") or "",
        "appointment_time": session.get("appointment_time") or "",
        "appointment_doctor_name": session.get("appointment_doctor_name") or "",
        "booking_visible_in_crm": bool(session.get("booking_visible_in_crm")),
        "crm_verification_lookup_called": bool(session.get("crm_verification_lookup_called")),
        "crm_verification_result": session.get("crm_verification_result") or {},
        "first_touch_allowed": bool(session.get("first_touch_allowed")),
        "first_touch_blocked_reason": session.get("first_touch_blocked_reason") or "",
        "is_booked_client": bool(session.get("is_booked_client")),
        "post_booking_support_active": bool(session.get("post_booking_support_active")),
        "created_by_ai": bool(session.get("created_by_ai")),
        "message_timestamp": session.get("message_timestamp") or "",
        "bot_activated_at": session.get("bot_activated_at") or getattr(get_settings(), "bot_activated_at", ""),
    }


def _log_dialog_result(chat_id: str, phone: str, answer: str) -> None:
    session = state.get_session(chat_id)
    debug = _dialog_debug(session, answer)
    state.log_event(chat_id, "dialog_result", {
        "phone": phone,
        "answer_empty": debug["answer_empty"],
        "answer_preview": _preview(answer, 160),
        **{k: debug[k] for k in ("source", "local_time", "bot_work_time_now", "working_hours_allowed", "step", "gate_reason", "no_reply_reason", "ai_muted", "manual_takeover", "ai_lead_started", "openai_used", "openai_skip_reason")},
    })
    if not answer:
        state.log_event(chat_id, "bot_no_reply", {
            "phone": phone,
            **{k: debug[k] for k in ("no_reply_reason", "gate_reason", "step", "ai_muted", "manual_takeover")},
        })


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "Neuro Balance WhatsApp Bot",
        "docs": "/docs",
    }


@app.on_event("startup")
def startup() -> None:
    state.init_db()
    settings = get_settings()
    allowed_channel_ids = [c.strip() for c in str(getattr(settings, "wazzup_allowed_channel_ids", "") or "").split(",") if c.strip()]
    state.log_event("startup", "bot_runtime_config", {
        "timezone": getattr(settings, "bot_timezone", "Asia/Almaty"),
        "bot_work_start": getattr(settings, "bot_work_start", "20:00"),
        "bot_work_end": getattr(settings, "bot_work_end", "08:00"),
        "production_log_active_window_only": bool(getattr(settings, "production_log_active_window_only", True)),
        "new_leads_only": bool(getattr(settings, "new_leads_only", True)),
        "wazzup_allowed_channel_ids_count": len(allowed_channel_ids),
    })


def _time_debug_payload(now: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    local = (now or astana_now()).astimezone(astana_now().tzinfo)
    utc_now = local.astimezone(timezone.utc)
    next_start, next_end = next_work_bounds(local)
    gate = time_gate_status(local)
    active = bool(gate["bot_work_time_now"])
    return {
        "utc_now": utc_now.isoformat(),
        "local_now": local.isoformat(),
        "utc_time": utc_now.isoformat(),
        "local_time": local.isoformat(),
        "timezone": getattr(settings, "bot_timezone", "Asia/Almaty"),
        "is_bot_work_time": active,
        "bot_work_time_now": active,
        "work_start": getattr(settings, "bot_work_start", "20:00"),
        "work_end": getattr(settings, "bot_work_end", "08:00"),
        "bot_work_start": getattr(settings, "bot_work_start", "20:00"),
        "bot_work_end": getattr(settings, "bot_work_end", "08:00"),
        "bot_test_window_enabled": bool(getattr(settings, "bot_test_window_enabled", False)),
        "bot_test_window_start": getattr(settings, "bot_test_window_start", "14:00"),
        "bot_test_window_end": getattr(settings, "bot_test_window_end", "14:30"),
        "bot_test_window_date": getattr(settings, "bot_test_window_date", "2026-07-08"),
        "bot_test_window_now": bool(gate["test_window_now"]),
        "normal_work_time_now": bool(gate["normal_work_time_now"]),
        "bot_auto_reply_enabled": bool(getattr(settings, "bot_auto_reply_enabled", True)),
        "new_leads_only": bool(getattr(settings, "new_leads_only", True)),
        "next_start": next_start.isoformat(),
        "next_end": next_end.isoformat(),
        "next_bot_start_at": next_start.isoformat(),
        "next_bot_end_at": next_end.isoformat(),
        "time_gate_reason": str(gate["time_gate_reason"]),
        "reason": str(gate["time_gate_reason"]),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    payload = _time_debug_payload()
    return {
        "ok": True,
        "timezone": payload["timezone"],
        "utc_time": payload["utc_time"],
        "local_time": payload["local_time"],
        "bot_work_start": payload["bot_work_start"],
        "bot_work_end": payload["bot_work_end"],
        "bot_work_time_now": payload["bot_work_time_now"],
        "bot_test_window_enabled": payload["bot_test_window_enabled"],
        "bot_test_window_start": payload["bot_test_window_start"],
        "bot_test_window_end": payload["bot_test_window_end"],
        "bot_test_window_date": payload["bot_test_window_date"],
        "bot_test_window_now": payload["bot_test_window_now"],
        "production_log_active_window_only": bool(getattr(get_settings(), "production_log_active_window_only", True)),
        "current_log_window_allowed": bool(state.log_window_status()["log_window_allowed"]),
        "log_policy": "active_window_only" if bool(getattr(get_settings(), "production_log_active_window_only", True)) else "all_events",
        "bot_auto_reply_enabled": payload["bot_auto_reply_enabled"],
        "new_leads_only": payload["new_leads_only"],
        "next_bot_start_at": payload["next_bot_start_at"],
        "next_bot_end_at": payload["next_bot_end_at"],
        "wazzup_api_key_configured": bool(getattr(get_settings(), "wazzup_api_key", "")),
        "wazzup_channel_id_configured": bool(getattr(get_settings(), "wazzup_channel_id", "")),
        "wazzup_instagram_channel_id_configured": bool(getattr(get_settings(), "wazzup_instagram_channel_id", "")),
        "wazzup_webhook_paths": list(WAZZUP_WEBHOOK_PATHS),
        **_wazzup_webhook_urls(),
    }


@app.get("/debug/time")
def debug_time() -> dict[str, Any]:
    return _time_debug_payload()


_ALLOWED_HUMANIZE_STEPS = {"start", "complaint", "age", "contraindications", "date", "time", "name"}
_ALLOWED_HUMANIZE_GATE_REASONS = {"new_lead", "new_lead_like_message", "active_ai_lead", "active_conversation_reply"}

def _set_openai_debug(session: dict[str, Any], debug: dict[str, Any], base_answer: str, final_answer: str) -> None:
    session["openai_used"] = bool(debug.get("openai_used"))
    session["openai_model"] = str(debug.get("openai_model") or "")
    session["openai_skip_reason"] = str(debug.get("openai_skip_reason") or "")
    session["openai_guard_failed"] = bool(debug.get("openai_guard_failed"))
    session["openai_error_type"] = str(debug.get("openai_error_type") or "")
    session["openai_error_message_preview"] = str(debug.get("openai_error_message_preview") or "")
    session["openai_error_detail"] = debug.get("openai_error_detail") or {}
    session["openai_config_missing_detail"] = debug.get("openai_config_missing_detail") or {}
    session["openai_missing_keys"] = debug.get("openai_missing_keys") or []
    session["openai_disabled_flags"] = debug.get("openai_disabled_flags") or []
    if "humanize_fallback_used" in debug:
        session["humanize_fallback_used"] = bool(debug.get("humanize_fallback_used"))
    session["base_answer_preview"] = str(debug.get("base_answer_preview") or _preview(base_answer, 160))
    session["final_answer_preview"] = str(debug.get("final_answer_preview") or _preview(final_answer, 160))


def _humanize_skip_reason(session: dict[str, Any], answer: str, *, voice_ignored: bool = False) -> str:
    if not (answer or "").strip():
        return "empty_answer"
    if voice_ignored or session.get("last_ignored_message_type") in {"voice", "audio"}:
        return "handoff"
    step = str(session.get("step") or session.get("current_step") or "start")
    if step in {"booked", "confirmed", "done"} or session.get("booked") or session.get("ai_muted"):
        return "booked_or_muted"
    if session.get("manual_takeover") or session.get("manual_admin_intervention") or session.get("do_not_reply"):
        return "handoff"
    if session.get("refund_claim_admin_required") or session.get("gate_reason") == "refund_claim_admin_required":
        return "refund_or_claim"
    if session.get("old_chat_ai_disabled") or session.get("gate_reason") == "old_chat_ai_disabled":
        return "handoff"
    if step in {"stopped"} or session.get("hard_contraindication_stop") or session.get("contraindication_hard_stop"):
        return "hard_stop"
    if step not in _ALLOWED_HUMANIZE_STEPS:
        return "not_allowed_step"
    gate_reason = str(session.get("gate_reason") or "")
    if not (session.get("ai_lead_started") is True or gate_reason in _ALLOWED_HUMANIZE_GATE_REASONS):
        return "not_ai_lead"
    return ""


async def _maybe_humanize_answer(chat_id: str, user_text: str, base_answer: str, *, voice_ignored: bool = False) -> str:
    session = _get_session_safe(chat_id)
    session["chat_id"] = chat_id
    if session.get("skip_humanize") or str(session.get("answer_source") or "").startswith("locked_template"):
        debug = {
            "openai_used": False,
            "openai_model": getattr(get_settings(), "openai_model", ""),
            "openai_skip_reason": "locked_template",
            "openai_guard_failed": False,
            "base_answer_preview": _preview(base_answer, 160),
            "final_answer_preview": _preview(base_answer, 160),
            "humanize_fallback_used": False,
        }
        session["humanize_skipped_because_locked_template"] = True
        session["humanize_fallback_used"] = False
        _set_openai_debug(session, debug, base_answer, base_answer)
        state.save_session(chat_id, session)
        state.log_event(chat_id, "humanize_skipped", {"chat_id": chat_id, "reason": "locked_template", "step": session.get("step") or session.get("current_step") or ""})
        return base_answer
    if session.get("openai_brain_used") and (base_answer or "").strip():
        session["humanize_skipped_because_brain_valid"] = True
        session["humanize_fallback_used"] = False
        debug = {
            "openai_used": True,
            "openai_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
            "openai_skip_reason": "",
            "openai_guard_failed": False,
            "base_answer_preview": _preview(base_answer, 160),
            "final_answer_preview": _preview(base_answer, 160),
            "humanize_fallback_used": False,
        }
        _set_openai_debug(session, debug, base_answer, base_answer)
        state.save_session(chat_id, session)
        state.log_event(chat_id, "humanize_skipped_because_brain_valid", {
            "chat_id": chat_id,
            "openai_brain_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
            "openai_brain_temperature": session.get("openai_brain_temperature") if session.get("openai_brain_temperature") is not None else getattr(get_settings(), "ai_brain_temperature", None),
            "openai_brain_used": True,
            "humanize_skipped_because_brain_valid": True,
            "humanize_fallback_used": False,
        })
        if not getattr(get_settings(), "openai_humanize_replies", True):
            state.log_event(chat_id, "humanize_skipped", {
                "chat_id": chat_id,
                "reason": "disabled",
                "step": session.get("step") or session.get("current_step") or "",
                "disabled_flags": ["OPENAI_HUMANIZE_REPLIES=false"],
            })
        return base_answer
    reason = _humanize_skip_reason(session, base_answer, voice_ignored=voice_ignored)
    if reason:
        debug = {
            "openai_used": False,
            "openai_model": getattr(get_settings(), "openai_model", ""),
            "openai_skip_reason": reason,
            "openai_guard_failed": False,
            "base_answer_preview": _preview(base_answer, 160),
            "final_answer_preview": _preview(base_answer, 160),
        }
        _set_openai_debug(session, debug, base_answer, base_answer)
        state.save_session(chat_id, session)
        state.log_event(chat_id, "openai_skipped", {"chat_id": chat_id, "reason": reason, "step": session.get("step") or session.get("current_step") or ""})
        return base_answer

    session["humanize_skipped_because_brain_valid"] = False
    session["humanize_fallback_used"] = True
    state.log_event(chat_id, "humanize_fallback_used", {"chat_id": chat_id, "reason": session.get("openai_brain_skip_reason") or session.get("openai_skip_reason") or "rule_based", "humanize_fallback_used": True})
    final_answer, debug = await humanize_reply_with_openai(base_answer=base_answer, user_text=user_text, session=session)
    debug["humanize_fallback_used"] = True
    _set_openai_debug(session, debug, base_answer, final_answer)
    state.save_session(chat_id, session)
    if not debug.get("openai_used"):
        if debug.get("openai_config_missing_detail"):
            state.log_event(chat_id, "openai_config_missing_detail", debug["openai_config_missing_detail"])
        disabled_flags = debug.get("openai_disabled_flags") or []
        missing_keys = debug.get("openai_missing_keys") or []
        payload = {"chat_id": chat_id, "reason": debug.get("openai_skip_reason") or "config_missing", "step": session.get("step") or session.get("current_step") or "", "missing_keys": missing_keys, "disabled_flags": disabled_flags}
        if disabled_flags == ["OPENAI_HUMANIZE_REPLIES=false"] and not missing_keys:
            state.log_event(chat_id, "humanize_skipped", {**payload, "reason": "disabled"})
        else:
            state.log_event(chat_id, "openai_skipped", payload)
    return final_answer





def _parse_message_datetime(value: Any):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None

def _bot_activated_at():
    raw = getattr(get_settings(), "bot_activated_at", "") or "2026-07-02T00:00:00+05:00"
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)

def _mark_no_reply(chat_id: str, reason: str, message: dict[str, Any], *, duplicate: bool = False, old: bool = False) -> None:
    session = _get_session_safe(chat_id)
    session["no_reply_reason"] = reason
    session["message_id"] = str(message.get("message_id") or message.get("message_key") or session.get("message_id") or "")
    session["message_timestamp"] = str(message.get("timestamp") or message.get("message_timestamp") or session.get("message_timestamp") or "")
    session["bot_activated_at"] = _bot_activated_at().isoformat()
    session["is_old_message"] = old
    session["is_duplicate_message"] = duplicate
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["wazzup_send_called"] = False
    session["guard_decision"] = GuardDecision(False, reason, False, False, False).to_dict()
    state.save_session(chat_id, session)
    state.log_event(chat_id, "bot_no_reply", {"no_reply_reason": reason, "message_id": session["message_id"], "message_timestamp": session["message_timestamp"], "bot_activated_at": session["bot_activated_at"]})

def _hard_inbound_block_reason(message: dict[str, Any], session: dict[str, Any] | None = None) -> tuple[str, bool, bool]:
    direction = str(message.get("direction") or "incoming").lower()
    from_me = str(message.get("is_from_me") or "").lower() == "true" or bool(message.get("from_me") or message.get("is_operator") or message.get("is_admin"))
    if direction in WAZZUP_OUTGOING_DIRECTIONS or from_me:
        return "not_client_incoming_message", False, False
    ts = _parse_message_datetime(message.get("timestamp") or message.get("message_timestamp"))
    activated = _bot_activated_at()
    if ts and ts < activated:
        return "old_message_before_bot_activation", False, True
    key = str(message.get("message_key") or message.get("message_id") or "")
    if key and state.is_processed_message(key):
        return "duplicate_message_already_processed", True, False
    s = session or {}
    if any(bool(s.get(k)) for k in ("manual_admin_intervention", "operator_replied_after_client")) or str(s.get("chat_status") or "") == "answered_by_admin" or s.get("wazzup_reply_needed") is False:
        return "admin_or_wazzup_marked_no_reply", False, False
    return "", False, False

def _mark_working_hours_disabled(
    *,
    chat_id: str,
    phone: str = "",
    source: str,
    force: bool = False,
    kind: str = "text",
    text: str = "",
) -> None:
    """Persist and log the daytime silence decision before any AI/CRM path runs."""
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = False
    session["no_reply_reason"] = "working_hours_ai_disabled"
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = "working_hours_ai_disabled"
    session["working_hours_bypassed_by_force"] = False
    state.save_session(chat_id, session)
    state.log_event(chat_id, "working_hours_blocked", {
        "chat_id": chat_id,
        "phone": phone,
        "current_time_astana": astana_now().isoformat(),
        "source": source,
        "force": force,
        "kind": kind,
        "text_preview": _preview(text, 120),
    })


def _mark_bot_auto_reply_disabled(*, chat_id: str, phone: str = "", source: str, force: bool = False, kind: str = "text", text: str = "") -> None:
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = is_bot_work_time()
    session["no_reply_reason"] = "bot_auto_reply_disabled"
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = "bot_auto_reply_disabled"
    state.save_session(chat_id, session)
    state.log_event(chat_id, "bot_auto_reply_disabled", {
        "chat_id": chat_id, "phone": phone, "source": source, "force": force, "kind": kind,
        "text_preview": _preview(text, 120), "current_time_astana": astana_now().isoformat(),
    })



def _apply_guard_block(
    decision: GuardDecision,
    *,
    chat_id: str,
    phone: str = "",
    source: str,
    force: bool = False,
    kind: str = "text",
    text: str = "",
) -> None:
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = is_bot_work_time() or bool(force)
    session["no_reply_reason"] = decision.no_reply_reason
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = decision.no_reply_reason
    session["crm_called"] = False
    session["wazzup_send_called"] = False
    session["guard_decision"] = decision.to_dict()
    session["message_id"] = session.get("message_id") or ""
    session["message_timestamp"] = session.get("message_timestamp") or ""
    session["bot_activated_at"] = _bot_activated_at().isoformat()
    state.save_session(chat_id, session)
    event = "working_hours_blocked" if decision.no_reply_reason == "working_hours_ai_disabled" else decision.no_reply_reason
    state.log_event(chat_id, event or "auto_reply_blocked", {
        "chat_id": chat_id, "phone": phone, "source": source, "force": force, "kind": kind,
        "text_preview": _preview(text, 120), "current_time_astana": astana_now().isoformat(),
        "openai_used": False, "openai_brain_used": False, "crm_called": False, "wazzup_send_called": False,
    })


def _get_session_safe(chat_id: str) -> dict[str, Any]:
    try:
        session = state.get_session(chat_id)
        return session if isinstance(session, dict) else {}
    except Exception:
        return {}


def _guard_answer(chat_id: str, answer: str) -> str:
    """Финальная защита перед отправкой пациенту.

    Не даёт ответу уйти в Wazzup без strict_prompt_guard:
    - убирает обращение по имени;
    - убирает лишние фразы;
    - ограничивает длинные ответы вне разрешённых шаблонов.
    """
    session = _get_session_safe(chat_id)
    if str(session.get("answer_source") or "").startswith("locked_template") or (answer or "").strip() == FIRST_TOUCH_CLINIC_INFO_RU:
        return (answer or "").strip()
    guarded = enforce_prompt_only(answer or "", session)
    return _guard_answer_language(chat_id, guarded, session)


def _guard_answer_language(chat_id: str, answer: str, session: dict[str, Any]) -> str:
    """Проверяет, что ответ написан на языке пациента.

    Единственная разрешённая правка — подстановка утверждённого казахского
    шаблона клиники вместо русского. Переводить текст на лету нельзя, поэтому
    остальные нарушения только логируются: доставить верный факт на другом
    языке безопаснее, чем сочинить казахский текст или промолчать.
    """
    expected = str(session.get("language") or "")
    if expected not in ("ru", "kk") or not (answer or "").strip():
        return answer
    try:
        repaired, violation = enforce_response_language(answer, expected)
    except Exception as exc:
        state.log_event(chat_id, "language_guard_error", {"error": str(exc)[:300]})
        return answer
    if violation:
        state.log_event(chat_id, "language_guard_violation", {
            "chat_id": chat_id,
            "expected_language": expected,
            "violation": violation,
            "repaired": repaired != answer,
            "answer_preview": _preview(answer, 160),
        })
    session["language_guard_result"] = violation or "ok"
    try:
        state.save_session(chat_id, session)
    except Exception:
        pass
    return repaired


def _voice_fallback_answer() -> str:
    return (
        "Спасибо за Ваше обращение 🌿 "
        "Передам информацию врачу — он свяжется с Вами в ближайшее время."
    )



def _deep_find_audio_items(obj: Any) -> list[dict[str, Any]]:
    """Ищет в payload Wazzup вложения/медиа, похожие на голосовое или аудио.

    Сделано максимально безопасно: если Wazzup пришлёт обычный текст,
    функция ничего не добавит и старый путь не изменится.
    """
    found: list[dict[str, Any]] = []

    audio_keys = {
        "audio", "voice", "ptt", "media", "attachment", "attachments",
        "file", "files", "document", "content", "message",
    }

    def walk(x: Any, parents: list[dict[str, Any]]) -> None:
        if isinstance(x, dict):
            low_keys = {str(k).lower() for k in x.keys()}
            type_text = " ".join(str(x.get(k, "")) for k in [
                "type", "messageType", "contentType", "mimeType", "mime", "mediaType",
            ]).lower()

            url_text = " ".join(str(x.get(k, "")) for k in [
                "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
                "contentUrl", "link", "href",
            ]).lower()

            filename_text = " ".join(str(x.get(k, "")) for k in [
                "filename", "fileName", "name",
            ]).lower()

            looks_audio = (
                "audio" in type_text
                or "voice" in type_text
                or "ptt" in type_text
                or "ogg" in filename_text
                or "oga" in filename_text
                or "opus" in filename_text
                or "mp3" in filename_text
                or "m4a" in filename_text
                or "wav" in filename_text
                or ".ogg" in url_text
                or ".oga" in url_text
                or ".mp3" in url_text
                or ".m4a" in url_text
                or ".wav" in url_text
            )

            if looks_audio and (low_keys & audio_keys or url_text):
                item = dict(x)
                for parent in reversed(parents):
                    for k in [
                        "chatId", "chat_id", "chat_id_external", "phone", "from", "sender",
                        "chatType", "chat_type", "channelId", "channel_id", "messageId",
                        "id", "messageKey", "message_key",
                    ]:
                        if k not in item and k in parent:
                            item[k] = parent[k]
                found.append(item)

            walk_parents = parents + [x]
            for v in x.values():
                walk(v, walk_parents)

        elif isinstance(x, list):
            for v in x:
                walk(v, parents)

    walk(obj, [])
    return found


def _first_value(d: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in d and d.get(key) not in (None, ""):
            return d.get(key)
    return None


def _message_from_audio_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Превращает найденное audio/media-вложение в формат сообщения для обработчика."""
    url = _first_value(item, [
        "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
        "contentUrl", "link", "href",
    ])

    # Иногда URL лежит глубже: file.url / media.url / audio.url
    if not url:
        for nested_key in ["file", "media", "audio", "voice", "attachment", "content"]:
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                url = _first_value(nested, [
                    "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
                    "contentUrl", "link", "href",
                ])
                if url:
                    break

    if not url:
        return None

    chat_id = _first_value(item, [
        "chat_id", "chatId", "chat_id_external", "phone", "from", "sender", "contactPhone",
    ])
    if isinstance(chat_id, dict):
        chat_id = _first_value(chat_id, ["phone", "id", "chatId", "chat_id"])

    phone = _first_value(item, ["phone", "from", "sender", "contactPhone"]) or chat_id
    if isinstance(phone, dict):
        phone = _first_value(phone, ["phone", "id", "chatId", "chat_id"]) or chat_id

    if not chat_id:
        return None

    message_key = _first_value(item, ["message_key", "messageKey", "messageId", "id"])
    filename = _first_value(item, ["filename", "fileName", "name"]) or "voice.ogg"
    content_type = _first_value(item, ["contentType", "mimeType", "mime"]) or ""

    return {
        "chat_id": str(chat_id),
        "phone": str(phone or chat_id),
        "chat_type": str(_first_value(item, ["chat_type", "chatType"]) or "whatsapp"),
        "channel_id": _first_value(item, ["channel_id", "channelId"]),
        "message_key": str(message_key or f"voice:{chat_id}:{url}"),
        "kind": "voice",
        "media_url": str(url),
        "file_url": str(url),
        "filename": str(filename),
        "content_type": str(content_type),
        "raw_audio_item": item,
    }


def _extract_voice_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _deep_find_audio_items(payload):
        msg = _message_from_audio_item(item)
        if not msg:
            continue
        key = str(msg.get("message_key") or msg.get("media_url") or "")
        if key in seen:
            continue
        seen.add(key)
        messages.append(msg)

    return messages


def _message_has_voice_url(message: dict[str, Any]) -> bool:
    if str(message.get("kind") or "").lower() == "voice":
        return True

    maybe = " ".join(str(message.get(k, "")) for k in [
        "media_url", "file_url", "url", "fileUrl", "mediaUrl", "downloadUrl",
        "content_type", "mimeType", "type",
    ]).lower()

    return any(x in maybe for x in ["audio", "voice", "ogg", "oga", "opus", "mp3", "m4a", "wav"])


def _voice_url_from_message(message: dict[str, Any]) -> str | None:
    for key in ["media_url", "file_url", "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri", "contentUrl"]:
        value = message.get(key)
        if value:
            return str(value)

    raw = message.get("raw_audio_item")
    if isinstance(raw, dict):
        nested_msg = _message_from_audio_item(raw)
        if nested_msg:
            return str(nested_msg.get("media_url") or "")

    return None


async def _download_voice_bytes(message: dict[str, Any]) -> tuple[bytes, str, str]:
    """Скачивает голосовое по URL из Wazzup.

    Если URL подписанный — хватит обычного GET.
    Если Wazzup требует авторизацию — добавляем Bearer/API headers, но только если ключ есть.
    """
    url = _voice_url_from_message(message)
    if not url:
        raise ValueError("voice media url not found")

    settings = get_settings()
    api_key = (
        getattr(settings, "wazzup_api_key", "")
        or getattr(settings, "wazzup_token", "")
        or getattr(settings, "wazzup_access_token", "")
        or ""
    )

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-KEY"] = str(api_key)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

    content_type = response.headers.get("content-type") or str(message.get("content_type") or "")
    filename = str(message.get("filename") or "")

    if not filename or "." not in filename:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".ogg"
        filename = f"voice{ext}"

    return response.content, filename, content_type


async def _transcribe_voice_message(message: dict[str, Any]) -> str:
    """Распознаёт голосовое максимально совместимо со старым кодом.

    Сначала пробуем существующую функцию transcribe_wazzup_voice(message).
    Если она не подходит под формат Wazzup — скачиваем файл и отправляем bytes в Whisper/OpenAI.
    """
    try:
        transcript = await transcribe_wazzup_voice(message, language="ru")
        text = str(getattr(transcript, "text", "") or "")
        if text.strip():
            return text.strip()
    except Exception as exc:
        state.log_event(str(message.get("chat_id") or "voice"), "voice_transcribe_wazzup_error", {"error": str(exc)[:1000]})

    data, filename, _content_type = await _download_voice_bytes(message)
    transcript = await transcribe_bytes(data, filename=filename, language="ru")
    return str(getattr(transcript, "text", "") or "").strip()


async def _build_answer_for_message(message: dict[str, Any]) -> str:
    chat_id = str(message["chat_id"])
    phone = str(message.get("phone") or chat_id)
    kind = str(message.get("kind") or "text")

    state.log_event(chat_id, "incoming_message_received", {"phone": phone, "kind": kind, "source": str(message.get("source") or "wazzup"), "text_preview": _preview(message.get("text"), 120)})

    pre_hard_session = _get_session_safe(chat_id)
    hard_reason, is_dup, is_old = _hard_inbound_block_reason(message, pre_hard_session)
    if hard_reason:
        _mark_no_reply(chat_id, hard_reason, message, duplicate=is_dup, old=is_old)
        return ""

    source = str(message.get("source") or "wazzup")
    if not bool(getattr(get_settings(), "bot_auto_reply_enabled", True)):
        _mark_bot_auto_reply_disabled(chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
        return ""
    if not is_bot_work_time():
        _mark_working_hours_disabled(chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
        return ""
    pre_session = _get_session_safe(chat_id)
    decision = should_auto_reply(message, pre_session, source=source, force=False, now=astana_now())
    # Tests and deployments patch main.is_bot_work_time as the public work-hours seam.
    if not decision.allowed and decision.no_reply_reason == "working_hours_ai_disabled" and is_bot_work_time():
        decision = GuardDecision(True, "", True, True, source in {"wazzup", "crm"})
    if not decision.allowed:
        _apply_guard_block(decision, chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
        return ""

    if kind == "voice" or _message_has_voice_url(message):
        try:
            transcript_text = await _transcribe_voice_message(message)
            user_text = voice_text_for_bot(transcript_text)
            state.log_event(chat_id, "voice_transcribed", {"text": transcript_text[:500]})
        except Exception as exc:
            state.log_event(chat_id, "voice_transcription_failed_fallback_doctor", {"error": str(exc)[:1000]})
            return _guard_answer(chat_id, _voice_fallback_answer())
    else:
        user_text = str(message.get("text") or "")

    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = True
    session["guard_decision"] = decision.to_dict()
    session["message_id"] = str(message.get("message_id") or message.get("message_key") or "")
    session["message_timestamp"] = str(message.get("timestamp") or "")
    session["bot_activated_at"] = _bot_activated_at().isoformat()
    session["crm_called"] = False
    session["wazzup_send_called"] = False
    state.save_session(chat_id, session)
    state.log_event(chat_id, "dialog_start", {"phone": phone, "text_preview": _preview(user_text, 120), "force": False, "source": message.get("source") or "wazzup"})
    base_answer = await handle_message(chat_id=chat_id, phone=phone, user_text=user_text)
    answer = await _maybe_humanize_answer(chat_id, user_text, base_answer)
    answer = _guard_answer(chat_id, answer)
    _log_dialog_result(chat_id, phone, answer)
    message_key = str(message.get("message_key") or message.get("message_id") or "")
    if message_key:
        state.mark_processed_message(message_key, chat_id)
    return answer


async def handle_incoming_message(message: dict[str, Any]) -> str:
    """Production message pipeline entry point.

    This is the explicit single ingress for one normalized incoming message.  It
    delegates to the guarded implementation that performs metadata validation,
    duplicate/old-message checks, night-hours gating, CRM lookup before any
    first-touch/LLM path, state-machine routing, final answer guard, and debug
    persistence. Sending to Wazzup remains in the webhook debounce worker so this
    function is safe for tests and debug callers that only need the answer.
    """
    return await _build_answer_for_message(message)


async def _send_answer_parts(
    *,
    chat_id: str,
    answer: str,
    chat_type: str,
    channel_id: str | None,
    phone: str = "",
) -> None:
    """Отправляет один dialog_result одним сообщением Wazzup."""
    safe_text = str(answer or "").replace("---", "\n")
    safe_text = re.sub(r"(?i)Вижу Ваш запрос|Ваш запрос принят", "", safe_text)
    safe_text = re.sub(r"\n{3,}", "\n\n", safe_text).strip()
    safe_text = _guard_answer(chat_id, safe_text)
    if not safe_text:
        return
    sess = _get_session_safe(chat_id)
    last_sent = str(sess.get("last_sent_answer") or "")
    if last_sent and (last_sent.strip() == safe_text or (FIRST_TOUCH_CLINIC_INFO_RU in last_sent and safe_text == FIRST_TOUCH_CLINIC_INFO_RU)):
        sess["wazzup_send_called"] = False
        sess["outgoing_duplicate_guard_blocked"] = True
        state.save_session(chat_id, sess)
        state.log_event(chat_id, "wazzup_send_blocked", {"phone": phone, "reason": "duplicate_answer"})
        return
    state.log_event(chat_id, "wazzup_send_attempt", {"phone": phone, "answer_preview": _preview(safe_text, 160)})
    try:
        decision_data = _get_session_safe(chat_id).get("guard_decision")
        if isinstance(decision_data, dict) and not bool(decision_data.get("should_send_wazzup", True)):
            state.log_event(chat_id, "wazzup_send_blocked", {"phone": phone, "reason": _get_session_safe(chat_id).get("no_reply_reason") or "send_guard"})
            return
        sess = _get_session_safe(chat_id)
        sess["wazzup_send_called"] = True
        sess["last_sent_answer"] = safe_text
        state.save_session(chat_id, sess)
        result = await send_text(chat_id=chat_id, text=safe_text, chat_type=chat_type, channel_id=channel_id)
        state.log_event(chat_id, "wazzup_send_result", {"phone": phone, "ok": True, "status_code": result.get("status_code")})
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        state.log_event(chat_id, "wazzup_send_result", {"phone": phone, "ok": False, "status_code": status_code, "error_preview": _preview(exc, 300)})
        raise


async def _debounced_process_and_send(message: dict[str, Any]) -> None:
    """Обрабатывает входящее сообщение в фоне.

    Webhook отвечает Wazzup сразу, а бот отвечает после debounce-паузы.
    Если клиент отправил несколько сообщений подряд, они объединяются в один текст.
    """
    chat_id = str(message["chat_id"])
    chat_type = str(message.get("chat_type") or "whatsapp")
    channel_id = message.get("channel_id") or None
    kind = str(message.get("kind") or "text")

    try:
        settings = get_settings()
        wait_seconds = max(0, int(getattr(settings, "message_debounce_seconds", 0) or 0))

        if wait_seconds > 0 and kind != "voice":
            batch_id = uuid.uuid4().hex
            state.append_pending_message(chat_id, batch_id, str(message.get("text") or ""))
            await asyncio.sleep(wait_seconds)

            if state.latest_pending_batch_id(chat_id) != batch_id:
                return

            combined_text = state.pop_pending_messages(chat_id)
            if not combined_text:
                return

            message = dict(message)
            message["text"] = combined_text

        answer = await _build_answer_for_message(message)
        session_after = _get_session_safe(chat_id)
        guard_decision = session_after.get("guard_decision") if isinstance(session_after.get("guard_decision"), dict) else {}
        if not answer or not bool(guard_decision.get("should_send_wazzup", True)):
            state.log_event(chat_id, "wazzup_send_blocked", {"phone": str(message.get("phone") or ""), "reason": session_after.get("no_reply_reason") or "empty_answer"})
            return

        await _send_answer_parts(
            chat_id=chat_id,
            answer=answer,
            chat_type=chat_type,
            channel_id=channel_id,
            phone=str(message.get("phone") or ""),
        )

    except Exception as exc:
        state.log_event(chat_id, "background_processing_error", {"error": str(exc)[:1000]})
        try:
            if kind == "voice" or _message_has_voice_url(message):
                fallback_text = _voice_fallback_answer()
            else:
                fallback_text = "Передам Ваш вопрос администратору, она ответит Вам в ближайшее время."

            fallback = _guard_answer(chat_id, fallback_text)
            await send_text(
                chat_id=chat_id,
                chat_type=chat_type,
                channel_id=channel_id,
                text=fallback,
            )
        except Exception:
            pass


def _dig_any(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _raw_wazzup_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("messages") or payload.get("message") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [m for m in raw if isinstance(m, dict)]



def _remember_ignored_voice_message(chat_id: str) -> None:
    session = _get_session_safe(chat_id)
    session["last_ignored_message_type"] = "voice"
    state.save_session(chat_id, session)


def _ignore_voice_message(chat_id: str, message_key: str, source: str) -> None:
    _remember_ignored_voice_message(chat_id)
    state.log_event(chat_id, "ignored_voice_message", {"message_key": message_key, "source": source})


def _looks_like_outgoing_message(msg: dict[str, Any]) -> bool:
    return bool(
        msg.get("isEcho") is True
        or msg.get("fromMe") is True
        or str(msg.get("direction") or msg.get("status") or "").lower() in {"out", "outgoing", "sent"}
        or str(_dig_any(msg, "message.direction") or "").lower() in {"out", "outgoing", "sent"}
    )


def _looks_like_api_bot_message(msg: dict[str, Any], session: dict[str, Any]) -> bool:
    source_text = " ".join(
        str(x or "")
        for x in [
            msg.get("source"), msg.get("sender"), msg.get("author"), msg.get("createdBy"),
            msg.get("from"), msg.get("type"), _dig_any(msg, "message.source"),
            _dig_any(msg, "message.sender"), _dig_any(msg, "message.author"),
        ]
    ).lower()
    if any(marker in source_text for marker in ["api", "bot", "integration", "webhook"]):
        return True
    if msg.get("fromApi") is True or msg.get("isApi") is True or _dig_any(msg, "message.fromApi") is True:
        return True

    text = str(
        msg.get("text")
        or msg.get("body")
        or msg.get("content")
        or _dig_any(msg, "message.text")
        or _dig_any(msg, "message.body")
        or ""
    ).strip()
    last_bot = str(session.get("last_assistant_answer") or session.get("last_bot_answer") or "").strip()
    return bool(text and last_bot and text == last_bot)


def _mark_manual_admin_interventions(payload: dict[str, Any]) -> None:
    for msg in _raw_wazzup_messages(payload):
        if not _looks_like_outgoing_message(msg):
            continue
        chat_id = str(msg.get("chatId") or msg.get("chat_id") or msg.get("from") or "").strip()
        if not chat_id:
            continue
        session = _get_session_safe(chat_id)
        if _looks_like_api_bot_message(msg, session):
            continue
        session["manual_admin_intervention"] = True
        session["manual_takeover"] = True
        session["ai_muted"] = True
        state.save_session(chat_id, session)
        state.log_event(chat_id, "manual_admin_intervention_detected", {"source": "wazzup_outgoing"})


def _deep_get(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _first_deep_value(obj: dict[str, Any], paths: list[str]) -> Any:
    for path in paths:
        value = _deep_get(obj, path)
        if value not in (None, ""):
            return value
    return None


def _primary_payload_message(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("messages") or payload.get("message") or payload
    if isinstance(raw, list):
        return next((item for item in raw if isinstance(item, dict)), payload)
    return raw if isinstance(raw, dict) else payload


WAZZUP_INCOMING_STATUSES = {"inbound", "incoming", "received"}
WAZZUP_OUTGOING_DIRECTIONS = {"out", "outbound", "outgoing", "sent"}


def _wazzup_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _is_wazzup_incoming(*, is_echo: Any, from_me: Any, status: str, direction: str) -> bool:
    if _wazzup_bool(is_echo) or _wazzup_bool(from_me):
        return False
    status_norm = (status or "").strip().lower()
    direction_norm = (direction or "").strip().lower()
    if status_norm in WAZZUP_OUTGOING_DIRECTIONS or direction_norm in WAZZUP_OUTGOING_DIRECTIONS:
        return False
    if status_norm in WAZZUP_INCOMING_STATUSES or direction_norm in WAZZUP_INCOMING_STATUSES:
        return True
    return ((is_echo is not None and not _wazzup_bool(is_echo)) or (from_me is not None and not _wazzup_bool(from_me)))


def _normalize_wazzup_message(payload: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
    merged = {**payload, **msg}
    chat_id = _first_deep_value(merged, ["chatId", "chat_id", "dialogId", "dialog_id", "conversationId", "conversation_id", "chat.id", "contact.id"])
    phone = _first_deep_value(merged, ["contact.phone", "phone", "contactPhone", "clientPhone", "userPhone", "contactData.phone", "authorPhone"])
    text = _first_deep_value(merged, ["text", "message", "body", "content", "message.text", "message.body"])
    if isinstance(text, dict):
        text = _first_deep_value(text, ["text", "body", "content"]) or ""
    message_id = _first_deep_value(merged, ["messageId", "message_id", "id", "message.id"])
    message_timestamp = _first_deep_value(merged, ["timestamp", "message_timestamp", "dateTime", "createdAt", "created_at", "message.createdAt"])
    message_type = str(_first_deep_value(merged, ["type", "message.type"]) or "").lower()
    explicit_direction = str(_first_deep_value(merged, ["direction", "message.direction"]) or "").lower()
    status = str(_first_deep_value(merged, ["status", "message.status"]) or "").lower()
    is_echo = _first_deep_value(merged, ["isEcho", "message.isEcho"])
    from_me = _first_deep_value(merged, ["fromMe", "isFromMe", "message.fromMe", "message.isFromMe"])
    is_incoming = _is_wazzup_incoming(is_echo=is_echo, from_me=from_me, status=status, direction=explicit_direction)
    direction = explicit_direction or status or ("incoming" if is_incoming else "outgoing")
    channel_id = str(_first_deep_value(merged, ["channelId", "channel_id", "channel.id", "message.channelId", "message.channel_id"]) or "").strip()
    chat_id = str(chat_id or phone or "").strip()
    phone = str(phone or chat_id).strip()
    return {
        "source": "wazzup",
        "chat_id": chat_id,
        "phone": phone,
        "text": str(text or "").strip(),
        "message_id": str(message_id or "").strip(),
        "message_timestamp": str(message_timestamp or "").strip(),
        "timestamp": str(message_timestamp or "").strip(),
        "channel_id": channel_id,
        "chat_type": str(_first_deep_value(merged, ["chatType", "chat_type"]) or "whatsapp"),
        "message_type": message_type,
        "direction": direction,
        "status": status,
        "is_echo": _wazzup_bool(is_echo),
        "from_me": _wazzup_bool(from_me),
        "is_incoming": is_incoming,
        "is_from_me": str(_wazzup_bool(from_me) or _wazzup_bool(is_echo)).lower(),
        "force": False,
    }


def _normalize_wazzup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _normalize_wazzup_message(payload, _primary_payload_message(payload))


def _result_answer(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("answer") or "")
    return str(getattr(result, "answer", "") or "")


def _result_should_send(result: Any, chat_id: str) -> bool:
    if isinstance(result, dict) and "should_send_wazzup" in result:
        return bool(result.get("should_send_wazzup"))
    if hasattr(result, "should_send_wazzup"):
        return bool(getattr(result, "should_send_wazzup"))
    decision = _get_session_safe(chat_id).get("guard_decision")
    if isinstance(decision, dict) and "should_send_wazzup" in decision:
        return bool(decision.get("should_send_wazzup"))
    return True


async def _safe_read_webhook_payload(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "")
    raw_preview = raw_body[:500].decode("utf-8", errors="replace")
    if not raw_body:
        return {}, {
            "ok": False,
            "reason": "empty_body",
            "content_type": content_type,
            "raw_preview": "",
        }
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
        payload = parsed if isinstance(parsed, dict) else {"payload": parsed}
        return payload, {
            "ok": True,
            "reason": "",
            "content_type": content_type,
            "raw_preview": raw_preview,
        }
    except Exception as exc:
        return {}, {
            "ok": False,
            "reason": "invalid_json",
            "error": str(exc),
            "content_type": content_type,
            "raw_preview": raw_preview,
        }


def _log_ignored_wazzup_webhook(request: Request, parse_meta: dict[str, Any]) -> None:
    reason = str(parse_meta.get("reason") or "invalid_json")
    event = "wazzup_webhook_empty_body" if reason == "empty_body" else "wazzup_webhook_invalid_json"
    payload = {
        "source": "wazzup",
        "path": request.url.path,
        "method": request.method,
        "content_type": parse_meta.get("content_type") or "",
        "payload_parse_ok": False,
        "payload_parse_reason": reason,
        "payload_keys": [],
        "raw_preview": parse_meta.get("raw_preview") or "",
    }
    if parse_meta.get("error"):
        payload["error"] = str(parse_meta.get("error"))[:500]
    state.log_event("wazzup", event, payload)
    state.log_event("wazzup", "wazzup_webhook_ignored", {"source": "wazzup", "reason": reason, "path": request.url.path})


def wazzup_webhook_health_response(request: Request) -> dict[str, Any]:
    return {
        "ok": True,
        "webhook": "wazzup",
        "method": request.method,
        "message": "Use POST for incoming Wazzup messages",
        "request_path": request.url.path,
        "available_webhook_paths": list(WAZZUP_WEBHOOK_PATHS),
        **_wazzup_webhook_urls(),
    }


async def _process_wazzup_message(request: Request, payload: dict[str, Any], raw_msg: dict[str, Any], parse_meta: dict[str, Any], *, send_enabled: bool = True) -> dict[str, Any]:
    message = _normalize_wazzup_message(payload, raw_msg)
    chat_id = message["chat_id"] or "wazzup"
    phone = str(message.get("phone") or chat_id)
    settings = get_settings()
    channel_id_env = str(getattr(settings, "wazzup_channel_id", "") or "")
    channel_id_from_payload = str(message.get("channel_id") or "")
    allowed_channel_ids = [c.strip() for c in str(getattr(settings, "wazzup_allowed_channel_ids", "") or "").split(",") if c.strip()]
    if allowed_channel_ids:
        channel_id_match = (not channel_id_from_payload) or channel_id_from_payload in allowed_channel_ids
    else:
        channel_id_match = (not channel_id_env) or channel_id_from_payload == channel_id_env
    channel_id_mismatch_blocks = bool((allowed_channel_ids or (channel_id_env and channel_id_env != "test-channel")) and channel_id_from_payload and not channel_id_match)
    received_log = {
        "source": "wazzup",
        "path": request.url.path,
        "method": request.method,
        "content_type": parse_meta.get("content_type") or "",
        "payload_parse_ok": True,
        "payload_parse_reason": "",
        "payload_keys": list(payload.keys()),
        "raw_preview": parse_meta.get("raw_preview") or "",
        "chat_id": message["chat_id"],
        "phone": phone,
        "text_preview": _preview(message["text"], 160),
        "message_id": message["message_id"],
        "message_timestamp": message["message_timestamp"],
        "message_type": message["message_type"],
        "direction": message["direction"],
        "status": message["status"],
        "is_echo": message["is_echo"],
        "from_me": message["from_me"],
        "is_incoming": message["is_incoming"],
        "channel_id": channel_id_from_payload,
    }
    state.log_event(chat_id, "wazzup_webhook_received", received_log)
    state.log_event(chat_id, "wazzup_message_normalized", {**received_log, "kind": message.get("kind") or "text"})

    def _decision_payload(*, answer: str = "", should_send_wazzup: bool = False, silent_reason: str = "", crm_error: str = "") -> dict[str, Any]:
        sess = _get_session_safe(chat_id)
        crm_lookup_ok = bool(sess.get("crm_lookup_called")) and not bool(sess.get("crm_lookup_error"))
        return {
            "phone": phone,
            "chat_id": chat_id,
            "channel_id_from_payload": channel_id_from_payload,
            "channel_id_env": channel_id_env,
            "allowed_channel_ids": allowed_channel_ids,
            "channel_id_match": channel_id_match,
            "bot_work_time_now": is_bot_work_time(),
            "bot_auto_reply_enabled": bool(getattr(settings, "bot_auto_reply_enabled", True)),
            "new_leads_only": bool(getattr(settings, "new_leads_only", False)),
            "crm_lookup_ok": crm_lookup_ok,
            "raw_crm_found": sess.get("raw_crm_found"),
            "raw_crm_isNew": sess.get("raw_crm_isNew"),
            "raw_crm_has_patient": bool(sess.get("raw_crm_has_patient")),
            "raw_crm_lead_status": sess.get("raw_crm_lead_status") or "",
            "raw_crm_has_lead": bool(sess.get("raw_crm_has_lead")),
            "raw_crm_has_lastAppointment": bool(sess.get("raw_crm_has_lastAppointment")),
            "raw_crm_hasActiveAppointment": bool(sess.get("raw_crm_hasActiveAppointment")),
            "crm_patient_state": sess.get("crm_patient_state") or "",
            "crm_state_reason": sess.get("crm_state_reason") or "",
            "should_send_wazzup": bool(should_send_wazzup),
            "silent_reason": silent_reason,
            "crm_error": crm_error,
            "answer_preview": _preview(answer, 160),
        }

    def _public_silent_reason(reason: str) -> str:
        reason = str(reason or "").strip()
        mapping = {
            "working_hours_ai_disabled": "outside_work_time",
            "bot_auto_reply_disabled": "auto_reply_disabled",
            "duplicate_message_already_processed": "duplicate_message",
            "non_incoming_message": "echo_or_outgoing_message",
            "voice_message_ignored": "empty_text",
            "empty_answer": "empty_text",
            "processing_failed": "crm_lookup_failed",
        }
        if reason in mapping:
            return mapping[reason]
        allowed = {
            "outside_work_time",
            "auto_reply_disabled",
            "old_lead_or_active_booking",
            "crm_lookup_failed",
            "channel_id_mismatch",
            "duplicate_message",
            "echo_or_outgoing_message",
            "empty_text",
            "send_failed",
        }
        return reason if reason in allowed else "empty_text"

    def _finish_silent(reason: str, *, crm_error: str = "", crm_patient_state: str = "") -> dict[str, Any]:
        public_reason = _public_silent_reason(reason)
        state.log_event(chat_id, "time_gate_result", {"phone": phone, "chat_id": chat_id, **time_gate_status(), "silent_reason": public_reason})
        if public_reason == "empty_text":
            state.log_event(chat_id, "crm_lookup_skipped", {"phone": phone, "chat_id": chat_id, "reason": "empty_text", "silent_reason": public_reason})
        else:
            state.log_event(chat_id, "crm_lookup_start", {"phone": phone, "chat_id": chat_id, "skipped": True, "silent_reason": public_reason})
            state.log_event(chat_id, "crm_lookup_result", {"phone": phone, "chat_id": chat_id, "crm_lookup_ok": False, "crm_patient_state": crm_patient_state, "crm_error": crm_error, "skipped": True, "silent_reason": public_reason})
        state.log_event(chat_id, "bot_decision", _decision_payload(silent_reason=public_reason, crm_error=crm_error))
        state.log_event(chat_id, "ai_or_template_response_ready", {"phone": phone, "chat_id": chat_id, "answer_preview": "", "has_answer": False, "silent_reason": public_reason})
        state.log_event(chat_id, "wazzup_send_start", {"chat_id": chat_id, "channel_id": channel_id_from_payload, "skipped": True, "silent_reason": public_reason})
        state.log_event(chat_id, "wazzup_send_result", {"status_code": None, "response_preview": "", "success": False, "skipped": True, "silent_reason": public_reason})
        state.log_event(chat_id, "bot_processing_finish", {"phone": phone, "chat_id": chat_id, "should_send_wazzup": False, "silent_reason": public_reason})
        return {"ok": True, "ignored": True, "answer": "", "should_send_wazzup": False, "no_reply_reason": public_reason}

    state.log_event(chat_id, "bot_processing_start", {"chat_id": chat_id, "phone": phone, "message_id": message["message_id"], "text_preview": _preview(message["text"], 160)})

    if is_audio_message_payload(raw_msg):
        _ignore_voice_message(chat_id, message["message_id"], "webhook_payload")
        return _finish_silent("empty_text")

    if not message["is_incoming"]:
        session = _get_session_safe(chat_id)
        session["no_reply_reason"] = "echo_or_outgoing_message"
        session["guard_decision"] = GuardDecision(False, "echo_or_outgoing_message", False, False, False).to_dict()
        state.save_session(chat_id, session)
        return _finish_silent("echo_or_outgoing_message")

    if not message["text"]:
        session = _get_session_safe(chat_id)
        session["no_reply_reason"] = "empty_text"
        session["guard_decision"] = GuardDecision(False, "empty_text", False, False, False).to_dict()
        state.save_session(chat_id, session)
        return _finish_silent("empty_text")

    gate = time_gate_status()
    work_time_now = bool(gate["bot_work_time_now"])
    state.log_event(chat_id, "time_gate_result", {"phone": phone, "chat_id": chat_id, **gate})
    state.log_event(chat_id, "crm_lookup_start", {"phone": phone, "chat_id": chat_id})

    answer = ""
    should_send = False
    no_reply_reason = ""
    crm_error = ""
    try:
        result = await handle_incoming_message(message)
        answer = _result_answer(result)
        should_send = _result_should_send(result, chat_id)
    except Exception as exc:
        crm_error = str(exc)[:500]
        no_reply_reason = "crm_lookup_failed"
        state.log_event(chat_id, "crm_lookup_result", {"phone": phone, "chat_id": chat_id, "crm_lookup_ok": False, "crm_error": crm_error})
        state.log_event(chat_id, "bot_decision", _decision_payload(silent_reason=no_reply_reason, crm_error=crm_error))
        state.log_event(chat_id, "ai_or_template_response_ready", {"phone": phone, "chat_id": chat_id, "answer_preview": "", "has_answer": False, "silent_reason": no_reply_reason})
        state.log_event(chat_id, "wazzup_send_start", {"chat_id": chat_id, "channel_id": channel_id_from_payload, "skipped": True, "silent_reason": no_reply_reason})
        state.log_event(chat_id, "wazzup_send_result", {"status_code": None, "response_preview": "", "success": False, "skipped": True, "silent_reason": no_reply_reason})
        state.log_event(chat_id, "bot_processing_finish", {"phone": phone, "chat_id": chat_id, "should_send_wazzup": False, "silent_reason": no_reply_reason})
        return {"ok": True, "ignored": False, "answer": "", "should_send_wazzup": False, "no_reply_reason": no_reply_reason, "crm_error": crm_error}

    session_after = _get_session_safe(chat_id)
    no_reply_reason = str(session_after.get("no_reply_reason") or "")
    crm_error = str(session_after.get("crm_lookup_error") or "")
    crm_lookup_ok = bool(session_after.get("crm_lookup_called")) and not crm_error
    crm_log_payload = {"phone": phone, "chat_id": chat_id, "crm_lookup_ok": crm_lookup_ok, "crm_error": crm_error, "new_leads_only": bool(getattr(settings, "new_leads_only", False)), "should_send_wazzup": bool(answer and should_send), "silent_reason": ""}
    for key in ("raw_crm_found", "raw_crm_isNew", "raw_crm_has_patient", "raw_crm_lead_status", "raw_crm_has_lead", "raw_crm_has_lastAppointment", "raw_crm_hasActiveAppointment", "crm_patient_state", "crm_state_reason"):
        crm_log_payload[key] = session_after.get(key) if key in {"raw_crm_found", "raw_crm_isNew"} else (bool(session_after.get(key)) if key.startswith("raw_crm_has") or key == "raw_crm_hasActiveAppointment" else session_after.get(key) or "")
    state.log_event(chat_id, "crm_lookup_result", crm_log_payload)

    silent_reason = _public_silent_reason(no_reply_reason) if no_reply_reason else ""
    if channel_id_mismatch_blocks:
        silent_reason = "channel_id_mismatch"
        should_send = False
    elif crm_error or no_reply_reason == "crm_lookup_failed":
        silent_reason = "crm_lookup_failed"
        should_send = False
    elif bool(getattr(settings, "new_leads_only", False)) and session_after.get("crm_patient_state") in {"RETURNING_PATIENT_NO_ACTIVE_BOOKING", "ACTIVE_BOOKING"}:
        silent_reason = "old_lead_or_active_booking"
        should_send = False
    elif not bool(getattr(settings, "bot_auto_reply_enabled", True)):
        silent_reason = "auto_reply_disabled"
        should_send = False
    elif not answer:
        silent_reason = silent_reason or "empty_text"
        should_send = False

    state.log_event(chat_id, "bot_decision", _decision_payload(answer=answer, should_send_wazzup=bool(answer and should_send), silent_reason=silent_reason, crm_error=crm_error))
    state.log_event(chat_id, "ai_or_template_response_ready", {"phone": phone, "chat_id": chat_id, "answer_preview": _preview(answer, 160), "has_answer": bool(answer), "silent_reason": silent_reason})

    send_result_payload: dict[str, Any] = {}
    if answer and should_send:
        if send_enabled:
            state.log_event(chat_id, "wazzup_send_start", {"chat_id": chat_id, "channel_id": channel_id_from_payload, "text_preview": _preview(answer, 160)})
            try:
                outbound_channel_id = channel_id_from_payload if (channel_id_from_payload and channel_id_match) else (channel_id_env or None)
                send_result = await send_wazzup_message(chat_id=chat_id, text=answer, chat_type=message.get("chat_type") or "whatsapp", channel_id=outbound_channel_id)
                send_result_payload = {"status_code": send_result.get("status_code"), "response_preview": _preview(send_result, 300), "success": True}
                state.log_event(chat_id, "wazzup_send_result", send_result_payload)
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                response_preview = _preview(getattr(getattr(exc, "response", None), "text", ""), 300)
                silent_reason = "send_failed"
                send_result_payload = {"status_code": status_code, "response_preview": response_preview or _preview(exc, 300), "success": False, "silent_reason": silent_reason}
                state.log_event(chat_id, "wazzup_send_result", send_result_payload)
                state.log_event(chat_id, "bot_decision", _decision_payload(answer=answer, should_send_wazzup=False, silent_reason=silent_reason, crm_error=crm_error))
        else:
            state.log_event(chat_id, "wazzup_send_result", {"status_code": None, "response_preview": "dry_run", "success": False, "dry_run": True})
    else:
        state.log_event(chat_id, "wazzup_send_start", {"chat_id": chat_id, "channel_id": channel_id_from_payload, "skipped": True, "silent_reason": silent_reason or "empty_text"})
        state.log_event(chat_id, "wazzup_send_result", {"status_code": None, "response_preview": "", "success": False, "skipped": True, "silent_reason": silent_reason or "empty_text"})
    state.log_event(chat_id, "bot_processing_finish", {"phone": phone, "chat_id": chat_id, "should_send_wazzup": bool(answer and should_send and send_enabled), "silent_reason": silent_reason, "send_result": send_result_payload})
    return {"ok": True, "ignored": False, "answer": answer, "should_send_wazzup": bool(answer and should_send), "no_reply_reason": silent_reason}


async def handle_wazzup_webhook(request: Request, *, send_enabled: bool = True) -> dict[str, Any]:
    global _LAST_WAZZUP_WEBHOOK_PAYLOAD, _LAST_WAZZUP_PARSE_META
    payload, parse_meta = await _safe_read_webhook_payload(request)
    if not parse_meta.get("ok"):
        _log_ignored_wazzup_webhook(request, parse_meta)
        return {"ok": True, "ignored": True, "reason": parse_meta.get("reason") or "invalid_json"}

    _LAST_WAZZUP_WEBHOOK_PAYLOAD = payload
    _LAST_WAZZUP_PARSE_META = dict(parse_meta)
    raw_messages = _raw_wazzup_messages(payload) or [_primary_payload_message(payload)]
    results = []
    processed_count = 0
    ignored_count = 0
    for raw_msg in raw_messages:
        result = await _process_wazzup_message(request, payload, raw_msg, parse_meta, send_enabled=send_enabled)
        results.append(result)
        if result.get("ignored"):
            ignored_count += 1
        else:
            processed_count += 1

    if len(results) == 1 and "messages" not in payload:
        result = dict(results[0])
        result.pop("ignored", None)
        return result
    return {"ok": True, "processed_count": processed_count, "ignored_count": ignored_count, "results": results}


@app.post("/wazzup/webhook")
async def wazzup_webhook_ingress(request: Request) -> dict[str, Any]:
    return await handle_wazzup_webhook(request)


@app.post("/webhook")
async def webhook_ingress(request: Request) -> dict[str, Any]:
    return await handle_wazzup_webhook(request)


@app.post("/api/wazzup/webhook")
async def api_wazzup_webhook_ingress(request: Request) -> dict[str, Any]:
    return await handle_wazzup_webhook(request)


@app.post("/webhook/wazzup")
async def webhook_wazzup_ingress(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings = get_settings()
    if settings.webhook_secret:
        expected = f"Bearer {settings.webhook_secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Bad webhook secret")
    return await handle_wazzup_webhook(request)


@app.get("/wazzup/webhook")
async def wazzup_webhook_health(request: Request) -> dict[str, Any]:
    return wazzup_webhook_health_response(request)


@app.get("/webhook")
async def webhook_health(request: Request) -> dict[str, Any]:
    return wazzup_webhook_health_response(request)


@app.get("/api/wazzup/webhook")
async def api_wazzup_webhook_health(request: Request) -> dict[str, Any]:
    return wazzup_webhook_health_response(request)


@app.get("/webhook/wazzup")
async def webhook_wazzup_health(request: Request) -> dict[str, Any]:
    return wazzup_webhook_health_response(request)


@app.get("/debug/wazzup/config")
def debug_wazzup_config() -> dict[str, Any]:
    settings = get_settings()
    return {
        "wazzup_api_key_configured": bool(getattr(settings, "wazzup_api_key", "")),
        "wazzup_channel_id_configured": bool(getattr(settings, "wazzup_channel_id", "")),
        "wazzup_instagram_channel_id_configured": bool(getattr(settings, "wazzup_instagram_channel_id", "")),
        "available_webhook_paths": list(WAZZUP_WEBHOOK_PATHS),
        **_wazzup_webhook_urls(),
    }


@app.get("/debug/last-events")
def debug_last_events(phone: str = Query(...), limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    events = state.recent_events_for_phone(phone, limit=limit)
    return {"ok": True, "phone": phone, "events": events}




@app.get("/debug/last-production-events")
def debug_last_production_events(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "events": state.recent_memory_events(limit=limit)}


@app.post("/debug/replay-last-wazzup")
async def debug_replay_last_wazzup(request: Request, dry_run: bool = Query(default=True)) -> dict[str, Any]:
    payload = dict(_LAST_WAZZUP_WEBHOOK_PAYLOAD or {})
    if not payload:
        return {"ok": False, "reason": "no_last_wazzup_payload"}
    parse_meta = dict(_LAST_WAZZUP_PARSE_META or {})
    parse_meta.setdefault("ok", True)
    parse_meta.setdefault("content_type", "application/json")
    parse_meta.setdefault("raw_preview", _preview(payload, 500))
    replay_request = SimpleNamespace(url=SimpleNamespace(path="/debug/replay-last-wazzup"), method="POST")
    raw_messages = _raw_wazzup_messages(payload) or [_primary_payload_message(payload)]
    results = []
    for raw_msg in raw_messages:
        results.append(await _process_wazzup_message(replay_request, payload, raw_msg, parse_meta, send_enabled=not dry_run))
    return {"ok": True, "dry_run": dry_run, "results": results}


@app.post("/debug/wazzup/incoming-test")
async def debug_wazzup_incoming_test(request: Request) -> dict[str, Any]:
    payload, parse_meta = await _safe_read_webhook_payload(request)
    if not parse_meta.get("ok"):
        _log_ignored_wazzup_webhook(request, parse_meta)
        reason = str(parse_meta.get("reason") or "invalid_json")
        return {
            "ok": True,
            "ignored": True,
            "reason": reason,
            "parsed_message": {},
            "handler_result": {"ok": True, "ignored": True, "reason": reason},
            "would_send_wazzup": False,
            "answer_preview": "",
            "no_reply_reason": reason,
        }
    response = await handle_wazzup_webhook(request, send_enabled=False)
    parsed = _normalize_wazzup_payload(payload)
    answer = response.get("answer") or ""
    return {
        "parsed_message": parsed,
        "handler_result": response,
        "would_send_wazzup": False,
        "answer_preview": _preview(answer, 160),
        "no_reply_reason": response.get("no_reply_reason") or "",
    }


@app.get("/debug/crm/lookup")
async def debug_crm_lookup_get(phone: str = Query(...)) -> dict[str, Any]:
    return await _run_debug_crm_lookup(phone)


@app.post("/debug/crm/lookup")
async def debug_crm_lookup_post(data: dict[str, Any]) -> dict[str, Any]:
    return await _run_debug_crm_lookup(str(data.get("phone") or ""))

@app.post("/debug/chat")
async def debug_chat(data: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(data.get("chat_id") or "test")
    phone = str(data.get("phone") or "77011234567")
    text = str(data.get("text") or "")
    force = bool(data.get("force") or False)
    debug_enabled = bool(data.get("debug") or False)

    state.log_event(chat_id, "incoming_message_received", {"phone": phone, "kind": "text", "source": "debug", "text_preview": _preview(text, 120), "force": force, "debug": debug_enabled})

    pre_session = state.get_session(chat_id)
    if not force and not is_bot_work_time():
        _mark_working_hours_disabled(chat_id=chat_id, phone=phone, source="debug", force=force, kind="text", text=text)
        answer = ""
        session = state.get_session(chat_id)
        return {
            "answer": answer,
            "session": session,
            "bot_work_time_now": is_bot_work_time(),
            "last_bot_question_type": session.get("last_bot_question_type"),
            "inferred_context_action": session.get("inferred_context_action"),
            "debug": _dialog_debug(session, answer),
        }
    decision = should_auto_reply(text, pre_session, source="debug", force=force, now=astana_now())
    if not decision.allowed and decision.no_reply_reason == "working_hours_ai_disabled" and is_bot_work_time():
        decision = GuardDecision(True, "", True, True, False)
    if not decision.allowed:
        _apply_guard_block(decision, chat_id=chat_id, phone=phone, source="debug", force=force, kind="text", text=text)
        answer = ""
    else:
        if force:
            session = state.get_session(chat_id)
            for key in ("manual_admin_intervention", "manual_takeover", "ai_muted"):
                session[key] = False
            session["working_hours_bypassed_by_force"] = not is_bot_work_time()
            state.save_session(chat_id, session)
        session = state.get_session(chat_id)
        session["source"] = "debug"
        session["local_time"] = astana_now().isoformat()
        session["working_hours_allowed"] = True
        session["guard_decision"] = decision.to_dict()
        session["message_id"] = str(data.get("message_id") or "")
        session["message_timestamp"] = str(data.get("timestamp") or "")
        session["bot_activated_at"] = _bot_activated_at().isoformat()
        state.save_session(chat_id, session)
        state.log_event(chat_id, "dialog_start", {"phone": phone, "text_preview": _preview(text, 120), "force": force, "debug": debug_enabled, "source": "debug"})
        raw_answer = await handle_message(chat_id=chat_id, phone=phone, user_text=text)
        answer = await _maybe_humanize_answer(chat_id, text, raw_answer)
        answer = _guard_answer(chat_id, answer)
        _log_dialog_result(chat_id, phone, answer)
    session = state.get_session(chat_id)

    return {
        "answer": answer,
        "session": session,
        "bot_work_time_now": is_bot_work_time(),
        "last_bot_question_type": session.get("last_bot_question_type"),
        "inferred_context_action": session.get("inferred_context_action"),
        "used_history_context": session.get("used_history_context"),
        "no_reply_reason": session.get("no_reply_reason"),
        "current_step": session.get("step"),
        "prior_complaint_text": session.get("prior_complaint_text") or session.get("complaint"),
        "openai_config_missing_detail": session.get("openai_config_missing_detail") or {},
        "openai_missing_keys": session.get("openai_missing_keys") or [],
        "openai_disabled_flags": session.get("openai_disabled_flags") or [],
        "debug": _dialog_debug(session, answer),
        "crm_lookup_called": bool(session.get("crm_lookup_called")),
        "crm_lookup_scope": session.get("crm_lookup_scope") or "",
        "crm_endpoint_url": session.get("crm_endpoint_url") or "",
        "crm_lookup_status_code": session.get("crm_lookup_status_code"),
        "crm_lookup_error": session.get("crm_lookup_error") or "",
        "phone_raw": session.get("phone_raw") or phone,
        "phone_normalized": session.get("phone_normalized") or crm.normalize_phone(phone),
        "phone_lookup_variants": session.get("phone_lookup_variants") or crm.phone_lookup_variants(phone),
        "active_appointment_found": bool(session.get("active_appointment_found")),
        "active_appointment_count": int(session.get("active_appointment_count") or 0),
        "appointments_raw_count": int(session.get("appointments_raw_count") or 0),
        "nearest_active_appointment": session.get("nearest_active_appointment"),
        "crm_patient_found": bool(session.get("crm_patient_found")),
        "crm_patient_is_new": bool(session.get("crm_patient_is_new")),
        "crm_patient_state": session.get("crm_patient_state") or "",
        "crm_patient_name": session.get("crm_patient_name") or "",
        "crm_lead_id": session.get("crm_lead_id") or "",
        "crm_lead_status": session.get("crm_lead_status") or "",
        "crm_lead_complaint": session.get("crm_lead_complaint") or "",
        "crm_lead_request": session.get("crm_lead_request") or "",
        "crm_last_appointment_id": session.get("crm_last_appointment_id") or "",
        "crm_last_appointment_date": session.get("crm_last_appointment_date") or "",
        "crm_last_appointment_time": session.get("crm_last_appointment_time") or "",
        "crm_last_appointment_status": session.get("crm_last_appointment_status") or "",
        "first_touch_allowed": bool(session.get("first_touch_allowed")),
        "first_touch_blocked_reason": session.get("first_touch_blocked_reason") or "",
    }


@app.post("/debug/reset")
async def debug_reset(data: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(data.get("chat_id") or "test")
    state.reset_session(chat_id)
    return {"ok": True}


@app.post("/debug/voice")
async def debug_voice(request: Request) -> dict[str, Any]:
    filename = request.headers.get("x-filename") or "voice.ogg"
    phone = request.headers.get("x-phone") or "77011234567"
    chat_id = request.headers.get("x-chat-id") or "test_voice"

    data = await request.body()
    transcript = await transcribe_bytes(data, filename=filename, language="ru")
    raw_answer = await handle_message(
        chat_id=chat_id,
        phone=phone,
        user_text=voice_text_for_bot(transcript.text),
    )
    answer = _guard_answer(chat_id, raw_answer)

    return {"transcript": transcript.text, "answer": answer}


@app.post("/debug/voice-file")
async def debug_voice_file(
    file: UploadFile = File(...),
    phone: str = "77011234567",
    chat_id: str = "test_voice_file",
) -> dict[str, Any]:
    transcript = await transcribe_upload(file, language="ru")
    raw_answer = await handle_message(
        chat_id=chat_id,
        phone=phone,
        user_text=voice_text_for_bot(transcript.text),
    )
    answer = _guard_answer(chat_id, raw_answer)

    return {
        "filename": transcript.filename,
        "content_type": transcript.content_type,
        "transcript": transcript.text,
        "answer": answer,
    }


@app.get("/debug/crm-check")
async def debug_crm_check(date: str = "2026-06-12"):
    """Direct CRM connectivity check from Railway.

    This endpoint does NOT send anything to WhatsApp.
    It only calls crm.check_slots(date) and returns the raw result or error.
    """
    try:
        import crm
        from config import get_settings

        settings = get_settings()
        data = await crm.check_slots(date)

        return {
            "ok": True,
            "date": date,
            "crm_base_url": getattr(settings, "crm_base_url", ""),
            "slots_count": len(data.get("slots", []) or []),
            "availability_count": len(data.get("availability", []) or []),
            "data": data,
        }
    except Exception as exc:
        return {
            "ok": False,
            "date": date,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

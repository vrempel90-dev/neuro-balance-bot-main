from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

import httpx


_REPAIR_ENDPOINT = os.getenv("CLAUDE_REPAIR_ENDPOINT", "").rstrip("/")
_REPAIR_SECRET = os.getenv("CLAUDE_REPAIR_SHARED_SECRET", "")
_ENABLED = str(os.getenv("CLAUDE_OBSERVER_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}


def is_explicit_new_lead(session: dict[str, Any]) -> bool:
    state = str(session.get("crm_patient_state") or "").strip().upper()
    if state in {"RETURNING_PATIENT_NO_ACTIVE_BOOKING", "ACTIVE_BOOKING", "RETURNING_PATIENT", "EXISTING_PATIENT"}:
        return False
    if session.get("raw_crm_isNew") is False or session.get("crm_patient_is_new") is False:
        return False
    if session.get("raw_crm_isNew") is True or session.get("crm_patient_is_new") is True:
        return True
    if state in {"NEW_LEAD", "NEW_PATIENT", "NEW_CLIENT", "NEW"}:
        return True
    # CRM found=false after a successful lookup is the strongest fallback signal
    # that this is a genuinely new lead, not an existing patient.
    if session.get("crm_lookup_called") is True and not session.get("crm_lookup_error") and session.get("raw_crm_found") is False:
        return True
    return False


def _safe_payload(*, chat_id: str, user_text: str, session: dict[str, Any], answer: str) -> dict[str, Any]:
    cid = hashlib.sha256(f"neuro-live-repair:{chat_id}".encode("utf-8")).hexdigest()[:16]
    return {
        "source": "real_wazzup_new_lead",
        "conversation_id": cid,
        "message_id": str(session.get("message_id") or "")[:120],
        "user_text": str(user_text or "")[:800],
        "answer": str(answer or "")[:800],
        "step": str(session.get("step") or session.get("current_step") or ""),
        "state_before_step": str(session.get("state_before_step") or ""),
        "crm_patient_state": str(session.get("crm_patient_state") or ""),
        "raw_crm_isNew": session.get("raw_crm_isNew"),
        "crm_patient_is_new": session.get("crm_patient_is_new"),
        "detected_intent": str(session.get("last_user_intent") or ""),
        "openai_brain_used": bool(session.get("openai_brain_used")),
        "openai_brain_intent": str(session.get("openai_brain_intent") or ""),
        "openai_brain_action": str(session.get("openai_brain_action") or ""),
        "openai_brain_guard_failed": bool(session.get("openai_brain_guard_failed")),
        "openai_brain_guard_reason": str(session.get("openai_brain_guard_reason") or ""),
        "fallback_reason": str(session.get("fallback_reason") or ""),
        "no_reply_reason": str(session.get("no_reply_reason") or ""),
        "language": str(session.get("language") or ""),
        "language_guard_result": str(session.get("language_guard_result") or ""),
        "last_slots_count": len(session.get("last_slots") or []),
        "selected_slot": session.get("selected_slot") or {},
        "booking_ready": bool(session.get("booking_ready")),
        "booking_confirmed": bool(session.get("booking_confirmed")),
        "booking_visible_in_crm": bool(session.get("booking_visible_in_crm")),
        "created_by_ai": bool(session.get("created_by_ai")),
    }


async def _send(payload: dict[str, Any]) -> None:
    if not (_ENABLED and _REPAIR_ENDPOINT and _REPAIR_SECRET):
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            await client.post(
                f"{_REPAIR_ENDPOINT}/ingest/new-lead",
                json=payload,
                headers={"X-Repair-Secret": _REPAIR_SECRET},
            )
    except Exception:
        # Observer is strictly fire-and-forget: never affect a patient reply.
        return


def dispatch_new_lead_turn(*, chat_id: str, user_text: str, session: dict[str, Any], answer: str) -> bool:
    if not _ENABLED or not is_explicit_new_lead(session):
        return False
    payload = _safe_payload(chat_id=chat_id, user_text=user_text, session=session, answer=answer)
    try:
        asyncio.create_task(_send(payload))
        return True
    except RuntimeError:
        return False

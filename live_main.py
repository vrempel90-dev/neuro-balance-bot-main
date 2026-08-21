from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Header, HTTPException

import ai_budget
import main as neuro
import state
from repair_observer import dispatch_new_lead_turn


_original_process_wazzup_message = neuro._process_wazzup_message


async def _process_wazzup_message_with_claude_observer(
    request: Any,
    payload: dict[str, Any],
    raw_msg: dict[str, Any],
    parse_meta: dict[str, Any],
    *,
    send_enabled: bool = True,
) -> dict[str, Any]:
    """Run the unchanged production Wazzup processor, then observe its final CRM state.

    The observer is strictly post-processing and fire-and-forget. Any observer
    failure is swallowed so it can never alter the patient response or Wazzup
    webhook result.
    """
    result = await _original_process_wazzup_message(
        request,
        payload,
        raw_msg,
        parse_meta,
        send_enabled=send_enabled,
    )
    try:
        message = neuro._normalize_wazzup_message(payload, raw_msg)
        if not message.get("is_incoming"):
            return result
        chat_id = str(message.get("chat_id") or "").strip()
        if not chat_id:
            return result
        session = state.get_session(chat_id)
        answer = str((result or {}).get("answer") or session.get("last_sent_answer") or session.get("final_answer_preview") or "")
        dispatch_new_lead_turn(
            chat_id=chat_id,
            user_text=str(message.get("text") or ""),
            session=session,
            answer=answer,
        )
    except Exception:
        pass
    return result


# handle_wazzup_webhook resolves this module-global function at runtime, so all
# existing routes/contracts stay identical while the post-processing observer
# gets the exact completed real Wazzup turn.
neuro._process_wazzup_message = _process_wazzup_message_with_claude_observer

app = neuro.app


def _openai_debug_admin_token() -> str:
    """Return the dedicated diagnostics token without ever exposing it in payloads."""
    return str(os.getenv("OPENAI_DEBUG_ADMIN_TOKEN") or "").strip()


def _require_openai_debug_admin(authorization: str | None) -> None:
    """Fail closed unless the caller presents the dedicated Bearer token."""
    token = _openai_debug_admin_token()
    if not token:
        raise HTTPException(status_code=503, detail="OpenAI diagnostics are disabled")

    provided = str(authorization or "").strip()
    expected = f"Bearer {token}"
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _openai_production_status() -> dict[str, Any]:
    """Return non-secret production OpenAI readiness and budget state."""
    settings = neuro.get_settings()
    api_key_present = bool(str(getattr(settings, "openai_api_key", "") or "").strip())
    ai_enabled = bool(getattr(settings, "ai_enabled", True))
    brain_enabled = bool(getattr(settings, "openai_brain_enabled", True))
    brain_model = str(getattr(settings, "ai_brain_model", "") or getattr(settings, "openai_model", "") or "").strip()

    try:
        budget = ai_budget.budget_status()
    except Exception:
        budget = {
            "budget_status_available": False,
            "over_budget": False,
            "daily_calls_exhausted": False,
        }

    blockers: list[str] = []
    if not api_key_present:
        blockers.append("OPENAI_API_KEY")
    if not ai_enabled:
        blockers.append("AI_ENABLED=false")
    if not brain_enabled:
        blockers.append("OPENAI_BRAIN_ENABLED=false")
    if not brain_model:
        blockers.append("AI_BRAIN_MODEL_or_OPENAI_MODEL")
    if bool(budget.get("over_budget")):
        blockers.append("monthly_budget_exceeded")
    if bool(budget.get("daily_calls_exhausted")):
        blockers.append("daily_call_limit_exceeded")

    return {
        "openai_api_key_present": api_key_present,
        "ai_enabled": ai_enabled,
        "openai_brain_enabled": brain_enabled,
        "ai_brain_model": brain_model,
        "ai_brain_temperature": float(getattr(settings, "ai_brain_temperature", 0.2) or 0.2),
        "ai_brain_max_completion_tokens": int(getattr(settings, "ai_brain_max_completion_tokens", 2000) or 2000),
        "openai_model": str(getattr(settings, "openai_model", "") or ""),
        "openai_humanize_replies": bool(getattr(settings, "openai_humanize_replies", True)),
        "monthly_ai_budget_usd": float(getattr(settings, "monthly_ai_budget_usd", 0.0) or 0.0),
        "ai_max_classifier_calls_per_day": int(getattr(settings, "ai_max_classifier_calls_per_day", 0) or 0),
        "budget": budget,
        "brain_config_ready": not blockers,
        "brain_blockers": blockers,
        "production_entrypoint": "live_main:app -> main.app",
    }


@app.get("/debug/openai/status")
def openai_production_status(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return protected, non-secret OpenAI runtime diagnostics for administrators."""
    _require_openai_debug_admin(authorization)
    return {"ok": True, "openai": _openai_production_status()}

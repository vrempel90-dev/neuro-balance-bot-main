from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

from config import get_settings
from schedule import astana_now, is_bot_work_time


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    no_reply_reason: str = ""
    should_call_openai: bool = False
    should_call_crm: bool = False
    should_send_wazzup: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _real_source(source: str) -> bool:
    return (source or "").strip().lower() in {"wazzup", "crm"}


def should_auto_reply(
    message: str | dict[str, Any],
    session: dict[str, Any] | None,
    source: str,
    force: bool = False,
    now: datetime | None = None,
) -> GuardDecision:
    """Single hard-guard decision before any AI/CRM/Wazzup side effect.

    Order mirrors the production pipeline: emergency switch, working-hours,
    protected/manual/booked state. Dialog-specific refusal is handled in
    dialog.py because it can send exactly one apology before muting.
    """
    session = session or {}
    source_norm = (source or "").strip().lower() or "wazzup"
    is_real = _real_source(source_norm)

    if is_real and not getattr(get_settings(), "bot_auto_reply_enabled", True) and not force:
        return GuardDecision(False, "bot_auto_reply_disabled", False, False, False)

    if is_real and not force and not is_bot_work_time(now or astana_now()):
        return GuardDecision(False, "working_hours_ai_disabled", False, False, False)

    # /debug/chat without force should obey the same guard; force is the only exception.
    if source_norm == "debug" and not force and not is_bot_work_time(now or astana_now()):
        return GuardDecision(False, "working_hours_ai_disabled", False, False, False)
    if source_norm == "debug" and not force and not getattr(get_settings(), "bot_auto_reply_enabled", True):
        return GuardDecision(False, "bot_auto_reply_disabled", False, False, False)

    if session.get("ai_muted") or session.get("manual_takeover") or session.get("manual_admin_intervention"):
        return GuardDecision(False, "manual_takeover", False, False, False)
    if session.get("escalated") or str(session.get("step") or "").lower() == "escalated":
        return GuardDecision(False, "manual_takeover", False, False, False)
    if session.get("booked") or str(session.get("step") or "").lower() in {"booked", "done", "confirmed", "appointment_confirmed"}:
        return GuardDecision(False, "booked_session_ai_disabled", False, False, False)

    return GuardDecision(True, "", True, True, is_real)

"""Python equivalents of the old bot's tool gates.

The legacy TypeScript bot used OpenAI function-calling tools plus programmatic
book gates.  This module keeps the same architecture in the Python controller:
the dialog may be deterministic, but every medically important transition still
marks a tool result and booking is allowed only after the required tools ran.
"""
from __future__ import annotations

from typing import Any

import clinic_info
import state

COMPLAINT_OK = "COMPLAINT_OK"
CONTRA_PROCEED = "proceed"
CONTRA_REFUSE = "refuse"
CONTRA_ESCALATE = "escalate"


def _tool_history(session: dict[str, Any]) -> list[dict[str, Any]]:
    history = session.get("tool_history")
    if not isinstance(history, list):
        history = []
        session["tool_history"] = history
    return history


def mark_tool(session: dict[str, Any], name: str, **payload: Any) -> None:
    """Store a compact in-session tool log for deterministic gates/tests."""
    _tool_history(session).append({"name": name, **payload})


def log_tool(chat_id: str, session: dict[str, Any], name: str, **payload: Any) -> None:
    mark_tool(session, name, **payload)
    try:
        state.log_bot_action(chat_id, name, payload=payload)
    except Exception:
        pass


def get_clinic_info(session: dict[str, Any], topic: str) -> str | None:
    text = clinic_info.get_clinic_info(topic)
    if text:
        mark_tool(session, "get_clinic_info", topic=topic)
        session["last_clinic_info_topic"] = topic
    return text


def record_chief_complaint(session: dict[str, Any], complaint: str, *, is_in_profile: bool) -> None:
    session["complaint"] = complaint
    session["profile_status"] = "profile" if is_in_profile else "non_profile"
    if is_in_profile:
        session["complaint_gate"] = COMPLAINT_OK
    else:
        session["complaint_gate"] = "NON_PROFILE"
    mark_tool(session, "record_chief_complaint", complaint=complaint, is_in_profile=is_in_profile)


def mark_irrelevant(session: dict[str, Any], reason: str = "non_profile") -> None:
    session["irrelevant"] = True
    session["complaint_gate"] = "NON_PROFILE"
    mark_tool(session, "mark_irrelevant", reason=reason)


def verify_contraindications(session: dict[str, Any], verdict: str, raw: str = "") -> None:
    session["contraindications_verdict"] = verdict
    if verdict == CONTRA_PROCEED:
        session["contraindications_ok"] = True
    else:
        session["contraindications_ok"] = False
    if raw:
        session["contraindications_raw"] = raw
    mark_tool(session, "verify_contraindications_check", verdict=verdict, raw=raw)


def escalate_to_human(session: dict[str, Any], reason: str) -> None:
    session["escalated"] = True
    session["step"] = "escalated"
    mark_tool(session, "escalate_to_human", reason=reason)


def booking_gate_status(session: dict[str, Any]) -> tuple[bool, str]:
    """Return whether book_appointment may execute, mirroring the old gates."""
    if session.get("complaint_gate") != COMPLAINT_OK or not session.get("complaint"):
        return False, "complaint"
    if session.get("contraindications_verdict") in {CONTRA_REFUSE, "stop"}:
        return False, "contra_refuse"
    if session.get("contraindications_verdict") != CONTRA_PROCEED or session.get("contraindications_ok") is not True:
        return False, "contra"
    return True, "ok"

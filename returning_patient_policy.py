"""Production policy for CRM patients who have no active appointment.

Historically these contacts were classified as RETURNING_PATIENT_NO_ACTIVE_BOOKING
and diverted into a greeting-only branch on every inbound turn. That meant a
patient could say "хочу записаться", receive the age question, answer "35" and
then be greeted as a returning patient again instead of continuing the funnel.

When NEW_LEADS_ONLY is disabled, an existing CRM contact without an active
appointment is allowed to start a fresh booking conversation. We therefore
reactivate only that CRM state as NEW_PATIENT for the dialog controller. Active
appointments remain ACTIVE_BOOKING and all manual-takeover/booked guards remain
unchanged.
"""
from __future__ import annotations

from typing import Any

import dialog
from config import get_settings


_INSTALLED = False
_ORIGINAL_SET_CRM_PATIENT_DEBUG = dialog._set_crm_patient_debug


def _set_crm_patient_debug_reactivated(
    session: dict[str, Any],
    lookup: dict[str, Any] | None,
    appt: dict[str, Any] | None,
) -> None:
    _ORIGINAL_SET_CRM_PATIENT_DEBUG(session, lookup, appt)

    if bool(getattr(get_settings(), "new_leads_only", True)):
        return
    if session.get("crm_patient_state") != "RETURNING_PATIENT_NO_ACTIVE_BOOKING":
        return
    if appt or session.get("raw_crm_hasActiveAppointment"):
        return

    session["crm_returning_reactivated"] = True
    session["crm_original_patient_state"] = "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    session["crm_patient_state"] = "NEW_PATIENT"
    session["crm_state_reason"] = (
        "existing CRM patient has no active appointment and NEW_LEADS_ONLY=false "
        "=> reactivated for a new AI booking conversation"
    )

    # A previous old-lead mute must not survive into the fresh booking flow.
    if session.get("no_reply_reason") in {"old_lead_from_crm", "active_booking_old_lead"}:
        session["no_reply_reason"] = ""
    if session.get("silent_old_lead"):
        session["silent_old_lead"] = False
        session["old_lead_reason"] = ""
    if session.get("manual_takeover") and session.get("first_touch_blocked_reason") in {
        "returning_patient_old_lead",
        "old_lead_from_crm",
    }:
        session["manual_takeover"] = False
        session["ai_muted"] = False


def install_returning_patient_policy() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    dialog._set_crm_patient_debug = _set_crm_patient_debug_reactivated
    _INSTALLED = True

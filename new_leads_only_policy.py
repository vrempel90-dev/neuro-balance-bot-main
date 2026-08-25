"""Hard production admission policy: AI may talk only to genuinely new CRM leads.

Business rule:
- NEW lead -> AI may handle the conversation.
- Existing/returning patient -> NO REPLY.
- Active/booked appointment -> NO REPLY.
- Post-booking support must not reopen the AI for an old/booked lead.

This policy is intentionally fail-closed and cannot be disabled by an accidental
Railway variable change. CRM classification in dialog.py remains the source of
truth for NEW_PATIENT vs RETURNING_PATIENT_NO_ACTIVE_BOOKING vs ACTIVE_BOOKING.
"""
from __future__ import annotations

from typing import Any

import dialog


_INSTALLED = False


def _production_new_leads_only() -> bool:
    return True


def _disable_post_booking_ai(session: dict[str, Any], chat_id: str = "") -> bool:
    # In NEW_LEADS_ONLY production mode an appointment makes the contact old for
    # AI admission immediately. A human administrator handles any later message.
    return False


def install_new_leads_only_policy() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # Make the business rule code-level, not merely an environment preference.
    dialog._is_new_leads_only_enabled = _production_new_leads_only

    # dialog.handle_message has an old compatibility exception that can allow an
    # ACTIVE_BOOKING contact through while post-booking support is active. Disable
    # that exception so ACTIVE_BOOKING and RETURNING contacts always hit NO REPLY.
    dialog._post_booking_support_is_active = _disable_post_booking_ai

    _INSTALLED = True

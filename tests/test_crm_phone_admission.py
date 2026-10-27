"""An old patient's phone can be stored in CRM with +7, 8 or 10 digits."""
from __future__ import annotations

import asyncio

import admission
import crm
import dialog
import pytest


PHONE = "77011234567"


def unknown():
    return {
        "ok": True, "found": False, "isNew": True,
        "patient": None, "lead": None, "lastAppointment": None,
        "hasActiveAppointment": False, "appointments": [],
    }


@pytest.mark.parametrize("matched_format", ["77011234567", "+77011234567", "87011234567", "7011234567"])
def test_existing_crm_number_in_any_format_blocks_bot(monkeypatch, matched_format):
    seen = []
    async def lookup(variant):
        seen.append(variant)
        if variant == matched_format:
            return {**unknown(), "found": True, "isNew": False, "patient": {"id": "existing"}}
        return unknown()
    monkeypatch.setattr(crm, "patient_lookup_raw", lookup)
    response = asyncio.run(crm.lookup_active_appointments_by_phone(PHONE))
    assert set(seen) == set(crm.phone_lookup_variants(PHONE))
    verdict = admission.classify(PHONE, response)
    assert verdict.state == admission.RETURNING
    assert not verdict.bot_may_reply


def test_any_variant_has_active_appointment(monkeypatch):
    async def lookup(variant):
        if variant.startswith("8"):
            return {
                **unknown(), "found": True, "isNew": False,
                "hasActiveAppointment": True, "patient": {"id": "booked"},
            }
        return unknown()
    monkeypatch.setattr(crm, "patient_lookup_raw", lookup)
    response = asyncio.run(crm.lookup_active_appointments_by_phone(PHONE))
    verdict = admission.classify(PHONE, response)
    assert verdict.state == admission.ACTIVE_BOOKING
    assert not verdict.bot_may_reply


def test_ambiguous_partial_phone_lookup_fails_closed(monkeypatch):
    async def lookup(variant):
        if variant.startswith("8"):
            raise TimeoutError("crm timeout")
        return unknown()
    monkeypatch.setattr(crm, "patient_lookup_raw", lookup)
    with pytest.raises(crm.CRMError, match="incomplete"):
        asyncio.run(crm.lookup_active_appointments_by_phone(PHONE))


def test_crm_returns_missing_isnew_never_treated_as_new():
    for payload in [
        {"ok": True, "found": False},
        {"ok": True, "isNew": True},
        {"ok": True, "found": None, "isNew": True},
        {"ok": True, "found": False, "isNew": "true"},
    ]:
        assert admission.classify(PHONE, payload).state == admission.CRM_UNAVAILABLE


def test_crm_new_lead_requires_absence_from_all_forms(monkeypatch):
    async def lookup(_):
        return unknown()
    monkeypatch.setattr(crm, "patient_lookup_raw", lookup)
    result = asyncio.run(crm.lookup_active_appointments_by_phone(PHONE))
    assert admission.classify(PHONE, result).state == admission.NEW


def test_past_appointment_and_new_flag_still_means_old_client():
    payload = {
        **unknown(), "appointments": [{
            "id": 42, "date": "2020-01-01",
            "status": "completed",
        }],
    }
    assert admission.classify(PHONE, payload).state == admission.RETURNING


def test_contradictory_crm_flags_do_not_admit_existing_phone():
    payload = {**unknown(), "found": True, "isNew": True}
    assert admission.classify(PHONE, payload).state == admission.RETURNING


def test_new_status_of_existing_lead_is_not_new():
    payload = {**unknown(), "found": True, "lead": {"id": 8, "status": "НОВАЯ"}}
    assert admission.classify(PHONE, payload).state == admission.RETURNING


def test_crm_client_does_not_log_full_number():
    assert crm._mask_phone_for_log("+77011234567") == "***4567"


def test_previous_verified_new_lead_can_continue_while_crm_creates_its_card():
    from datetime import datetime, timezone
    session = {
        "ai_admission_started_at": datetime.now(timezone.utc).isoformat(),
        "ai_admission_closed_at": None,
    }
    assert dialog._ai_admission_lease_active(session) is True
    assert "crm_lead_already_exists" in dialog._AI_CONTINUATION_REASONS


def test_booked_client_cannot_use_continuation_lease():
    from datetime import datetime, timezone
    session = {
        "ai_admission_started_at": datetime.now(timezone.utc).isoformat(),
        "booking_confirmed": True,
    }
    assert dialog._ai_admission_lease_active(session) is False

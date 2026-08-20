from __future__ import annotations

import asyncio

import repair_live
import repair_observer


def test_observer_accepts_only_explicit_new_leads() -> None:
    assert repair_observer.is_explicit_new_lead({"crm_patient_state": "NEW_LEAD"})
    assert repair_observer.is_explicit_new_lead({"raw_crm_isNew": True})
    assert repair_observer.is_explicit_new_lead({"crm_patient_is_new": True})
    assert repair_observer.is_explicit_new_lead({"crm_lookup_called": True, "raw_crm_found": False, "crm_lookup_error": ""})


def test_observer_rejects_returning_or_existing_clients() -> None:
    assert not repair_observer.is_explicit_new_lead({"crm_patient_state": "RETURNING_PATIENT_NO_ACTIVE_BOOKING", "raw_crm_isNew": False})
    assert not repair_observer.is_explicit_new_lead({"crm_patient_state": "ACTIVE_BOOKING"})
    assert not repair_observer.is_explicit_new_lead({"crm_patient_state": "RETURNING_PATIENT"})
    assert not repair_observer.is_explicit_new_lead({"raw_crm_isNew": False})


def test_repair_service_has_same_new_lead_gate() -> None:
    assert repair_live._is_new({"crm_patient_state": "NEW_LEAD"})
    assert repair_live._is_new({"raw_crm_isNew": True})
    assert not repair_live._is_new({"crm_patient_state": "RETURNING_PATIENT_NO_ACTIVE_BOOKING", "raw_crm_isNew": False})
    assert not repair_live._is_new({"crm_patient_state": "ACTIVE_BOOKING"})


def test_safe_payload_contains_no_phone_or_patient_name() -> None:
    payload = repair_observer._safe_payload(
        chat_id="77001234567",
        user_text="Болит колено",
        session={
            "message_id": "m1",
            "crm_patient_state": "NEW_LEAD",
            "patient_name": "Secret Name",
            "phone": "77001234567",
            "crm_patient_name": "Secret Name",
            "step": "complaint",
        },
        answer="Уточните возраст",
    )
    text = str(payload)
    assert "77001234567" not in text
    assert "Secret Name" not in text
    assert "phone" not in payload
    assert "patient_name" not in payload


def test_dispatch_is_fire_and_forget_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(repair_observer, "_ENABLED", False)
    result = repair_observer.dispatch_new_lead_turn(
        chat_id="1",
        user_text="hello",
        session={"crm_patient_state": "NEW_LEAD"},
        answer="hi",
    )
    assert result is False

"""Offline regression tests: identity admission and persisted-booking readback.

All patients/appointments here are synthetic. Every CRM call is a fake; these
tests prove controller behavior and do not claim production integration success.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import admission
import crm
import services


PHONE = "77001230001"


def new_lookup(**changes):
    body = {
        "ok": True, "found": False, "isNew": True, "patient": None,
        "lead": None, "lastAppointment": None, "hasActiveAppointment": False,
        "appointments": [],
    }
    body.update(changes)
    return body


@pytest.mark.parametrize("body", [
    {"ok": True}, {"found": False}, {"isNew": True},
    {"found": "false", "isNew": True},
    {"found": False, "isNew": "true"},
    {"found": False, "isNew": True, "hasActiveAppointment": "false"},
    {"ok": True, "raw": {"ok": False, "found": False, "isNew": True}},
    {"found": False, "isNew": True, "patient": {}},
    {"found": False, "isNew": True, "appointments": "unavailable"},
])
def test_indeterminate_crm_cannot_admit_new_patient(body):
    assert admission.classify(PHONE, body).state == admission.CRM_UNAVAILABLE


@pytest.mark.parametrize("phone", ["", "123", "79001230001", "15551234567", "abc"])
def test_non_kz_or_invalid_phone_is_never_admitted(phone):
    assert not admission.classify(phone, new_lookup()).bot_may_reply
    assert crm.phone_lookup_variants(phone) == []


def test_fresh_inbound_lead_is_not_confused_with_existing_clinic_patient():
    body = new_lookup(found=True, lead={"id": "synthetic-lead", "status": "НОВАЯ"})
    assert admission.classify(PHONE, body).bot_may_reply
    body["patient"] = {"id": "synthetic-patient"}
    assert admission.classify(PHONE, body).state == admission.RETURNING


def test_past_or_cancelled_history_excludes_existing_patient_even_if_isnew_true():
    body = new_lookup(records=[{"id": 10, "date": "2020-01-01", "status": "cancelled"}])
    assert admission.classify(PHONE, body).state == admission.RETURNING


def test_lookup_checks_all_formats_and_existing_match_wins(monkeypatch):
    calls = []

    async def lookup(value):
        calls.append(value)
        return new_lookup(found=True, isNew=False, patient={"id": "patient-1"}) if value.startswith("8") else new_lookup()

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))
    assert set(calls) == set(crm.phone_lookup_variants(PHONE))
    assert admission.classify(PHONE, result).state == admission.RETURNING


def test_one_unresolved_phone_format_cannot_be_assumed_new(monkeypatch):
    async def lookup(value):
        if value.startswith("8"):
            raise httpx.ReadTimeout("synthetic network timeout")
        return new_lookup()

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    with pytest.raises(crm.CRMError, match="incomplete"):
        asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))


def test_all_formats_confirmed_unknown_admits_new_patient(monkeypatch):
    async def lookup(value):
        return new_lookup()

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))
    assert admission.classify(PHONE, result).bot_may_reply


def test_crm_logs_do_not_contain_body_phone_or_medical_text(caplog):
    response = httpx.Response(500, json={"phone": PHONE, "complaint": "synthetic private complaint"})
    with caplog.at_level(logging.INFO, logger="crm"):
        with pytest.raises(crm.CRMResponseError) as exc:
            crm._raise_for_crm(response, "patient-lookup")
    assert PHONE not in caplog.text + str(exc.value)
    assert "synthetic private complaint" not in caplog.text + str(exc.value)


@pytest.mark.parametrize("body", [[], None, {"ok": False}, {"error": "synthetic error"}])
def test_successful_http_does_not_hide_malformed_or_failed_crm_read(body):
    response = httpx.Response(200, json=body)
    with pytest.raises(crm.CRMError):
        crm._read_object(response, "patient-lookup")


def saved_appointment(**changes):
    appointment = {
        "id": "synthetic-appointment", "phone": PHONE, "doctorLogin": "doctor.test",
        "date": "2031-01-03", "timeStart": "10:00", "status": "booked",
    }
    appointment.update(changes)
    return appointment


def verify():
    return asyncio.run(crm.verify_saved_booking(
        {"appointmentId": "synthetic-appointment"}, phone=PHONE,
        doctor_login="doctor.test", date="2031-01-03", time_start="10:00",
    ))


def test_saved_booking_is_confirmed_only_after_matching_readback(monkeypatch):
    async def by_id(self, appointment_id):
        return {"appointment": saved_appointment()}

    monkeypatch.setattr(crm.CRMClient, "lookup_appointment_by_id", by_id)
    assert verify()["verified"] is True


@pytest.mark.parametrize("changes", [
    {"id": "other-id"}, {"phone": "77001230002"},
    {"doctorLogin": "other-doctor"}, {"date": "2031-01-04"}, {"timeStart": "11:00"},
    {"status": "cancelled"}, {"status": "Отменена"}, {"status": "НЕ_ПРИШЁЛ"},
])
def test_mismatching_persisted_booking_is_not_confirmed(monkeypatch, changes):
    async def by_id(self, appointment_id):
        return {"appointment": saved_appointment(**changes)}

    async def by_phone(phone):
        return new_lookup()

    monkeypatch.setattr(crm.CRMClient, "lookup_appointment_by_id", by_id)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", by_phone)
    assert verify()["verified"] is False


def test_bare_ok_response_cannot_confirm_saved_booking():
    result = asyncio.run(crm.verify_saved_booking(
        {"ok": True}, phone=PHONE, doctor_login="doctor.test",
        date="2031-01-03", time_start="10:00",
    ))
    assert result == {"verified": False, "reason": "appointment_id_missing"}


def test_phone_scoped_readback_can_verify_without_id_endpoint(monkeypatch):
    async def by_id(self, appointment_id):
        raise crm.CRMError("unsupported read endpoint")

    async def by_phone(phone):
        appointment = saved_appointment()
        appointment.pop("phone")
        return new_lookup(found=True, isNew=False, lastAppointment=appointment)

    monkeypatch.setattr(crm.CRMClient, "lookup_appointment_by_id", by_id)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", by_phone)
    assert verify()["verified"] is True


@pytest.mark.parametrize("body, expected", [({}, False), ({"ok": True}, True), ({"success": True}, True)])
def test_handoff_requires_actual_crm_acknowledgement(monkeypatch, body, expected):
    class FakeClient:
        async def post(self, *args, **kwargs):
            return httpx.Response(200, json=body)

    monkeypatch.setattr(crm, "_client", FakeClient)
    result = asyncio.run(crm.escalate_to_operator(phone=PHONE, reason="synthetic human request"))
    assert result["ok"] is expected


@pytest.mark.parametrize("complaint", ["арқам ауырады", "белім ауырады", "тізем ауырады", "мойным емес, мойын ауырады", "arkam auyrady"])
def test_kazakh_profile_complaints_do_not_trigger_repeated_profile_question(complaint):
    assert services.classify_by_keywords(complaint)["can_help"] is True

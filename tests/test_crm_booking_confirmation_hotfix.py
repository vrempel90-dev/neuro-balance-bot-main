from __future__ import annotations

import httpx
import pytest

import crm


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def post(self, *args, **kwargs):
        return self.response


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://crm.test/api/bot/book"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": "queued"},
        {"status": "Записан"},
    ],
)
async def test_book_appointment_rejects_ambiguous_2xx(monkeypatch, payload):
    """HTTP 2xx alone must never be enough to tell the patient they are booked."""
    monkeypatch.setattr(crm, "_client", lambda: _FakeClient(_response(payload)))

    with pytest.raises(crm.CRMError, match="unconfirmed"):
        await crm.book_appointment(
            patient_name="Тест",
            phone="77000000000",
            doctor_login="doctor.test",
            doctor_name="Тестовый врач",
            date="2026-08-25",
            time_start="10:00",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"success": True},
        {"booked": True},
        {"created": True},
        {"appointmentId": "apt-123"},
        {"bookingId": "book-123"},
        {"id": "row-123"},
    ],
)
async def test_book_appointment_accepts_explicit_crm_confirmation(monkeypatch, payload):
    """Only an explicit success flag or CRM appointment identifier is confirmation."""
    monkeypatch.setattr(crm, "_client", lambda: _FakeClient(_response(payload)))

    result = await crm.book_appointment(
        patient_name="Тест",
        phone="77000000000",
        doctor_login="doctor.test",
        doctor_name="Тестовый врач",
        date="2026-08-25",
        time_start="10:00",
    )

    original_keys = set(payload)
    assert set(result[crm.CRM_RESPONSE_KEYS_FIELD]) == original_keys

from __future__ import annotations

import asyncio

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


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": "queued"},
        {"status": "Записан"},
    ],
)
def test_book_appointment_does_not_confirm_ambiguous_2xx(monkeypatch, payload):
    """HTTP 2xx alone must never be enough to tell the patient they are booked."""
    import agent

    monkeypatch.setattr(crm, "_client", lambda: _FakeClient(_response(payload)))

    result = asyncio.run(
        crm.book_appointment(
            patient_name="Тест",
            phone="77000000000",
            doctor_login="doctor.test",
            doctor_name="Тестовый врач",
            date="2026-08-25",
            time_start="10:00",
        )
    )

    assert agent._crm_booking_succeeded(result) is False


@pytest.mark.parametrize(
    ("payload", "confirmation_key"),
    [
        ({"ok": True}, "ok"),
        ({"success": True}, "success"),
        ({"booked": True}, "booked"),
        ({"created": True}, "created"),
        ({"appointmentId": "apt-123"}, "appointmentId"),
        ({"bookingId": "book-123"}, "bookingId"),
        ({"id": "row-123"}, "id"),
    ],
)
def test_book_appointment_accepts_explicit_crm_confirmation(monkeypatch, payload, confirmation_key):
    """Only an explicit success flag or CRM appointment identifier is confirmation."""
    original_confirmation = payload[confirmation_key]
    monkeypatch.setattr(crm, "_client", lambda: _FakeClient(_response(payload)))

    result = asyncio.run(
        crm.book_appointment(
            patient_name="Тест",
            phone="77000000000",
            doctor_login="doctor.test",
            doctor_name="Тестовый врач",
            date="2026-08-25",
            time_start="10:00",
        )
    )

    original_keys = set(payload)
    assert set(result[crm.CRM_RESPONSE_KEYS_FIELD]) == original_keys
    if confirmation_key in {"ok", "success", "booked", "created"}:
        assert original_confirmation is True
        assert result[confirmation_key] is True
    else:
        assert str(original_confirmation).strip()
        assert result[confirmation_key] == original_confirmation
    assert (
        any(result.get(key) is True for key in ("ok", "success", "booked", "created"))
        or any(
            str(result.get(key) or "").strip()
            for key in ("id", "appointmentId", "bookingId")
        )
    )


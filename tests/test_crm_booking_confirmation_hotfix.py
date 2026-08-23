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


def test_dialog_book_keeps_ambiguous_crm_result_uncertain(monkeypatch):
    import agent
    import bot_tools
    import dialog
    import state

    slot = {
        "doctorLogin": "doctor.test",
        "doctorName": "Тестовый врач",
        "date": "2026-08-25",
        "timeStart": "10:00",
    }
    session = {
        "patient_name": "Тест",
        "complaint": "Боль в спине",
        "complaint_gate": bot_tools.COMPLAINT_OK,
        "contraindications_ok": True,
        "contraindications_verdict": bot_tools.CONTRA_PROCEED,
        "selected_slot": slot,
        "last_slots": [slot],
    }
    post_count = 0

    async def ambiguous_book(**kwargs):
        nonlocal post_count
        post_count += 1
        return {
            "ok": True,
            "status": "Записан",
            crm.CRM_RESPONSE_KEYS_FIELD: [],
        }

    monkeypatch.setattr(crm, "book_appointment", ambiguous_book)

    first_answer = asyncio.run(dialog._book("ambiguous-dialog-book", session, "77000000000"))
    claim_key = agent._slot_key("2026-08-25", "10:00", "doctor.test") + "|77000000000"

    assert post_count == 1
    assert session["booking_confirmed"] is False
    assert session["booking_uncertain"] is True
    assert "запись подтверждена" not in first_answer.lower()
    assert state.booking_claim_status(claim_key) == "uncertain"

    second_answer = asyncio.run(dialog._book("ambiguous-dialog-book", session, "77000000000"))

    assert post_count == 1
    assert session["booking_confirmed"] is False
    assert session["booking_uncertain"] is True
    assert "запись подтверждена" not in second_answer.lower()
    assert state.booking_claim_status(claim_key) == "uncertain"

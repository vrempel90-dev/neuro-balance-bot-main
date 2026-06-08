from __future__ import annotations

from typing import Any

import httpx

from config import get_settings


def _base_url() -> str:
    settings = get_settings()
    return (getattr(settings, "crm_base_url", "") or "").rstrip("/")


def _secret() -> str:
    settings = get_settings()
    return (
        getattr(settings, "crm_bot_secret", "")
        or getattr(settings, "external_booking_api_secret", "")
        or getattr(settings, "bot_api_secret", "")
        or ""
    )


def _headers() -> dict[str, str]:
    secret = _secret()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if secret:
        headers["x-bot-secret"] = secret
    return headers


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=8.0, read=20.0, write=20.0, pool=8.0)


async def check_slots(date: str, doctor_login: str | None = None, doctor: str | None = None) -> dict[str, Any]:
    """
    Official CRM contract.

    GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=<login>
    Header: x-bot-secret: <key>

    Response:
    {
      "availability": [
        {
          "doctorLogin": "...",
          "doctorName": "...",
          "date": "YYYY-MM-DD",
          "availableSlots": ["10:00", "12:30"]
        }
      ]
    }

    doctor is optional.
    """
    base = _base_url()
    if not base:
        raise RuntimeError("CRM_BASE_URL is not configured")

    params: dict[str, str] = {"date": date}
    selected_doctor = doctor_login or doctor
    if selected_doctor:
        params["doctor"] = selected_doctor

    url = f"{base}/api/bot/check-slots"

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.get(url, params=params, headers=_headers())

    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("CRM check-slots returned non-object JSON")

    availability = data.get("availability")
    if availability is None:
        data["availability"] = []
    elif not isinstance(availability, list):
        raise RuntimeError("CRM check-slots field 'availability' must be a list")

    return data


async def book_appointment(
    patient_name: str,
    phone: str,
    doctor_login: str,
    date: str,
    time_start: str,
    doctor_name: str | None = None,
    notes: str | None = None,
    conversation_id: str | None = None,
    lead_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Official CRM contract.

    POST /api/bot/book
    Header: x-bot-secret: <key>

    Required body:
    {
      "patientName": "...",
      "phone": "77011234567",
      "doctorLogin": "...",
      "date": "YYYY-MM-DD",
      "timeStart": "10:00"
    }

    Optional:
    doctorName, notes, conversationId, leadId

    Success response status: 201
    {
      "appointmentId": 123,
      "doctorName": "...",
      "date": "YYYY-MM-DD",
      "timeStart": "10:00",
      "timeEnd": "10:30"
    }
    """
    base = _base_url()
    if not base:
        raise RuntimeError("CRM_BASE_URL is not configured")

    if not patient_name:
        raise ValueError("patient_name is required")
    if not phone:
        raise ValueError("phone is required")
    if not doctor_login:
        raise ValueError("doctor_login is required")
    if not date:
        raise ValueError("date is required")
    if not time_start:
        raise ValueError("time_start is required")

    payload: dict[str, Any] = {
        "patientName": patient_name,
        "phone": phone,
        "doctorLogin": doctor_login,
        "date": date,
        "timeStart": time_start,
    }

    if doctor_name:
        payload["doctorName"] = doctor_name
    if notes:
        payload["notes"] = notes
    if conversation_id:
        payload["conversationId"] = conversation_id
    if lead_id:
        payload["leadId"] = lead_id

    url = f"{base}/api/bot/book"

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(url, json=payload, headers=_headers())

    # Official success is 201, but accept any 2xx to avoid false failures
    # if CRM returns 200 in staging.
    if response.status_code < 200 or response.status_code >= 300:
        response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("CRM book returned non-object JSON")

    return data


async def patient_lookup(phone: str) -> dict[str, Any]:
    """
    Optional helper for "I am already booked, remind me the time".

    If CRM later provides this endpoint, we will use:
    GET /api/bot/patient-lookup?phone=77011234567

    If the endpoint is missing, this function safely returns no active appointment.
    This prevents the bot from crashing and lets dialog.py fallback to admin/coordinator.
    """
    base = _base_url()
    if not base:
        return {"hasActiveAppointment": False}

    url = f"{base}/api/bot/patient-lookup"
    params = {"phone": phone}

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.get(url, params=params, headers=_headers())

        if response.status_code == 404:
            return {"hasActiveAppointment": False}

        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return data

        return {"hasActiveAppointment": False}

    except Exception:
        return {"hasActiveAppointment": False}


async def create_fallback_lead(
    phone: str,
    text: str,
    notes: str | None = None,
    conversation_id: str | None = None,
    lead_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """
    Optional fallback endpoint.

    If CRM later provides a fallback/task endpoint, this can create a manual task:
    POST /api/bot/fallback-lead

    Current dialog can work without this. If endpoint is missing, return ok=false.
    """
    base = _base_url()
    if not base:
        return {"ok": False, "reason": "CRM_BASE_URL is not configured"}

    payload: dict[str, Any] = {
        "phone": phone,
        "text": text,
    }

    if notes:
        payload["notes"] = notes
    if conversation_id:
        payload["conversationId"] = conversation_id
    if lead_id:
        payload["leadId"] = lead_id
    payload.update(extra)

    url = f"{base}/api/bot/fallback-lead"

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.post(url, json=payload, headers=_headers())

        if response.status_code == 404:
            return {"ok": False, "reason": "fallback endpoint not found"}

        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return data

        return {"ok": True}

    except Exception as exc:
        return {"ok": False, "reason": str(exc)}

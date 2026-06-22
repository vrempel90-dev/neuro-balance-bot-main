from __future__ import annotations

from functools import lru_cache
from typing import Any
import logging
import time
import re

import httpx

from config import get_settings


class CRMError(Exception):
    pass


class CRMResponseError(CRMError):
    def __init__(self, label: str, response: httpx.Response, data: dict[str, Any] | None = None):
        self.label = label
        self.status_code = response.status_code
        self.response_text = response.text
        self.data = data or {}
        self.code = str(self.data.get("code") or "")
        message = self.data.get("error") or self.data.get("message") or self.response_text
        super().__init__(f"CRM {label} error {self.status_code}: {message}")


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0))


def _headers() -> dict[str, str]:
    settings = get_settings()
    secret = (
        getattr(settings, "crm_bot_secret", "")
        or getattr(settings, "external_booking_api_secret", "")
        or getattr(settings, "bot_api_secret", "")
        or ""
    )

    # Railway/копирование с телефона иногда добавляет пробел или перевод строки.
    # httpx тогда падает с LocalProtocolError: Illegal header value.
    secret = str(secret or "").strip()

    return {"x-bot-secret": secret}
def _url(path: str) -> str:
    settings = get_settings()
    base = getattr(settings, "crm_base_url", "https://neuro-balance-crm.vercel.app")
    return f"{str(base).rstrip('/')}{path}"


def _response_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _log_crm_response(response: httpx.Response, label: str) -> dict[str, Any] | None:
    data = _response_json(response)
    logger.info(
        "CRM %s response status=%s text=%s json=%s",
        label,
        response.status_code,
        response.text[:2000],
        data,
    )
    return data


def _raise_for_crm(response: httpx.Response, label: str) -> None:
    data = _log_crm_response(response, label)
    if response.status_code >= 400:
        raise CRMResponseError(label, response, data)


def clear_slots_cache(date: str | None = None) -> None:
    if not date:
        _SLOTS_CACHE.clear()
        return
    for key in list(_SLOTS_CACHE.keys()):
        if key[0] == date:
            _SLOTS_CACHE.pop(key, None)


_SLOTS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_SLOTS_CACHE_TTL = 25.0

_DOCTORS_CACHE: tuple[float, dict[str, Any]] | None = None
_SERVICES_CACHE: tuple[float, dict[str, Any]] | None = None
_META_CACHE_TTL = 300.0


def _normalize_phone(phone: str | None) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


async def patient_lookup(phone: str) -> dict[str, Any]:
    response = await _client().get(
        _url("/api/bot/patient-lookup"),
        params={"phone": _normalize_phone(phone)},
        headers=_headers(),
    )
    _raise_for_crm(response, "patient-lookup")
    data = response.json()

    if data.get("lastAppointment") and not data.get("appointment"):
        data["appointment"] = data.get("lastAppointment")

    data.setdefault("hasActiveAppointment", bool(data.get("lastAppointment") or data.get("appointment")))
    data.setdefault("ok", True)
    return data


async def get_doctors(force: bool = False) -> dict[str, Any]:
    global _DOCTORS_CACHE

    now = time.monotonic()
    if not force and _DOCTORS_CACHE and now - _DOCTORS_CACHE[0] < _META_CACHE_TTL:
        return _DOCTORS_CACHE[1]

    response = await _client().get(
        _url("/api/bot/doctors"),
        headers=_headers(),
    )
    _raise_for_crm(response, "doctors")

    data = response.json()
    data.setdefault("ok", True)
    _DOCTORS_CACHE = (now, data)
    return data


async def get_services(force: bool = False) -> dict[str, Any]:
    global _SERVICES_CACHE

    now = time.monotonic()
    if not force and _SERVICES_CACHE and now - _SERVICES_CACHE[0] < _META_CACHE_TTL:
        return _SERVICES_CACHE[1]

    response = await _client().get(
        _url("/api/bot/services"),
        headers=_headers(),
    )
    _raise_for_crm(response, "services")

    data = response.json()
    data.setdefault("treatable", [])
    data.setdefault("notTreatable", [])
    data.setdefault("doctorCount", 0)
    data.setdefault("ok", True)

    _SERVICES_CACHE = (now, data)
    return data


async def check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    if not date:
        raise CRMError("CRM check-slots error: date is required")

    params: dict[str, str] = {"date": date}
    if doctor_login:
        params["doctor"] = doctor_login

    cache_key = (date, doctor_login or "")
    cached = _SLOTS_CACHE.get(cache_key)
    now = time.monotonic()

    if cached and now - cached[0] < _SLOTS_CACHE_TTL:
        return cached[1]

    response = await _client().get(
        _url("/api/bot/check-slots"),
        params=params,
        headers=_headers(),
    )
    _raise_for_crm(response, "check-slots")

    data = response.json()
    availability = data.get("availability") or []
    slots: list[dict[str, Any]] = []

    for item in availability:
        if not isinstance(item, dict):
            continue

        doctor_login_item = item.get("doctorLogin") or item.get("doctor_login")
        doctor_name = item.get("doctorName") or item.get("doctor_name") or "Врач клиники"
        item_date = item.get("date") or date
        available_slots = item.get("availableSlots") or item.get("slots") or []

        if not isinstance(available_slots, list):
            continue

        for time_start in available_slots:
            if isinstance(time_start, dict):
                time_start = time_start.get("timeStart") or time_start.get("time")
            if not time_start:
                continue

            slots.append(
                {
                    "doctorLogin": doctor_login_item,
                    "doctor_login": doctor_login_item,
                    "doctorName": doctor_name,
                    "doctor_name": doctor_name,
                    "date": item_date,
                    "timeStart": str(time_start),
                    "time_start": str(time_start),
                    "time": str(time_start),
                }
            )

    normalized = {
        **data,
        "availability": availability,
        "slots": slots,
        "ok": True,
    }

    _SLOTS_CACHE[cache_key] = (now, normalized)
    return normalized


async def book_appointment(
    *,
    patient_name: str,
    phone: str,
    doctor_login: str,
    date: str,
    time_start: str,
    doctor_name: str | None = None,
    notes: str | None = None,
    conversation_id: str | int | None = None,
    lead_id: str | int | None = None,
) -> dict[str, Any]:
    if not patient_name:
        patient_name = "Пациент"
    if not phone:
        raise CRMError("CRM book error: phone is required")
    if not doctor_login:
        raise CRMError("CRM book error: doctorLogin is required")
    if not date:
        raise CRMError("CRM book error: date is required")
    if not time_start:
        raise CRMError("CRM book error: timeStart is required")

    payload: dict[str, Any] = {
        "patientName": patient_name,
        "phone": _normalize_phone(phone),
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

    response = await _client().post(
        _url("/api/bot/book"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    _raise_for_crm(response, "book")

    clear_slots_cache(date)

    data = response.json()
    data.setdefault("ok", True)
    data.setdefault("status", "Записан")
    data.setdefault("date", date)
    data.setdefault("timeStart", time_start)
    if doctor_name:
        data.setdefault("doctorName", doctor_name)

    return data


async def cancel_appointment(
    *,
    phone: str,
    appointment_id: int | str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    if not phone:
        raise CRMError("CRM appointment/cancel error: phone is required")

    payload: dict[str, Any] = {
        "phone": _normalize_phone(phone),
        "reason": reason or "отмена через бота",
    }

    if appointment_id:
        payload["appointmentId"] = appointment_id

    response = await _client().post(
        _url("/api/bot/appointment/cancel"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    _raise_for_crm(response, "appointment/cancel")

    data = response.json()
    data.setdefault("ok", bool(data.get("success", True)))
    data.setdefault("cancelled", bool(data.get("success") or data.get("alreadyCancelled")))
    return data


async def reschedule_appointment(
    *,
    phone: str,
    new_date: str,
    new_time_start: str,
    appointment_id: int | str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    if not phone:
        raise CRMError("CRM appointment/reschedule error: phone is required")
    if not new_date:
        raise CRMError("CRM appointment/reschedule error: newDate is required")
    if not new_time_start:
        raise CRMError("CRM appointment/reschedule error: newTimeStart is required")

    payload: dict[str, Any] = {
        "phone": _normalize_phone(phone),
        "newDate": new_date,
        "newTimeStart": new_time_start,
        "reason": reason or "перенос через бота",
    }

    if appointment_id:
        payload["appointmentId"] = appointment_id

    response = await _client().post(
        _url("/api/bot/appointment/reschedule"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    _raise_for_crm(response, "appointment/reschedule")

    data = response.json()
    data.setdefault("ok", True)
    data.setdefault("rescheduled", True)

    for key in list(_SLOTS_CACHE.keys()):
        if key[0] == new_date:
            _SLOTS_CACHE.pop(key, None)

    return data


async def escalate_to_operator(
    *,
    phone: str | None = None,
    conversation_id: int | str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": reason or "нужен живой оператор",
    }

    if phone:
        payload["phone"] = _normalize_phone(phone)
    if conversation_id:
        payload["conversationId"] = conversation_id

    if not payload.get("phone") and not payload.get("conversationId"):
        raise CRMError("CRM escalate error: phone or conversationId is required")

    response = await _client().post(
        _url("/api/bot/escalate"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    _raise_for_crm(response, "escalate")

    data = response.json()
    data.setdefault("ok", True)
    return data


async def log_outcome(
    *,
    outcome: str,
    phone: str | None = None,
    conversation_id: int | str | None = None,
    appointment_id: int | str | None = None,
    note: str = "",
) -> dict[str, Any]:
    allowed = {
        "booked",
        "rejected",
        "escalated",
        "abandoned",
        "no_show",
        "attended",
        "out_of_scope",
        "contraindicated",
    }

    if outcome not in allowed:
        raise CRMError(f"CRM outcome error: invalid outcome {outcome!r}")

    payload: dict[str, Any] = {
        "outcome": outcome,
        "note": note,
    }

    if appointment_id:
        payload["appointmentId"] = appointment_id
    if phone:
        payload["phone"] = _normalize_phone(phone)
    if conversation_id:
        payload["conversationId"] = conversation_id

    if not payload.get("phone") and not payload.get("conversationId"):
        raise CRMError("CRM outcome error: phone or conversationId is required")

    response = await _client().post(
        _url("/api/bot/outcome"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    _raise_for_crm(response, "outcome")

    data = response.json()
    data.setdefault("ok", True)
    return data

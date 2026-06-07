from __future__ import annotations

from typing import Any
import time
import httpx
from functools import lru_cache
from config import get_settings

class CRMError(Exception):
    pass

@lru_cache(maxsize=1)
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0))

def _headers() -> dict[str, str]:
    settings = get_settings()
    return {"x-bot-secret": settings.crm_bot_secret}

def _url(path: str) -> str:
    settings = get_settings()
    return f"{settings.crm_base_url.rstrip('/')}{path}"

def _raise_for_crm(response: httpx.Response, label: str) -> None:
    if response.status_code == 401:
        raise CRMError("CRM вернула 401: неверный CRM_BOT_SECRET")
    if response.status_code == 403:
        raise CRMError(f"CRM {label} error 403: запись не принадлежит этому телефону")
    if response.status_code == 404:
        raise CRMError(f"CRM {label} error 404: запись не найдена")
    if response.status_code == 409:
        raise CRMError(f"CRM {label} error 409: слот уже занят")
    if response.status_code == 410:
        raise CRMError(f"CRM {label} error 410: запись уже отменена")
    if response.status_code == 429:
        try:
            retry = response.json().get("retryAfterSec")
        except Exception:
            retry = None
        raise CRMError(f"CRM {label} error 429: превышен лимит" + (f", повтор через {retry} сек" if retry else ""))
    if response.status_code >= 400:
        raise CRMError(f"CRM {label} error {response.status_code}: {response.text}")

_SLOTS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_SLOTS_CACHE_TTL = 25.0
_DOCTORS_CACHE: tuple[float, dict[str, Any]] | None = None
_SERVICES_CACHE: tuple[float, dict[str, Any]] | None = None
_META_CACHE_TTL = 300.0

async def patient_lookup(phone: str) -> dict[str, Any]:
    response = await _client().get(_url("/api/bot/patient-lookup"), params={"phone": phone}, headers=_headers())
    _raise_for_crm(response, "patient-lookup")
    return response.json()

async def get_doctors(force: bool = False) -> dict[str, Any]:
    global _DOCTORS_CACHE
    now = time.monotonic()
    if not force and _DOCTORS_CACHE and now - _DOCTORS_CACHE[0] < _META_CACHE_TTL:
        return _DOCTORS_CACHE[1]
    response = await _client().get(_url("/api/bot/doctors"), headers=_headers())
    _raise_for_crm(response, "doctors")
    data = response.json()
    _DOCTORS_CACHE = (now, data)
    return data

async def get_services(force: bool = False) -> dict[str, Any]:
    global _SERVICES_CACHE
    now = time.monotonic()
    if not force and _SERVICES_CACHE and now - _SERVICES_CACHE[0] < _META_CACHE_TTL:
        return _SERVICES_CACHE[1]
    response = await _client().get(_url("/api/bot/services"), headers=_headers())
    _raise_for_crm(response, "services")
    data = response.json()
    _SERVICES_CACHE = (now, data)
    return data

async def check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {"date": date}
    if doctor_login:
        params["doctor"] = doctor_login
    cache_key = (date, doctor_login or "")
    cached = _SLOTS_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _SLOTS_CACHE_TTL:
        return cached[1]
    response = await _client().get(_url("/api/bot/check-slots"), params=params, headers=_headers())
    _raise_for_crm(response, "check-slots")
    data = response.json()
    _SLOTS_CACHE[cache_key] = (now, data)
    return data

async def book_appointment(*, patient_name: str, phone: str, doctor_login: str, date: str, time_start: str, doctor_name: str | None = None, notes: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"patientName": patient_name, "phone": phone, "doctorLogin": doctor_login, "date": date, "timeStart": time_start}
    if doctor_name:
        payload["doctorName"] = doctor_name
    if notes:
        payload["notes"] = notes
    response = await _client().post(_url("/api/bot/book"), json=payload, headers={**_headers(), "Content-Type": "application/json"})
    _raise_for_crm(response, "book")
    for key in list(_SLOTS_CACHE.keys()):
        if key[0] == date:
            _SLOTS_CACHE.pop(key, None)
    return response.json()

async def cancel_appointment(*, phone: str, appointment_id: int | None = None, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"phone": phone, "reason": reason or "отмена через бота"}
    if appointment_id:
        payload["appointmentId"] = appointment_id
    response = await _client().post(_url("/api/bot/appointment/cancel"), json=payload, headers={**_headers(), "Content-Type": "application/json"})
    _raise_for_crm(response, "appointment/cancel")
    return response.json()

async def reschedule_appointment(*, phone: str, new_date: str, new_time_start: str, appointment_id: int | None = None, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"phone": phone, "newDate": new_date, "newTimeStart": new_time_start, "reason": reason or "перенос через бота"}
    if appointment_id:
        payload["appointmentId"] = appointment_id
    response = await _client().post(_url("/api/bot/appointment/reschedule"), json=payload, headers={**_headers(), "Content-Type": "application/json"})
    _raise_for_crm(response, "appointment/reschedule")
    return response.json()

async def escalate_to_operator(*, phone: str | None = None, conversation_id: int | str | None = None, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"reason": reason or "нужен живой оператор"}
    if phone:
        payload["phone"] = phone
    if conversation_id:
        payload["conversationId"] = conversation_id
    response = await _client().post(_url("/api/bot/escalate"), json=payload, headers={**_headers(), "Content-Type": "application/json"})
    _raise_for_crm(response, "escalate")
    return response.json()

async def log_outcome(*, phone: str | None = None, conversation_id: int | str | None = None, outcome: str, appointment_id: int | None = None, note: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"outcome": outcome, "appointmentId": appointment_id, "note": note}
    if phone:
        payload["phone"] = phone
    if conversation_id:
        payload["conversationId"] = conversation_id
    response = await _client().post(_url("/api/bot/outcome"), json=payload, headers={**_headers(), "Content-Type": "application/json"})
    _raise_for_crm(response, "outcome")
    return response.json()

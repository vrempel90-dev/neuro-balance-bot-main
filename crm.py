from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any
from datetime import date as date_cls, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import time
import re
import weakref
from zoneinfo import ZoneInfo

import httpx

from config import get_settings
from phone import sanitize_kz_phone


class CRMError(Exception):
    pass


class CRMResponseError(CRMError):
    def __init__(self, label: str, response: httpx.Response, data: dict[str, Any] | None = None):
        self.label = label
        self.status_code = response.status_code
        self.response_text = response.text
        self.data = data or {}
        code = str(self.data.get("code") or "")
        self.code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code) else ""
        # Exceptions are logged by callers. Response bodies can contain patient
        # names, phones, complaints and proxy credentials, so keep them out of
        # the exception string even when the CRM itself returns them as errors.
        super().__init__(f"CRM {label} error {self.status_code}")


logger = logging.getLogger(__name__)

# Name of the field book_appointment adds listing the keys the CRM actually
# returned, so a caller can tell a real confirmation from a defaulted one.
CRM_RESPONSE_KEYS_FIELD = "_crm_response_keys"


@lru_cache(maxsize=1)
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0))


def _redacted_headers() -> dict[str, str]:
    headers = _headers()
    return {k: ("***" if "secret" in k.lower() or "token" in k.lower() else v) for k, v in headers.items()}


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
        "CRM %s response status=%s json_object=%s",
        label,
        response.status_code,
        isinstance(data, dict),
    )
    return data


def _raise_for_crm(response: httpx.Response, label: str) -> None:
    data = _log_crm_response(response, label)
    if response.status_code >= 400:
        raise CRMResponseError(label, response, data)


def _read_object(response: httpx.Response, label: str) -> dict[str, Any]:
    """A successful HTTP read must still satisfy the CRM JSON contract."""
    data = _response_json(response)
    if data is None:
        raise CRMError(f"CRM {label} returned a non-object JSON response")
    if data.get("ok") is False or data.get("success") is False or data.get("error"):
        raise CRMError(f"CRM {label} returned an unsuccessful result")
    return data


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


def normalize_phone(phone: str | None) -> str:
    return sanitize_kz_phone(phone)


def phone_lookup_variants(phone: str | None) -> list[str]:
    normalized = normalize_phone(phone)
    if not normalized:
        return []
    variants: list[str] = []
    if normalized:
        variants.extend([normalized, f"+{normalized}"])
        if normalized.startswith("7") and len(normalized) == 11:
            variants.extend(["8" + normalized[1:], normalized[1:]])
    raw_digits = re.sub(r"\D+", "", phone or "")
    if raw_digits:
        variants.append(raw_digits)
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def _normalize_phone(phone: str | None) -> str:
    return normalize_phone(phone)


def _patient_lookup_url() -> str:
    return _url("/api/bot/patient-lookup")


def _normalize_patient_lookup_response(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CRMError("CRM patient-lookup returned a non-object response")
    for field in ("found", "isNew", "hasActiveAppointment"):
        if field in data and not isinstance(data[field], bool):
            raise CRMError("CRM patient-lookup returned an invalid classification flag")
    for key in ("patient", "lead", "appointment", "lastAppointment", "activeAppointment"):
        if data.get(key) is not None and (not isinstance(data[key], dict) or not data[key]):
            raise CRMError("CRM patient-lookup returned invalid identity data")
    for key in ("appointments", "records", "items"):
        if key in data and (not isinstance(data[key], list) or any(not isinstance(item, dict) for item in data[key])):
            raise CRMError("CRM patient-lookup returned invalid appointment history")
    data = dict(data)
    data.setdefault("hasActiveAppointment", False)
    data.setdefault("ok", True)
    return data


_LOOKUP_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_LOOKUP_RETRY_DELAY_SECONDS = 0.25
_LOOKUP_MAX_RETRY_AFTER_SECONDS = 2.0
# Each event loop shares a two-request limit across all clients and phone
# variants. Both the key and value are weak: a contended Semaphore references
# its loop, so storing a strong value would otherwise keep finished loops alive.
_LOOKUP_LIMITERS: weakref.WeakKeyDictionary[Any, weakref.ReferenceType[asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def _lookup_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    reference = _LOOKUP_LIMITERS.get(loop)
    limiter = reference() if reference is not None else None
    if limiter is None:
        limiter = asyncio.Semaphore(2)
        _LOOKUP_LIMITERS[loop] = weakref.ref(limiter)
    return limiter


def _lookup_retry_delay(response: httpx.Response) -> float | None:
    """Respect Retry-After, or fail closed when its wait exceeds our budget."""
    header = response.headers.get("Retry-After")
    if header is None:
        return _LOOKUP_RETRY_DELAY_SECONDS
    try:
        delay = float(header)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = (parsed - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay) or delay > _LOOKUP_MAX_RETRY_AFTER_SECONDS:
        return None
    return max(_LOOKUP_RETRY_DELAY_SECONDS, delay)


async def patient_lookup_raw(phone_param: str) -> dict[str, Any]:
    endpoint = _patient_lookup_url()
    params = {"phone": phone_param}
    # Only this read-only endpoint is retried. Booking POSTs retain their
    # existing uncertain-outcome handling and are never replayed here.
    async with _lookup_limiter():
        for attempt in range(2):
            logger.info("CRM patient-lookup request attempt=%s", attempt + 1)
            try:
                response = await _client().get(endpoint, params=params, headers=_headers())
            except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                logger.warning("CRM patient-lookup network error_type=%s retry=%s", type(exc).__name__, attempt == 0)
                if attempt == 1:
                    raise
                await asyncio.sleep(_LOOKUP_RETRY_DELAY_SECONDS)
                continue
            if attempt == 0 and response.status_code in _LOOKUP_RETRY_STATUSES:
                delay = _lookup_retry_delay(response)
                logger.warning("CRM patient-lookup transient status=%s retry=%s", response.status_code, delay is not None)
                if delay is not None:
                    # Keep this permit during backoff to avoid replacing a
                    # throttled variant with another immediate request burst.
                    await asyncio.sleep(delay)
                    continue
            _raise_for_crm(response, "patient-lookup")
            data = _read_object(response, "patient-lookup")
            return _normalize_patient_lookup_response(data)
    raise CRMError("CRM patient-lookup attempts exhausted")  # defensive only


async def patient_lookup(phone: str) -> dict[str, Any]:
    return await patient_lookup_raw(_normalize_phone(phone))


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

    data = _read_object(response, "doctors")
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

    data = _read_object(response, "services")
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

    data = _read_object(response, "check-slots")
    availability = data.get("availability") or []
    if not isinstance(availability, list):
        raise CRMError("CRM check-slots returned invalid availability")
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


def _iso_date(value: str | date_cls) -> str:
    if isinstance(value, date_cls):
        return value.isoformat()
    return str(value)[:10]

def _time_hour(slot: dict[str, Any]) -> int:
    t = str(slot.get("time") or slot.get("timeStart") or slot.get("time_start") or "0:00")
    try:
        return int(t.split(":", 1)[0])
    except Exception:
        return 0

def _filter_time_preference(slots: list[dict[str, Any]], time_preference: str | None) -> list[dict[str, Any]]:
    low = str(time_preference or "").lower()
    if not low:
        return slots
    if any(x in low for x in ("до обеда", "утром", "с утра", "morning")):
        return [s for s in slots if _time_hour(s) < 12]
    if any(x in low for x in ("после обеда", "днем", "днём", "afternoon")):
        return [s for s in slots if 12 <= _time_hour(s) < 18]
    if any(x in low for x in ("вечер", "evening")):
        return [s for s in slots if _time_hour(s) >= 17]
    return slots

async def check_slots_for_date(date: str | date_cls, doctor_login: str | None = None) -> dict[str, Any]:
    return await check_slots(_iso_date(date), doctor_login=doctor_login)

async def check_slots_range(date_from: str | date_cls, date_to: str | date_cls, doctor_login: str | None = None) -> dict[str, Any]:
    start = datetime.fromisoformat(_iso_date(date_from)).date()
    end = datetime.fromisoformat(_iso_date(date_to)).date()
    grouped: dict[str, list[dict[str, Any]]] = {}
    slots: list[dict[str, Any]] = []
    current = start
    while current <= end:
        data = await check_slots_for_date(current, doctor_login=doctor_login)
        day_slots = list(data.get("slots") or [])
        if day_slots:
            grouped[current.isoformat()] = day_slots
            slots.extend(day_slots)
        current += timedelta(days=1)
    return {"ok": True, "date_from": start.isoformat(), "date_to": end.isoformat(), "grouped": grouped, "slots": slots}

async def find_nearest_available_slots(start_date: str | date_cls, days_ahead: int = 14, doctor_login: str | None = None, time_preference: str | None = None) -> dict[str, Any]:
    start = datetime.fromisoformat(_iso_date(start_date)).date()
    end = start + timedelta(days=max(0, int(days_ahead or 14)) - 1)
    data = await check_slots_range(start, end, doctor_login=doctor_login)
    grouped: dict[str, list[dict[str, Any]]] = {}
    all_slots: list[dict[str, Any]] = []
    for day, day_slots in (data.get("grouped") or {}).items():
        filtered = _filter_time_preference(list(day_slots or []), time_preference)
        if filtered:
            grouped[day] = filtered
            all_slots.extend(filtered)
    return {**data, "grouped": grouped, "slots": all_slots, "all_slots_unfiltered": data.get("slots") or [], "ok": True}


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

    try:
        data = response.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        # A 2xx whose body is not a JSON object (a list, a scalar, null, or not
        # JSON at all). There is nothing here to read a confirmation out of, and
        # the convenience defaults below would turn it into a fabricated
        # "Записан". Note what is deliberately absent from the returned dict:
        # no ``error`` key and no failure ``status``, because this answer does
        # NOT prove the CRM created nothing — it may have written the
        # appointment and mangled the response. Callers must treat it as
        # unconfirmed and keep the slot claimed, not as a rejection.
        logger.warning("CRM book returned a non-object body (%s)", type(data).__name__)
        return {
            "ok": False,
            "malformed_response": True,
            "message": "CRM вернула ответ, который невозможно разобрать.",
            CRM_RESPONSE_KEYS_FIELD: [],
        }

    # Record which keys the CRM itself returned, BEFORE the convenience
    # defaults below are applied. Without this a bare `200 {}` becomes
    # {"ok": True, "status": "Записан"} and is indistinguishable from a real
    # confirmation — a caller would tell the patient they are booked for an
    # appointment the CRM may never have created. The defaults are kept for
    # backwards compatibility with existing callers; this field lets a caller
    # that cares ask what was actually confirmed.
    data[CRM_RESPONSE_KEYS_FIELD] = sorted(str(k) for k in data)

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

    data = _read_object(response, "appointment/cancel")
    data.setdefault("ok", any(data.get(key) is True for key in ("success", "cancelled", "alreadyCancelled")))
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

    data = _read_object(response, "appointment/reschedule")
    data.setdefault("ok", any(data.get(key) is True for key in ("success", "rescheduled")))
    data.setdefault("rescheduled", data.get("success") is True)

    for key in list(_SLOTS_CACHE.keys()):
        if key[0] == new_date:
            _SLOTS_CACHE.pop(key, None)

    return data



def _appointment_datetime(appt: dict[str, Any]) -> datetime | None:
    date_value = appt.get("date") or appt.get("appointmentDate") or appt.get("appointment_date")
    time_value = appt.get("timeStart") or appt.get("time_start") or appt.get("time") or "00:00"
    if not date_value:
        return None
    try:
        return datetime.fromisoformat(f"{str(date_value)[:10]}T{str(time_value)[:5]}")
    except Exception:
        return None


def _appointment_id(appt: dict[str, Any]) -> Any:
    return appt.get("id") or appt.get("appointmentId") or appt.get("record_id") or appt.get("recordId") or appt.get("bookingId")


def _extract_appointments(data: dict[str, Any]) -> list[dict[str, Any]]:
    # Production lookup should consume CRM list contracts. Legacy tests/fallbacks
    # often expose only lastAppointment for the old on-demand lookup flow; do not
    # let that singleton make every new lead look booked before first-touch.
    candidates = data.get("appointments") or data.get("records") or data.get("items") or []
    if not isinstance(candidates, list):
        candidates = []
    appointments = [a for a in candidates if isinstance(a, dict)]
    for key in ("appointment", "activeAppointment", "lastAppointment"):
        candidate = data.get(key)
        if isinstance(candidate, dict) and candidate and candidate not in appointments:
            appointments.append(candidate)
    return appointments


def _is_active_future_appointment(appt: dict[str, Any]) -> bool:
    status = str(appt.get("status") or appt.get("appointmentStatus") or "").lower()
    if status in {"cancelled", "canceled", "completed", "deleted", "no_show", "no-show", "done", "отменена", "отменён", "отменен", "отменено", "не_пришел", "не_пришёл", "завершен", "завершён"}:
        return False
    if not _appointment_id(appt):
        return False
    dt = _appointment_datetime(appt)
    if dt is None:
        return bool(appt)
    clinic_now = datetime.now(ZoneInfo("Asia/Almaty")).replace(tzinfo=None)
    return dt >= clinic_now


class CRMClient:
    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        """Look up EVERY supported number representation before admitting a lead.

        A 200/unknown response for +7 must not shadow an existing patient whose
        CRM phone is saved as 8... or 10 digits. Never treat partial responses
        as permission for an AI conversation.
        """
        variants = phone_lookup_variants(phone)
        if not variants:
            raise CRMError("CRM lookup failed: invalid_phone")
        endpoint = _patient_lookup_url()

        async def _one(variant: str) -> tuple[str, dict[str, Any] | None, str]:
            try:
                # Some integrations intentionally override patient_lookup.
                if getattr(patient_lookup, "__module__", "crm") != __name__:
                    data = await patient_lookup(variant)
                else:
                    data = await patient_lookup_raw(variant)
                if not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
                    return variant, None, "crm_invalid_response"
                if not isinstance(data.get("found"), bool) or not isinstance(data.get("isNew"), bool):
                    return variant, None, "crm_missing_identity_fields"
                return variant, data, ""
            except Exception as exc:
                logger.warning("CRM phone variant lookup failed error=%s", type(exc).__name__)
                return variant, None, type(exc).__name__

        results = await asyncio.gather(*(_one(variant) for variant in variants))
        successes = [(variant, data) for variant, data, error in results if data is not None]
        failures = [(variant, error) for variant, _, error in results if error]
        if not successes:
            raise CRMError("CRM lookup failed for all phone formats")
        if failures:
            # Even one unresolved format could hide an already booked patient.
            raise CRMError("CRM lookup incomplete for phone formats")

        def _priority(payload: dict[str, Any]) -> int:
            appointments = _extract_appointments(payload)
            active = any(_is_active_future_appointment(x) for x in appointments)
            if active or payload.get("hasActiveAppointment") is True:
                return 6
            if payload.get("patient") or payload.get("lastAppointment") or appointments:
                return 5
            if payload.get("lead"):
                return 4
            if payload.get("found") is True or payload.get("isNew") is False:
                return 3
            return 1

        variant, data = max(successes, key=lambda item: _priority(item[1]))
        all_appointments = [
            a for _, item in successes for a in _extract_appointments(item)
        ]
        active = [a for a in all_appointments if _is_active_future_appointment(a)]
        # De-duplicate cross-format CRM matches by appointment id.
        deduped: dict[str, dict[str, Any]] = {}
        for appt in active:
            key = str(_appointment_id(appt) or "")
            if key:
                deduped[key] = appt
        active = list(deduped.values())
        active.sort(key=lambda a: _appointment_datetime(a) or datetime.max)
        # If any format found an active appointment, admission must see it,
        # even if the selected response contained only the patient record.
        merged = dict(data)
        if active:
            merged["hasActiveAppointment"] = True
        return {
            "ok": True,
            "status_code": 200,
            "phone_raw": phone,
            "phone_normalized": normalize_phone(phone),
            "phone_variants": variants,
            "endpoint_url": endpoint,
            "query_params": {"phone": variant},
            "appointments_raw_count": len(all_appointments),
            "appointments": active,
            "appointment": active[0] if active else None,
            "appointments_cache": active[1:],
            "active_count": len(active),
            "raw": merged,
        }

    async def lookup_appointment_by_id(self, appointment_id: str | int) -> dict[str, Any]:
        response = await _client().get(_url("/api/bot/appointment"), params={"appointmentId": appointment_id}, headers=_headers())
        _raise_for_crm(response, "appointment")
        data = _read_object(response, "appointment"); data.setdefault("ok", True); return data

    async def check_slots_for_date(self, date: str | date_cls, doctor_login: str | None = None) -> dict[str, Any]:
        return await check_slots_for_date(date, doctor_login)

    async def check_slots_range(self, date_from: str | date_cls, date_to: str | date_cls, doctor_login: str | None = None) -> dict[str, Any]:
        return await check_slots_range(date_from, date_to, doctor_login)

    async def find_nearest_available_slots(self, start_date: str | date_cls, days_ahead: int = 14, doctor_login: str | None = None, time_preference: str | None = None) -> dict[str, Any]:
        return await find_nearest_available_slots(start_date, days_ahead, doctor_login, time_preference)

    async def book_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await book_appointment(**payload)

    async def cancel_appointment(self, appointment_id: str | int, reason: str | None = None) -> dict[str, Any]:
        return await cancel_appointment(phone="0", appointment_id=appointment_id, reason=reason or "отмена через бота")

    async def reschedule_appointment(self, appointment_id: str | int, new_slot: dict[str, Any]) -> dict[str, Any]:
        return await reschedule_appointment(phone=str(new_slot.get("phone") or "0"), appointment_id=appointment_id, new_date=str(new_slot.get("date") or new_slot.get("newDate") or ""), new_time_start=str(new_slot.get("timeStart") or new_slot.get("time") or new_slot.get("newTimeStart") or ""))


async def lookup_active_appointments_by_phone(phone: str) -> dict[str, Any]:
    return await CRMClient().lookup_active_appointments_by_phone(phone)


def _booking_matches(
    appointment: dict[str, Any], *, appointment_id: str, phone: str,
    doctor_login: str, date: str, time_start: str, doctor_name: str | None,
    phone_scoped: bool,
) -> bool:
    """Compare persisted fields, never convenience defaults from the POST."""
    if str(_appointment_id(appointment) or "") != appointment_id:
        return False
    status = str(appointment.get("status") or appointment.get("appointmentStatus") or "").strip().lower()
    if status in {"cancelled", "canceled", "deleted", "rejected", "completed", "done", "no_show", "no-show", "отменена", "отменен", "отменён", "отменено", "не_пришел", "не_пришёл", "не пришел", "не пришёл", "завершен", "завершён"} or any(marker in status for marker in ("отмен", "cancel", "reject", "удален", "удалён")):
        return False
    stored_phone = appointment.get("phone") or appointment.get("patientPhone")
    if stored_phone and normalize_phone(str(stored_phone)) != normalize_phone(phone):
        return False
    if not phone_scoped and not stored_phone:
        return False
    stored_date = appointment.get("date") or appointment.get("appointmentDate")
    stored_time = appointment.get("timeStart") or appointment.get("time_start") or appointment.get("time")
    if str(stored_date or "")[:10] != date[:10] or str(stored_time or "")[:5] != time_start[:5]:
        return False
    stored_doctor = appointment.get("doctorLogin") or appointment.get("doctor_login")
    if stored_doctor:
        return str(stored_doctor) == str(doctor_login)
    stored_name = appointment.get("doctorName") or appointment.get("doctor_name")
    return bool(doctor_name and stored_name and str(stored_name).strip() == str(doctor_name).strip())


async def verify_saved_booking(
    response: dict[str, Any], *, phone: str, doctor_login: str,
    date: str, time_start: str, doctor_name: str | None = None,
) -> dict[str, Any]:
    """Read back a persisted booking before the caller tells a patient success.

    No POST is retried here. A failed/mismatching read is an uncertain outcome,
    so the caller must retain its durable booking claim for reconciliation.
    """
    appointment_id = str(_appointment_id(response) or "")
    if not appointment_id and isinstance(response.get("appointment"), dict):
        appointment_id = str(_appointment_id(response["appointment"]) or "")
    if not appointment_id:
        return {"verified": False, "reason": "appointment_id_missing"}
    expected = {
        "appointment_id": appointment_id, "phone": phone,
        "doctor_login": doctor_login, "date": date, "time_start": time_start,
        "doctor_name": doctor_name,
    }
    try:
        data = await CRMClient().lookup_appointment_by_id(appointment_id)
        candidates = _extract_appointments(data)
        if _appointment_id(data):
            candidates.append(data)
        for appointment in candidates:
            if _booking_matches(appointment, **expected, phone_scoped=False):
                return {"verified": True, "reason": "appointment_readback", "appointment": appointment}
    except Exception as exc:
        logger.warning("CRM booking ID verification unavailable error=%s", type(exc).__name__)
    try:
        lookup = await lookup_active_appointments_by_phone(phone)
        if not isinstance(lookup, dict) or lookup.get("ok") is False:
            return {"verified": False, "reason": "lookup_unavailable"}
        raw = lookup.get("raw") if isinstance(lookup.get("raw"), dict) else lookup
        if raw.get("ok") is False or raw.get("error"):
            return {"verified": False, "reason": "lookup_unavailable"}
        for appointment in _extract_appointments(raw) + _extract_appointments(lookup):
            if _booking_matches(appointment, **expected, phone_scoped=True):
                return {"verified": True, "reason": "phone_lookup_readback", "appointment": appointment}
    except Exception as exc:
        logger.warning("CRM booking phone verification unavailable error=%s", type(exc).__name__)
    return {"verified": False, "reason": "appointment_not_verified"}

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

    data = _read_object(response, "escalate")
    accepted = any(data.get(key) is True for key in ("ok", "success", "escalated", "accepted"))
    accepted = accepted or bool(data.get("escalationId") or data.get("ticketId"))
    data["ok"] = bool(accepted)
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

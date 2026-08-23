from __future__ import annotations

from functools import lru_cache
from typing import Any
from datetime import date as date_cls, datetime, timedelta
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


def normalize_phone(phone: str | None) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 10 and digits.startswith("7"):
        digits = "7" + digits
    elif digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


def phone_lookup_variants(phone: str | None) -> list[str]:
    normalized = normalize_phone(phone)
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
    data.setdefault("hasActiveAppointment", bool(data.get("appointment")))
    data.setdefault("ok", True)
    return data


async def patient_lookup_raw(phone_param: str) -> dict[str, Any]:
    endpoint = _patient_lookup_url()
    params = {"phone": phone_param}
    logger.info("CRM patient-lookup request endpoint=%s params=%s headers=%s", endpoint, params, _redacted_headers())
    response = await _client().get(endpoint, params=params, headers=_headers())
    logger.info("CRM patient-lookup status_code=%s endpoint=%s params=%s", response.status_code, endpoint, params)
    _raise_for_crm(response, "patient-lookup")
    data = response.json()
    return _normalize_patient_lookup_response(data)


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

    data = response.json()
    # Record which keys the CRM itself returned, BEFORE the convenience
    # defaults below are applied. Without this a bare `200 {}` becomes
    # {"ok": True, "status": "Записан"} and is indistinguishable from a real
    # confirmation — a caller would tell the patient they are booked for an
    # appointment the CRM may never have created. The defaults are kept for
    # backwards compatibility with existing callers; this field lets a caller
    # that cares ask what was actually confirmed.
    data[CRM_RESPONSE_KEYS_FIELD] = sorted(str(k) for k in data) if isinstance(data, dict) else []

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
    return [a for a in candidates if isinstance(a, dict)]


def _is_active_future_appointment(appt: dict[str, Any]) -> bool:
    status = str(appt.get("status") or appt.get("appointmentStatus") or "").lower()
    if status in {"cancelled", "canceled", "completed", "deleted", "no_show", "no-show", "done"}:
        return False
    if not _appointment_id(appt):
        return False
    dt = _appointment_datetime(appt)
    if dt is None:
        return bool(appt)
    return dt >= datetime.now().replace(tzinfo=None)


class CRMClient:
    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        variants = phone_lookup_variants(phone)
        errors: list[str] = []
        last_status: int | None = None
        endpoint = _patient_lookup_url()
        last_params: dict[str, Any] = {}
        for variant in variants or [phone]:
            last_params = {"phone": variant}
            try:
                if getattr(patient_lookup, "__module__", "crm") != __name__:
                    data = await patient_lookup(variant)
                else:
                    data = await patient_lookup_raw(variant)
                last_status = 200
            except CRMResponseError as exc:
                last_status = exc.status_code
                errors.append(str(exc))
                logger.exception("CRM active appointment lookup response error endpoint=%s params=%s status_code=%s", endpoint, last_params, exc.status_code)
                continue
            except Exception as exc:
                errors.append(str(exc))
                logger.exception("CRM active appointment lookup failed endpoint=%s params=%s", endpoint, last_params)
                continue
            appointments = _extract_appointments(data)
            active = [a for a in appointments if _is_active_future_appointment(a)]
            active.sort(key=lambda a: _appointment_datetime(a) or datetime.max)
            return {
                "ok": True,
                "status_code": last_status or 200,
                "phone_raw": phone,
                "phone_normalized": normalize_phone(phone),
                "phone_variants": variants,
                "endpoint_url": endpoint,
                "query_params": last_params,
                "appointments_raw_count": len(appointments),
                "appointments": active,
                "appointment": active[0] if active else None,
                "appointments_cache": active[1:],
                "active_count": len(active),
                "raw": data,
            }
        err = CRMError("CRM lookup failed: " + "; ".join(errors[-3:]))
        setattr(err, "status_code", last_status)
        setattr(err, "endpoint_url", endpoint)
        setattr(err, "query_params", last_params)
        raise err

    async def lookup_appointment_by_id(self, appointment_id: str | int) -> dict[str, Any]:
        response = await _client().get(_url("/api/bot/appointment"), params={"appointmentId": appointment_id}, headers=_headers())
        _raise_for_crm(response, "appointment")
        data = response.json(); data.setdefault("ok", True); return data

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

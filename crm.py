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
    secret = (
        getattr(settings, "crm_bot_secret", "")
        or getattr(settings, "external_booking_api_secret", "")
        or getattr(settings, "bot_api_secret", "")
        or ""
    )
    return {"x-bot-secret": secret}


def _url(path: str) -> str:
    settings = get_settings()
    return f"{settings.crm_base_url.rstrip('/')}{path}"


def _raise_for_crm(response: httpx.Response, label: str) -> None:
    if response.status_code == 401:
        raise CRMError("CRM вернула 401: неверный CRM_BOT_SECRET / EXTERNAL_BOOKING_API_SECRET")
    if response.status_code == 403:
        raise CRMError(f"CRM {label} error 403: запись не принадлежит этому телефону")
    if response.status_code == 404:
        raise CRMError(f"CRM {label} error 404: endpoint/запись не найдены")
    if response.status_code == 409:
        raise CRMError(f"CRM {label} error 409: слот уже занят")
    if response.status_code == 410:
        raise CRMError(f"CRM {label} error 410: запись уже отменена")
    if response.status_code == 429:
        try:
            retry = response.json().get("retryAfterSec")
        except Exception:
            retry = None
        raise CRMError(
            f"CRM {label} error 429: превышен лимит"
            + (f", повтор через {retry} сек" if retry else "")
        )
    if response.status_code >= 400:
        raise CRMError(f"CRM {label} error {response.status_code}: {response.text}")


_SLOTS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_SLOTS_CACHE_TTL = 25.0

_DOCTORS_CACHE: tuple[float, dict[str, Any]] | None = None
_SERVICES_CACHE: tuple[float, dict[str, Any]] | None = None
_META_CACHE_TTL = 300.0


async def patient_lookup(phone: str) -> dict[str, Any]:
    response = await _client().get(
        _url("/api/bot/patient-lookup"),
        params={"phone": phone},
        headers=_headers(),
    )
    _raise_for_crm(response, "patient-lookup")
    return response.json()


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
    _SERVICES_CACHE = (now, data)
    return data


async def check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    """Проверка свободных слотов по официальному CRM-контракту.

    CRM контракт:
        GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=<login>
        Header: x-bot-secret: <ключ>

    CRM возвращает:
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

    Для совместимости с dialog.py/services.py дополнительно возвращаем
    нормализованный список:
        data["slots"] = [
          {
            "doctorLogin": "...",
            "doctorName": "...",
            "date": "YYYY-MM-DD",
            "timeStart": "10:00",
            "time": "10:00"
          }
        ]
    """
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

    # Нормализация CRM-ответа в простой список слотов.
    # Это защищает нас, если dialog.py ждёт slots/ok, а CRM отдаёт availability.
    slots: list[dict[str, Any]] = []

    for item in availability:
        if not isinstance(item, dict):
            continue

        doctor_login_item = item.get("doctorLogin")
        doctor_name = item.get("doctorName")
        item_date = item.get("date") or date
        available_slots = item.get("availableSlots") or []

        if not isinstance(available_slots, list):
            continue

        for time_start in available_slots:
            if not time_start:
                continue

            slots.append(
                {
                    "doctorLogin": doctor_login_item,
                    "doctorName": doctor_name,
                    "date": item_date,
                    "timeStart": str(time_start),
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
    """Создание записи по официальному CRM-контракту.

    CRM контракт:
        POST /api/bot/book
        Header: x-bot-secret: <ключ>

    Обязательные поля:
        patientName, phone, doctorLogin, date, timeStart

    Опционально:
        doctorName, notes, conversationId, leadId
    """
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

    response = await _client().post(
        _url("/api/bot/book"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )

    _raise_for_crm(response, "book")

    # После успешной записи чистим кэш слотов на эту дату,
    # чтобы бот не предложил уже занятое время повторно.
    for key in list(_SLOTS_CACHE.keys()):
        if key[0] == date:
            _SLOTS_CACHE.pop(key, None)

    data = response.json()

    # Для внутренней логики помечаем успешный статус.
    # Реальный статус лида "Записан" обычно должна выставлять CRM после успешного /book.
    data.setdefault("ok", True)
    data.setdefault("status", "Записан")

    return data


async def cancel_appointment(
    *,
    phone: str,
    appointment_id: int | None = None,
    reason: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phone": phone,
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
    return response.json()


async def reschedule_appointment(
    *,
    phone: str,
    new_date: str,
    new_time_start: str,
    appointment_id: int | None = None,
    reason: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phone": phone,
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
    return response.json()


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
        payload["phone"] = phone
    if conversation_id:
        payload["conversationId"] = conversation_id

    response = await _client().post(
        _url("/api/bot/escalate"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )

    _raise_for_crm(response, "escalate")
    return response.json()


async def log_outcome(
    *,
    phone: str | None = None,
    conversation_id: int | str | None = None,
    outcome: str,
    appointment_id: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome": outcome,
        "appointmentId": appointment_id,
        "note": note,
    }

    if phone:
        payload["phone"] = phone
    if conversation_id:
        payload["conversationId"] = conversation_id

    response = await _client().post(
        _url("/api/bot/outcome"),
        json=payload,
        headers={**_headers(), "Content-Type": "application/json"},
    )

    _raise_for_crm(response, "outcome")
    return response.json()

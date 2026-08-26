"""Допуск лида: с кем ИИ-консультант вообще разговаривает.

Правило клиники одно: бот работает только с новыми обращениями. Пациент,
который уже есть в CRM, и пациент с действующей записью — работа живого
администратора, а не бота.

Раньше это правило было размазано по монкипатчам: new_leads_only_policy на
импорте подменял в dialog.py признак «только новые лиды», а
returning_patient_policy — саму классификацию, переводя вернувшегося пациента
обратно в нового; подключены они были по-разному. Поведение продакшена зависело
от порядка импортов, поэтому баги не воспроизводились. Теперь классификация —
одна чистая функция без побочных эффектов, а решение молчать принимает
вызывающий код.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import crm


NEW = "NEW"
RETURNING = "RETURNING"
ACTIVE_BOOKING = "ACTIVE_BOOKING"
CRM_UNAVAILABLE = "CRM_UNAVAILABLE"

# Статусы записи, при которых визит ещё предстоит.
ACTIVE_APPOINTMENT_STATUSES = (
    "active", "scheduled", "confirmed", "booked", "new_booking",
    "активна", "активный", "запланирован", "подтвержден", "подтверждён",
    "записан", "записана", "подтвердил", "подтвердила", "подтвердил_заранее",
    "ожидает",
)

# Статусы лида, которые означают «обращение ещё не в работе».
NEW_LEAD_STATUSES = {"", "новая", "new"}

# Часовой пояс клиники (Астана, UTC+5): «сегодня» считаем по нему, иначе
# утренняя запись выглядела бы прошедшей для половины смены.
_CLINIC_UTC_OFFSET = timedelta(hours=5)


@dataclass(frozen=True)
class Admission:
    """Кто написал: результат классификации по ответу CRM."""

    state: str
    reason: str
    phone: str = ""
    appointment: dict[str, Any] | None = None

    @property
    def bot_may_reply(self) -> bool:
        """Отвечает бот только новому лиду. Всё остальное — молчание."""
        return self.state == NEW


def _low(value: Any) -> str:
    return str(value or "").strip().lower()


def _raw(crm_lookup: dict[str, Any] | None) -> dict[str, Any]:
    """Тело ответа CRM: ``lookup_active_appointments_by_phone`` кладёт его в raw."""
    if not isinstance(crm_lookup, dict):
        return {}
    raw = crm_lookup.get("raw")
    return raw if isinstance(raw, dict) else crm_lookup


def _appointment_is_active(appointment: dict[str, Any] | None) -> bool:
    """Запись, к которой пациент ещё придёт: не отменённая и не в прошлом."""
    if not isinstance(appointment, dict) or not appointment:
        return False
    status = _low(appointment.get("status") or appointment.get("appointmentStatus"))
    if status and not any(marker in status for marker in ACTIVE_APPOINTMENT_STATUSES):
        return False
    date_value = str(appointment.get("date") or appointment.get("appointmentDate") or "")[:10]
    if date_value:
        try:
            today = (datetime.now(timezone.utc) + _CLINIC_UTC_OFFSET).date()
            return datetime.fromisoformat(date_value).date() >= today
        except ValueError:
            pass
    return bool(status)


def _active_appointment(crm_lookup: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any] | None:
    for appointment in crm_lookup.get("appointments") or []:
        if _appointment_is_active(appointment):
            return appointment
    for candidate in (crm_lookup.get("appointment"), raw.get("appointment"), raw.get("activeAppointment"), raw.get("lastAppointment")):
        if _appointment_is_active(candidate):
            return candidate
    return None


def _lookup_failed(crm_lookup: dict[str, Any] | None) -> str:
    """CRM ответила ошибкой или не ответила вовсе."""
    if not isinstance(crm_lookup, dict) or not crm_lookup:
        return "no_crm_answer"
    if crm_lookup.get("ok") is False:
        return "crm_error"
    if str(crm_lookup.get("error") or "").strip():
        return "crm_error"
    return ""


def classify(phone: str, crm_lookup: dict[str, Any] | None) -> Admission:
    """Единственная точка, где решается, новый это лид или нет.

    ``crm_lookup`` — ответ ``crm.lookup_active_appointments_by_phone``. Функция
    ничего не запрашивает и ничего не меняет: её легко проверить тестом на
    каждый пограничный случай CRM.

    Неизвестный исход всегда закрытый: без номера, который CRM понимает, и без
    её ответа мы не знаем, кто пишет, — а начать анкету пациенту, который уже
    записан, хуже, чем промолчать.
    """
    normalized = crm.normalize_phone(phone or "")
    if not normalized:
        return Admission(state=CRM_UNAVAILABLE, reason="invalid_phone", phone="")

    failure = _lookup_failed(crm_lookup)
    if failure:
        return Admission(state=CRM_UNAVAILABLE, reason=failure, phone=normalized)

    lookup = crm_lookup if isinstance(crm_lookup, dict) else {}
    raw = _raw(lookup)

    appointment = _active_appointment(lookup, raw)
    if appointment is not None or raw.get("hasActiveAppointment") is True:
        return Admission(
            state=ACTIVE_BOOKING,
            reason="active_appointment",
            phone=normalized,
            appointment=appointment,
        )

    patient = raw.get("patient") if isinstance(raw.get("patient"), dict) else None
    lead = raw.get("lead") if isinstance(raw.get("lead"), dict) else None
    last_appointment = raw.get("lastAppointment") if isinstance(raw.get("lastAppointment"), dict) else None

    if patient:
        return Admission(state=RETURNING, reason="patient_exists", phone=normalized)
    if last_appointment:
        # Запись есть, но она отменена или уже в прошлом: пациент не новый.
        return Admission(state=RETURNING, reason="past_or_cancelled_appointment", phone=normalized)
    if lead and _low(lead.get("status")) not in NEW_LEAD_STATUSES:
        return Admission(state=RETURNING, reason="lead_in_progress", phone=normalized)
    if raw.get("isNew") is False:
        return Admission(state=RETURNING, reason="crm_says_not_new", phone=normalized)
    if raw.get("isNew") is not True and raw.get("found") is True:
        return Admission(state=RETURNING, reason="crm_found_contact", phone=normalized)

    return Admission(state=NEW, reason="no_existing_contact", phone=normalized)

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
NEW_LEAD_IN_PROGRESS = "NEW_LEAD_IN_PROGRESS"
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
NEW_LEAD_STATUSES = {"новая", "new"}

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
    lead_id: str = ""

    @property
    def bot_may_reply(self) -> bool:
        """Отвечает бот только новому лиду. Всё остальное — молчание."""
        return self.state in {NEW, NEW_LEAD_IN_PROGRESS}


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
    if raw.get("ok") is False or raw.get("success") is False or raw.get("error"):
        return Admission(state=CRM_UNAVAILABLE, reason="crm_error", phone=normalized)
    # CRM must explicitly certify a complete identity lookup. Missing fields
    # must not be silently interpreted as "new client".
    if not isinstance(raw.get("found"), bool) or not isinstance(raw.get("isNew"), bool):
        return Admission(state=CRM_UNAVAILABLE, reason="incomplete_crm_lookup", phone=normalized)
    if "hasActiveAppointment" in raw and not isinstance(raw["hasActiveAppointment"], bool):
        return Admission(state=CRM_UNAVAILABLE, reason="incomplete_crm_lookup", phone=normalized)
    for key in ("patient", "lead", "appointment", "lastAppointment", "activeAppointment"):
        if raw.get(key) is not None and (not isinstance(raw[key], dict) or not raw[key]):
            return Admission(state=CRM_UNAVAILABLE, reason="incomplete_crm_lookup", phone=normalized)
    for key in ("appointments", "records", "items"):
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(item, dict) for item in raw[key])):
            return Admission(state=CRM_UNAVAILABLE, reason="incomplete_crm_lookup", phone=normalized)

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
    if lookup.get("appointments") or raw.get("appointments") or raw.get("records") or raw.get("items") or raw.get("appointment"):
        # A finished/cancelled appointment is still proof of an existing patient.
        return Admission(state=RETURNING, reason="appointment_history_exists", phone=normalized)
    if lead and _low(lead.get("status")) not in NEW_LEAD_STATUSES:
        return Admission(state=RETURNING, reason="lead_in_progress", phone=normalized)
    if raw.get("isNew") is False:
        return Admission(state=RETURNING, reason="crm_says_not_new", phone=normalized)
    if raw.get("found") is True and not (raw.get("isNew") is True and lead):
        return Admission(state=RETURNING, reason="crm_found_contact", phone=normalized)

    return Admission(state=NEW, reason="no_existing_contact", phone=normalized)


def classify_session(phone: str, crm_lookup: dict[str, Any] | None, session: dict[str, Any]) -> Admission:
    """Continue only an authenticated CRM card bound to this random AI session.

    Optional CRM field lead.aiSessionId must originate from the CRM integration,
    never from messages or model extraction. Legacy CRM responses fail closed.
    No new endpoint or timing-based attribution is implied by this extension.
    """
    base = classify(phone, crm_lookup)
    if base.state == CRM_UNAVAILABLE or base.reason not in {"lead_in_progress", "crm_says_not_new"}:
        return base
    if any(session.get(k) for k in ("ai_admission_closed_at", "manual_takeover", "manual_admin_intervention", "escalated", "booking_confirmed", "ai_muted")):
        return base
    token = session.get("ai_admission_session_id")
    if not isinstance(token, str) or len(token) < 32 or session.get("ai_admission_phone") != base.phone:
        return base
    try:
        started = datetime.fromisoformat(str(session.get("ai_admission_started_at") or ""))
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    except (ValueError, TypeError):
        return base
    if not 0 <= elapsed <= 12 * 3600:
        return base
    lookup = crm_lookup or {}
    variants = [lookup, _raw(lookup), *(lookup.get("identity_variants") or [])]
    expected_id = str(session.get("ai_admission_lead_id") or "")
    observed = set()
    for payload in variants:
        if not isinstance(payload, dict):
            return base
        raw = _raw(payload)
        lead = raw.get("lead")
        # Normalized wrappers may have no identity fields; the raw envelope
        # remains mandatory and is checked separately.
        if payload is lookup and isinstance(lookup.get("raw"), dict):
            if lookup.get("appointments"):
                return base
            continue
        if not isinstance(lead, dict) or lead.get("aiSessionId") != token:
            return base
        lead_id = str(lead.get("id") or "")
        if not lead_id or (expected_id and expected_id != lead_id):
            return base
        observed.add(lead_id)
        if str(lead.get("status") or "").strip().lower() not in NEW_LEAD_STATUSES | {"in_progress", "in progress", "в работе"}:
            return base
        if any(raw.get(k) or lead.get(k) for k in ("assignedTo", "assigned_to", "managerId", "operatorId", "staffId", "manualTakeover", "staffTakeover", "closedAt")):
            return base
        candidate = dict(raw, isNew=True, lead=dict(lead, status="NEW"))
        if classify(phone, candidate).state != NEW:
            return base
    if len(observed) != 1:
        return base
    return Admission(state=NEW_LEAD_IN_PROGRESS, reason="crm_session_provenance_verified", phone=base.phone, lead_id=next(iter(observed)))

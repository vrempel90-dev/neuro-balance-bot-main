
from __future__ import annotations

import json
from typing import Any

import crm
import state
from clinic_info import get_clinic_info
from phone import sanitize_kz_phone


CANCEL_WORDS = [
    "отменить запись", "отмените запись", "отмена записи", "не приду",
    "не смогу прийти", "передумал", "уберите запись", "снимите запись",
    "просто отмените", "отменить прием", "отмените прием", "отменить приём", "отмените приём",
    "жазбаны отмен", "келмеймін", "бара алмаймын",
]

RESCHEDULE_WORDS = [
    "перенести", "перенесите", "другой день", "другое время", "не получается подойти",
    "ауыстыру", "басқа күн", "басқа уақыт",
]

WHEN_WORDS = [
    "во сколько", "когда у меня", "на когда", "подтвердите запись", "у меня запись",
    "қай уақытта", "қашан",
]

SIDE_INFO_WORDS = [
    "адрес", "где находитесь", "где вы", "как доехать", "2gis", "2 гис",
    "сколько стоит", "цена", "стоимость", "график", "работаете", "во сколько открываетесь",
]

BOOKING_INTENT_WORDS = [
    "хочу записаться", "записаться", "запишите", "можно на прием", "можно на приём",
    "на консультацию", "по акции", "прием", "приём", "консультац", "жазыл",
]


def _low(text: str) -> str:
    return (text or "").lower().strip()


def is_cancel_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in CANCEL_WORDS)


def is_reschedule_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in RESCHEDULE_WORDS)


def is_when_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in WHEN_WORDS)


def is_side_info_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in SIDE_INFO_WORDS)


def is_booking_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in BOOKING_INTENT_WORDS)


def active_appointment_from_lookup(lookup: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(lookup, dict):
        return None
    if not lookup.get("hasActiveAppointment"):
        return None
    appt = lookup.get("lastAppointment") or lookup.get("appointment") or {}
    return appt if isinstance(appt, dict) and appt else None


def appointment_id(appt: dict[str, Any] | None) -> int | None:
    if not appt:
        return None
    for key in ("appointmentId", "id", "appointment_id"):
        val = appt.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None


def appointment_human(appt: dict[str, Any]) -> str:
    date = appt.get("date") or appt.get("appointmentDate") or ""
    time = appt.get("timeStart") or appt.get("time_start") or appt.get("time") or ""
    doctor = appt.get("doctorName") or appt.get("doctor_name") or ""
    parts = []
    if date:
        parts.append(str(date))
    if time:
        parts.append(str(time))
    if doctor:
        parts.append(str(doctor))
    return ", ".join(parts) if parts else "активная запись"


async def refresh_lookup(chat_id: str, phone: str, session: dict[str, Any]) -> dict[str, Any]:
    normalized = sanitize_kz_phone(phone) or sanitize_kz_phone(session.get("phone") or "") or phone
    try:
        data = await crm.patient_lookup(normalized)
        session["phone"] = normalized
        session["patient_lookup"] = data
        session["patient_lookup_done"] = True
        state.save_session(chat_id, session)
        state.log_bot_action(
            chat_id,
            "tool_call",
            "Проверка существующей записи",
            tool_name="find_existing_appointment",
            tool_args={"phone": normalized},
            tool_result=json.dumps(data, ensure_ascii=False)[:4000],
        )
    except Exception as exc:
        state.log_bot_action(
            chat_id,
            "error",
            "patient_lookup failed",
            tool_name="find_existing_appointment",
            tool_args={"phone": normalized},
            tool_result=str(exc)[:1000],
        )
    return session


async def pre_handle_message(chat_id: str, phone: str, user_text: str, session: dict[str, Any]) -> str | None:
    """Python-аналог ранних гейтов bot-engine.ts.

    Запускается ДО questionnaire и ДО GPT:
    - отмена активной записи;
    - запрос «когда моя запись»;
    - перенос — не создаём дубль, а ведём к переносу/оператору.
    """
    normalized = sanitize_kz_phone(phone) or sanitize_kz_phone(session.get("phone") or "") or phone

    booking_intent = is_booking_intent(user_text)

    # При явной отмене/переносе/вопросе о записи/новой записи CRM обязана быть источником правды.
    # Это защищает от дублей: если пациент уже записан, новую анкету не запускаем.
    if is_cancel_intent(user_text) or is_reschedule_intent(user_text) or is_when_intent(user_text) or booking_intent or not session.get("patient_lookup_done"):
        session = await refresh_lookup(chat_id, normalized, session)

    appt = active_appointment_from_lookup(session.get("patient_lookup"))

    if booking_intent and appt and not is_cancel_intent(user_text) and not is_reschedule_intent(user_text):
        state.log_bot_action(chat_id, "guard_blocked", "Пациент уже записан, новая запись заблокирована")
        return f"Вы уже записаны на приём: {appointment_human(appt)} 🌿\nЕсли хотите перенести или отменить запись — напишите, я помогу."

    if is_cancel_intent(user_text):
        if not appt:
            state.log_bot_action(chat_id, "guard_blocked", "Пациент просит отмену, активная запись не найдена")
            try:
                await crm.escalate_to_operator(phone=normalized, reason="Пациент просит отменить запись, но активная запись не найдена")
            except Exception:
                pass
            return "Поняла Вас. Я передам администратору, чтобы он проверил запись и помог с отменой 🌿"

        appt_id = appointment_id(appt)
        try:
            result = await crm.cancel_appointment(phone=normalized, appointment_id=appt_id, reason="отмена по просьбе пациента через WhatsApp-бота")
            state.log_bot_action(
                chat_id,
                "tool_call",
                "Отмена записи",
                tool_name="cancel_appointment",
                tool_args={"phone": normalized, "appointmentId": appt_id},
                tool_result=json.dumps(result, ensure_ascii=False)[:4000],
            )
            try:
                await crm.log_outcome(phone=normalized, outcome="cancelled", appointment_id=appt_id, note="Пациент отменил запись через WhatsApp-бота")
            except Exception:
                pass
            # Сбросим поля записи, но не всю историю.
            session["patient_lookup"] = {}
            session["selected_date"] = ""
            session["selected_time"] = ""
            session["selected_doctor_login"] = ""
            session["selected_doctor_name"] = ""
            session["questionnaire_step"] = "start"
            state.save_session(chat_id, session)
            return "Хорошо, запись отменили 🌿"
        except Exception as exc:
            state.log_bot_action(
                chat_id,
                "error",
                "Не удалось отменить запись",
                tool_name="cancel_appointment",
                tool_args={"phone": normalized, "appointmentId": appt_id},
                tool_result=str(exc)[:1000],
            )
            try:
                await crm.escalate_to_operator(phone=normalized, reason=f"Не удалось отменить запись автоматически: {exc}")
            except Exception:
                pass
            return "Не смогла отменить запись автоматически. Передам администратору, чтобы он проверил и помог с отменой 🌿"

    if is_when_intent(user_text) and appt:
        state.log_bot_action(chat_id, "guard_blocked", "Ответ по активной записи без запуска новой анкеты")
        return f"Вы уже записаны: {appointment_human(appt)} 🌿"
    if is_side_info_intent(user_text) and appt:
        low = _low(user_text)
        if "адрес" in low or "где" in low or "2gis" in low or "2 гис" in low:
            return (get_clinic_info("address") or "Адрес клиники: Кабанбай батыра 28, Астана 🌿") + f"\n\nВаша запись: {appointment_human(appt)} 🌿"
        if "график" in low or "работаете" in low:
            return (get_clinic_info("schedule") or "Приём ведётся по записи 🌿") + f"\n\nВаша запись: {appointment_human(appt)} 🌿"
        if "стоимость" in low or "цена" in low or "сколько стоит" in low:
            return (get_clinic_info("price_first_visit") or "Первичная консультация стоит 5 000 тг 🌿") + f"\n\nВаша запись: {appointment_human(appt)} 🌿"


    if is_reschedule_intent(user_text) and appt:
        session["reschedule_pending"] = True
        session["questionnaire_step"] = "day"
        state.save_session(chat_id, session)
        state.log_bot_action(chat_id, "guard_blocked", "Пациент хочет перенос активной записи")
        return "Хорошо, давайте подберём другой день 🌿 На какой день Вам удобно перенести запись?"

    return None

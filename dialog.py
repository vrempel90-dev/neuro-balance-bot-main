"""Один входящий ход пациента: очистка → допуск лида → агент → ответ.

Почему модуль такой маленький
-----------------------------
Здесь жила вторая, детерминированная воронка диалога: 7940 строк, 227
ветвлений в ``handle_message`` и 33 перезаписи ``answer`` в ``_finalize``.
Она дублировала GPT-агента и спорила с ним: агент двигал разговор вперёд,
guard сверял ответ со своим ``step``, не соглашался и подставлял прежний
вопрос шага. Пациент отвечал — guard снова не соглашался. Так рождалось
зацикливание, а ``duplicate_answer_guard`` превращал следующий такой повтор
в молчание.

Теперь разговор ведёт агент (``agent.run_agent_turn``), а Python остаётся
исполнительным слоем и держит ровно три обязанности:

1. решить, разговаривает ли бот с этим номером вообще (только новые лиды);
2. выполнить ход агента и отдать его текст пациенту без правок;
3. заблокировать ответ и позвать администратора, если ход небезопасен —
   красный флаг в сообщении пациента, пустой ответ или факт, которого не
   было ни в одном результате инструмента этого диалога.

Блокировать — можно. Переписывать текст агента — нет: именно подмена
формулировок и возвращала пациента к уже пройденному вопросу.

Состояние диалога живёт в истории сообщений агента и в реестрах CRM-фактов
внутри сессии (``crm_offered_slots``, ``crm_active_appointments``), которые
заполняют инструменты в ``agent.py``. Второго источника истины о шаге
диалога (``step`` / ``questionnaire_step``) больше нет.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import agent
import crm
import state
from config import get_settings

try:
    from phone import sanitize_kz_phone
except Exception:  # pragma: no cover - phone.py is always importable in production
    def sanitize_kz_phone(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        return digits

try:
    from language_guard import analyze_language as analyze_message_language
    from language_guard import explicit_language_request as _lg_explicit_language_request
except Exception:  # pragma: no cover
    analyze_message_language = None
    _lg_explicit_language_request = None


# ---------------------------------------------------------------------------
# Тексты, которые принадлежат Python, а не агенту
# ---------------------------------------------------------------------------
# Их ровно два, и оба означают «дальше говорит человек». Всё остальное, что
# получает пациент, пишет агент.

OPERATOR_HANDOFF_RU = "Секунду, подключаю администратора 🌿"
OPERATOR_HANDOFF_KK = "Бір сәт, әкімшіні қосамын 🌿"

EMERGENCY_103_RU = (
    "То, что Вы описываете, может быть неотложным состоянием. Пожалуйста, "
    "сразу позвоните 103 или обратитесь в скорую помощь 🌿 Я передаю Ваше "
    "сообщение администратору."
)
EMERGENCY_103_KK = (
    "Сіз сипаттаған жағдай шұғыл көмекті қажет етуі мүмкін. Өтінемін, дереу "
    "103-ке қоңырау шалыңыз немесе жедел жәрдемге хабарласыңыз 🌿 "
    "Хабарламаңызды әкімшіге беремін."
)

# Острые симптомы, при которых пациенту нужна скорая, а не запись на приём.
# Список намеренно узкий: каждое срабатывание уводит живой диалог к человеку.
RED_FLAG_MARKERS = (
    "боль в груди", "боли в груди", "болит грудь", "болит в груди",
    "давит в груди", "давит грудь", "сжимает в груди", "сжимает грудь",
    "жжет в груди", "не могу дышать", "задыхаюсь", "нечем дышать",
    "потерял сознание", "потеряла сознание", "теряю сознание", "обморок",
    "перекосило лицо", "онемела половина", "отнялась рука", "отнялась нога",
    "отнялись ноги", "отказали ноги", "речь пропала", "не могу говорить",
    "кровотечение", "рвота кровью", "кровь изо рта",
    # казахский после нормализации (қ→к, ү/ұ→у, і→и, ә→а, ө→о, ң→н, ғ→г)
    "журек ауырады", "кокирегим ауырады", "тыныс ала алмаймын",
    "есинен танды", "кан кетип жатыр",
)

# Те же слова в рассказе о прошлом — это анамнез, а не неотложное состояние.
# Реабилитация после инсульта и есть профиль клиники: без этой проверки
# «после инсульта отнялась рука, хотим восстановление» уводило бы в 103.
RED_FLAG_HISTORY_MARKERS = (
    "после", "перенес", "перенёс", "перенесла", "был", "была", "было",
    "назад", "реабилитац", "восстановлен", "лечил", "лечу", "хроничес",
    "кейин", "болган", "калпына",
)

CRM_ACTIVE_STATUSES = {
    "active", "scheduled", "confirmed", "booked", "new_booking",
    "активна", "активный", "запланирован", "подтвержден", "подтверждён",
    "записан", "записана", "подтвердил", "подтвердила", "подтвердил_заранее",
    "ожидает",
}
CRM_NEW_LEAD_STATUSES = {"", "новая", "new"}

# Технические причины, по которым агент не отработал, но пациент всё равно
# должен получить ответ: ключа нет, бюджет исчерпан, сеть упала.
# Всё остальное — молчание: за диалогом уже следит человек.
SILENT_SKIP_REASONS = {"empty_text", "manual_takeover", "old_chat_ai_disabled"}

# no_reply_reason / first_touch_blocked_reason по состоянию лида. Значения —
# существующий контракт логов, дашборда и repair_service, поэтому не меняются.
_ADMISSION_MUTE_REASONS = {"old_lead_from_crm", "active_booking_old_lead", "crm_lookup_failed",
                           "returning_patient_old_lead"}

_LEAD_SILENCE_REASONS = {
    "RETURNING_PATIENT_NO_ACTIVE_BOOKING": ("old_lead_from_crm", "returning_patient_old_lead"),
    "ACTIVE_BOOKING": ("active_booking_old_lead", "active_booking_old_lead"),
    "CRM_UNAVAILABLE": ("crm_lookup_failed", "crm_lookup_failed"),
}

LANGUAGE_SWITCH_CONFIDENCE = 0.6
_SHORT_ANSWER_RE = re.compile(
    r"\s*(?:\d{1,3}\s*(?:жаста|жас|лет|года|год)?|жоқ|жок|ия|иә|жақсы|жаксы|рахмет|"
    r"рақмет|спасибо|ок|окей|нет|да|ага|угу|неа|иа)\s*[.!?🙏🌿]*\s*"
)

_KZ_TO_RU_CHARS = {"і": "и", "ұ": "у", "ү": "у", "қ": "к", "ң": "н", "ғ": "г", "ә": "а", "ө": "о", "һ": "х"}

_TIME_IN_TEXT = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
_ISO_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NUMERIC_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    "кантар": 1, "акпан": 2, "наурыз": 3, "суир": 4, "мамыр": 5, "маусым": 6,
    "шилде": 7, "тамыз": 8, "кыркуиек": 9, "казан": 10, "караша": 11, "желтоксан": 12,
}
_TEXT_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{1,2})\s*(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")[а-я]*")
_DOCTOR_IN_TEXT = re.compile(
    r"(?:врач|врача|врачу|доктор|доктора|доктору|даригер|маман)\s*[—:-]?\s*"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})"
)


# ---------------------------------------------------------------------------
# Мелкие помощники
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _clean_outgoing(text: str) -> str:
    """Нормализует исходящий текст, сохраняя абзацы."""
    out = re.sub(r"[ \t]+", " ", str(text or "").strip())
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out)


def _low(text: str) -> str:
    """Нормализует сообщение пациента для сопоставления со списками слов.

    Приводит казахские буквы к русским и схлопывает растянутые буквы
    («болиииит»), чтобы «жүрек» и «журек» были одним словом. Это разбор
    входящего текста — ответ пациенту здесь не меняется никогда.
    """
    low = _clean(text).lower().replace("ё", "е")
    for kz, ru in _KZ_TO_RU_CHARS.items():
        low = low.replace(kz, ru)
    return re.sub(r"([а-яa-z])\1{2,}", r"\1\1", low)


def _tr(session_or_lang: dict[str, Any] | str, ru: str, kk: str) -> str:
    lang = session_or_lang.get("language") if isinstance(session_or_lang, dict) else session_or_lang
    return kk if lang == "kk" else ru


def _safe_save(chat_id: str, session: dict[str, Any]) -> None:
    try:
        state.save_session(chat_id, session)
    except Exception:
        pass


def _safe_add_message(chat_id: str, role: str, text: str) -> None:
    try:
        state.add_message(chat_id, role, text)
    except Exception:
        pass


def _safe_log(chat_id: str, event: str, payload: dict[str, Any]) -> None:
    try:
        state.log_event(chat_id, event, payload)
    except Exception:
        pass


def _is_new_leads_only_enabled() -> bool:
    try:
        return bool(getattr(get_settings(), "new_leads_only", True))
    except Exception:
        return True


def _valid_crm_phone(phone: str | None) -> bool:
    """Без номера, который понимает CRM, ни поиск, ни запись невозможны."""
    normalized = crm.normalize_phone(phone or "") or sanitize_kz_phone(phone or "")
    if re.search(r"[xх]", str(phone or ""), flags=re.IGNORECASE):
        return False
    return bool(re.fullmatch(r"77\d{9}", normalized or ""))


def _strip_quoted_bot_text(text: str) -> str:
    """Убирает из входящего текста служебные строки Wazzup-реплая.

    В реплае в payload иногда попадает цитата предыдущего сообщения плюс
    реальный ответ пациента. Нам нужен ответ.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return text
    kept = [line for line in lines if _low(line) not in ("phone", "api", "admin", "api · admin", "api · null")]
    return "\n".join(kept) if kept else text


def _recent_history_for_brain(chat_id: str, session: dict[str, Any], current_text: str = "") -> list[dict[str, Any]]:
    """Компактная история диалога для агента.

    Текущее сообщение агент получает отдельным аргументом, а в истории оно уже
    записано, поэтому последнее вхождение убирается: иначе модель видит одну и
    ту же реплику дважды подряд. Настоящий дубль (пациент прислал одно и то же
    два раза) при этом сохраняется — в истории он лежит двумя записями.
    """
    raw = state.get_history(chat_id, limit=24) if hasattr(state, "get_history") else []
    items: list[dict[str, Any]] = []
    for item in raw[-20:]:
        text = str(item.get("text") or item.get("content") or "").strip()
        role = str(item.get("role") or "").strip()
        if not text or role not in {"user", "assistant", "bot", "admin", "human", "operator", "manager"}:
            continue
        items.append({"role": "assistant" if role == "bot" else role, "text": text})
    current = str(current_text or "").strip()
    if current and items and items[-1]["role"] == "user" and items[-1]["text"] == current:
        items.pop()
    return items


# ---------------------------------------------------------------------------
# Язык диалога
# ---------------------------------------------------------------------------


def _is_short_language_neutral_answer(text: str) -> bool:
    """Короткое подтверждение/число, которое не должно менять язык диалога."""
    low = _low(text)
    if _SHORT_ANSWER_RE.fullmatch(low):
        return True
    words = re.findall(r"[a-zA-Zа-яёәғқңөұүһі]+", low)
    return len(words) <= 1


def _detect_lang(text: str, session: dict[str, Any]) -> str:
    """Определяет язык ответа: пациент может перейти на другой язык посреди диалога.

    Переключение требует уверенного сигнала: явная просьба сильнее всего,
    короткие ответы («иә», «да», «44») и смешанные сообщения язык не меняют.
    """
    current = session.get("language")
    established = current in ("ru", "kk")
    if not established:
        current = "ru"

    if _lg_explicit_language_request:
        try:
            explicit = _lg_explicit_language_request(text)
            if explicit in ("ru", "kk"):
                return explicit
        except Exception:
            pass

    if session.get("language_locked") and established:
        return current
    if not analyze_message_language:
        return current
    try:
        analysis = analyze_message_language(text, current)
    except Exception:
        return current

    detected = analysis.detected_language
    if not established:
        preferred = analysis.preferred_response_language
        return preferred if preferred in ("ru", "kk") else "ru"
    if detected in ("unknown", "mixed") or detected == current:
        return current
    if _is_short_language_neutral_answer(text):
        return current
    if analysis.confidence >= LANGUAGE_SWITCH_CONFIDENCE:
        return detected
    return current


# ---------------------------------------------------------------------------
# Допуск лида: бот работает только с новыми обращениями
# ---------------------------------------------------------------------------


def _crm_raw(lookup: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(lookup, dict):
        return {}
    raw = lookup.get("raw")
    return raw if isinstance(raw, dict) else lookup


def _crm_appt_is_active(appt: dict[str, Any] | None) -> bool:
    if not isinstance(appt, dict) or not appt:
        return False
    status = _low(str(appt.get("status") or appt.get("appointmentStatus") or ""))
    date_s = str(appt.get("date") or appt.get("appointmentDate") or "")[:10]
    if status and not any(s in status for s in CRM_ACTIVE_STATUSES):
        return False
    if date_s:
        try:
            future_or_today = datetime.fromisoformat(date_s).date() >= (datetime.now(timezone.utc) + timedelta(hours=5)).date()
            return future_or_today if not status else (future_or_today and any(s in status for s in CRM_ACTIVE_STATUSES))
        except Exception:
            pass
    return any(s in status for s in CRM_ACTIVE_STATUSES)


def _set_crm_patient_state(session: dict[str, Any], lookup: dict[str, Any] | None, appt: dict[str, Any] | None) -> str:
    """Единственная классификация пациента: NEW_PATIENT / RETURNING / ACTIVE_BOOKING."""
    raw = _crm_raw(lookup)
    patient = raw.get("patient") if isinstance(raw.get("patient"), dict) else None
    lead = raw.get("lead") if isinstance(raw.get("lead"), dict) else None
    last = raw.get("lastAppointment") if isinstance(raw.get("lastAppointment"), dict) else None
    lead_status = _low(str((lead or {}).get("status") or ""))
    is_new = raw.get("isNew") is True
    has_active = raw.get("hasActiveAppointment") is True

    session["crm_patient_found"] = bool(raw.get("found")) if "found" in raw else bool(patient or lead or last)
    session["crm_patient_is_new"] = bool(raw.get("isNew")) if "isNew" in raw else not bool(patient or lead or last or appt)
    session["crm_patient_name"] = (patient or {}).get("name") or raw.get("patientName") or ""

    if appt or has_active or _crm_appt_is_active(last) or raw.get("activeAppointment"):
        state_value = "ACTIVE_BOOKING"
    elif (
        (is_new or (raw.get("isNew") is not False and raw.get("found") is not True))
        and not patient
        and not last
        and ((lead and lead_status in CRM_NEW_LEAD_STATUSES) or not lead)
    ):
        state_value = "NEW_PATIENT"
    else:
        state_value = "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    session["crm_patient_state"] = state_value
    return state_value


async def _classify_lead(chat_id: str, phone: str, session: dict[str, Any]) -> str:
    """Спрашивает CRM про пациента и возвращает его состояние.

    ``CRM_UNAVAILABLE`` — отдельное состояние, а не «наверное новый»: молчать
    из-за недоступной CRM безопаснее, чем начать анкету пациенту, который уже
    записан.
    """
    normalized = crm.normalize_phone(phone or session.get("phone") or "")
    session["phone_normalized"] = normalized
    session["crm_lookup_called"] = True
    session["crm_lookup_error"] = ""
    try:
        lookup = await crm.lookup_active_appointments_by_phone(normalized)
    except Exception as exc:
        session["crm_lookup_error"] = type(exc).__name__
        session["crm_patient_state"] = "CRM_UNAVAILABLE"
        _safe_log(chat_id, "crm_lookup_failed", {"chat_id": chat_id, "error_type": type(exc).__name__})
        return "CRM_UNAVAILABLE"

    appointments = [a for a in list((lookup or {}).get("appointments") or []) if _crm_appt_is_active(a)]
    appt = appointments[0] if appointments else None
    if appt is None and _crm_appt_is_active((lookup or {}).get("appointment")):
        appt = (lookup or {}).get("appointment")
    session["active_appointment_found"] = bool(appt)
    session["active_appointment_count"] = len(appointments)
    return _set_crm_patient_state(session, lookup, appt)


async def _notify_admin_once(chat_id: str, session: dict[str, Any], phone: str, reason: str) -> None:
    """Сообщает администратору о ходе, который бот не ведёт — раз на диалог.

    Пациент либо не получает ответа, либо получает «подключаю администратора»,
    поэтому единственный способ не потерять обращение — передать чат человеку.
    Повторять уведомление на каждое сообщение значило бы спамить, поэтому флаг
    живёт в сессии. Ошибка уведомления не должна ломать ход: администратор
    увидит событие в логе.
    """
    if session.get("admin_notified_reason") == reason:
        return
    session["admin_notified_reason"] = reason
    _safe_log(chat_id, "admin_notified", {"chat_id": chat_id, "reason": reason})
    try:
        await crm.escalate_to_operator(phone=phone or session.get("phone") or "", reason=reason)
    except Exception as exc:
        _safe_log(chat_id, "admin_notify_failed", {"chat_id": chat_id, "error_type": type(exc).__name__})


# ---------------------------------------------------------------------------
# Проверка фактов: всё, что бот называет пациенту, вернула CRM
# ---------------------------------------------------------------------------


def _times_in(text: str) -> set[str]:
    return {f"{int(h):02d}:{m}" for h, m in _TIME_IN_TEXT.findall(str(text or ""))}


def _dates_in(text: str) -> set[tuple[int, int]]:
    """Даты как пара (месяц, день) — год в живом тексте почти не пишут."""
    value = str(text or "")
    found: set[tuple[int, int]] = set()
    for _, month, day in _ISO_DATE_IN_TEXT.findall(value):
        found.add((int(month), int(day)))
    for day, month, _year in _NUMERIC_DATE_IN_TEXT.findall(value):
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            found.add((int(month), int(day)))
    for day, month_word in _TEXT_DATE_IN_TEXT.findall(_low(value)):
        month = _MONTHS.get(month_word)
        if month and 1 <= int(day) <= 31:
            found.add((month, int(day)))
    return found


def _crm_fact_sources(chat_id: str, session: dict[str, Any], result: Any) -> str:
    """Всё, что в этом диалоге пришло из CRM, плюс слова самого пациента.

    Слова пациента — тоже законный источник: когда бот повторяет названный
    пациентом день («на 3 сентября окошек нет»), он ничего не выдумывает.
    Выдумкой считается дата, время или врач, которых не было ни в результате
    инструмента, ни в сообщении пациента.
    """
    parts: list[str] = []
    tool_results = getattr(result, "tool_results", None) or []
    parts.append(json.dumps(tool_results, ensure_ascii=False))
    for key in ("crm_offered_slots", "crm_active_appointments", "last_slots", "crm_known_doctors"):
        value = session.get(key)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False))
    parts.extend(
        str(session.get(key) or "")
        for key in (
            "appointment_date", "appointment_time", "appointment_doctor_name",
            "selected_date", "selected_time", "selected_doctor_name", "last_user_text",
        )
    )
    try:
        history = state.get_history(chat_id, limit=12) if hasattr(state, "get_history") else []
    except Exception:
        history = []
    parts.extend(
        str(item.get("text") or item.get("content") or "")
        for item in history
        if str(item.get("role") or "") == "user"
    )
    return "\n".join(parts)


def _known_doctor_names(session: dict[str, Any], result: Any) -> set[str]:
    names: set[str] = set()

    def _add(value: Any) -> None:
        name = str(value or "").strip().lower()
        if name:
            names.add(name)

    for slot in (session.get("crm_offered_slots") or {}).values() if isinstance(session.get("crm_offered_slots"), dict) else []:
        if isinstance(slot, dict):
            _add(slot.get("doctor_name"))
    for appointment in (session.get("crm_active_appointments") or {}).values() if isinstance(session.get("crm_active_appointments"), dict) else []:
        if isinstance(appointment, dict):
            _add(appointment.get("doctor_name"))
    for name in (session.get("crm_known_doctors") or {}).values() if isinstance(session.get("crm_known_doctors"), dict) else []:
        _add(name)
    for key in ("appointment_doctor_name", "selected_doctor_name"):
        _add(session.get(key))
    for tool_result in getattr(result, "tool_results", None) or []:
        if not isinstance(tool_result, dict):
            continue
        _add(tool_result.get("doctor_name"))
        for collection in ("slots", "appointments", "doctors"):
            for item in tool_result.get(collection) or []:
                if isinstance(item, dict):
                    _add(item.get("doctor_name"))
    return names


def _unverified_fact(chat_id: str, session: dict[str, Any], answer: str, result: Any) -> str:
    """Возвращает факт из ответа, которого не было ни в одном результате инструмента."""
    sources = _crm_fact_sources(chat_id, session, result)

    known_times = _times_in(sources)
    for value in _times_in(answer):
        if value not in known_times:
            return f"time:{value}"

    known_dates = _dates_in(sources)
    for month, day in _dates_in(answer):
        if (month, day) not in known_dates:
            return f"date:{month:02d}-{day:02d}"

    known_doctors = _known_doctor_names(session, result)
    for candidate in _DOCTOR_IN_TEXT.findall(str(answer or "")):
        name = candidate.strip().lower()
        if not any(name in known or known in name for known in known_doctors):
            return "doctor"
    return ""


def _has_red_flag(text: str) -> bool:
    """Острые симптомы, при которых нужна скорая, а не запись."""
    low = _low(text)
    if not any(marker in low for marker in RED_FLAG_MARKERS):
        return False
    return not any(marker in low for marker in RED_FLAG_HISTORY_MARKERS)


# ---------------------------------------------------------------------------
# Три исхода хода: ответ агента, молчание, передача администратору
# ---------------------------------------------------------------------------


def _no_reply(chat_id: str, session: dict[str, Any], reason: str) -> str:
    """Сохраняет состояние и ничего не отправляет пациенту."""
    session["no_reply_reason"] = reason
    session["should_send_wazzup"] = False
    decision = session.get("guard_decision") if isinstance(session.get("guard_decision"), dict) else {}
    decision.update({"allowed": False, "no_reply_reason": reason, "should_send_wazzup": False})
    session["guard_decision"] = decision
    _safe_save(chat_id, session)
    _safe_log(chat_id, "no_reply", {"chat_id": chat_id, "reason": reason})
    return ""


def _admission_muted(session: dict[str, Any]) -> bool:
    """Молчание поставил допуск лида, а не человек."""
    return bool(
        session.get("silent_old_lead")
        or str(session.get("old_lead_reason") or "") in _ADMISSION_MUTE_REASONS
        or str(session.get("no_reply_reason") or "") in _ADMISSION_MUTE_REASONS
    )


def _human_took_over(session: dict[str, Any]) -> bool:
    """В чате работает человек: бот молчит и не возвращается сам.

    Собственная блокировка допуска сюда не относится: CRM могла быть недоступна
    или ещё не знать пациента, и тогда следующее сообщение нужно
    переклассифицировать заново, иначе одна временная ошибка CRM молчаливо
    выключала бы бота для нового лида навсегда.
    """
    if session.get("manual_admin_intervention"):
        return True
    if not (session.get("manual_takeover") or session.get("ai_muted")):
        return False
    return not _admission_muted(session)


def _clear_admission_mute(session: dict[str, Any]) -> None:
    """CRM говорит, что это новый лид: снимаем прошлую блокировку допуска."""
    session["silent_old_lead"] = False
    session["old_lead_reason"] = ""
    session["ai_muted"] = False
    session["manual_takeover"] = False
    session["no_reply_reason"] = ""
    session["openai_skip_reason"] = ""
    session.pop("admin_notified_reason", None)


async def _mute_lead(chat_id: str, session: dict[str, Any], phone: str, lead_state: str) -> str:
    """Не новый лид: бот молчит, обращение уходит администратору.

    Значения ``no_reply_reason`` — существующий контракт логов и дашборда,
    поэтому они те же, что и раньше.
    """
    reason, blocked = _LEAD_SILENCE_REASONS.get(lead_state, ("crm_lookup_failed", "crm_lookup_failed"))
    session["silent_old_lead"] = True
    session["old_lead_reason"] = blocked
    session["ai_muted"] = True
    # CRM молчит — мы не знаем, кто пишет. Дальше говорит человек, а не бот.
    session["manual_takeover"] = lead_state == "CRM_UNAVAILABLE" or bool(session.get("manual_takeover"))
    session["first_touch_allowed"] = False
    session["first_touch_blocked_reason"] = blocked
    await _notify_admin_once(chat_id, session, phone, blocked)
    return _no_reply(chat_id, session, reason)


async def _handoff(chat_id: str, session: dict[str, Any], answer: str, reason: str, *, human_owns: bool = True) -> str:
    """Единственный ответ Python пациенту: «дальше говорит человек».

    ``human_owns=False`` — техническая недоступность агента (нет ключа, сеть,
    бюджет). Администратор уже уведомлён, но чат за ним не закрепляется:
    как только агент снова работает, бот продолжает диалог сам. Иначе один
    таймаут OpenAI молча выключал бы бота для этого пациента навсегда.
    """
    session["escalated"] = True
    session["manual_takeover"] = bool(human_owns) or bool(session.get("manual_takeover"))
    session["answer_source"] = "python_template"
    session["skip_humanize"] = True
    session["handoff_reason"] = reason
    session["last_assistant_answer"] = answer
    _safe_log(chat_id, "handoff_to_operator", {"chat_id": chat_id, "reason": reason})
    await _notify_admin_once(chat_id, session, str(session.get("phone") or ""), reason)
    _safe_add_message(chat_id, "assistant", answer)
    _safe_save(chat_id, session)
    return answer


async def _finalize(chat_id: str, session: dict[str, Any], answer: str, result: Any = None) -> str:
    """Последний барьер перед отправкой: только пропустить или заблокировать.

    Ровно три проверки, и ни одна не переписывает текст агента — блокировка
    означает передачу диалога администратору.
    """
    answer = _clean_outgoing(answer)

    # 1. Красный флаг в сообщении пациента важнее любого ответа бота.
    if _has_red_flag(str(session.get("last_user_text") or "")):
        _safe_log(chat_id, "red_flag_detected", {"chat_id": chat_id})
        return await _handoff(chat_id, session, _tr(session, EMERGENCY_103_RU, EMERGENCY_103_KK), "red_flag")

    # 2. Пустой ответ — это молчание в живом диалоге.
    if not answer:
        return await _handoff(chat_id, session, _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK), "empty_answer")

    # 3. Дата, время или врач, которых не было ни в одном результате инструмента.
    unverified = _unverified_fact(chat_id, session, answer, result)
    if unverified:
        _safe_log(chat_id, "unverified_fact_blocked", {"chat_id": chat_id, "fact": unverified})
        return await _handoff(chat_id, session, _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK), "unverified_fact")

    session["last_assistant_answer"] = answer
    session["final_answer_preview"] = answer[:160]
    session["no_reply_reason"] = ""
    session["technical_handoff_done"] = False
    _safe_add_message(chat_id, "assistant", answer)
    _safe_save(chat_id, session)
    return answer


def _start_turn(chat_id: str, session: dict[str, Any], phone: str, text: str) -> None:
    """Состояние хода: всё, что относится к предыдущему сообщению, обнуляется."""
    session["chat_id"] = chat_id
    session["phone"] = phone or session.get("phone") or ""
    session["last_user_text"] = text
    session["language"] = _detect_lang(text, session)
    session["NEW_LEADS_ONLY"] = _is_new_leads_only_enabled()
    session["answer_source"] = ""
    session["skip_humanize"] = True
    session["handoff_reason"] = ""
    session["crm_called"] = False
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_skip_reason"] = ""
    session["openai_brain_skip_reason"] = ""
    for key in ("crm_lookup_called", "crm_lookup_error", "crm_patient_state",
                "active_appointment_found", "active_appointment_count", "final_answer_preview"):
        session.pop(key, None)


async def handle_message(chat_id: str, phone: str, user_text: str) -> str:
    """Главная функция, которую вызывает main.py.

    Очистка текста → допуск лида → ход агента → ответ пациенту.
    """
    text = _clean(_strip_quoted_bot_text(str(user_text or "")))
    session = state.get_session(chat_id)
    if not isinstance(session, dict):
        session = {}
    _start_turn(chat_id, session, phone, text)
    if text:
        _safe_add_message(chat_id, "user", text)

    if not text:
        return _no_reply(chat_id, session, "empty_text")
    if not _valid_crm_phone(phone or session.get("phone") or ""):
        return _no_reply(chat_id, session, "invalid_phone_for_crm_lookup")
    if _human_took_over(session):
        return _no_reply(chat_id, session, "manual_takeover")

    lead_state = await _classify_lead(chat_id, phone, session)
    if lead_state != "NEW_PATIENT":
        # Не новый лид (или CRM молчит и мы не знаем, кто это) — fail-closed.
        return await _mute_lead(chat_id, session, phone, lead_state)

    if _admission_muted(session):
        _clear_admission_mute(session)
    session["first_touch_allowed"] = True
    session["ai_lead_started"] = True
    session["gate_reason"] = "new_lead"
    try:
        result = await agent.run_agent_turn(
            chat_id=chat_id,
            phone=phone,
            session=session,
            user_text=text,
            recent_history=_recent_history_for_brain(chat_id, session, text),
        )
    except Exception as exc:  # агент не должен ронять входящий вебхук
        _safe_log(chat_id, "agent_unexpected_error", {"chat_id": chat_id, "error_type": type(exc).__name__})
        return await _handoff(chat_id, session, _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK), "agent_exception")

    if not result.used:
        # Агент технически не отработал: ключа нет, бюджет исчерпан, сеть упала.
        # Вторая воронка не вызывается ни при каких условиях — её больше нет.
        session["openai_skip_reason"] = result.skip_reason
        session["openai_brain_skip_reason"] = result.skip_reason
        if result.skip_reason in SILENT_SKIP_REASONS:
            return _no_reply(chat_id, session, result.skip_reason)
        if session.get("technical_handoff_done"):
            # Администратор уже позван на прошлом ходе: повторять то же самое
            # сообщение на каждое сообщение пациента — спам, а не помощь.
            return _no_reply(chat_id, session, result.skip_reason)
        session["technical_handoff_done"] = True
        return await _handoff(
            chat_id, session, _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK),
            result.skip_reason, human_owns=False,
        )

    session["openai_used"] = True
    session["openai_brain_used"] = True
    session["answer_source"] = "gpt_agent"
    session["agent_outcome"] = result.outcome
    session["agent_tool_calls"] = [call["tool"] for call in result.tool_calls]
    return await _finalize(chat_id, session, result.reply, result)

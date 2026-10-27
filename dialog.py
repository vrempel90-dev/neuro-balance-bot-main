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
from datetime import datetime, timezone
from typing import Any

import admission
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

OPERATOR_HANDOFF_RU = "Обращение передано сотруднику клиники 🌿"
OPERATOR_HANDOFF_KK = "Өтінішіңіз клиника қызметкеріне жіберілді 🌿"
HANDOFF_UNAVAILABLE_RU = "Сейчас не удалось обработать запрос. Пожалуйста, свяжитесь с клиникой по телефону из её официальных контактов."
HANDOFF_UNAVAILABLE_KK = "Қазір сұрауыңызды өңдеу мүмкін болмады. Клиниканың ресми байланыс нөміріне хабарласыңызшы."

EMERGENCY_103_RU = (
    "То, что Вы описываете, может быть неотложным состоянием. Пожалуйста, "
    "сразу позвоните 103 или обратитесь в скорую помощь 🌿"
)
EMERGENCY_103_KK = (
    "Сіз сипаттаған жағдай шұғыл көмекті қажет етуі мүмкін. Өтінемін, дереу "
    "103-ке қоңырау шалыңыз немесе жедел жәрдемге хабарласыңыз 🌿"
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

# Технические причины, по которым агент не отработал, но пациент всё равно
# должен получить ответ: ключа нет, бюджет исчерпан, сеть упала.
# Всё остальное — молчание: за диалогом уже следит человек.
SILENT_SKIP_REASONS = {"empty_text", "manual_takeover", "old_chat_ai_disabled"}

# no_reply_reason / first_touch_blocked_reason по состоянию лида. Значения —
# существующий контракт логов, дашборда и repair_service, поэтому не меняются.
# Значения crm_patient_state в сессии — существующий контракт отладочной
# выдачи main.py, поэтому имена состояний admission.py переводятся в них.
_CRM_PATIENT_STATE = {
    admission.NEW: "NEW_PATIENT",
    admission.RETURNING: "RETURNING_PATIENT_NO_ACTIVE_BOOKING",
    admission.ACTIVE_BOOKING: "ACTIVE_BOOKING",
    admission.CRM_UNAVAILABLE: "CRM_UNAVAILABLE",
}

_ADMISSION_MUTE_REASONS = {"old_lead_from_crm", "active_booking_old_lead", "crm_lookup_failed",
                           "returning_patient_old_lead"}

_LEAD_SILENCE_REASONS = {
    admission.RETURNING: ("old_lead_from_crm", "returning_patient_old_lead"),
    admission.ACTIVE_BOOKING: ("active_booking_old_lead", "active_booking_old_lead"),
    admission.CRM_UNAVAILABLE: ("crm_lookup_failed", "crm_lookup_failed"),
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


def _is_instagram_session(session: dict[str, Any]) -> bool:
    return "instagram" in str(
        session.get("chat_type") or session.get("inbound_channel") or ""
    ).strip().lower()


def _extract_kz_phone_from_text(text: str) -> str:
    """Extract an explicitly supplied KZ mobile number from a free-form reply."""
    raw = str(text or "")
    candidates = re.findall(r"(?:\+?7|8)[\d\s()\-]{9,20}", raw)
    for candidate in candidates:
        normalized = sanitize_kz_phone(candidate)
        if _valid_crm_phone(normalized):
            return normalized
    stripped = raw.strip()
    normalized = sanitize_kz_phone(stripped)
    return normalized if _valid_crm_phone(normalized) else ""


def _instagram_phone_request(session: dict[str, Any]) -> str:
    if str(session.get("language") or "ru") == "kk":
        return (
            "Жазылуды жалғастыру үшін, деректерді тексеруге және жазылуды рәсімдеуге "
            "болатын телефон нөміріңізді +7 7XX XXX XX XX форматында жазыңыз."
        )
    return (
        "Для продолжения записи напишите, пожалуйста, номер телефона, по которому "
        "можно проверить данные и оформить запись, в формате +7 7XX XXX XX XX."
    )


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

    # A persisted language lock must not override the patient switching languages.
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


def _store_crm_lookup_debug(session: dict[str, Any], lookup: dict[str, Any] | None) -> None:
    """Сырой ответ CRM в отладочную выдачу main.py.

    Классификацию делает admission.py, но при разборе инцидента «почему бот
    решил, что это старый лид» нужно видеть, что именно ответила CRM, а не
    только вывод.
    """
    raw = lookup.get("raw") if isinstance(lookup, dict) and isinstance(lookup.get("raw"), dict) else lookup
    raw = raw if isinstance(raw, dict) else {}
    lead = raw.get("lead") if isinstance(raw.get("lead"), dict) else None
    session["raw_crm_found"] = raw.get("found")
    session["raw_crm_isNew"] = raw.get("isNew")
    session["raw_crm_has_patient"] = isinstance(raw.get("patient"), dict)
    session["raw_crm_has_lead"] = bool(lead)
    session["raw_crm_lead_status"] = str((lead or {}).get("status") or "")
    session["raw_crm_has_lastAppointment"] = isinstance(raw.get("lastAppointment"), dict)
    session["raw_crm_hasActiveAppointment"] = raw.get("hasActiveAppointment") is True


def _activate_ai_admission_lease(session: dict[str, Any]) -> None:
    if not session.get("ai_admission_started_at"):
        session["ai_admission_started_at"] = datetime.now(timezone.utc).isoformat()
    session.pop("ai_admission_closed_at", None)


def _close_ai_admission_lease(session: dict[str, Any], reason: str) -> None:
    session["ai_admission_closed_at"] = datetime.now(timezone.utc).isoformat()
    session["ai_admission_closed_reason"] = str(reason or "terminal")


async def _classify_lead(chat_id: str, phone: str, session: dict[str, Any]) -> admission.Admission:
    """Спрашивает CRM про пациента и отдаёт классификацию admission.py.

    Сам запрос живёт здесь (ход диалога — место для ввода-вывода), а решение
    «новый / вернувшийся / записан / CRM молчит» принимает чистая функция,
    которую можно проверить тестом на каждый пограничный ответ CRM.
    """
    normalized = crm.normalize_phone(phone or session.get("phone") or "")
    session["phone_normalized"] = normalized
    session["crm_lookup_called"] = True
    session["crm_lookup_error"] = ""
    lookup: dict[str, Any] | None
    try:
        lookup = await crm.lookup_active_appointments_by_phone(normalized)
    except Exception as exc:
        session["crm_lookup_error"] = type(exc).__name__
        _safe_log(chat_id, "crm_lookup_failed", {"chat_id": chat_id, "error_type": type(exc).__name__})
        lookup = None

    verdict = admission.classify(normalized, lookup)
    # The fresh CRM verdict is authoritative on EVERY message. A local
    # admission timestamp cannot turn an existing or staff-owned lead into a
    # new patient; genuinely fresh CRM leads already classify as NEW.

    session["crm_patient_state"] = _CRM_PATIENT_STATE[verdict.state]
    session["crm_state_reason"] = verdict.reason
    _store_crm_lookup_debug(session, lookup)
    session["active_appointment_found"] = verdict.appointment is not None
    session["active_appointment_count"] = len([
        a for a in (lookup or {}).get("appointments") or [] if isinstance(a, dict)
    ]) if verdict.state == admission.ACTIVE_BOOKING else 0
    session["crm_patient_is_new"] = verdict.state == admission.NEW
    session["crm_patient_found"] = verdict.state in {admission.RETURNING, admission.ACTIVE_BOOKING}
    _safe_log(chat_id, "lead_admission", {"chat_id": chat_id, "state": verdict.state, "reason": verdict.reason})
    return verdict


async def _notify_admin_once(chat_id: str, session: dict[str, Any], phone: str, reason: str) -> bool:
    """Only an acknowledged CRM escalation counts as a delivered handoff.

    A failed request remains retryable on a later inbound turn. No callback or
    live operator is promised merely because we set a local session flag.
    """
    if session.get("operator_handoff_confirmed") is True:
        return True
    try:
        response = await crm.escalate_to_operator(phone=phone or session.get("phone") or "", reason=reason)
        if not isinstance(response, dict) or response.get("ok") is not True or response.get("error"):
            raise ValueError("unconfirmed_operator_handoff")
    except Exception as exc:
        session["operator_handoff_confirmed"] = False
        session["operator_handoff_error"] = type(exc).__name__
        _safe_log(chat_id, "admin_notify_failed", {"reason": reason, "error_type": type(exc).__name__})
        return False
    session["admin_notified_reason"] = reason
    session["operator_handoff_confirmed"] = True
    session.pop("operator_handoff_error", None)
    _safe_log(chat_id, "admin_notified", {"reason": reason})
    return True


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
    for option in session.get("doctor_options") or []:
        if isinstance(option, dict):
            _add(option.get("doctor_name"))
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


_DIAGNOSIS_TERMS = (
    r"остеохондроз|артрит|артроз|радикулит|ишиас|невралги[яи]|"
    r"грыж[ауыи]|протрузи[яи]|инсульт|инфаркт|тромбоз|диабет|"
    r"эпилепси[яи]|пневмони[яи]|диагноз|остеохондрозы|жарық"
)
_MEDICATION_TERMS = (
    r"препарат|таблет|капсул|обезболива|антибиотик|ибупрофен|"
    r"диклофенак|парацетамол|анальгин|кеторол|нимесулид|аспирин|"
    r"дәрі(?:ні|лер|сін)?|д[әа]рмек|\d+(?:[.,]\d+)?\s*(?:мг|мл|mg|ml)\b"
)


def _medical_advice_violation(answer: str) -> bool:
    """Block explicit diagnosis/prescription, not general registrar information.

    This intentionally narrow deterministic check complements the model's
    instructions. It is not a medical classifier and does not certify every
    possible clinical statement as safe.
    """
    low = str(answer or "").casefold()
    diagnosis = (
        r"(?:у\s+вас|у\s+пациента)\s+(?:(?:вероятно|точно|возможно|нет)\s+|"
        r"скорее\s+всего\s+|может\s+быть\s+)?(?:" + _DIAGNOSIS_TERMS + r")"
        r"|(?:ваш\s+диагноз|ставлю\s+(?:вам\s+)?диагноз|диагноз\s*[:—–])"
        r"|(?:сізде\s+(?:" + _DIAGNOSIS_TERMS + r")|сіздің\s+диагнозыңыз|"
        r"диагнозыңыз\s*[:—–]|диагноз\s+қоямын)"
    )
    if re.search(diagnosis, low):
        return True
    # A drug/dose plus a direct patient instruction is a prescription. General
    # descriptions such as 'врач назначит лечение' have no direct instruction.
    for sentence in re.split(r"[.!?\n]+", low):
        drug_or_dose = bool(re.search(_MEDICATION_TERMS, sentence))
        directive_text = re.sub(r"\bне\s+(?:принимайте|примите|пейте|попейте|назначаю|назначаем|начните|рекомендую)\b", " ", sentence)
        directive = bool(re.search(
            r"\b(?:принимайте|примите|пейте|попейте|назначаю|назначаем)\b|"
            r"(?:начните\s+принимать|рекомендую\s+(?:принимать|пить)|"
            r"вам\s+(?:нужно|следует)\s+(?:принимать|пить|колоть)|"
            r"қабылдаңыз|ішіңіз|ішініз|тағайындаймын|егіңіз)", directive_text))
        if drug_or_dose and directive:
            return True
    return False


def _unverified_fact(chat_id: str, session: dict[str, Any], answer: str, result: Any) -> str:
    """Возвращает факт из ответа, которого не было ни в одном результате инструмента."""
    low = str(answer or "").casefold()
    if _medical_advice_violation(answer):
        return "medical_diagnosis_or_prescription"
    if session.get("booking_uncertain") is True and session.get("booking_confirmed") is not True:
        return "booking_uncertain"
    confirmation = re.search(r"(?:запись\s+(?:успешно\s+)?(?:подтверждена|создана|оформлена)|вы\s+(?:успешно\s+)?записаны|\bзаписал[аи]?\b|жазылуыңыз\s+расталды|сізді\s+жаздым|жазып\s+қойдым|жазба\s+расталды)", low)
    confirmed = bool(getattr(result, "booked", False)) or (session.get("booking_confirmed") is True and bool(session.get("appointment_id") or session.get("crm_booking_id")))
    if confirmation and not confirmed:
        return "booking_confirmation"
    if re.search(r"(?:подключ(?:аю|у|им)\s+(?:вам\s+)?(?:администратора|оператора)|переда(?:ю|м)\s+.*(?:администратору|оператору)|әкімшіні\s+қосамын)", low) and session.get("operator_handoff_confirmed") is not True:
        return "handoff_unconfirmed"
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
    session["manual_takeover"] = lead_state == admission.CRM_UNAVAILABLE or bool(session.get("manual_takeover"))
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
    delivered = await _notify_admin_once(chat_id, session, str(session.get("phone") or ""), reason)
    session["escalated"] = True
    session["manual_takeover"] = (bool(human_owns) and delivered) or bool(session.get("manual_admin_intervention"))
    if reason != "red_flag":
        answer = _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK) if delivered else _tr(session, HANDOFF_UNAVAILABLE_RU, HANDOFF_UNAVAILABLE_KK)
        if session.get("booking_uncertain") is True and session.get("booking_confirmed") is not True:
            answer = _tr(session, "Статус Вашей записи пока не подтверждён. ", "Жазылуыңыздың мәртебесі әлі расталған жоқ. ") + answer
    session["answer_source"] = "python_template"
    session["skip_humanize"] = True
    session["handoff_reason"] = reason
    session["last_assistant_answer"] = answer
    _safe_log(chat_id, "handoff_to_operator", {"chat_id": chat_id, "reason": reason})
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
        if unverified == "medical_diagnosis_or_prescription":
            answer = _tr(session,
                "Я могу помочь с информацией о клинике и записью. Диагноз и лечение определяет врач на приёме.",
                "Мен клиника туралы ақпарат беріп, қабылдауға жазылуға көмектесе аламын. Диагнозды дәрігер қойып, емді қабылдауда тағайындайды.")
            session["answer_source"] = "medical_safety_template"
        else:
            return await _handoff(chat_id, session, _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK), "unverified_fact")

    # Bind only CRM slots actually named to the patient. Otherwise a time
    # shared by several doctors cannot be resolved safely on the next turn.
    agent._remember_presented_slots(session, answer)
    # A failed language-only correction must never leak a Russian response
    # into a Kazakh conversation (or vice versa). Verified booking facts can
    # use the deterministic bilingual confirmation without replaying tools.
    from language_guard import enforce_response_language, response_language_violation
    answer, language_status = enforce_response_language(answer, str(session.get("language") or "ru"))
    if response_language_violation(answer, str(session.get("language") or "ru")):
        if getattr(result, "booked", False):
            answer = agent._safety_net_reply(session, result)
        else:
            answer = _tr(session,
                "Не удалось подготовить корректный ответ. Пожалуйста, попробуйте написать позже.",
                "Дұрыс жауап дайындау мүмкін болмады. Кейінірек қайта жазыңызшы.")
        language_status = "unresolved_language_blocked"
    session["language_guard_result"] = language_status or "ok"
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
    incoming_phone = str(phone or "").strip()
    existing_phone = str(session.get("phone") or "").strip()
    session["transport_identity"] = incoming_phone or str(session.get("transport_identity") or "")
    # For Instagram Wazzup passes the username as chatId. Never overwrite a
    # phone the patient already supplied with that social identifier.
    if _valid_crm_phone(incoming_phone):
        session["phone"] = crm.normalize_phone(incoming_phone) or sanitize_kz_phone(incoming_phone)
    elif _is_instagram_session(session) and _valid_crm_phone(existing_phone):
        session["phone"] = existing_phone
    else:
        session["phone"] = ""
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
    for key in ("crm_lookup_called", "crm_lookup_error", "crm_patient_state", "crm_state_reason",
                "active_appointment_found", "active_appointment_count", "final_answer_preview",
                "raw_crm_found", "raw_crm_isNew", "raw_crm_has_patient", "raw_crm_has_lead",
                "raw_crm_lead_status", "raw_crm_has_lastAppointment", "raw_crm_hasActiveAppointment"):
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

    if _is_instagram_session(session):
        supplied_phone = _extract_kz_phone_from_text(text)
        if supplied_phone:
            session["phone"] = supplied_phone
            session["instagram_phone_verified_from_message"] = True
            session["instagram_identity_pending"] = False
            _safe_save(chat_id, session)
        if not _valid_crm_phone(session.get("phone") or ""):
            session["instagram_identity_pending"] = True
            session["answer_source"] = "instagram_identity_gate"
            session["skip_humanize"] = True
            return await _finalize(chat_id, session, _instagram_phone_request(session))

    effective_phone = str(session.get("phone") or phone or "").strip()
    if not _valid_crm_phone(effective_phone):
        return _no_reply(chat_id, session, "invalid_phone_for_crm_lookup")
    if _human_took_over(session):
        return _no_reply(chat_id, session, "manual_takeover")
    if session.get("booking_confirmed") is True:
        # A successfully booked patient is no longer a new lead. Do not depend
        # on CRM propagation timing to enforce NEW_LEADS_ONLY.
        session["ai_muted"] = True
        session["manual_takeover"] = True
        _close_ai_admission_lease(session, "booking_already_completed")
        return _no_reply(chat_id, session, "booking_completed_ai_disabled")

    verdict = await _classify_lead(chat_id, effective_phone, session)
    if not verdict.bot_may_reply:
        # Не новый лид (или CRM молчит и мы не знаем, кто это) — fail-closed.
        return await _mute_lead(chat_id, session, effective_phone, verdict.state)

    if _admission_muted(session):
        _clear_admission_mute(session)
    session["first_touch_allowed"] = True
    session["ai_lead_started"] = True
    _activate_ai_admission_lease(session)
    session["gate_reason"] = "new_lead"
    try:
        result = await agent.run_agent_turn(
            chat_id=chat_id,
            phone=effective_phone,
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

    if result.escalate:
        # Если ход завершён передачей человеку, AI больше не должен продолжать
        # этот диалог на следующем сообщении.
        session["manual_takeover"] = True
        session["escalated"] = True
        _close_ai_admission_lease(session, "agent_escalation")
        delivered = await _notify_admin_once(chat_id, session, effective_phone, "agent_escalation")
        session["manual_takeover"] = delivered
        result.reply = _tr(session, OPERATOR_HANDOFF_RU, OPERATOR_HANDOFF_KK) if delivered else _tr(session, HANDOFF_UNAVAILABLE_RU, HANDOFF_UNAVAILABLE_KK)
    elif result.booked:
        _close_ai_admission_lease(session, "booking_confirmed")
    session["openai_used"] = True
    session["openai_brain_used"] = True
    session["answer_source"] = "gpt_agent"
    session["agent_outcome"] = result.outcome
    session["agent_tool_calls"] = [call["tool"] for call in result.tool_calls]
    return await _finalize(chat_id, session, result.reply, result)

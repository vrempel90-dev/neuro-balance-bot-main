
from __future__ import annotations

import re
from typing import Any

from language_guard import detect_language


GENERIC_BOOKING_WORDS = [
    "хочу записаться", "записаться", "запишите", "прием", "приём",
    "консультац", "по акции", "акция", "50%", "скидк", "instagram", "инстаграм",
]
COMPLAINT_WORDS = [
    "болит", "боль", "ноет", "тянет", "ломит", "отдает", "отдаёт", "онемение",
    "немеет", "спина", "шея", "поясница", "сустав", "колено", "плечо", "нога",
    "рука", "грыжа", "протруз", "артроз", "артрит", "ауырады", "ауыр", "бел",
    "мойын", "буын", "аяқ", "қол", "омыртқа", "жарық",
]
BAD_EMOJIS = ["😅", "🤣", "💪", "🎯", "⚡"]
BAD_COORDINATOR_FALLBACKS = [
    "передам ваш вопрос координатору",
    "передам вопрос координатору",
    "она ответит вам в ближайшее время",
    "передам координатору",
]
BAD_NO_ACTIVE_BOOKING_PHRASES = [
    "у вас нет активной записи",
    "нет активной записи",
    "хотите записаться на диагностику",
]
BAD_REFUSAL_ON_COMPLAINT_PHRASES = [
    "не могу записать вас на консультацию",
    "не можем записать вас на консультацию",
    "рекомендуем обратиться на очную консультацию к врачу",
]
KZ_UNIQUE_RE = re.compile(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]")
FORBIDDEN_NAME_WORDS = {
    "приду", "буду", "да", "ок", "окей", "хорошо", "жарайды", "келемін",
    "келемин", "барамын", "нет", "жоқ", "завтра", "сегодня",
}


def _low(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def has_booking_intent(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in GENERIC_BOOKING_WORDS)


def has_complaint(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in COMPLAINT_WORDS)


def _remove_forbidden_name_addressing(answer: str, session: dict[str, Any]) -> str:
    """Убирает полное обращение по имени.

    В старом промпте было «Здравствуйте, {Имя}!», но в нашем рабочем сценарии
    это ломало WhatsApp-диалоги и выглядело неестественно. Поэтому guard
    удаляет обращение по имени на уровне финального ответа.
    """
    names: list[str] = []
    for key in ("patient_name", "name"):
        val = str(session.get(key) or "").strip()
        if val and len(val) <= 60:
            names.append(val)

    lookup = session.get("patient_lookup") or {}
    if isinstance(lookup, dict):
        patient = lookup.get("patient") or {}
        if isinstance(patient, dict):
            val = str(patient.get("name") or "").strip()
            if val and len(val) <= 60:
                names.append(val)

    out = answer
    for name in set(names):
        if not name:
            continue
        first = re.escape(name.split()[0])
        full = re.escape(name)
        # Здравствуйте, Виктор! / Добрый день, Виктор Ремпель!
        out = re.sub(rf"\b(Здравствуйте|Добрый день|Доброе утро|Добрый вечер),?\s+({full}|{first})[!\.,]?", r"\1!", out, flags=re.I)
        # Виктор, подскажите...
        out = re.sub(rf"^\s*({full}|{first}),\s*", "", out, flags=re.I)
    return out


def _fix_bad_name_from_confirmation(answer: str) -> str:
    # Убираем «Очень приятно, Приду» и похожее.
    for word in FORBIDDEN_NAME_WORDS:
        answer = re.sub(rf"Очень приятно,?\s+{re.escape(word)}[!\.]?", "Хорошо 🌿", answer, flags=re.I)
        answer = re.sub(rf"Рад[аы] познакомиться,?\s+{re.escape(word)}[!\.]?", "Хорошо 🌿", answer, flags=re.I)
    return answer


def _default_booking_start(lang: str) -> str:
    if lang == "kk":
        return "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿\nАйтыңызшы, Сізді не мазалайды?"
    return "Здравствуйте! Да, можно записаться на консультацию по акции 🌿\nПодскажите, пожалуйста, что Вас беспокоит?"


def validate_answer(chat_id: str, user_text: str, answer: str, session: dict[str, Any]) -> tuple[str, list[str]]:
    """Финальная проверка ответа перед отправкой пациенту.

    Это не заменяет промпт. Это ремень безопасности:
    - не даём GPT уйти в «координатора» на обычную запись;
    - не даём спрашивать имя первым;
    - не даём принять «хочу записаться» за жалобу;
    - не даём обращаться по имени;
    - чистим запрещённые эмодзи и лексику.
    """
    violations: list[str] = []
    lang = session.get("language") or "ru"
    user_low = _low(user_text)
    ans_low = _low(answer)
    user_lang = detect_language(user_text, session.get("language") or "ru")

    # v28: если пациент пишет по-русски, финальный ответ не должен внезапно
    # переходить на казахский. Для опасных автопереключений отдаём безопасный RU-ответ.
    if user_lang == "ru" and KZ_UNIQUE_RE.search(answer):
        if has_booking_intent(user_text):
            violations.append("wrong_language_kk_on_ru_booking")
            return _default_booking_start("ru"), violations
        if "адрес" in _low(user_text) or "где" in _low(user_text) or "астане" in _low(user_text):
            violations.append("wrong_language_kk_on_ru_address")
            return (
                "📍 Адрес: Кабанбай батыра 28, внутренний двор, подъезд 3, Астана.\n"
                "Заезд со стороны Кунаева, после ворот повернуть направо."
            ), violations

        violations.append("wrong_language_kk_on_ru_general")
        if session.get("last_slots") and not session.get("selected_time"):
            return "Какое время из вариантов выше Вам удобно?", violations
        if not session.get("complaint"):
            return "Подскажите, пожалуйста, что Вас беспокоит? 🌿", violations
        if not session.get("age"):
            return "Подскажите, пожалуйста, сколько Вам лет?", violations

    # v28: на обычную заявку нельзя отвечать «у вас нет активной записи».
    # Это внутренний факт CRM, пациент просит НОВУЮ запись — начинаем анкету.
    if has_booking_intent(user_text) and any(bad in ans_low for bad in BAD_NO_ACTIVE_BOOKING_PHRASES):
        violations.append("no_active_record_leaked_on_booking")
        return _default_booking_start("ru" if user_lang == "ru" else user_lang), violations

    # v29: грыжа/протрузия/боль/после перелома — это жалоба, а не повод для отказа.
    # На такие сообщения бот должен вести к возрасту/записи, а не отправлять к стороннему врачу.
    if has_complaint(user_text) and any(bad in ans_low for bad in BAD_REFUSAL_ON_COMPLAINT_PHRASES):
        violations.append("wrong_refusal_on_treatable_complaint")
        return (
            "Понимаю Вас 🙏🏻 С такой жалобой можно прийти на первичную консультацию. "
            "Врач осмотрит и подскажет дальнейший план.\n"
            "Подскажите, пожалуйста, сколько Вам лет?"
        ), violations

    # 1) Обычная заявка на запись без жалобы не должна уходить координатору,
    # не должна спрашивать возраст/имя и не должна говорить «с такой жалобой».
    if has_booking_intent(user_text) and not has_complaint(user_text):
        if any(bad in ans_low for bad in BAD_COORDINATOR_FALLBACKS):
            violations.append("booking_fell_to_coordinator")
        if "с такой жалобой" in ans_low:
            violations.append("booking_treated_as_complaint")
        if "сколько вам лет" in ans_low or "жасыңыз" in ans_low:
            violations.append("age_asked_before_complaint")
        if "как вас зовут" in ans_low or "ваше имя" in ans_low or "имя для записи" in ans_low:
            violations.append("name_asked_too_early")
        if violations:
            return _default_booking_start(lang), violations

    # 2) Нельзя спрашивать имя до выбранного времени и противопоказаний.
    has_selected_time = bool(session.get("selected_time"))
    contra_ok = session.get("contraindications_verdict") == "proceed" or session.get("contraindications_ok") is True
    if not (has_selected_time and contra_ok):
        if "имя для записи" in ans_low or "как вас зовут" in ans_low or "ваше имя" in ans_low:
            violations.append("name_before_required_gates")
            # Безопаснее вернуть следующий недостающий шаг.
            if not session.get("complaint"):
                return _default_booking_start(lang), violations
            if not session.get("age"):
                return ("Подскажите, пожалуйста, сколько Вам лет?" if lang != "kk" else "Жасыңыз нешеде?"), violations
            if not session.get("preferred_date"):
                return ("На какой день Вам удобно прийти?" if lang != "kk" else "Қай күн ыңғайлы?"), violations
            if not has_selected_time:
                return ("Выберите, пожалуйста, одно из предложенных времён выше 🌿" if lang != "kk" else "Жоғарыдағы уақыттардың бірін таңдаңызшы 🌿"), violations
            return ("Перед записью уточню: есть ли у Вас противопоказания?" if lang != "kk" else "Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"), violations

    # v30: пациенту нельзя показывать внутреннее слово «Резерв».
    if "резерв" in ans_low or "reserve" in ans_low:
        answer = re.sub(r"(?i)\breserve\b", "дежурный врач", answer)
        answer = re.sub(r"(?i)\bрезерв\b", "дежурный врач", answer)
        violations.append("reserve_word_hidden")
        ans_low = _low(answer)

    # 3) Убираем запрещённые эмодзи.
    for emoji in BAD_EMOJIS:
        if emoji in answer:
            answer = answer.replace(emoji, "")
            violations.append("bad_emoji_removed")

    # 4) Нормализуем лексику — у нас в текущей версии принято не «окошки», а «свободное время».
    if "окошк" in ans_low:
        answer = answer.replace("окошки", "свободное время").replace("Окошки", "Свободное время")
        answer = answer.replace("окошко", "свободное время").replace("Окошко", "Свободное время")
        violations.append("okoshki_replaced")

    # 5) Убираем обращения по имени и ошибки имени.
    before = answer
    answer = _remove_forbidden_name_addressing(answer, session)
    answer = _fix_bad_name_from_confirmation(answer)
    if answer != before:
        violations.append("name_addressing_removed")

    # 6) Если GPT всё равно выдал координатора на обычную заявку — переписываем.
    if has_booking_intent(user_text) and any(bad in _low(answer) for bad in BAD_COORDINATOR_FALLBACKS):
        violations.append("coordinator_fallback_rewritten")
        return _default_booking_start(lang), violations

    return answer.strip(), violations

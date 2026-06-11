from __future__ import annotations

import re
from typing import Any


# ============================================================
# Neuro Balance strict prompt guard
# Финальный фильтр перед отправкой ответа пациенту.
# Цель: не дать боту уйти от промпта, обратиться по имени,
# написать лишнюю рекламу или длинную отсебятину.
# ============================================================

FORBIDDEN_PATTERNS = [
    r"(?im)^\s*Здравствуйте,\s*[^!\n]{2,80}!\s*",
    r"(?im)^\s*Добрый день,\s*[^!\n]{2,80}!\s*",
    r"(?im)^\s*Доброе утро,\s*[^!\n]{2,80}!\s*",
    r"(?im)^\s*Добрый вечер,\s*[^!\n]{2,80}!\s*",
    r"(?im)^\s*Уважаемый\(-ая\)\s*[^!\n]{1,80}!\s*",
    r"(?im)^\s*Уважаемый\s*[^!\n]{1,80}!\s*",
    r"(?im)^\s*Уважаемая\s*[^!\n]{1,80}!\s*",
    r"(?im)^\s*Спасибо,\s*[^!\n.]{2,80}[.!]?\s*",
    r"(?i)как\s+могу\s+к\s+вам\s+обращаться\??",
    r"(?i)как\s+вас\s+зовут\??",
    r"(?i)очень\s+приятно[^.!?\n]*[.!?]?",
    r"(?i)меня\s+зовут\s+администратор[^.!?\n]*[.!]?",
]

FORBIDDEN_EXTRA_TEXT = [
    "гарантируем результат",
    "обязательно вылечим",
    "лучшие специалисты",
    "индивидуальный подход к каждому",
    "мы команда профессионалов",
    "не переживайте, мы вас вылечим",
    "у нас лучшие врачи",
    "самые лучшие врачи",
]

# Слова, которые пациент может написать как согласие/подтверждение.
# Их нельзя считать именем.
NOT_A_NAME_WORDS = {
    "приду", "буду", "да", "ок", "окей", "хорошо", "завтра", "сегодня",
    "подтверждаю", "не знаю", "спасибо", "понял", "поняла",
    "ертең", "ертен", "бүгін", "бугин", "жоқ", "жок", "ия", "иә",
    "жарайды", "жаксы", "жақсы", "келемін", "келемин", "барамын",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _low(text: str) -> str:
    return _clean(text).lower().replace("ё", "е")


def _lang(session: dict[str, Any] | None) -> str:
    if isinstance(session, dict) and session.get("language") in ("ru", "kk"):
        return str(session.get("language"))
    return "ru"


def _fallback(session: dict[str, Any] | None) -> str:
    return "Рақмет, қабылданды 🌿" if _lang(session) == "kk" else "Спасибо, принято 🌿"


def _remove_name_addressing(answer: str, session: dict[str, Any] | None = None) -> str:
    """Удаляет любые обращения по имени/нику/контакту в начале строк."""
    if not answer:
        return answer

    cleaned = str(answer)

    # Универсальные обращения: "Здравствуйте, Айжан!", "Уважаемый(-ая) Съемка!".
    for pattern in FORBIDDEN_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned).strip()

    session = session or {}
    possible_names = [
        session.get("patient_name"),
        session.get("name"),
        session.get("client_name"),
        session.get("patientName"),
        session.get("contact_name"),
        session.get("contactName"),
        session.get("wazzup_name"),
        session.get("wazzupName"),
    ]

    for name in possible_names:
        if not name:
            continue
        n = str(name).strip()
        if not n or _low(n) in NOT_A_NAME_WORDS:
            continue

        cleaned = re.sub(rf"(?im)^\s*{re.escape(n)}\s*,\s*", "", cleaned)
        cleaned = re.sub(rf"(?im)Здравствуйте,\s*{re.escape(n)}!", "Здравствуйте!", cleaned)
        cleaned = re.sub(rf"(?im)Добрый день,\s*{re.escape(n)}!", "Добрый день!", cleaned)
        cleaned = re.sub(rf"(?im)Доброе утро,\s*{re.escape(n)}!", "Доброе утро!", cleaned)
        cleaned = re.sub(rf"(?im)Добрый вечер,\s*{re.escape(n)}!", "Добрый вечер!", cleaned)
        cleaned = re.sub(rf"(?im)Спасибо,\s*{re.escape(n)}[.!]?", "Спасибо.", cleaned)
        cleaned = re.sub(rf"(?im)Уважаемый\(-ая\)\s*{re.escape(n)}!", "", cleaned)

    return cleaned.strip()


def _remove_extra_text(answer: str) -> str:
    if not answer:
        return answer

    cleaned = answer
    for fragment in FORBIDDEN_EXTRA_TEXT:
        cleaned = re.sub(re.escape(fragment), "", cleaned, flags=re.I)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _allow_long_answer(answer: str) -> bool:
    low = _low(answer)
    allowed_markers = [
        "перед записью мне нужно уточнить",
        "перед записью нужно подтвердить",
        "кардиостимулятор",
        "противопоказан",
        "ваш визит в neuro balance",
        "запись подтверждена",
        "neuro balance қабылдауы",
        "қабылдауы",
        "мекенжай",
        "📅",
        "⏰",
        "📍",
    ]
    return any(marker in low for marker in allowed_markers)


def _limit_length(answer: str) -> str:
    """Обычные ответы режем до 1-2 предложений или максимум 3 блоков."""
    if not answer:
        return answer

    if _allow_long_answer(answer):
        return answer.strip()

    blocks = [b.strip() for b in answer.split("---") if b.strip()]
    if len(blocks) > 3:
        blocks = blocks[:3]
    if blocks:
        answer = "\n---\n".join(blocks)

    # Если нет дробления на блоки и текст длинный — оставляем первые 2 предложения.
    if "---" not in answer:
        sentences = re.split(r"(?<=[.!?])\s+", answer.strip())
        if len(sentences) > 2:
            answer = " ".join(sentences[:2]).strip()

    return answer.strip()


def _normalize_empty_lines(answer: str) -> str:
    answer = re.sub(r"[ \t]+\n", "\n", answer or "")
    answer = re.sub(r"\n{3,}", "\n\n", answer)
    return answer.strip()


def enforce_prompt_only(answer: str, session: dict[str, Any] | None = None) -> str:
    """Главная функция.

    Вызывать перед отправкой любого сообщения пациенту:
        answer = enforce_prompt_only(answer, session)
        await wazzup.send_message(chat_id, answer)
    """
    if not answer:
        return "Сізді не мазалайды? 🌿" if _lang(session) == "kk" else "Подскажите, пожалуйста, что Вас беспокоит? 🌿"

    cleaned = str(answer).strip()
    cleaned = _remove_name_addressing(cleaned, session)
    cleaned = _remove_extra_text(cleaned)
    cleaned = _limit_length(cleaned)
    cleaned = _normalize_empty_lines(cleaned)

    return cleaned or _fallback(session)


# Backward-compatible alias, если в коде удобнее назвать sanitize.
def sanitize_patient_message(answer: str, session: dict[str, Any] | None = None) -> str:
    return enforce_prompt_only(answer, session)

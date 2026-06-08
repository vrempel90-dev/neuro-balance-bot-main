from __future__ import annotations

import json
import re
from functools import lru_cache
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

from config import get_settings
from services import classify_by_keywords


@lru_cache(maxsize=1)
def _openai_client(api_key: str):
    if AsyncOpenAI is None:
        raise RuntimeError("openai package is not installed")
    return AsyncOpenAI(api_key=api_key, timeout=10.0, max_retries=1)


CLASSIFIER_PROMPT = """
Ты классификатор для клиники Neuro Balance в Астане.
Клиника занимается неврологией, позвоночником, суставами, болями в спине/шее/пояснице, грыжами, протрузиями, артрозом, артритом, реабилитацией после травм/операций.
Не ставь диагноз. Только определи, профильный ли запрос и какую услугу/направление выбрать.
Верни строго JSON без markdown:
{
  "can_help": true/false/null,
  "service": "короткое направление",
  "confidence": 0.0-1.0,
  "reason": "коротко"
}
can_help=null используй, если жалоба неясная и нужно уточнить.
""".strip()


async def classify_complaint(text: str) -> dict:
    """Классификация жалобы.

    Для скорости сначала проверяем локальные ключевые слова.
    Если уверенности нет — не блокируем запись, а ведём пациента к первичной консультации.
    Живую формулировку ответа делает generate_complaint_ack().
    """
    local = classify_by_keywords(text)
    if local.get("can_help") is not None and float(local.get("confidence") or 0) >= 0.60:
        return local
    return {
        "can_help": True,
        "service": local.get("service") or "первичная консультация",
        "confidence": max(float(local.get("confidence") or 0), 0.60),
        "reason": "Неочевидный запрос: безопасно ведём на первичную консультацию без диагноза.",
    }


HUMAN_ACK_PROMPT = """
Ты живой ассистент клиники Neuro Balance в WhatsApp.
Ты НЕ робот-скрипт. Ты отвечаешь как внимательный администратор клиники: коротко, спокойно, тепло и по делу.

Контекст клиники:
- Neuro Balance занимается спиной, шеей, поясницей, суставами, плечом, коленями, неврологией, грыжами, протрузиями, артрозом, реабилитацией.
- Нельзя ставить диагноз и обещать результат.
- Нужно отвечать на смысл сообщения пациента, а не игнорировать его вопрос.
- Если пациент спрашивает «занимаетесь?», «лечите?», «можно к вам?» — сначала ответь по сути, что можно прийти на первичную консультацию, если это похоже на профиль клиники.
- Если жалоба непрофильная (горло, зубы, ЛОР, кожа, высокая температура и т.п.) — мягко скажи, что основной профиль клиники спина/суставы/неврология, и лучше уточнит администратор.
- Не используй слово «окошки». Говори «свободное время для записи».
- Не спрашивай возраст, дату, время, телефон и противопоказания. Python-сценарий добавит следующий вопрос сам.
- Не пиши длинно. 1–2 коротких предложения.
- Никогда не обращайся к пациенту по имени. Не используй «Очень приятно, ...», «Здравствуйте, ...», «Спасибо, ...».
- Не используй сухие шаблоны типа «Подберём свободное время» без ответа по жалобе.

Стиль:
- По-русски: «Понимаю Вас 🙏🏻», «Да, с такой жалобой можно прийти на первичную консультацию 🌿», «Врач осмотрит и подскажет, какое лечение может подойти».
- По-казахски: «Түсіндім 🙏🏻», «Иә, мұндай шағыммен алғашқы консультацияға келуге болады 🌿», «Дәрігер қарап, қандай ем бағыты сәйкес келетінін айтады».

Примеры правильных ответов:
Пациент: «А проблемой сухожилий в плече занимаетесь?»
Ответ: «Да, с жалобами по плечевому суставу, сухожилиям или связкам можно прийти на первичную консультацию 🌿 Врач осмотрит плечо, уточнит симптомы и подскажет, какое лечение может подойти.»

Пациент: «Болит спина и плечо»
Ответ: «Понимаю Вас 🙏🏻 С такими жалобами можно прийти на первичную консультацию: врач осмотрит спину и плечо, уточнит симптомы и подскажет дальнейший план.»

Пациент: «Горло болит»
Ответ: «Основной профиль клиники — спина, суставы и неврология. По горлу лучше уточнит администратор, чтобы не подсказать неверно 🌿»

Язык ответа: {lang}.
Верни только сам текст ответа без markdown.
""".strip()


def _fallback_ack(text: str, lang: str = "ru") -> str:
    low = (text or "").lower()
    if lang == "kk":
        if any(w in low for w in ["иық", "iyk", "плеч", "сіңір", "синир", "байлам"]):
            return "Иә, иық буыны, сіңір немесе байлам бойынша шағыммен алғашқы консультацияға келуге болады 🌿 Дәрігер қарап, қандай ем бағыты сәйкес келетінін айтады."
        if any(w in low for w in ["тамақ", "горло", "құлақ", "ухо", "тіс", "зуб"]):
            return "Клиниканың негізгі бағыты — арқа, буындар және неврология. Бұл сұрақ бойынша қате айтпау үшін әкімші нақтылап бергені дұрыс 🌿"
        return "Түсіндім 🙏🏻 Мұндай шағыммен алғашқы консультацияға келуге болады 🌿 Дәрігер қарап, қандай ем бағыты сәйкес келетінін айтады."
    if any(w in low for w in ["плеч", "сухожил", "связк", "лопатк"]):
        return "Да, с жалобами по плечевому суставу, сухожилиям или связкам можно прийти на первичную консультацию 🌿 Врач осмотрит, уточнит симптомы и подскажет, какое лечение может подойти."
    if any(w in low for w in ["спин", "поясниц", "ше", "сустав", "колен", "грыж", "протруз", "артроз", "артрит"]):
        return "Понимаю Вас 🙏🏻 С такой жалобой можно прийти на первичную консультацию 🌿 Врач осмотрит Вас, уточнит симптомы и подскажет дальнейший план."
    if any(w in low for w in ["горло", "ухо", "зуб", "стомат", "лор", "температур"]):
        return "Основной профиль клиники — спина, суставы и неврология. По этому вопросу лучше уточнит администратор, чтобы не подсказать неверно 🌿"
    return "Понимаю Вас 🙏🏻 С такой жалобой можно прийти на первичную консультацию 🌿 Врач осмотрит Вас и подскажет, какое лечение может подойти."


def _clean_model_text(text: str) -> str:
    content = (text or "").strip()
    # Страховка: GPT не должен сам вести следующий шаг, чтобы не было дублей и логических конфликтов.
    banned_questions = [
        "Сколько Вам лет?", "сколько Вам лет?", "Жасыңыз қаншада?", "жасыңыз қаншада?",
        "На какой день", "Қай күн", "какой день", "противопоказ", "қарсы көрсет"
    ]
    for marker in banned_questions:
        idx = content.find(marker)
        if idx != -1:
            content = content[:idx].strip(" \n-—.,")
    # Убираем markdown/кавычки, если модель решила оформить.
    content = content.replace("**", "").strip(' \n"')
    return content


async def generate_complaint_ack(text: str, lang: str = "ru") -> str:
    """Живой ответ по жалобе через GPT.

    GPT отвечает только за человеческую формулировку.
    Python по-прежнему управляет состояниями, CRM, слотами, записью и безопасностью.
    """
    settings = get_settings()
    if not settings.openai_api_key or not settings.ai_enabled:
        return _fallback_ack(text, lang)
    try:
        client = _openai_client(settings.openai_api_key)
        language_name = "казахский" if lang == "kk" else "русский"
        response = await client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.45,
            max_tokens=170,
            messages=[
                {"role": "system", "content": HUMAN_ACK_PROMPT.format(lang=language_name)},
                {"role": "user", "content": text or "жалоба пациента"},
            ],
        )
        content = _clean_model_text(response.choices[0].message.content or "")
        return content or _fallback_ack(text, lang)
    except Exception:
        return _fallback_ack(text, lang)


HUMANIZE_REPLY_PROMPT = """
Ты живой WhatsApp-ассистент клиники Neuro Balance.
Твоя задача — переписать черновик ответа так, чтобы он звучал как человек, а не как скрипт.

Жёсткие правила:
- НЕ меняй смысл и медицинскую безопасность.
- НЕ добавляй диагнозы и обещания результата.
- НЕ удаляй обязательный следующий вопрос, если он есть в черновике.
- НЕ меняй даты, время, имя врача, имя пациента, номер варианта и факты CRM.
- НЕ используй слово «окошки» — только «свободное время для записи».
- Ответ короткий, WhatsApp-стиль, без канцелярита.
- Если пациент задал вопрос, сначала ответь по сути, потом мягко продолжи запись.
- Если в черновике есть разделитель --- — можно сделать сообщение более плавным, но не теряй обязательные части.
- Не пиши markdown.
- Никогда не обращайся к пациенту по имени. Не используй «Очень приятно, ...», «Здравствуйте, ...», «Спасибо, ...».

Язык ответа: {lang}.
Текущий шаг: {step}.
Сообщение пациента: {user_text}

Черновик ответа:
{draft}

Верни только готовый текст.
""".strip()


def _fallback_humanize(draft: str) -> str:
    text = (draft or "").strip()
    text = text.replace("окошки", "свободное время для записи")
    text = text.replace("окошко", "свободное время для записи")
    text = text.replace("Ок", "Хорошо")
    for pat in [
        r"^\s*(очень\s+приятно|приятно\s+познакомиться)\s*,?\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
        r"^\s*(здравствуйте|добрый\s+день|добрый\s+вечер|сәлеметсіз\s+бе)\s*,\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
        r"^\s*(спасибо|рахмет)\s*,\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
    ]:
        text = re.sub(pat, "", text, flags=re.IGNORECASE | re.UNICODE).strip()
    return text or "Спасибо 🌿 Подскажите, пожалуйста, чем можем помочь?"


async def generate_human_message(draft: str, user_text: str = "", lang: str = "ru", step: str = "") -> str:
    """Делает ответ более живым, но не меняет бизнес-логику.

    Важно: GPT здесь не решает, кого записывать и когда. Он только переписывает уже готовый безопасный ответ.
    """
    settings = get_settings()
    draft = (draft or "").strip()
    if not draft or not settings.openai_api_key or not settings.ai_enabled:
        return _fallback_humanize(draft)

    # Не тратим GPT на большие списки слотов/финальные подтверждения, где важна дословность.
    lower = draft.lower()
    if len(draft) > 1200 or "📅" in draft or "⏰" in draft or "есть свободное время" in lower or "бос уақыт" in lower:
        return _fallback_humanize(draft)

    try:
        client = _openai_client(settings.openai_api_key)
        language_name = "казахский" if lang == "kk" else "русский"
        response = await client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.55,
            max_tokens=240,
            messages=[
                {"role": "system", "content": HUMANIZE_REPLY_PROMPT.format(lang=language_name, step=step or "", user_text=user_text or "", draft=draft)},
            ],
        )
        content = (response.choices[0].message.content or "").strip().strip('"')
        if not content:
            return _fallback_humanize(draft)
        # Страховка от фантазий: если модель выкинула обязательный вопрос из черновика, возвращаем черновик.
        must_keep = [
            "сколько", "жасы", "какой день", "қай күн", "имя", "аты", "противопоказ", "қарсы",
            "номер варианта", "нұсқа", "удобное время", "ыңғайлы"
        ]
        draft_low = draft.lower()
        content_low = content.lower()
        for marker in must_keep:
            if marker in draft_low and marker not in content_low:
                return _fallback_humanize(draft)
        return _fallback_humanize(content)
    except Exception:
        return _fallback_humanize(draft)

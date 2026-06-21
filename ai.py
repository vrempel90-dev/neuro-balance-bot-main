from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

from config import get_settings

try:
    import state
except Exception:
    state = None
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


async def generate_complaint_ack(text: str, lang: str = "ru", chat_id: str = "") -> str:
    """Живой ответ по жалобе через GPT.

    GPT отвечает только за человеческую формулировку.
    Python по-прежнему управляет состояниями, CRM, слотами, записью и безопасностью.
    """
    settings = get_settings()
    if not settings.openai_api_key or not settings.ai_enabled:
        if state is not None:
            state.log_event(chat_id or "system", "openai_skipped", {"chat_id": chat_id, "reason": "disabled_or_missing_api_key"})
        return _fallback_ack(text, lang)
    try:
        if state is not None:
            state.log_event(chat_id or "system", "openai_called", {"chat_id": chat_id, "model": settings.openai_model, "purpose": "complaint_ack"})
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


async def generate_human_message(draft: str, user_text: str = "", lang: str = "ru", step: str = "", chat_id: str = "") -> str:
    """Делает ответ более живым, но не меняет бизнес-логику.

    Важно: GPT здесь не решает, кого записывать и когда. Он только переписывает уже готовый безопасный ответ.
    """
    settings = get_settings()
    draft = (draft or "").strip()
    if not draft or not settings.openai_api_key or not settings.ai_enabled:
        if state is not None:
            reason = "empty_draft" if not draft else "disabled_or_missing_api_key"
            state.log_event(chat_id or "system", "openai_skipped", {"chat_id": chat_id, "reason": reason})
        return _fallback_humanize(draft)

    # Не тратим GPT на большие списки слотов/финальные подтверждения, где важна дословность.
    lower = draft.lower()
    if len(draft) > 1200 or "📅" in draft or "⏰" in draft or "есть свободное время" in lower or "бос уақыт" in lower:
        if state is not None:
            state.log_event(chat_id or "system", "openai_skipped", {"chat_id": chat_id, "reason": "deterministic_reply"})
        return _fallback_humanize(draft)

    try:
        if state is not None:
            state.log_event(chat_id or "system", "openai_called", {"chat_id": chat_id, "model": settings.openai_model, "purpose": "humanize_reply"})
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


STRICT_HUMANIZE_REPLY_PROMPT = """
Ты — живой администратор клиники Neuro Balance в WhatsApp.

Твоя задача — только переформулировать готовый безопасный ответ клиники более живо и по-человечески.

Нельзя менять смысл ответа.
Нельзя менять этап диалога.
Нельзя добавлять новые вопросы, кроме тех, которые уже есть в base_answer.
Нельзя спрашивать имя, если base_answer не спрашивает имя.
Нельзя предлагать дату или время, если base_answer этого не делает.
Нельзя убирать вопрос про возраст, если он есть в base_answer.
Нельзя убирать вопрос про противопоказания, если он есть в base_answer.
Нельзя удалять пункты противопоказаний.
Нельзя обещать лечение, результат, гарантию или точную стоимость курса.
Нельзя говорить, что вылечим.
Нельзя ставить диагноз.
Нельзя советовать медицинское лечение.
Нельзя отвечать на старые записи/возвраты/жалобы — если base_answer пустой, верни пусто.

Пиши коротко, как администратор в WhatsApp.
Стиль: спокойно, заботливо, без канцелярита.
Можно использовать 1 emoji 🌿, но не больше.
Не используй фразы:
- “С такими жалобами к нам обращаются”
- “это наша специализация” слишком часто
- “Вижу Ваш запрос”
- “передам врачу” без причины

Верни только итоговый текст ответа, без комментариев.
""".strip()


def _has_age_question(text: str) -> bool:
    low = _low_for_guard(text)
    return any(x in low for x in ["сколько вам лет", "сколько вам полных лет", "ваш возраст", "жасыңыз", "жасыныз", "қаншада", "каншада"])


def _has_name_question(text: str) -> bool:
    low = _low_for_guard(text)
    return any(x in low for x in ["ваше имя", "как вас зовут", "имя для записи", "атыңыз", "атыныз", "есіміңіз", "есиминиз"])


def _has_date_question(text: str) -> bool:
    low = _low_for_guard(text)
    return any(x in low for x in ["на какой день", "когда удобно", "какая дата", "қай күн", "кай кун", "қашан ыңғайлы", "кашан ынгайлы"])


def _has_contra_question(text: str) -> bool:
    low = _low_for_guard(text)
    return "противопоказаний нет" in low or "қарсы көрсетілім" in low or "карсы корсет" in low


def _low_for_guard(text: str) -> str:
    return (text or "").replace("ё", "е").lower()


def _contra_markers(text: str) -> set[str]:
    low = _low_for_guard(text)
    markers = {
        "кардиостимулятор": ["кардиостимулятор"],
        "онкология": ["онколог", "онко"],
        "беременность": ["беремен", "жүкт", "жукт"],
        "острые инфекции": ["остр", "инфек", "қызу", "кызу"],
    }
    return {name for name, variants in markers.items() if any(v in low for v in variants)}


def _humanize_guard_ok(base_answer: str, humanized: str) -> bool:
    if not (base_answer or "").strip():
        return not (humanized or "").strip()
    if _has_age_question(base_answer) and not _has_age_question(humanized):
        return False
    if _has_contra_question(base_answer):
        if not _has_contra_question(humanized):
            return False
        if not _contra_markers(base_answer).issubset(_contra_markers(humanized)):
            return False
    if not _has_name_question(base_answer) and _has_name_question(humanized):
        return False
    if not _has_date_question(base_answer) and _has_date_question(humanized):
        return False
    return True


async def humanize_reply_with_openai(
    *,
    base_answer: str,
    user_text: str,
    session: dict[str, Any],
    recent_history: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Safely rephrase a deterministic dialog answer without changing business logic."""
    settings = get_settings()
    model = settings.openai_model
    debug: dict[str, Any] = {
        "openai_used": False,
        "openai_model": model,
        "openai_skip_reason": "",
        "openai_guard_failed": False,
        "base_answer_preview": (base_answer or "").replace("\n", " ")[:160],
        "final_answer_preview": (base_answer or "").replace("\n", " ")[:160],
    }
    base_answer = (base_answer or "").strip()
    if not base_answer:
        debug["openai_skip_reason"] = "empty_answer"
        return "", debug
    if not settings.ai_enabled or not settings.openai_humanize_replies or not settings.openai_api_key or AsyncOpenAI is None:
        debug["openai_skip_reason"] = "config_missing"
        return base_answer, debug

    step = str(session.get("step") or session.get("current_step") or "start")
    chat_id = str(session.get("chat_id") or session.get("phone") or "")
    summary = {
        "step": step,
        "language": session.get("language"),
        "complaint": session.get("complaint") or session.get("prior_complaint_text"),
        "age": session.get("age"),
        "contraindications_ok": session.get("contraindications_ok"),
    }
    try:
        if state is not None:
            state.log_event(chat_id or "system", "openai_called", {"chat_id": chat_id, "model": model, "purpose": "humanize_reply", "step": step})
        client = _openai_client(settings.openai_api_key)
        response = await client.chat.completions.create(
            model=model,
            temperature=0.35,
            max_tokens=260,
            messages=[
                {"role": "system", "content": STRICT_HUMANIZE_REPLY_PROMPT},
                {"role": "user", "content": (
                    f"Текущий этап: {step}\n"
                    f"Краткий контекст сессии: {json.dumps(summary, ensure_ascii=False)}\n"
                    f"Сообщение клиента: {user_text or ''}\n"
                    "Безопасный базовый ответ, который нельзя менять по смыслу:\n"
                    f"{base_answer}\n\n"
                    "Переформулируй базовый ответ живее, но сохрани смысл, этап и обязательные вопросы."
                )},
            ],
        )
        humanized = (response.choices[0].message.content or "").strip().strip('"')
        debug["openai_used"] = True
        if not _humanize_guard_ok(base_answer, humanized):
            debug["openai_guard_failed"] = True
            debug["openai_skip_reason"] = "guard_failed_returned_base"
            return base_answer, debug
        final = humanized or base_answer
        debug["final_answer_preview"] = final.replace("\n", " ")[:160]
        if state is not None:
            state.log_event(chat_id or "system", "openai_humanized", {"chat_id": chat_id, "model": model, "step": step, "base_preview": debug["base_answer_preview"], "humanized_preview": debug["final_answer_preview"]})
        return final, debug
    except Exception as exc:
        debug["openai_skip_reason"] = "openai_error"
        debug["openai_error_preview"] = str(exc)[:300]
        return base_answer, debug

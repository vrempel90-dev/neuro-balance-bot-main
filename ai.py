from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
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
def _rendered_system_prompt() -> str:
    """Load the rendered prompt as the canonical clinic source of truth."""
    path = Path(__file__).resolve().parent / "SYSTEM_PROMPT_rendered.md"
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


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



DIALOG_BRAIN_INTENTS = {
    "medical_question", "faq", "complaint", "age_answer", "contraindications_answer",
    "contraindication_term_question", "date_preference", "slot_choice", "ask_human",
    "booking_name", "unknown",
}
DIALOG_BRAIN_NEXT_STEPS = {
    "complaint", "age", "contraindications", "date", "time", "name", "booked",
    "escalated", "keep_current",
}
DIALOG_BRAIN_ACTIONS = {
    "ask_complaint", "ask_age", "ask_contraindications", "ask_date", "show_slots",
    "select_slot", "ask_name", "answer_faq_and_continue", "stop_contraindication",
    "handoff_admin", "no_reply", "fallback_rule_based",
}
DIALOG_BRAIN_TOOLS = {"none", "check_slots", "book_appointment", "refresh_slots", "handoff_admin", "cancel", "handoff"}

OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT = """
Ты — живой AI-администратор клиники Neuro Balance в WhatsApp.

Твоя задача — понимать пациента как нормальный живой человек, а не по ключевым словам.

Ты читаешь историю диалога и текущее сообщение. Пациент может писать:
- с ошибками;
- коротко;
- не по порядку;
- несколько вопросов в одном сообщении;
- на русском, казахском или смешанно;
- с уточнениями, сомнениями и бытовыми фразами.

Ты должен понять смысл и вернуть JSON-решение для Python.

Ты НЕ выполняешь CRM-запись сам.
Ты НЕ придумываешь слоты.
Ты НЕ ставишь диагноз.
Ты НЕ обещаешь лечение.
Ты НЕ нарушаешь порядок записи.

Клиника Neuro Balance помогает с:
- болью в спине, пояснице, шее;
- грыжами и протрузиями;
- защемлением нервов;
- болью, отдающей в руку или ногу;
- суставами;
- онемением;
- восстановлением после травм/операций;
- нарушением походки, парезами.

Клиника НЕ занимается:
- зубами;
- животом/ЖКТ;
- сердцем/скорой;
- ЛОР;
- глазами;
- кожей;
- гинекологией/урологией;
- психиатрией;
- возвратами/рассрочками/претензиями.

Строгий сценарий:
1. Понять жалобу.
2. Спросить возраст.
3. Спросить противопоказания.
4. После отсутствия противопоказаний спросить дату.
5. Python показывает реальные слоты.
6. После выбора слота спросить имя.
7. Python бронирует запись.

Нельзя:
- спрашивать имя до выбора слота;
- предлагать дату до противопоказаний;
- записывать без противопоказаний;
- обещать лечение/результат/гарантию;
- ставить диагноз;
- говорить “точно вылечим”;
- отвечать booked/manual/refund/old-chat сценариям;
- начинать новую запись, если пациент уже записан;
- придумывать слоты;
- придумывать врачей;
- придумывать цену курса;
- придумывать противопоказания;
- менять “нет” на “да”.

Важное правило:
Отличай вопрос о термине от подтверждения противопоказания.

Пример:
Пациент: "Что такое кохлеарный имплант?"
Это НЕ значит, что он у него есть.
Нужно объяснить термин и снова уточнить, есть ли это у пациента.

Пациент: "У меня есть кохлеарный имплант"
Это уже противопоказание / hard stop.

Пример:
Пациент: "А сколько длится процедура?"
Это FAQ. Ответь на вопрос и вернись к текущему шагу.

Пример:
Пациент: "Позови человека"
Это запрос живого администратора. Нужно handoff_admin.

Примеры:
"34, все чисто, можно в понеддельник не рано?"
= age 34 + contraindications_clear true + preferred_date_text "в понедельник" + time_preference "не рано".

"2 варик"
= slot_choice 2, если слоты уже показаны.

"поясница бкспокоит"
= "поясница беспокоит".

"все чисто", "ничего такого нет", "по всем нет", "жоқ", "жок"
= противопоказаний нет, только если контекст — вопрос противопоказаний.

Можно:
- отвечать на FAQ внутри сценария;
- исправлять опечатки по смыслу;
- понимать “все чисто” как “противопоказаний нет” только на этапе противопоказаний;
- понимать “2 варик” как выбор второго слота, если слоты уже показаны;
- понимать “в понеддельник” как “в понедельник”;
- понимать “как на видео?” как вопрос о процедуре, без гарантии 1 в 1;
- понимать “жоқ/жок” как “нет” по контексту;
- отвечать на языке клиента: русский или казахский.

Цена:
Первичный приём — 5 000 тг.
Курс лечения рассчитывается только после осмотра врача.

МРТ:
МРТ/снимки заранее делать не обязательно. Если есть старые снимки/заключения — можно взять с собой. Врач на осмотре скажет, нужно ли новое обследование.

Без операции:
В клинике применяются безоперационные методы, но подойдёт ли пациенту такой вариант — врач скажет после осмотра.

Адрес:
Астана, Кабанбай батыра 28, внутренний двор, подъезд 3. Заезд со стороны Кунаева, после ворот направо.

Выходные:
Суббота/воскресенье — процедурные дни. Первичных пациентов лучше записывать в будние дни.

Стиль:
Пиши как живой администратор в WhatsApp.
Коротко, спокойно, понятно.
Без канцелярита.
Не пиши длинные простыни.
Можно использовать 🌿, но не чаще одного раза.
Не повторяй “Здравствуйте” в каждом сообщении.
Не используй фразу “С такими жалобами к нам обращаются”.
Не используй фразу “Вижу Ваш запрос” без необходимости.

Верни только JSON строго по схеме:
{
  "intent": "medical_question | faq | complaint | age_answer | contraindications_answer | contraindication_term_question | date_preference | slot_choice | ask_human | booking_name | unknown",
  "patient_meaning": "что пациент имел в виду",
  "reply": "живой ответ пациенту",
  "next_step": "complaint | age | contraindications | date | time | name | booked | escalated | keep_current",
  "extracted": {
    "complaint": "",
    "age": null,
    "contraindications_clear": null,
    "contraindication_confirmed": false,
    "contraindication_term_asked": "",
    "preferred_date_text": "",
    "time_preference": "",
    "slot_choice": null,
    "patient_name": "",
    "wants_human": false,
    "faq_type": "",
    "language": "ru"
  },
  "needs_python_tool": "none | check_slots | book_appointment | refresh_slots | handoff_admin",
  "safety": {
    "hard_stop": false,
    "reason": "",
    "unsafe_medical_claim": false,
    "tries_to_book_without_rules": false
  }
}
""".strip()


def _full_dialog_brain_system_prompt() -> str:
    rendered = _rendered_system_prompt()
    if not rendered:
        return OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT
    overrides = """

PROJECT OVERRIDES — Python enforces these above any older prompt text:
- AI works only outside contact-center hours: 20:00–08:00 Astana.
- Ask name only after a CRM slot was selected.
- Booking order: complaint → age → contraindications → date → CRM slots → time choice → name → CRM booking.
- Contraindications are always before slots.
- CRM is the only source of slots/availability; session.last_slots is the only source of shown slots.
- selected_slot is the only source of booking payload.
- Contraindication term questions are not hard stops.
- booked/manual/refund/voice/escalated/old-chat states must not call OpenAI.
"""
    return rendered + overrides + "\n\n" + OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT


def _dialog_brain_fallback(reason: str) -> tuple[dict, dict]:
    decision = {
        "intent": "unknown",
        "action": "fallback_rule_based",
        "next_step": "keep_current",
        "patient_meaning": "",
        "reply": "",
        "extracted": {
            "complaint": "", "age": None, "contraindications_clear": None,
            "contraindication_confirmed": False, "contraindication_term_asked": "",
            "contraindication_red_flags": [], "preferred_date_text": "", "time_preference": "", "slot_choice": None,
            "patient_name": "", "wants_human": False, "faq_type": "", "language": "ru",
        },
        "needs_python_tool": "none",
        "safety": {"hard_stop": False, "reason": "", "unsafe_medical_claim": False, "tries_to_book_without_rules": False},
        "safety_flags": {
            "promised_cure": False, "asked_name_too_early": False,
            "offered_date_before_contra": False, "medical_diagnosis": False,
        },
    }
    if state is not None:
        try:
            state.log_event("system", "openai_brain_fallback_rule_based", {"reason": reason})
        except Exception:
            pass
    return decision, {"openai_brain_used": False, "openai_brain_intent": "unknown", "openai_brain_fallback_used": True, "openai_brain_skip_reason": reason}


def _action_from_structured(intent: str, next_step: str, tool: str, safety: dict, extracted: dict) -> str:
    if safety.get("hard_stop") or extracted.get("contraindication_confirmed") is True:
        return "stop_contraindication"
    if intent == "ask_human" or extracted.get("wants_human") is True or tool in {"handoff_admin", "handoff"} or next_step == "escalated":
        return "handoff_admin"
    if intent == "contraindication_term_question":
        return "answer_faq_and_continue"
    if intent == "faq":
        return "answer_faq_and_continue"
    if intent == "complaint" or next_step == "age":
        return "ask_age"
    if intent == "age_answer" or next_step == "contraindications":
        return "ask_contraindications"
    if tool == "check_slots" or next_step == "time":
        return "show_slots"
    if intent == "date_preference" or next_step == "date":
        return "ask_date"
    if intent == "slot_choice" or next_step == "name":
        return "select_slot" if extracted.get("slot_choice") else "ask_name"
    if intent == "booking_name" or next_step == "booked":
        return "ask_name"
    return "fallback_rule_based" if intent == "unknown" else "answer_faq_and_continue"


def _normalize_dialog_brain_decision(raw: Any) -> tuple[dict, str]:
    if not isinstance(raw, dict):
        return {}, "not_object"
    allowed_top = {"intent", "action", "next_step", "patient_meaning", "reply", "extracted", "needs_python_tool", "safety", "safety_flags"}
    if any(k not in allowed_top for k in raw.keys()):
        return {}, "schema_extra_top_level"
    intent = str(raw.get("intent") or "")
    next_step = str(raw.get("next_step") or "keep_current")
    extracted = raw.get("extracted") if isinstance(raw.get("extracted"), dict) else {}
    safety = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}
    allowed_extracted = {
        "complaint", "age", "contraindications_clear", "contraindication_confirmed",
        "contraindication_term_asked", "contraindication_red_flags", "preferred_date_text",
        "time_preference", "slot_choice", "patient_name", "wants_human", "faq_type", "language",
    }
    allowed_safety = {"hard_stop", "reason", "unsafe_medical_claim", "tries_to_book_without_rules"}
    if any(k not in allowed_extracted for k in extracted.keys()):
        return {}, "schema_extra_extracted"
    if any(k not in allowed_safety for k in safety.keys()):
        return {}, "schema_extra_safety"
    tool = str(raw.get("needs_python_tool") or "none")
    action = str(raw.get("action") or "")
    if not action and intent:
        if intent not in DIALOG_BRAIN_INTENTS:
            return {}, "invalid_intent"
        if next_step not in DIALOG_BRAIN_NEXT_STEPS:
            return {}, "invalid_next_step"
        action = _action_from_structured(intent, next_step, tool, safety, extracted)
    if not intent:
        intent = {
            "ask_age": "complaint", "ask_contraindications": "age_answer", "ask_date": "contraindications_answer",
            "show_slots": "date_preference", "select_slot": "slot_choice", "ask_name": "booking_name",
            "answer_faq_and_continue": "faq", "stop_contraindication": "contraindications_answer",
            "handoff_admin": "ask_human", "fallback_rule_based": "unknown", "no_reply": "unknown",
        }.get(action, "unknown")
    if intent not in DIALOG_BRAIN_INTENTS:
        return {}, "invalid_intent"
    if action not in DIALOG_BRAIN_ACTIONS:
        return {}, "invalid_action"
    if tool not in DIALOG_BRAIN_TOOLS:
        return {}, "invalid_tool"
    safety_flags = raw.get("safety_flags") if isinstance(raw.get("safety_flags"), dict) else {}
    decision = {
        "intent": intent,
        "action": action,
        "next_step": next_step,
        "patient_meaning": str(raw.get("patient_meaning") or ""),
        "reply": str(raw.get("reply") or ""),
        "extracted": {
            "complaint": str(extracted.get("complaint") or ""),
            "age": extracted.get("age"),
            "contraindications_clear": extracted.get("contraindications_clear"),
            "contraindication_confirmed": bool(extracted.get("contraindication_confirmed")),
            "contraindication_term_asked": str(extracted.get("contraindication_term_asked") or ""),
            "contraindication_red_flags": extracted.get("contraindication_red_flags") if isinstance(extracted.get("contraindication_red_flags"), list) else [],
            "preferred_date_text": str(extracted.get("preferred_date_text") or ""),
            "time_preference": str(extracted.get("time_preference") or ""),
            "slot_choice": extracted.get("slot_choice"),
            "patient_name": str(extracted.get("patient_name") or ""),
            "wants_human": bool(extracted.get("wants_human")),
            "faq_type": str(extracted.get("faq_type") or ""),
            "language": str(extracted.get("language") or "ru"),
        },
        "needs_python_tool": tool,
        "safety": {
            "hard_stop": bool(safety.get("hard_stop")),
            "reason": str(safety.get("reason") or ""),
            "unsafe_medical_claim": bool(safety.get("unsafe_medical_claim")),
            "tries_to_book_without_rules": bool(safety.get("tries_to_book_without_rules")),
        },
        "safety_flags": {
            "promised_cure": bool(safety_flags.get("promised_cure") or safety.get("unsafe_medical_claim")),
            "asked_name_too_early": bool(safety_flags.get("asked_name_too_early")),
            "offered_date_before_contra": bool(safety_flags.get("offered_date_before_contra") or safety.get("tries_to_book_without_rules")),
            "medical_diagnosis": bool(safety_flags.get("medical_diagnosis")),
        },
    }
    if action not in {"no_reply", "fallback_rule_based", "show_slots", "select_slot"} and not decision["reply"].strip():
        return {}, "empty_reply"
    return decision, ""


async def run_openai_dialog_brain(
    *,
    user_text: str,
    session: dict,
    recent_history: list | None = None,
    available_slots: list | None = None,
    clinic_context: dict | None = None,
) -> tuple[dict, dict]:
    settings = get_settings()
    model = getattr(settings, "ai_brain_model", "") or getattr(settings, "openai_model", "")
    temperature = float(getattr(settings, "ai_brain_temperature", 0.2) or 0.2)
    debug = {"openai_brain_used": False, "openai_brain_intent": "", "openai_brain_action": "", "openai_brain_needs_python_tool": "", "openai_brain_extracted": {}, "openai_brain_guard_failed": False, "openai_brain_guard_reason": "", "openai_brain_skip_reason": "", "openai_brain_fallback_used": False, "openai_brain_model": model, "openai_brain_temperature": temperature, "openai_model": model}
    if not getattr(settings, "ai_enabled", True) or not getattr(settings, "openai_api_key", "") or AsyncOpenAI is None:
        decision, fb = _dialog_brain_fallback("config_missing")
        debug.update(fb)
        return decision, debug
    try:
        summary = {
            "step": session.get("step") or session.get("current_step") or "start",
            "complaint": session.get("complaint") or "",
            "age": session.get("age"),
            "contraindications_ok": session.get("contraindications_ok"),
            "last_slots": available_slots if available_slots is not None else session.get("last_slots") or [],
            "selected_slot": session.get("selected_slot") or {},
            "language": session.get("language") or "ru",
            "clinic_context": clinic_context or {},
        }
        if state is not None:
            state.log_event(str(session.get("chat_id") or "system"), "openai_brain_called", {"chat_id": str(session.get("chat_id") or "system"), "model": model, "openai_brain_model": model, "openai_brain_temperature": temperature, "step": summary["step"], "action": "", "needs_python_tool": "", "guard_failed": False, "guard_reason": "", "extracted_preview": {}})
        client = _openai_client(settings.openai_api_key)
        response = await client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=700,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _full_dialog_brain_system_prompt()},
                {"role": "user", "content": json.dumps({"user_text": user_text or "", "session": summary, "recent_history": recent_history or []}, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content or ""
        try:
            raw = json.loads(content)
        except Exception:
            decision, fb = _dialog_brain_fallback("invalid_json")
            debug.update(fb)
            return decision, debug
        decision, reason = _normalize_dialog_brain_decision(raw)
        if reason:
            decision, fb = _dialog_brain_fallback(reason)
            debug.update(fb)
            return decision, debug
        debug.update({"openai_brain_used": True, "openai_brain_intent": decision["intent"], "openai_brain_action": decision["action"], "openai_brain_needs_python_tool": decision["needs_python_tool"], "openai_brain_extracted": decision["extracted"], "openai_brain_model": model, "openai_brain_temperature": temperature})
        if state is not None:
            state.log_event(str(session.get("chat_id") or "system"), "openai_brain_decision", {"chat_id": str(session.get("chat_id") or "system"), "step": summary["step"], "action": decision["action"], "needs_python_tool": decision["needs_python_tool"], "guard_failed": False, "guard_reason": "", "extracted_preview": {k: v for k, v in decision["extracted"].items() if v not in (None, "", [], {})}})
        return decision, debug
    except Exception as exc:
        decision, fb = _dialog_brain_fallback("openai_error")
        debug.update(fb)
        debug["openai_error_preview"] = str(exc)[:200]
        return decision, debug


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
    model = getattr(settings, "ai_humanize_model", "") or settings.openai_model
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

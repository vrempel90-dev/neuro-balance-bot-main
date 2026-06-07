from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

import crm
import state
from clinic_info import get_clinic_info, topics
from config import get_settings
from contraindications import evaluate_contraindications
from phone import sanitize_kz_phone
from questionnaire import handle_questionnaire
from original_engine import pre_handle_message
from response_guard import validate_answer
from language_guard import detect_language as detect_message_language


SYSTEM_PROMPT_PATHS = [
    Path(__file__).resolve().parent / "system_prompt_original.md",
    Path(__file__).resolve().parent / "prompts" / "system_prompt_original.md",
]

KZ_MARKERS = [
    "сәлем", "салем", "salem", "ассала", "assalamu", "менің", "меним", "атым",
    "белім", "белим", "belim", "ауырады", "аурат", "aurat", "ертең", "ертен", "erten",
    "бүгін", "бугин", "bugin", "жоқ", "жок", "joq", "jok", "иә", "ия", "бар",
    "қарсы", "карсы", "көрсетілім", "корсетилим", "qarsy", "korsetilim",
    "аяқ", "аяг", "қол", "кол", "ауыр", "ауру", "жазыл", "жазылғым", "келеді",
    "жас", "жастамын", "жақсы", "рахмет", "уақыт", "барма", "бар ма", "дүйсенбі",
    "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі", "омыртқа",
    "жарық", "грыжа", "мүмкін", "қатты", "белгі", "белгілері", "ауырсыну"
]




CONFIRMATION_MARKERS = {
    "приду", "буду", "ок", "окей", "хорошо", "да", "ага", "понял", "поняла",
    "подтверждаю", "келемін", "келемин", "барамын", "жарайды", "ия", "иә", "иә", "иа",
}

GENERIC_BOOKING_INTENT_WORDS = {
    "хочу записаться", "записаться", "запишите", "консультац", "по акции", "акция",
    "50%", "скидк", "инстаграм", "instagram", "ссылка", "салем", "здравствуйте",
    "добрый", "привет", "ассалаумағалейку", "жазыл", "консультацияға"
}

COMPLAINT_BODY_WORDS = {
    "болит", "боль", "ноет", "тянет", "отдает", "онемение", "немеет", "хрустит",
    "спина", "шея", "поясница", "сустав", "колено", "плечо", "локоть", "кисть",
    "стопа", "нога", "рука", "голова", "грыжа", "протруз", "артроз", "артрит",
    "ауыр", "ауырады", "бел", "мойын", "буын", "тізе", "иық", "қол", "аяқ"
}


SYMPTOM_NOT_CONTRAINDICATION_WORDS = {
    "болит", "боль", "сильно болит", "грыжа", "протруз", "онемение", "немеет",
    "тянет", "отдает", "омыртқа", "жарық", "грыжа болуы", "мүмкін", "қатты",
    "ауырып", "ауырсыну", "аяқ", "бел", "мойын", "буын", "ауру белгілері"
}


PARENT_QUESTION_WORDS = {
    "с родителем", "с родителями", "родитель", "мама", "папа", "законным представителем",
    "ата-анам", "ата ана", "ата-анамен", "әкем", "анам", "родител"
}

YES_WORDS = {"да", "ага", "хорошо", "смогу", "могу", "приду", "иә", "ия", "болады", "келемін", "келемин"}
NO_WORDS = {"нет", "не могу", "не смогу", "жоқ", "жок", "жүре алмаймын", "бара алмаймын"}


def _asks_about_parent(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in PARENT_QUESTION_WORDS)


def _is_yes_answer(text: str) -> bool:
    clean = (text or "").strip().lower().replace(".", "").replace("!", "").replace(",", "")
    return clean in YES_WORDS or any(clean == w for w in YES_WORDS)


def _is_no_answer(text: str) -> bool:
    clean = (text or "").strip().lower().replace(".", "").replace("!", "").replace(",", "")
    return clean in NO_WORDS or any(w in clean for w in NO_WORDS)


IMMOBILITY_CONCERN_WORDS = {
    "ходить не могу", "не могу ходить", "не передвигаюсь", "не может ходить",
    "лежач", "коляск", "инвалидная коляска", "сам не дойду", "сама не дойду",
    "жүре алмаймын", "жүре алмайды", "жүре алмай", "жатады", "арбамен"
}


def _has_immobility_concern(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in IMMOBILITY_CONCERN_WORDS)


def _is_no_contra_answer(text: str) -> bool:
    low = (text or "").lower().strip()
    if not low:
        return False
    markers = [
        "нет", "не было", "до этого не было", "раньше не было", "противопоказаний нет",
        "нету", "жоқ", "болған жоқ", "қарсы көрсетілім жоқ", "жоқ еді", "жоқ қой"
    ]
    return any(m in low for m in markers)


def _contra_text_is_only_symptoms(args: dict) -> bool:
    raw_parts = []
    for key in ("raw_text", "notes", "contraindications_raw", "patient_answer"):
        val = args.get(key)
        if val:
            raw_parts.append(str(val))
    for item in args.get("uncertain_items") or []:
        raw_parts.append(str(item))
    raw = " ".join(raw_parts).lower()
    if not raw:
        return False
    has_symptom = any(w in raw for w in SYMPTOM_NOT_CONTRAINDICATION_WORDS)
    has_real_contra = any(w in raw for w in [
        "кардиостимулятор", "имплант", "беремен", "жүктілік", "онколог",
        "рак", "эпилеп", "тромб", "кровотеч", "қан кет", "температур",
        "инфекц", "диабет", "псих", "жүрек", "сердеч"
    ])
    bool_flags = [
        "has_pacemaker_or_implant", "has_pregnancy", "has_active_cancer", "has_epilepsy",
        "has_thrombosis_or_bleeding", "has_decompensated_endocrine",
        "has_acute_infection_or_fever", "has_severe_cardio_or_respiratory",
        "has_severe_psychiatric",
    ]
    any_true_flag = any(args.get(k) is True for k in bool_flags)
    return has_symptom and not has_real_contra and not any_true_flag


def _is_generic_booking_intent_without_complaint(text: str) -> bool:
    low = (text or "").lower()
    if not low:
        return True
    has_intent = any(w in low for w in GENERIC_BOOKING_INTENT_WORDS)
    has_complaint = any(w in low for w in COMPLAINT_BODY_WORDS)
    return has_intent and not has_complaint

def _is_short_confirmation(text: str) -> bool:
    clean = (text or "").strip().lower().replace(".", "").replace("!", "").replace(",", "")
    # короткие подтверждения не являются именем и не должны запускать сценарий сначала
    return clean in CONFIRMATION_MARKERS or (len(clean.split()) <= 2 and any(w in clean for w in CONFIRMATION_MARKERS))

def _active_appointment_from_lookup(lookup: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(lookup, dict):
        return None
    if not lookup.get("hasActiveAppointment"):
        return None
    appt = lookup.get("lastAppointment") or {}
    return appt if isinstance(appt, dict) and appt else None

async def _safe_refresh_patient_lookup(chat_id: str, phone: str, session: dict[str, Any]) -> dict[str, Any]:
    """CRM является источником правды. Держим данные пациента свежими, чтобы GPT не путал
    подтверждения типа «Приду» с именем и не создавал дубли. Ошибки CRM не ломают диалог.
    """
    normalized_phone = sanitize_kz_phone(phone) or sanitize_kz_phone(session.get("phone") or "") or phone
    try:
        data = await crm.patient_lookup(normalized_phone)
        session["phone"] = normalized_phone
        session["patient_lookup"] = data
        session["patient_lookup_done"] = True
        patient = data.get("patient") or {}
        if patient.get("name") and not session.get("patient_name"):
            session["patient_name"] = patient.get("name")
        state.save_session(chat_id, session)
    except Exception as exc:
        state.log_event(chat_id, "patient_lookup_failed", {"error": str(exc)[:500]})
    return session

async def _smart_confirmation_reply(user_text: str, session: dict[str, Any], lang: str) -> str:
    """Отдельная GPT-защита от кейсов «Приду»/«Буду»/«Ок».
    Это всё ещё GPT-ответ, но с жёстким контекстом: НЕ принимать подтверждение за имя.
    """
    settings = get_settings()
    appt = _active_appointment_from_lookup(session.get("patient_lookup"))
    if not settings.openai_api_key:
        if lang == "kk":
            return "Жақсы, қабылдадық 🌿 Кездескенше!" if appt else "Жақсы 🌿 Нақтылап жіберейін: жазылғыңыз келе ме?"
        return "Хорошо, приняли 🌿 Будем ждать Вас!" if appt else "Хорошо 🌿 Подскажите, пожалуйста, Вы хотите записаться на консультацию?"
    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=12.0, max_retries=1)
    appt_text = json.dumps(appt or {}, ensure_ascii=False)
    language = "казахском" if lang == "kk" else "русском"
    response = await client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.2,
        max_tokens=120,
        messages=[
            {"role": "system", "content": f"Ты живой администратор Neuro Balance. Ответь на {language}. Сообщение пациента — короткое подтверждение, а НЕ имя. Не начинай новый сценарий, не спрашивай имя. Если есть активная запись, просто тепло подтверди, что ждём пациента. Если активной записи нет, мягко уточни, хочет ли он записаться."},
            {"role": "system", "content": f"Активная запись из CRM: {appt_text}"},
            {"role": "user", "content": user_text},
        ],
    )
    return (response.choices[0].message.content or "").strip()
def _astana_now_text() -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=5)
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"{now.strftime('%d.%m.%Y')} ({days_ru[now.weekday()]}), {now.strftime('%H:%M')} (Астана, UTC+5)"


def _load_system_prompt() -> str:
    for path in SYSTEM_PROMPT_PATHS:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "Ты — живой администратор клиники Neuro Balance. Общайся тепло, по делу, без диагнозов."


def _detect_language(text: str, current: str | None = None) -> str:
    # v20: единый language_guard. Русские мед. слова «грыжа/протрузия»
    # больше не переключают диалог на казахский.
    return detect_message_language(text, current)


def _runtime_prompt(session: dict[str, Any], phone: str) -> str:
    lang = session.get("language") or "ru"
    return f"""

## Runtime-добавление Python-версии / ВАЖНО
Текущая дата и время: {_astana_now_text()}.
Телефон пациента из WhatsApp: {phone}.
Зафиксированный язык диалога: {lang}. Отвечай на языке последнего сообщения пациента. Если пациент пишет на казахском — отвечай на казахском, даже если раньше диалог был на русском. Не смешивай русский и казахский в одном ответе.

Ты работаешь как ЖИВОЙ AI-администратор, а не как сухой скрипт. Важно: критические шаги анкеты контролирует questionnaire-controller; не спорь с его состоянием и не перескакивай шаги.
С этого момента GPT является главным мозгом диалога: именно ты понимаешь намерение, контекст, язык и смысл сообщения. Python только исполняет CRM-инструменты и технически отправляет ответ.
Всегда сначала отвечай на смысл сообщения пациента, затем мягко переходи к следующему шагу записи.

КРИТИЧНО ПРО КОНТЕКСТ И ИМЯ:
- НЕ принимай любое короткое сообщение за имя.
- Слова «приду», «буду», «ок», «хорошо», «да», «понял», «поняла», «жарайды», «келемін», «барамын» — это подтверждение/согласие, НЕ имя пациента.
- Никогда не пиши «Очень приятно, Приду/Буду/Хорошо/Да/Ок».
- Если пациент пишет только «Приду», «Буду», «Да», «Ок», «Хорошо», «Жарайды», «Келемін» — это подтверждение/согласие, а НЕ имя, НЕ жалоба и НЕ новая заявка.
- Если активная запись уже есть — подтверждение означает, что пациент придёт. Не задавай заново имя, возраст и жалобу.
- Не обращайся к пациенту по имени в приветствиях и обычных ответах, даже если имя есть в CRM/сессии. Пиши просто «Здравствуйте!», «Добрый день!», без имени.
- НЕ спрашивай имя в начале диалога, при выяснении жалобы, возраста или выборе времени.
- Имя спрашивай только когда пациент уже реально готов записаться: жалоба профильная, возраст подходит, выбран конкретный слот и противопоказаний нет.
- Имя сохраняй только если это похоже на имя человека или есть формулировки «меня зовут ...», «я ...», «менің атым ...», «атым ...».
- Запрещено использовать в качестве имени: приду, буду, да, ок, хорошо, завтра, сегодня, ертең, бүгін, жоқ, жоқпын, подтверждаю, не знаю, хочу записаться.
- Если контекст неполный и сообщение выглядит как ответ на предыдущее сообщение администратора, не начинай сценарий заново. Сначала аккуратно уточни или подтверди смысл.

КРИТИЧНО ПРО ОБСЛЕДОВАНИЯ, КОТОРЫХ НЕТ В КЛИНИКЕ:
- Если пациент спрашивает про МРТ, КТ, рентген, УЗИ, анализы, снимки, диагностику или стоимость обследования — НЕ отвечай сухим отказом «у нас не делают».
- Всегда сначала помоги: объясни, что снимок/обследование заранее делать не обязательно, врач после консультации и осмотра подскажет, какое обследование действительно нужно.
- Для МРТ/КТ/рентгена/УЗИ/анализов/снимков GPT сам должен понять смысл вопроса и ОБЯЗАТЕЛЬНО вызвать tool get_clinic_info(topic="mri_needed"). Python не должен отвечать за это ключевыми словами — решение принимает GPT по контексту сообщения.
- Цель ответа — не потерять лида: успокоить, объяснить следующий шаг и мягко вернуть к консультации.
- Правильный смысл ответа: «Снимки в клинике не делают. Заранее делать не обязательно: при необходимости врач назначит после консультации с учётом симптомов и осмотра. Готовые снимки иногда бывают неинформативными, поэтому лучше сначала получить рекомендации специалиста».
- Нельзя писать: «мы не можем предоставить информацию» как финальный отказ. Это звучит как отказ и теряет пациента.

КРИТИЧНО ПРО ЖИВОЙ ДИАЛОГ:
- Каждый ответ должен звучать как сообщение администратора в WhatsApp, а не как FAQ или робот.
- Если клиент задал вопрос, сначала ответь на вопрос, затем продолжай запись.
- Не задавай следующий вопрос, пока не отреагировал на смысл последнего сообщения.
- Не используй канцелярит: «предоставить информацию», «данное учреждение», «рекомендуем обратиться» без помощи.
- Пиши коротко, тепло, но по делу.

КРИТИЧНО ПРО ПЕРВОЕ СООБЩЕНИЕ И ЖАЛОБУ:
- Если пациент пишет только «хочу записаться», «хочу на консультацию», «по акции 50%», отправил ссылку/заявку из Instagram или просто поздоровался — это НЕ жалоба.
- В таком случае НЕЛЬЗЯ писать «с такой жалобой можно прийти» и НЕЛЬЗЯ спрашивать возраст.
- Правильный ответ: «Здравствуйте! Да, можно записаться на консультацию по акции 🌿 Подскажите, пожалуйста, что Вас беспокоит?»
- Слова «консультация», «акция», «хочу записаться», «здравствуйте» не являются жалобой.
- Возраст спрашивай только после того, как пациент реально написал, что болит/что беспокоит.

Если пациент уже написал реальную жалобу или спрашивает «занимаетесь/лечите/можно к вам?» по конкретной проблеме, ОБЯЗАТЕЛЬНО сначала скажи, относится ли это к профилю клиники Neuro Balance:
- профиль клиники: спина, шея, поясница, суставы, плечо, сухожилия/связки, неврология, грыжи, протрузии, артроз/артрит, реабилитация;
- непрофильное: горло, зубы, ЛОР, кожа, гинекология, онкология как основное заболевание, острые неотложные состояния.
Если профильное — скажи, что с такой жалобой можно прийти на первичную консультацию, врач осмотрит и подскажет план. Только потом спроси следующий недостающий пункт.
Если непрофильное — не записывай автоматически, вызови escalate_to_human и log outcome через mark_irrelevant.

Главная архитектура:
- GPT ведёт живой диалог, понимает язык, смысл, намерение, жалобу, возражения и контекст переписки.
- GPT НЕ должен слепо идти по состоянию session, если последнее сообщение клиента явно противоречит этому состоянию. Например, если session ждёт имя, но клиент написал «Приду», это подтверждение, а не имя.
- Python-инструменты выполняют реальные действия CRM: пациент, врачи, услуги, слоты, запись, перенос, отмена, оператор, outcome.
- Не выдумывай врачей, даты, время и факт записи.
- Если пациент хочет записаться или называет удобный день — ОБЯЗАТЕЛЬНО сначала вызови check_available_slots для этой даты и покажи пациенту свободные записи из CRM.
- Нельзя сразу вызывать book_appointment без того, чтобы пациенту были предложены свободные варианты из check_available_slots и пациент выбрал конкретный вариант.
- Предлагай только реальные свободные записи из CRM: дата, время, врач/дежурный врач, если он есть в ответе CRM.
- Показывай максимум 3–5 вариантов времени, не отправляй огромный список. Выбирай ближайшие или самые удобные варианты.
- Если свободных записей нет — честно скажи, что на выбранный день свободного времени нет, и предложи проверить другой день.
- Для расписания используй только check_available_slots. Для записи только book_appointment после выбора пациентом конкретного свободного времени.
- Не спрашивай телефон: он уже известен из WhatsApp.
- Не используй слово «окошки». Говори «свободное время для записи».
- Проверку противопоказаний делай коротко: задай простой вопрос «Перед записью уточню: есть ли у Вас противопоказания?» / «Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?». НЕ отправляй полный чек-лист, если пациент сам не просит пояснить. Если пациент пишет «нет противопоказаний / жоқ / қарсы көрсетілім жоқ» — это proceed.
- Перед book_appointment обязательно должны быть: профильная жалоба, возраст 16–74, выбранный слот из CRM, verify_contraindications_check со статусом proceed и имя пациента для записи. Если имени нет — спроси его только на этом этапе.
- Если пациенту 16 или 17 лет — запись возможна только с родителем или законным представителем. Обязательно предупреди: «Так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿». Не записывай несовершеннолетнего без этого предупреждения.
- Если несовершеннолетний сам спрашивает «с родителем приходить?» — ответь прямо: «Да, так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿». Потом продолжи с того шага, где остановились: если время уже выбрано — спроси противопоказания; если противопоказания уже уточнены — спроси имя для записи.
- Если у пациента уже есть активная запись по patient_lookup, не создавай дубль: предложи подтвердить, отменить или перенести.
- Если пациент просит отменить запись, пишет «отмените», «не смогу прийти», «не приду», «передумал», «уберите запись», «отмена записи» — это намерение отмены, а не обычное возражение.
- При явной просьбе отменить запись ОБЯЗАТЕЛЬНО сначала используй cancel_appointment. Не уговаривай и не предлагай акцию до отмены.
- После успешной отмены коротко подтверди: «Хорошо, запись отменили. Если захотите подобрать другое время — напишите, помогу 🌿».
- Если активной записи не найдено или CRM вернула ошибку — не придумывай, что отменил. Передай оператору через escalate_to_human и напиши, что администратор проверит запись.
- Если пациент просит перенести запись — используй reschedule_appointment, а не создавай новую запись.

Текущее сохранённое состояние сессии:
{json.dumps(session, ensure_ascii=False, indent=2)}
""".strip()


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "find_existing_appointment", "description": "Проверить пациента по телефону: новый/повторный, активная запись, последний приём, статус лида. Вызывать в начале нового диалога и при отмене/переносе.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_services", "description": "Получить из CRM список того, что клиника лечит и что не лечит. Используй для профильности жалобы.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_doctor_info", "description": "Получить врачей и специализации из CRM для подбора врача по жалобе.", "parameters": {"type": "object", "properties": {"diagnosis": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "record_chief_complaint", "description": "Зафиксировать РЕАЛЬНУЮ жалобу пациента и профильность. Вызывать только после того, как пациент сообщил, что именно болит/беспокоит. Не вызывать на фразы «хочу записаться», «консультация», «акция 50%», «здравствуйте».", "parameters": {"type": "object", "properties": {"complaint": {"type": "string"}, "is_in_profile": {"type": "boolean"}, "service": {"type": "string"}}, "required": ["complaint", "is_in_profile"]}}},
    {"type": "function", "function": {"name": "check_available_slots", "description": "Проверить свободное время для записи в CRM на дату YYYY-MM-DD. Если doctor_login не указан — все врачи.", "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}, "doctor_login": {"type": "string"}}, "required": ["date"]}}},
    {"type": "function", "function": {"name": "verify_contraindications_check", "description": "Обязательная проверка противопоказаний перед записью. Возвращает proceed/refuse/escalate.", "parameters": {"type": "object", "properties": {"patient_age": {"type": "number"}, "has_pacemaker_or_implant": {"type": "boolean"}, "has_pregnancy": {"type": "boolean"}, "has_active_cancer": {"type": "boolean"}, "has_epilepsy": {"type": "boolean"}, "has_thrombosis_or_bleeding": {"type": "boolean"}, "has_decompensated_endocrine": {"type": "boolean"}, "has_acute_infection_or_fever": {"type": "boolean"}, "has_severe_cardio_or_respiratory": {"type": "boolean"}, "has_severe_psychiatric": {"type": "boolean"}, "uncertain_items": {"type": "array", "items": {"type": "string"}}}, "required": ["patient_age", "has_pacemaker_or_implant", "has_pregnancy", "has_active_cancer", "has_epilepsy", "has_thrombosis_or_bleeding", "has_decompensated_endocrine", "has_acute_infection_or_fever", "has_severe_cardio_or_respiratory", "has_severe_psychiatric", "uncertain_items"]}}},
    {"type": "function", "function": {"name": "book_appointment", "description": "Создать запись в CRM. Только после профильной жалобы, возраста 16–74, выбранного слота, противопоказаний proceed и имени пациента. Имя спрашивать только на финальном этапе записи.", "parameters": {"type": "object", "properties": {"patient_name": {"type": "string"}, "doctor_login": {"type": "string"}, "doctor_name": {"type": "string"}, "date": {"type": "string"}, "time_start": {"type": "string"}, "notes": {"type": "string"}}, "required": ["patient_name", "doctor_login", "date", "time_start"]}}},
    {"type": "function", "function": {"name": "cancel_appointment", "description": "Отменить активную запись пациента через CRM. Вызывать обязательно, если пациент явно просит отменить запись: отмените, не приду, не смогу прийти, передумал, уберите запись.", "parameters": {"type": "object", "properties": {"appointment_id": {"type": "number"}, "reason": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "reschedule_appointment", "description": "Перенести активную запись на новую дату и время. Слот должен быть доступен.", "parameters": {"type": "object", "properties": {"appointment_id": {"type": "number"}, "new_date": {"type": "string"}, "new_time_start": {"type": "string"}, "reason": {"type": "string"}}, "required": ["new_date", "new_time_start"]}}},
    {"type": "function", "function": {"name": "escalate_to_human", "description": "Передать диалог оператору в CRM.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "mark_irrelevant", "description": "Отметить непрофильный запрос/out_of_scope в CRM outcome.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "get_clinic_info", "description": "Получить готовый шаблон ответа по типовым вопросам: цена, адрес, МРТ, график, рассрочка, методы, возражения и т.д.", "parameters": {"type": "object", "properties": {"topic": {"type": "string", "enum": topics()}}, "required": ["topic"]}}},
]


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _active_appointment_id(lookup: dict[str, Any] | None) -> int | None:
    try:
        appt = (lookup or {}).get("lastAppointment") or {}
        if appt and appt.get("id") is not None:
            return int(appt.get("id"))
    except Exception:
        return None
    return None


async def _execute_tool(chat_id: str, phone: str, name: str, raw_args: str) -> str:
    session = state.get_session(chat_id)
    normalized_phone = sanitize_kz_phone(phone) or sanitize_kz_phone(session.get("phone") or "") or phone
    try:
        args = json.loads(raw_args or "{}")
    except Exception:
        args = {}

    try:
        if name == "find_existing_appointment":
            data = await crm.patient_lookup(normalized_phone)
            session["phone"] = normalized_phone
            session["patient_lookup"] = data
            session["patient_lookup_done"] = True
            patient = data.get("patient") or {}
            if patient.get("name") and not session.get("patient_name"):
                session["patient_name"] = patient.get("name")
            state.save_session(chat_id, session)
            return _json_dumps(data)

        if name == "get_services":
            data = await crm.get_services()
            return _json_dumps(data)

        if name == "get_doctor_info":
            data = await crm.get_doctors()
            return _json_dumps(data)

        if name == "record_chief_complaint":
            complaint = str(args.get("complaint") or "").strip()
            if _is_generic_booking_intent_without_complaint(complaint):
                return (
                    "COMPLAINT_MISSING: пациент ещё не сообщил жалобу. "
                    "Не пиши «с такой жалобой». Ответь, что можно записаться на консультацию по акции, "
                    "и спроси: «Подскажите, пожалуйста, что Вас беспокоит?»"
                )
            session["complaint"] = complaint
            session["service"] = str(args.get("service") or session.get("service") or "первичная консультация")
            session["can_help"] = bool(args.get("is_in_profile"))
            state.save_session(chat_id, session)
            if session["can_help"]:
                return "COMPLAINT_OK: профильная жалоба зафиксирована. Сначала ответь по жалобе живо, затем мягко продолжай к недостающему шагу записи."
            return "COMPLAINT_NOT_IN_PROFILE: жалоба непрофильная. Не записывай автоматически. Вежливо объясни и вызови mark_irrelevant/escalate_to_human."

        if name == "check_available_slots":
            data = await crm.check_slots(str(args["date"]), args.get("doctor_login") or None)
            session["last_slots_raw"] = data
            state.save_session(chat_id, session)
            return _json_dumps(data)

        if name == "verify_contraindications_check":
            raw_answer = " ".join(str(args.get(k) or "") for k in ("raw_text", "notes", "contraindications_raw", "patient_answer"))
            if _is_no_contra_answer(raw_answer):
                args = dict(args)
                for k in [
                    "has_pacemaker_or_implant", "has_pregnancy", "has_active_cancer", "has_epilepsy",
                    "has_thrombosis_or_bleeding", "has_decompensated_endocrine",
                    "has_acute_infection_or_fever", "has_severe_cardio_or_respiratory",
                    "has_severe_psychiatric",
                ]:
                    args[k] = False
                args["uncertain_items"] = []
            if _contra_text_is_only_symptoms(args):
                return (
                    "CONTRAINDICATIONS_NOT_CONFIRMED: пациент написал симптомы/жалобу, а не противопоказания. "
                    "Не отказывай в записи и не передавай оператору. Уточни коротко: "
                    "«Понимаю. Это больше похоже на жалобу, а не на противопоказание. "
                    "Уточню именно по противопоказаниям: они у Вас есть?» "
                    "На казахском ответь на казахском."
                )
            decision = evaluate_contraindications(args)
            session["contraindications_verdict"] = decision["verdict"]
            session["contraindications_reason"] = decision["reason"]
            if args.get("patient_age"):
                session["age"] = int(args.get("patient_age") or 0)
                if 16 <= session["age"] < 18:
                    session["minor_parent_required"] = True
            state.save_session(chat_id, session)
            if 16 <= int(args.get("patient_age") or 0) < 18 and decision.get("verdict") == "proceed":
                return decision["messageForBot"] + " Пациенту нет 18 лет: обязательно предупреди, что на консультацию нужно прийти с родителем или законным представителем."
            return decision["messageForBot"]

        if name == "book_appointment":
            if not session.get("can_help"):
                return "BOOKING_BLOCKED: жалоба не зафиксирована как профильная. Сначала record_chief_complaint(is_in_profile=true)."
            if session.get("contraindications_verdict") != "proceed":
                return "BOOKING_BLOCKED: перед записью нужен verify_contraindications_check с verdict=proceed."
            if 16 <= int(session.get("age") or 0) < 18 and not session.get("minor_parent_notice_given"):
                session["minor_parent_notice_given"] = True
                state.save_session(chat_id, session)
                return "BOOKING_BLOCKED: пациенту нет 18 лет. Перед записью обязательно напиши: «Так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿». После подтверждения можно продолжить запись."
            patient_name = str(args.get("patient_name") or session.get("patient_name") or "").strip()
            if not patient_name:
                return "BOOKING_BLOCKED: не указано имя пациента. Пациент уже готов к записи — спроси коротко: Подскажите, пожалуйста, Ваше имя для записи? Не обращайся по имени в приветствии."
            # Перед записью повторно проверяем слот, чтобы уменьшить риск гонки.
            date = str(args["date"])
            time_start = str(args["time_start"])
            doctor_login = str(args["doctor_login"])
            slots = await crm.check_slots(date, doctor_login)
            available = []
            for item in slots.get("availability", []):
                if (item.get("doctorLogin") or "") == doctor_login:
                    available.extend(item.get("availableSlots") or [])
            if time_start not in available:
                return "BOOKING_BLOCKED: выбранное время уже занято или недоступно. Предложи пациенту другое свободное время из check_available_slots."
            booked = await crm.book_appointment(
                patient_name=patient_name,
                phone=normalized_phone,
                doctor_login=doctor_login,
                doctor_name=args.get("doctor_name"),
                date=date,
                time_start=time_start,
                notes=args.get("notes") or session.get("complaint") or "Запись через WhatsApp-бота",
            )
            session["patient_name"] = patient_name
            session["last_booking"] = booked
            state.save_session(chat_id, session)
            try:
                await crm.log_outcome(phone=normalized_phone, outcome="booked", appointment_id=booked.get("appointmentId"), note="Записан через бота")
            except Exception:
                pass
            return "BOOKING_CREATED: " + _json_dumps(booked)

        if name == "cancel_appointment":
            lookup = session.get("patient_lookup") or None
            appointment_id = args.get("appointment_id") or _active_appointment_id(lookup)
            result = await crm.cancel_appointment(phone=normalized_phone, appointment_id=int(appointment_id) if appointment_id else None, reason=args.get("reason") or "отмена через бота")
            try:
                await crm.log_outcome(phone=normalized_phone, outcome="rejected", appointment_id=result.get("appointmentId"), note="Отмена записи через бота")
            except Exception:
                pass
            return "APPOINTMENT_CANCELLED: " + _json_dumps(result)

        if name == "reschedule_appointment":
            lookup = session.get("patient_lookup") or None
            appointment_id = args.get("appointment_id") or _active_appointment_id(lookup)
            result = await crm.reschedule_appointment(phone=normalized_phone, appointment_id=int(appointment_id) if appointment_id else None, new_date=str(args["new_date"]), new_time_start=str(args["new_time_start"]), reason=args.get("reason") or "перенос через бота")
            try:
                await crm.log_outcome(phone=normalized_phone, outcome="booked", appointment_id=result.get("appointmentId"), note="Перенос записи через бота")
            except Exception:
                pass
            return "APPOINTMENT_RESCHEDULED: " + _json_dumps(result)

        if name == "escalate_to_human":
            result = await crm.escalate_to_operator(phone=normalized_phone, reason=str(args.get("reason") or "нужен оператор")[:500])
            try:
                await crm.log_outcome(phone=normalized_phone, outcome="escalated", note=str(args.get("reason") or "нужен оператор")[:500])
            except Exception:
                pass
            return "ESCALATION_RESULT: " + _json_dumps(result)

        if name == "mark_irrelevant":
            try:
                await crm.log_outcome(phone=normalized_phone, outcome="out_of_scope", note=str(args.get("reason") or "непрофильный запрос")[:500])
            except Exception:
                pass
            return "OUTCOME_LOGGED: out_of_scope"

        if name == "get_clinic_info":
            lang = session.get("language") or "ru"
            text = get_clinic_info(str(args.get("topic") or ""), lang)
            return text or "NO_TEMPLATE_FOUND"

        return f"UNKNOWN_TOOL: {name}"
    except Exception as exc:
        return f"TOOL_ERROR {name}: {exc}"



def _remove_name_addressing(answer: str, session: dict[str, Any]) -> str:
    """Убирает обращение по имени из обычных ответов.

    Имя может быть нужно CRM для записи, но пациенту не пишем «Здравствуйте, Асель!»
    или «Добрый день, Виктор!» — это было отдельное требование.
    """
    text = answer or ""
    names: list[str] = []
    patient_name = str(session.get("patient_name") or "").strip()
    if patient_name:
        names.extend(part for part in re.split(r"\s+", patient_name) if len(part) > 1)
    lookup_patient = (session.get("patient_lookup") or {}).get("patient") or {}
    crm_name = str(lookup_patient.get("name") or "").strip()
    if crm_name:
        names.extend(part for part in re.split(r"\s+", crm_name) if len(part) > 1)

    for name in sorted(set(names), key=len, reverse=True):
        safe = re.escape(name)
        text = re.sub(rf"^(Здравствуйте|Добрый день|Доброе утро|Добрый вечер),\s*{safe}[!,.]?", r"\1!", text, flags=re.IGNORECASE)
        text = re.sub(rf"(^|[\n\-—])\s*{safe},\s*", r"\1", text, flags=re.IGNORECASE)
    return text


def _finalize_answer(chat_id: str, user_text: str, answer: str, session: dict[str, Any]) -> str:
    """Единая точка перед отправкой пациенту.

    Любой ответ — от original_engine, questionnaire или GPT — проходит через
    response_guard. Если guard нашёл нарушение промпта/логики, он исправляет
    ответ и пишет событие в SQLite.
    """
    answer = answer.replace("окошки", "свободное время для записи").replace("Окошки", "Свободное время для записи")
    answer = _remove_name_addressing(answer, session)
    fixed, violations = validate_answer(chat_id, user_text, answer, session)
    if violations:
        try:
            state.log_bot_action(
                chat_id,
                "guard_blocked",
                "Prompt compliance guard fixed answer",
                tool_name="response_guard",
                tool_args={"user_text": user_text, "violations": violations},
                tool_result=fixed,
            )
        except Exception:
            pass
    return fixed


async def handle_message(chat_id: str, phone: str, user_text: str) -> str:
    settings = get_settings()

    session = state.get_session(chat_id)
    if not session.get("language"):
        session["language"] = _detect_language(user_text)
    else:
        session["language"] = _detect_language(user_text, session.get("language"))
    session["phone"] = sanitize_kz_phone(phone) or phone
    # Всегда пробуем подтянуть свежую карточку пациента: это помогает GPT видеть активную запись
    # и не создавать дубли/не принимать «Приду» за имя.
    session = await _safe_refresh_patient_lookup(chat_id, phone, session)
    state.save_session(chat_id, session)

    state.add_message(chat_id, "user", user_text)

    # Original-engine слой: ранние гейты как в старом боте.
    # Работает ДО questionnaire и ДО GPT: отмена/перенос/активная запись.
    pre_answer = await pre_handle_message(chat_id=chat_id, phone=phone, user_text=user_text, session=session)
    if pre_answer:
        pre_answer = _finalize_answer(chat_id, user_text, pre_answer, session)
        state.add_message(chat_id, "assistant", pre_answer)
        return pre_answer

    # Жёсткий контроллер анкеты: не даёт GPT перескакивать и путать шаги записи.
    controlled_answer = await handle_questionnaire(chat_id=chat_id, phone=phone, user_text=user_text, session=session)
    if controlled_answer:
        controlled_answer = _finalize_answer(chat_id, user_text, controlled_answer, session)
        state.add_message(chat_id, "assistant", controlled_answer)
        return controlled_answer

    # GPT нужен только для нестандартных вопросов. Базовая анкета выше должна работать даже без OpenAI.
    if not settings.openai_api_key:
        answer = "Подскажите, пожалуйста, что Вас беспокоит? 🌿"
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    # Если несовершеннолетний уточняет про родителя — отвечаем прямо и не путаем это с противопоказаниями.
    if 16 <= int(session.get("age") or 0) < 18 and _asks_about_parent(user_text):
        session["minor_parent_notice_given"] = True
        state.save_session(chat_id, session)
        if session.get("language") == "kk":
            answer = "Иә, Сіз 18 жасқа толмағандықтан, консультацияға ата-анаңызбен немесе заңды өкіліңізбен келу қажет 🌿"
        else:
            answer = "Да, так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿"
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    # Ответ «да» на уточнение про возможность прийти означает, что пациент МОЖЕТ прийти.
    # Это нельзя трактовать как отказ/лежачее состояние.
    if session.get("mobility_check_pending") and _is_yes_answer(user_text):
        session["mobility_check_pending"] = False
        session["mobility_ok"] = True
        state.save_session(chat_id, session)
        answer = "Хорошо, тогда продолжим запись 🌿 Перед записью уточню: есть ли у Вас противопоказания?"
        if session.get("language") == "kk":
            answer = "Жақсы, онда жазылуды жалғастырайық 🌿 Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    if session.get("mobility_check_pending") and _is_no_answer(user_text):
        session["mobility_check_pending"] = False
        state.save_session(chat_id, session)
        answer = get_clinic_info("immobility_refuse", session.get("language") or "ru") or "К сожалению, в таком случае лечение в клинике может быть затруднительным. Передам администратору для проверки."
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    # Короткие подтверждения обрабатываем отдельным GPT-ответом с жёстким контекстом.
    # Это исправляет критичный кейс: «Очень приятно, Приду».
    if _is_short_confirmation(user_text):
        answer = await _smart_confirmation_reply(user_text, session, session.get("language") or "ru")
        answer = answer.replace("окошки", "свободное время для записи").replace("Окошки", "Свободное время для записи")
        answer = _remove_name_addressing(answer, session)
        if "самостоятельно прийти" in answer.lower() or "передвигаться по клинике" in answer.lower():
            session["mobility_check_pending"] = True
            state.save_session(chat_id, session)
        if "родителем или законным представителем" in answer.lower() or "заңды өкілі" in answer.lower():
            session["minor_parent_notice_given"] = True
            state.save_session(chat_id, session)
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=1)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "system", "content": _runtime_prompt(session, phone)},
        {"role": "system", "content": "Финальный контроль качества: отвечай как живой администратор, но не фантазируй. Перед каждым ответом проверь: 1) не принял ли ты подтверждение за имя; 2) не спросил ли имя слишком рано — имя спрашивается только на финальном этапе записи; 3) не обратился ли к пациенту по имени — в обычных сообщениях имя не используем; 4) не перескочил ли язык; 5) ответил ли на вопрос пациента по смыслу; 5.1) не написал ли «с такой жалобой», если пациент ещё не указал жалобу; 5.2) ответ на языке пациента; 5.3) симптомы не приняты за противопоказания; 5.4) ответ «до этого не было/нет/жоқ» на вопрос противопоказаний принят как отсутствие противопоказаний; 5.5) «ходить не могу» обработано как ограничение передвижения; 5.6) если пациенту 16–17 лет — предупредил про родителя/законного представителя; 5.7) вопрос «с родителем приходить?» обработан прямым ответом «да»; 5.8) ответ «да» на вопрос про возможность прийти не принят за отказ; 6) не создаёшь ли дубль при активной записи; 7) не выдумываешь ли врача/время/факт записи без CRM-инструмента; 7.1) перед book_appointment были ли реально предложены свободные записи из check_available_slots и выбран ли пациентом конкретный вариант; 8) если пациент просит отменить запись — обязательно вызвал ли cancel_appointment до текстового ответа; 9) если вопрос про МРТ/КТ/рентген/УЗИ/анализы/снимки — GPT должен сам понять контекст, вызвать get_clinic_info(topic=\"mri_needed\") и не отказывать сухо; 10) вопрос противопоказаний должен быть коротким, без полного чек-листа, если пациент сам не просит подробности. Если не уверен — уточни или передай оператору."},
    ]
    if getattr(settings, "learn_admin_dialogs_enabled", True):
        examples = state.get_recent_admin_style_examples(getattr(settings, "admin_style_examples_limit", 18))
        if examples:
            messages.append({
                "role": "system",
                "content": "Ниже примеры реальных дневных ответов администраторов клиники. Используй их ТОЛЬКО как стиль общения: тон, краткость, мягкость, порядок фраз. Это НЕ история текущего пациента и НЕ контекст для продолжения дневного диалога. Не копируй персональные данные, даты, время, имена, диагнозы и факты из этих примеров. Факты по текущему пациенту бери только из текущей истории, CRM-инструментов и шаблонов.\n\n" + examples,
            })

    if hasattr(state, "get_history"):
        messages.extend(state.get_history(chat_id, limit=24))
    else:
        messages.append({"role": "user", "content": user_text})

    for _ in range(8):
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=getattr(settings, 'openai_dialog_temperature', 0.25),
            max_tokens=getattr(settings, 'openai_max_tokens', 1000),
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                result = await _execute_tool(chat_id, phone, call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            continue

        answer = (msg.content or "").strip()
        if not answer:
            answer = "Подскажите, пожалуйста, что Вас беспокоит? 🌿"
        answer = answer.replace("окошки", "свободное время для записи").replace("Окошки", "Свободное время для записи")
        answer = _remove_name_addressing(answer, session)
        if "самостоятельно прийти" in answer.lower() or "передвигаться по клинике" in answer.lower():
            session["mobility_check_pending"] = True
            state.save_session(chat_id, session)
        if "родителем или законным представителем" in answer.lower() or "заңды өкілі" in answer.lower():
            session["minor_parent_notice_given"] = True
            state.save_session(chat_id, session)
        answer = _finalize_answer(chat_id, user_text, answer, session)
        state.add_message(chat_id, "assistant", answer)
        return answer

    answer = "Подскажите, пожалуйста, что Вас беспокоит? 🌿"
    answer = _finalize_answer(chat_id, user_text, answer, session)
    state.add_message(chat_id, "assistant", answer)
    return answer

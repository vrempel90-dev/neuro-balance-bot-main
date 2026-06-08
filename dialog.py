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


SYSTEM_PROMPT_PATHS = [
    Path(__file__).resolve().parent / "system_prompt_original.md",
    Path(__file__).resolve().parent / "prompts" / "system_prompt_original.md",
]

KZ_MARKERS = [
    "сәлем", "салем", "salem", "ассала", "assalamu", "менің", "меним", "атым",
    "белім", "белим", "belim", "ауырады", "аурат", "aurat", "ертең", "ертен", "erten",
    "бүгін", "бугин", "bugin", "жоқ", "жок", "joq", "jok", "иә", "ия", "бар",
    "қарсы", "карсы", "көрсетілім", "корсетилим", "qarsy", "korsetilim",
]




CONFIRMATION_MARKERS = {
    "приду", "буду", "ок", "окей", "хорошо", "да", "ага", "понял", "поняла",
    "подтверждаю", "келемін", "келемин", "барамын", "жарайды", "ия", "иә", "иә", "иа",
}


NO_NAME_PATTERNS = [
    r"^\s*(очень\s+приятно|приятно\s+познакомиться)\s*,?\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
    r"^\s*(здравствуйте|добрый\s+день|добрый\s+вечер|сәлеметсіз\s+бе)\s*,\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
    r"^\s*(спасибо|рахмет)\s*,\s*[^.!?\n]{1,40}[.!?]?(\s+|$)",
]

def _strip_name_address(answer: str) -> str:
    """Финальная защита: не даём боту обращаться по имени.

    Даже если GPT/промпт ошибся и написал «Очень приятно, Приду», вырезаем
    обращение перед отправкой в WhatsApp. Имя остаётся только внутри CRM.
    """
    text = (answer or "").strip()
    if not text:
        return text
    for pat in NO_NAME_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE | re.UNICODE).strip()
    # Частые артефакты после удаления приветствия/имени
    text = re.sub(r"^[-—,\.\s]+", "", text).strip()
    text = text.replace("окошки", "свободное время для записи").replace("Окошки", "Свободное время для записи")
    text = text.replace("окошко", "свободное время для записи").replace("Окошко", "Свободное время для записи")
    # Если удалили всё из-за короткой фразы, даём безопасный нейтральный ответ
    return text or "Спасибо 🌿 Подскажите, пожалуйста, чем можем помочь?"

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
    return _strip_name_address(response.choices[0].message.content or "")
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
    if current in {"ru", "kk"}:
        return current
    low = (text or "").lower()
    if any(m in low for m in KZ_MARKERS):
        return "kk"
    return "ru"


def _runtime_prompt(session: dict[str, Any], phone: str) -> str:
    lang = session.get("language") or "ru"
    return f"""

## Runtime-добавление Python-версии / ВАЖНО
Текущая дата и время: {_astana_now_text()}.
Телефон пациента из WhatsApp: {phone}.
Зафиксированный язык диалога: {lang}. Отвечай строго на этом языке до конца диалога. Не перескакивай между русским и казахским.

Ты работаешь как ЖИВОЙ AI-администратор, а не как сухой скрипт.
С этого момента GPT является главным мозгом диалога: именно ты понимаешь намерение, контекст, язык и смысл сообщения. Python только исполняет CRM-инструменты и технически отправляет ответ.
Всегда сначала отвечай на смысл сообщения пациента, затем мягко переходи к следующему шагу записи.

КРИТИЧНО ПРО КОНТЕКСТ И ИМЯ:
- НИКОГДА не обращайся к пациенту по имени в ответах. Даже если имя известно из CRM или пользователь его сообщил, не пиши «Очень приятно, ...», «Здравствуйте, ...», «Спасибо, ...». Имя нужно только для записи в CRM.
- НЕ принимай любое короткое сообщение за имя.
- Слова «приду», «буду», «ок», «хорошо», «да», «понял», «поняла», «жарайды», «келемін», «барамын» — это подтверждение/согласие, НЕ имя пациента.
- Никогда не пиши «Очень приятно, Приду/Буду/Хорошо/Да/Ок».
- Если пациент пишет только «Приду», «Буду», «Да», «Ок», «Хорошо», «Жарайды», «Келемін» — это подтверждение/согласие, а НЕ имя, НЕ жалоба и НЕ новая заявка.
- Если активная запись уже есть — подтверждение означает, что пациент придёт. Не задавай заново имя, возраст и жалобу.
- Имя сохраняй только если это похоже на имя человека или есть формулировки «меня зовут ...», «я ...», «менің атым ...», «атым ...».
- Запрещено использовать в качестве имени: приду, буду, да, ок, хорошо, завтра, сегодня, ертең, бүгін, жоқ, жоқпын, подтверждаю, не знаю, хочу записаться.
- Если контекст неполный и сообщение выглядит как ответ на предыдущее сообщение администратора, не начинай сценарий заново. Сначала аккуратно уточни или подтверди смысл.

КРИТИЧНО ПРО ОБСЛЕДОВАНИЯ, КОТОРЫХ НЕТ В КЛИНИКЕ:
- Если пациент спрашивает про МРТ, КТ, рентген, УЗИ, анализы, снимки, диагностику или стоимость обследования — НЕ отвечай сухим отказом «у нас не делают».
- Всегда сначала помоги: объясни, что снимок/обследование заранее делать не обязательно, врач после консультации и осмотра подскажет, какое обследование действительно нужно.
- Для МРТ/снимков ОБЯЗАТЕЛЬНО вызывай get_clinic_info(topic="mri_needed") и используй его смысл/шаблон.
- Цель ответа — не потерять лида: успокоить, объяснить следующий шаг и мягко вернуть к консультации.
- Правильный смысл ответа: «Снимки в клинике не делают. Заранее делать не обязательно: при необходимости врач назначит после консультации с учётом симптомов и осмотра. Готовые снимки иногда бывают неинформативными, поэтому лучше сначала получить рекомендации специалиста».
- Нельзя писать: «мы не можем предоставить информацию» как финальный отказ. Это звучит как отказ и теряет пациента.

КРИТИЧНО ПРО ЖИВОЙ ДИАЛОГ:
- Каждый ответ должен звучать как сообщение администратора в WhatsApp, а не как FAQ или робот.
- Если клиент задал вопрос, сначала ответь на вопрос, затем продолжай запись.
- Не задавай следующий вопрос, пока не отреагировал на смысл последнего сообщения.
- Не используй канцелярит: «предоставить информацию», «данное учреждение», «рекомендуем обратиться» без помощи.
- Пиши коротко, тепло, но по делу.

Если пациент пишет жалобу или спрашивает «занимаетесь/лечите/можно к вам?», ОБЯЗАТЕЛЬНО сначала скажи, относится ли это к профилю клиники Neuro Balance:
- профиль клиники: спина, шея, поясница, суставы, плечо, сухожилия/связки, неврология, грыжи, протрузии, артроз/артрит, реабилитация;
- непрофильное: горло, зубы, ЛОР, кожа, гинекология, онкология как основное заболевание, острые неотложные состояния.
Если профильное — скажи, что с такой жалобой можно прийти на первичную консультацию, врач осмотрит и подскажет план. Только потом спроси следующий недостающий пункт.
Если непрофильное — не записывай автоматически, вызови escalate_to_human и log outcome через mark_irrelevant.

Главная архитектура:
- GPT ведёт живой диалог, понимает язык, смысл, намерение, жалобу, возражения и контекст переписки.
- GPT НЕ должен слепо идти по состоянию session, если последнее сообщение клиента явно противоречит этому состоянию. Например, если session ждёт имя, но клиент написал «Приду», это подтверждение, а не имя.
- Python-инструменты выполняют реальные действия CRM: пациент, врачи, услуги, слоты, запись, перенос, отмена, оператор, outcome.
- Не выдумывай врачей, даты, время и факт записи. Для расписания используй только check_available_slots. Для записи только book_appointment.
- Не спрашивай телефон: он уже известен из WhatsApp.
- Не используй слово «окошки». Говори «свободное время для записи».
- Проверку противопоказаний делай коротко: попроси подтвердить отсутствие противопоказаний или указать, что есть. Если пациент пишет «нет противопоказаний / жоқ / қарсы көрсетілім жоқ» — это proceed.
- Перед book_appointment обязательно должны быть: профильная жалоба, возраст 16–74, выбранный слот из CRM, verify_contraindications_check со статусом proceed.
- Если у пациента уже есть активная запись по patient_lookup, не создавай дубль: предложи подтвердить, отменить или перенести.
- Если пациент просит отменить/перенести — используй cancel_appointment/reschedule_appointment.

Текущее сохранённое состояние сессии:
{json.dumps(session, ensure_ascii=False, indent=2)}
""".strip()


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "find_existing_appointment", "description": "Проверить пациента по телефону: новый/повторный, активная запись, последний приём, статус лида. Вызывать в начале нового диалога и при отмене/переносе.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_services", "description": "Получить из CRM список того, что клиника лечит и что не лечит. Используй для профильности жалобы.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_doctor_info", "description": "Получить врачей и специализации из CRM для подбора врача по жалобе.", "parameters": {"type": "object", "properties": {"diagnosis": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "record_chief_complaint", "description": "Зафиксировать жалобу пациента и профильность. Вызывать после того, как пациент сообщил жалобу.", "parameters": {"type": "object", "properties": {"complaint": {"type": "string"}, "is_in_profile": {"type": "boolean"}, "service": {"type": "string"}}, "required": ["complaint", "is_in_profile"]}}},
    {"type": "function", "function": {"name": "check_available_slots", "description": "Проверить свободное время для записи в CRM на дату YYYY-MM-DD. Если doctor_login не указан — все врачи.", "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}, "doctor_login": {"type": "string"}}, "required": ["date"]}}},
    {"type": "function", "function": {"name": "verify_contraindications_check", "description": "Обязательная проверка противопоказаний перед записью. Возвращает proceed/refuse/escalate.", "parameters": {"type": "object", "properties": {"patient_age": {"type": "number"}, "has_pacemaker_or_implant": {"type": "boolean"}, "has_pregnancy": {"type": "boolean"}, "has_active_cancer": {"type": "boolean"}, "has_epilepsy": {"type": "boolean"}, "has_thrombosis_or_bleeding": {"type": "boolean"}, "has_decompensated_endocrine": {"type": "boolean"}, "has_acute_infection_or_fever": {"type": "boolean"}, "has_severe_cardio_or_respiratory": {"type": "boolean"}, "has_severe_psychiatric": {"type": "boolean"}, "uncertain_items": {"type": "array", "items": {"type": "string"}}}, "required": ["patient_age", "has_pacemaker_or_implant", "has_pregnancy", "has_active_cancer", "has_epilepsy", "has_thrombosis_or_bleeding", "has_decompensated_endocrine", "has_acute_infection_or_fever", "has_severe_cardio_or_respiratory", "has_severe_psychiatric", "uncertain_items"]}}},
    {"type": "function", "function": {"name": "book_appointment", "description": "Создать запись в CRM. Только после профильной жалобы, выбранного слота и противопоказаний proceed.", "parameters": {"type": "object", "properties": {"patient_name": {"type": "string"}, "doctor_login": {"type": "string"}, "doctor_name": {"type": "string"}, "date": {"type": "string"}, "time_start": {"type": "string"}, "notes": {"type": "string"}}, "required": ["patient_name", "doctor_login", "date", "time_start"]}}},
    {"type": "function", "function": {"name": "cancel_appointment", "description": "Отменить активную запись пациента через CRM.", "parameters": {"type": "object", "properties": {"appointment_id": {"type": "number"}, "reason": {"type": "string"}}}}},
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
            decision = evaluate_contraindications(args)
            session["contraindications_verdict"] = decision["verdict"]
            session["contraindications_reason"] = decision["reason"]
            if args.get("patient_age"):
                session["age"] = int(args.get("patient_age") or 0)
            state.save_session(chat_id, session)
            return decision["messageForBot"]

        if name == "book_appointment":
            if not session.get("can_help"):
                return "BOOKING_BLOCKED: жалоба не зафиксирована как профильная. Сначала record_chief_complaint(is_in_profile=true)."
            if session.get("contraindications_verdict") != "proceed":
                return "BOOKING_BLOCKED: перед записью нужен verify_contraindications_check с verdict=proceed."
            patient_name = str(args.get("patient_name") or session.get("patient_name") or "").strip()
            if not patient_name:
                return "BOOKING_BLOCKED: не указано имя пациента. Спроси имя."
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
            text = get_clinic_info(str(args.get("topic") or ""))
            return text or "NO_TEMPLATE_FOUND"

        return f"UNKNOWN_TOOL: {name}"
    except Exception as exc:
        return f"TOOL_ERROR {name}: {exc}"


async def handle_message(chat_id: str, phone: str, user_text: str) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        return "Передам ваш вопрос координатору, она ответит вам в ближайшее время."

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

    # Короткие подтверждения обрабатываем отдельным GPT-ответом с жёстким контекстом.
    # Это исправляет критичный кейс: «Очень приятно, Приду».
    if _is_short_confirmation(user_text):
        answer = await _smart_confirmation_reply(user_text, session, session.get("language") or "ru")
        answer = _strip_name_address(answer)
        state.add_message(chat_id, "assistant", answer)
        return answer

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=1)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "system", "content": _runtime_prompt(session, phone)},
        {"role": "system", "content": "Финальный контроль качества: отвечай как живой администратор, но не фантазируй. Никогда не обращайся к пациенту по имени. Перед каждым ответом проверь: 1) не принял ли ты подтверждение за имя; 2) не перескочил ли язык; 3) ответил ли на вопрос пациента по смыслу; 4) не создаёшь ли дубль при активной записи; 5) не выдумываешь ли врача/слот/факт записи без CRM-инструмента; 6) если вопрос про МРТ/КТ/рентген/УЗИ/анализы/снимки — не отказывай сухо, объясни, что заранее делать не обязательно и врач подскажет после консультации; 7) не используй фразу 'не можем предоставить информацию' как финальный отказ. Если не уверен — уточни или передай оператору."},
    ]
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
            temperature=0.35,
            max_tokens=900,
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
            answer = "Передам ваш вопрос координатору, она ответит вам в ближайшее время."
        answer = _strip_name_address(answer)
        state.add_message(chat_id, "assistant", answer)
        return answer

    answer = "Передам ваш вопрос координатору, она ответит вам в ближайшее время."
    state.add_message(chat_id, "assistant", answer)
    return answer

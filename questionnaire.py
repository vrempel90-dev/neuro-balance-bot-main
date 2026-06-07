
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import crm
import state
from clinic_info import get_clinic_info
from phone import sanitize_kz_phone
from language_guard import detect_language as detect_message_language
from doctor_router import choose_doctor_for_complaint


# -----------------------------
# Маленький контроллер анкеты
# -----------------------------
# Задача контроллера: держать железный порядок записи.
# GPT остаётся живым администратором для формулировок и нестандартных вопросов,
# но критичные шаги анкеты не должны зависеть от фантазии модели.


KZ_MARKERS = [
    "сәлем", "салем", "аяқ", "қол", "бел", "мойын", "буын", "ауырады", "ауыр",
    "жас", "жастамын", "жоқ", "иә", "жазыл", "келемін", "уақыт",
    "дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі",
    "ата", "ана", "ата-ан", "қарсы", "көрсетілім", "жүре", "алмаймын",
]
KZ_LETTERS = set("әғқңөұүһі")

GENERIC_BOOKING = [
    "хочу записаться", "записаться", "запишите", "консультац", "прием", "приём",
    "по акции", "акция", "50%", "скид", "instagram", "инстаграм", "здравствуйте",
    "добрый", "привет", "сәлем", "салем", "жазыл", "консультацияға",
]
COMPLAINT_WORDS = [
    "болит", "боль", "ноет", "тянет", "ломит", "отдает", "отдаёт", "онемение",
    "немеет", "хрустит", "спина", "шея", "поясница", "сустав", "колено", "плечо",
    "нога", "рука", "голова", "грыжа", "протруз", "протрузия", "остеохондроз",
    "сколиоз", "кифоз", "радикулит", "артроз", "артрит", "после перелома",
    "перелом", "травм", "ходить не могу", "день поясница", "дня поясница",
    "ауырады", "ауыр", "бел", "мойын", "буын", "тізе", "иық", "аяқ", "қол",
    "омыртқа", "жарық", "жарақат", "сынған",
]

YES_WORDS = ["да", "ага", "хорошо", "смогу", "могу", "приду", "подтверждаю", "иә", "ия", "болады", "келемін", "келемин"]
NO_CONTRA = [
    "нет", "нету", "не было", "до этого не было", "раньше не было", "противопоказаний нет",
    "жоқ", "жок", "болған жоқ", "қарсы көрсетілім жоқ", "жоқ еді"
]
GENERIC_CONTRA_YES = ["есть", "да есть", "есть противопоказания", "иә бар", "бар"]
REAL_CONTRA = [
    "кардиостимулятор", "имплант", "беремен", "беременность", "жүктілік",
    "онколог", "рак", "эпилеп", "тромб", "кровотеч", "қан кет", "температур",
    "инфекц", "диабет", "жүрек", "сердеч", "псих"
]
SYMPTOM_NOT_CONTRA = [
    "болит", "боль", "грыжа", "протруз", "онемение", "немеет", "тянет", "ломит",
    "отдает", "отдаёт", "омыртқа", "жарық", "мүмкін", "қатты", "ауырып",
    "ауырсыну", "аяқ", "бел", "мойын", "буын", "после перелома", "перелом"
]
IMMOBILITY = ["ходить не могу", "не могу ходить", "не передвигаюсь", "лежач", "коляск", "жүре алмаймын", "бара алмаймын"]
PARENT_Q = ["с родителем", "с родителями", "родитель", "мама", "папа", "законным представителем", "ата-ан", "әкем", "анам"]
STOP_BOOKING_Q = [
    "не надо", "не нужно", "не хочу", "передумал", "отмена", "отмените",
    "керек емес", "қажет емес", "жоқ керек емес", "жазылмаймын"
]

NO_COMPLAINT_PHRASES = [
    "ничего", "ниче", "ничего не беспокоит", "не беспокоит", "просто диагностику",
    "просто провериться", "провериться", "для профилактики", "ештеңе", "мазаламайды",
]
WHAT_TO_BRING_Q = [
    "что взять", "что брать", "с собой", "документы", "удостоверение", "снимки брать",
    "мрт брать", "анализы брать", "необходимо взять", "алып келу", "не алып келем",
]
TREATMENT_Q = [
    "какое лечение", "какие лечения", "чем лечите", "как лечите", "что за лечение",
    "методы лечения", "какие методы", "процедуры", "какие процедуры", "что делаете",
    "лечение вообще", "ем қандай", "қалай емдейсіз", "қандай ем", "процедура",
]
ADDRESS_Q = [
    "адрес", "где находитесь", "где вы находитесь", "как доехать", "локация",
    "2гис", "2gis", "геолокация", "местоположение", "мекенжай", "қай жерде",
]
SCHEDULE_Q = [
    "завтра вы не работаете", "завтра работаете", "вы работаете завтра",
    "график", "режим работы", "во сколько работаете", "до скольки работаете",
    "сегодня работаете", "в понедельник работаете", "в субботу работаете",
    "ертең жұмыс", "жұмыс уақыты", "график",
]
PRICE_Q = ["тг ма", "тг?", "5000", "5 000", "сколько стоит", "цена", "стоимость", 
    "сколько стоит", "цена", "стоимость", "прайс", "сколько будет стоить",
    "сколько стоит лечебная процедура", "стоимость лечения", "цена лечения",
    "5000", "5 000", "тг", "тенге", "ма?",
    "қанша тұрады", "бағасы", "құны",
]


def _lang(text: str, session: dict[str, Any]) -> str:
    # v30: язык берём по последнему сообщению, а не по старой сессии.
    # Если пациент пишет по-русски («грыжа», «протрузия», «сколько стоит») —
    # ответ строго на русском, даже если раньше в чате был казахский.
    detected = detect_message_language(text, session.get("language") or "ru")
    return detected


def _ru(lang: str, ru: str, kk: str) -> str:
    return kk if lang == "kk" else ru


def _clean(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


def _has_any(text: str, words: list[str]) -> bool:
    low = _clean(text)
    return any(w in low for w in words)


def _is_no_complaint_answer(text: str) -> bool:
    low = _clean(text)
    return any(w == low or w in low for w in NO_COMPLAINT_PHRASES)


def _asks_what_to_bring(text: str) -> bool:
    return _has_any(text, WHAT_TO_BRING_Q)


def _asks_treatment_methods(text: str) -> bool:
    return _has_any(text, TREATMENT_Q)


def _asks_address(text: str) -> bool:
    return _has_any(text, ADDRESS_Q)


def _asks_schedule(text: str) -> bool:
    return _has_any(text, SCHEDULE_Q)


def _asks_price(text: str) -> bool:
    return _has_any(text, PRICE_Q)


def _is_stop_booking(text: str) -> bool:
    low = _clean(text)
    return any(w == low or w in low for w in STOP_BOOKING_Q)


def _safe_continue(lang: str, session: dict[str, Any]) -> str:
    cont = _continue_current_step(lang, session)
    # Если список слотов уже был отправлен, не надо снова слать весь список.
    if session.get("last_slots") and not session.get("selected_time"):
        return "Какое время из вариантов выше Вам удобно?" if lang != "kk" else "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"
    return cont


def _price_answer(lang: str, text: str) -> str:
    low = _clean(text)
    if "лечеб" in low or "процедур" in low or "лечение" in low:
        return get_clinic_info("price_course", lang) or (
            "Стоимость лечения подбирается индивидуально после консультации врача, потому что зависит от диагноза и количества процедур 🌿"
        )
    return get_clinic_info("price_first_visit", lang) or ("Алғашқы консультация 5 000 тг 🌿" if lang == "kk" else "Первичная консультация стоит 5 000 тг 🌿")


def _continue_current_step(lang: str, session: dict[str, Any]) -> str:
    """Мягко возвращает к текущему шагу без повторного списка слотов."""
    if not session.get("complaint"):
        return "Подскажите, пожалуйста, что Вас беспокоит?" if lang != "kk" else "Айтыңызшы, Сізді не мазалайды?"
    if not session.get("age"):
        return "Подскажите, пожалуйста, сколько Вам лет?" if lang != "kk" else "Жасыңыз нешеде?"
    if session.get("selected_time") and not session.get("contraindications_verdict"):
        return "Перед записью уточню: есть ли у Вас противопоказания?" if lang != "kk" else "Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
    if not session.get("preferred_date") and not session.get("selected_date"):
        return "На какой день Вам удобно прийти?" if lang != "kk" else "Қай күн ыңғайлы?"
    if not session.get("selected_time"):
        # Если слоты уже показывали, не дублируем весь список снова.
        if session.get("last_slots"):
            return "Какое время из вариантов выше Вам удобно?" if lang != "kk" else "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"
        return "Какое время Вам удобно?" if lang != "kk" else "Қай уақыт ыңғайлы?"
    if not session.get("contraindications_verdict"):
        return "Перед записью уточню: есть ли у Вас противопоказания?" if lang != "kk" else "Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
    if not session.get("patient_name"):
        return "Подскажите, пожалуйста, Ваше имя для записи?" if lang != "kk" else "Жазу үшін аты-жөніңізді жазып жіберіңізші."
    return ""


def _treatment_methods_answer(lang: str) -> str:
    if lang == "kk":
        return (
            "Бізде емді дәрігер консультациядан және қараудан кейін жеке таңдайды 🌿 "
            "Клиникада магнитотерапия, лазерлік терапия, соққы-толқын терапиясы, PRP плазмотерапия, "
            "иглотерапия және ЛФК қолданылады. Нақты қандай процедура керек екенін дәрігер жағдайыңызға қарап айтады."
        )
    return (
        "В нашей клинике лечение подбирает врач индивидуально после консультации и осмотра 🌿 "
        "Используются магнитотерапия, лазерная терапия, ударно-волновая терапия, плазмотерапия PRP, "
        "иглотерапия и ЛФК. Что именно подойдёт в Вашем случае, врач скажет после осмотра."
    )


def _what_to_bring_answer(lang: str, session: dict[str, Any]) -> str:
    minor = 16 <= int(session.get("age") or 0) < 18
    if lang == "kk":
        base = "Өзіңізбен жеке куәлік болса жеткілікті. Егер дайын МРТ/рентген/қорытынды болса, ала келуге болады, бірақ алдын ала арнайы жасату міндетті емес — дәрігер қажет болса консультациядан кейін айтады 🌿"
        if minor:
            base = "Сіз 18 жасқа толмағандықтан, ата-анаңызбен немесе заңды өкіліңізбен келу қажет 🌿\n" + base
        return base
    base = "С собой достаточно взять удостоверение. Если есть готовые снимки, МРТ/рентген или заключения — можно взять с собой, но заранее специально делать не обязательно: врач после осмотра подскажет, что действительно нужно 🌿"
    if minor:
        base = "Так как Вам нет 18 лет, нужно прийти с родителем или законным представителем 🌿\n" + base
    return base


def _is_booking_intent(text: str) -> bool:
    return _has_any(text, GENERIC_BOOKING)


def _has_complaint(text: str) -> bool:
    return _has_any(text, COMPLAINT_WORDS)


def _looks_like_name(text: str) -> bool:
    clean = (text or "").strip()
    low = _clean(clean)
    if not clean or len(clean) > 60:
        return False
    banned = set(YES_WORDS + NO_CONTRA + ["завтра", "сегодня", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"])
    if low in banned:
        return False
    if any(ch.isdigit() for ch in clean):
        return False
    return bool(re.match(r"^[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,}$", clean))


def _extract_age(text: str, step: str = "") -> int | None:
    low = _clean(text)

    # v25: не путать длительность боли с возрастом:
    # «3 день поясница», «2 недели болит», «5 месяцев» — это НЕ возраст.
    duration_patterns = [
        r"\b\d{1,2}\s*(день|дня|дней|недел|неделя|недели|месяц|месяца|месяцев|сутки|суток)\b",
        r"\b(день|дня|дней|недел|неделя|недели|месяц|месяца|месяцев)\s*\d{1,2}\b",
    ]
    if any(re.search(p, low) for p in duration_patterns):
        if not any(w in low for w in ["лет", "год", "жас", "жастамын", "мне"]):
            return None

    nums = re.findall(r"\b(\d{1,2})\b", low)
    if not nums:
        return None

    # На шаге возраста одиночное число — возраст, но только если это не длительность/время.
    if step == "age":
        # «16.00», «16:00» — это время, не возраст.
        if re.search(r"\b\d{1,2}[:\.]\d{2}\b", low):
            return None
        return int(nums[0])

    # Возраст должен иметь явный контекст.
    m = re.search(r"\b(?:мне\s*)?(\d{1,2})\s*(?:лет|года|год|жас|жастамын)\b", low)
    if m:
        return int(m.group(1))
    m = re.search(r"\bмне\s*(\d{1,2})\b", low)
    if m:
        return int(m.group(1))
    return None


def _astana_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5)


WEEKDAYS_RU = {
    "понедельник": 0, "понедельник": 0, "в понедельник": 0,
    "вторник": 1, "во вторник": 1,
    "среда": 2, "среду": 2, "в среду": 2,
    "четверг": 3, "в четверг": 3,
    "пятница": 4, "пятницу": 4, "в пятницу": 4,
    "суббота": 5, "субботу": 5, "в субботу": 5,
    "воскресенье": 6, "в воскресенье": 6,
}
WEEKDAYS_KK = {
    "дүйсенбі": 0, "дуйсенби": 0,
    "сейсенбі": 1, "сейсенби": 1,
    "сәрсенбі": 2, "сарсенби": 2,
    "бейсенбі": 3, "бейсенби": 3,
    "жұма": 4, "жума": 4,
    "сенбі": 5, "сенби": 5,
    "жексенбі": 6, "жексенби": 6,
}


def _parse_date(text: str) -> str | None:
    low = _clean(text)
    now = _astana_now().date()
    if "сегодня" in low or "бүгін" in low or "бугин" in low:
        return now.isoformat()
    if "завтра" in low or "ертең" in low or "ертен" in low:
        return (now + timedelta(days=1)).isoformat()
    # dd.mm or dd/mm
    m = re.search(r"\b(\d{1,2})[\.\/\-](\d{1,2})(?:[\.\/\-](\d{2,4}))?\b", low)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else now.year
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    for name, wd in {**WEEKDAYS_RU, **WEEKDAYS_KK}.items():
        if name in low:
            delta = (wd - now.weekday()) % 7
            if delta == 0:
                delta = 7
            return (now + timedelta(days=delta)).isoformat()
    return None


def _format_date_human(date_iso: str, lang: str) -> str:
    try:
        dt = datetime.fromisoformat(date_iso).date()
    except Exception:
        return date_iso
    ru_days = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
    kk_days = ["дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі"]
    if lang == "kk":
        return kk_days[dt.weekday()]
    return ru_days[dt.weekday()]


def _is_sunday(date_iso: str) -> bool:
    try:
        return datetime.fromisoformat(date_iso).date().weekday() == 6
    except Exception:
        return False


def _next_monday_after(date_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(date_iso).date()
    except Exception:
        dt = _astana_now().date()
    delta = (0 - dt.weekday()) % 7
    if delta == 0:
        delta = 1
    return (dt + timedelta(days=delta)).isoformat()


def _sunday_primary_notice(lang: str, monday_iso: str) -> str:
    if lang == "kk":
        return (
            "Жексенбі күні клиника тек процедуралар үшін жұмыс істейді, алғашқы пациенттерді қабылдамайды 🌿\n"
            f"Алғашқы консультацияға {_format_date_human(monday_iso, lang)} күнге қараймыз."
        )
    return (
        "В воскресенье клиника работает только на процедуры, первичных пациентов не принимаем 🌿\n"
        f"Для первичной консультации посмотрю ближайший день — {_format_date_human(monday_iso, lang)}."
    )



def _parse_time(text: str, slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    low = _clean(text)
    # exact time, 10:00 / 10 00 / 10
    m = re.search(r"\b(\d{1,2})(?::|\s)?(\d{2})?\b", low)
    if not m:
        return None
    h = int(m.group(1))
    minute = m.group(2)
    candidates = []
    if minute is not None:
        candidates.append(f"{h:02d}:{int(minute):02d}")
    else:
        candidates.append(f"{h:02d}:00")
        candidates.append(f"{h}:00")
    for slot in slots or []:
        if str(slot.get("time")) in candidates or str(slot.get("time")).lstrip("0") in [c.lstrip("0") for c in candidates]:
            return slot
    return None


def _parse_requested_time(text: str) -> str | None:
    low = _clean(text)
    # Ищем именно время, а не первое число в сообщении. Например:
    # «мне 35, хочу завтра в 10:00» → нужно взять 10:00, а не 35.
    matches = list(re.finditer(r"\b(\d{1,2})(?::|\.|\s)?(\d{2})?\b", low))
    for m in matches:
        h = int(m.group(1))
        if h > 23:
            continue
        minute = m.group(2)
        before = low[max(0, m.start() - 8):m.start()]
        after = low[m.end():m.end() + 8]
        has_time_context = any(w in before or w in after for w in ["в ", "на ", "к ", "сағат", "утра", "дня", "вечера", ":"])
        if minute is not None:
            return f"{h:02d}:{int(minute):02d}"
        if has_time_context:
            return f"{h:02d}:00"
    return None


def _slot_by_requested_time(slots: list[dict[str, Any]], requested_time: str | None) -> dict[str, Any] | None:
    if not requested_time:
        return None
    for slot in slots or []:
        if str(slot.get("time")).lstrip("0") == requested_time.lstrip("0"):
            return slot
    return None


def _extract_slots(data: dict[str, Any], date_iso: str, limit: int = 5) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in data.get("availability", []) or []:
        doctor_login = item.get("doctorLogin") or item.get("login") or item.get("doctor_login") or "reserve"
        doctor_name = item.get("doctorName") or item.get("name") or item.get("doctor_name") or "дежурный врач"
        for t in item.get("availableSlots") or item.get("slots") or []:
            if len(result) >= limit:
                return result
            result.append({
                "date": date_iso,
                "time": str(t),
                "doctor_login": str(doctor_login),
                "doctor_name": str(doctor_name),
            })
    return result


def _slots_text(slots: list[dict[str, Any]], date_iso: str, lang: str) -> str:
    date_h = _format_date_human(date_iso, lang)
    def display_doctor(s: dict[str, Any], kk: bool = False) -> str:
        login = str(s.get("doctor_login") or "").lower()
        name = str(s.get("doctor_name") or "").strip()
        if login == "reserve" or name.lower() in {"reserve", "резерв", "без закреплённого врача", "без закрепленного врача"}:
            return "дәрігер" if kk else "дежурный врач"
        return name or ("дәрігер" if kk else "врач")

    if lang == "kk":
        lines = [f"{date_h.capitalize()} күні мына уақыттар бар:"]
        for s in slots:
            lines.append(f"— {s['time']} — {display_doctor(s, True)}")
        lines.append("")
        lines.append("Қай уақыт ыңғайлы?")
        return "\n".join(lines)
    lines = [f"На {date_h} есть свободное время для записи:"]
    for s in slots:
        lines.append(f"— {s['time']} — {display_doctor(s, False)}")
    lines.append("")
    lines.append("Какое время Вам удобно?")
    return "\n".join(lines)


def _is_no_contra(text: str) -> bool:
    return _has_any(text, NO_CONTRA)


def _is_real_contra(text: str) -> bool:
    return _has_any(text, REAL_CONTRA)


def _is_generic_contra_yes(text: str) -> bool:
    low = _clean(text)
    return low in GENERIC_CONTRA_YES or ("противопоказ" in low and "нет" not in low) or ("қарсы" in low and "жоқ" not in low)


def _is_symptom_not_contra(text: str) -> bool:
    return _has_any(text, SYMPTOM_NOT_CONTRA) and not _is_real_contra(text)


def _asks_nearest(text: str) -> bool:
    low = _clean(text)
    return any(w in low for w in ["ближай", "любой", "как можно раньше", "первый свобод", "ертерек", "жақын", "кез келген"])


async def _show_nearest_slots(chat_id: str, session: dict[str, Any], lang: str, days: int = 10) -> str:
    start = _astana_now().date()
    for offset in range(0, max(1, days)):
        date_iso = (start + timedelta(days=offset)).isoformat()
        try:
            data = await crm.check_slots(date_iso)
        except Exception:
            continue
        slots = _extract_slots(data, date_iso, limit=5)
        if slots:
            session["preferred_date"] = date_iso
            session["last_slots"] = slots
            session["questionnaire_step"] = "time"
            state.save_session(chat_id, session)
            return _slots_text(slots, date_iso, lang)
    return _ru(lang,
        "На ближайшие дни свободного времени не нашла. Передам администратору, чтобы он помог подобрать запись 🌿",
        "Жақын күндерге бос уақыт таппадым. Әкімшіге жіберемін, ол жазылуға көмектеседі 🌿"
    )


async def _show_slots(chat_id: str, session: dict[str, Any], date_iso: str, lang: str) -> str:
    sunday_notice = ""
    if _is_sunday(date_iso):
        monday_iso = _next_monday_after(date_iso)
        sunday_notice = _sunday_primary_notice(lang, monday_iso) + "\n\n"
        date_iso = monday_iso
        session["preferred_date"] = date_iso

    # v26: выбираем врача по жалобе через CRM, но не теряем пациента при слабом совпадении.
    doctor_filter = None
    selected_by_router = None
    if session.get("complaint"):
        selected_by_router = await choose_doctor_for_complaint(chat_id, str(session.get("complaint") or ""))
        if selected_by_router and selected_by_router.get("doctor_login"):
            doctor_filter = selected_by_router["doctor_login"]
            session["preferred_doctor_login"] = doctor_filter
            session["preferred_doctor_name"] = selected_by_router.get("doctor_name") or ""

    try:
        data = await crm.check_slots(date_iso, doctor_login=doctor_filter)
        slots = _extract_slots(data, date_iso, limit=5)
        # Если у выбранного врача нет свободного времени — fallback на общее расписание.
        if doctor_filter and not slots:
            try:
                state.log_bot_action(
                    chat_id,
                    "guard_blocked",
                    "selected doctor has no slots, fallback to all",
                    tool_name="check_available_slots",
                    tool_args={"date": date_iso, "doctor": doctor_filter},
                    tool_result="no slots",
                )
            except Exception:
                pass
            data = await crm.check_slots(date_iso)
    except Exception:
        return _ru(lang, "Не смогла сейчас проверить свободное время в CRM. Передам администратору, чтобы он помог с записью 🌿",
                   "Қазір CRM-нан бос уақытты тексере алмадым. Әкімшіге жіберемін, ол жазылуға көмектеседі 🌿")
    slots = _extract_slots(data, date_iso, limit=5)
    session["preferred_date"] = date_iso
    session["last_slots"] = slots

    requested = session.get("requested_time") or None
    selected = _slot_by_requested_time(slots, requested)
    if selected:
        session["selected_date"] = selected["date"]
        session["selected_time"] = selected["time"]
        session["selected_doctor_login"] = selected["doctor_login"]
        session["selected_doctor_name"] = selected["doctor_name"]
        session["questionnaire_step"] = "contra"
        state.save_session(chat_id, session)
        return sunday_notice + _ru(lang,
            f"На {_format_date_human(date_iso, lang)} в {selected['time']} есть свободная запись 🌿\nПеред записью уточню: есть ли у Вас противопоказания?",
            f"{_format_date_human(date_iso, lang).capitalize()} күні сағат {selected['time']} бос уақыт бар 🌿\nЖазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
        )

    session["questionnaire_step"] = "time"
    state.save_session(chat_id, session)
    if not slots:
        return sunday_notice + _ru(lang,
            f"На {_format_date_human(date_iso, lang)} свободного времени для записи нет. Могу проверить другой день — какой Вам удобен?",
            f"{_format_date_human(date_iso, lang).capitalize()} күні бос уақыт жоқ. Басқа күнді тексерейін — қай күн ыңғайлы?"
        )
    return sunday_notice + _slots_text(slots, date_iso, lang)


async def handle_questionnaire(chat_id: str, phone: str, user_text: str, session: dict[str, Any]) -> str | None:
    """Гибкая анкета.

    Человек может написать данные не по порядку:
    «Болит спина, мне 35, хочу завтра в 10».
    Контроллер сначала сохраняет всё, что понял, потом спрашивает только недостающее.
    """
    lang = _lang(user_text, session)
    session["language"] = lang
    text = user_text or ""
    low = _clean(text)
    step = session.get("questionnaire_step") or session.get("step") or "start"

    # v21: пациент спрашивает, что взять с собой — сначала отвечаем по смыслу,
    # затем мягко продолжаем тот шаг анкеты, на котором остановились.
    if _asks_what_to_bring(text):
        answer = _what_to_bring_answer(lang, session)
        if not session.get("complaint"):
            answer += "\n\nПодскажите, пожалуйста, что Вас беспокоит?" if lang != "kk" else "\n\nАйтыңызшы, Сізді не мазалайды?"
        elif not session.get("age"):
            answer += "\n\nПодскажите, пожалуйста, сколько Вам лет?" if lang != "kk" else "\n\nЖасыңыз нешеде?"
        elif not session.get("preferred_date"):
            answer += "\n\nНа какой день Вам удобно прийти?" if lang != "kk" else "\n\nҚай күн ыңғайлы?"
        elif not session.get("selected_time"):
            answer += "\n\nКакое время Вам удобно?" if lang != "kk" else "\n\nҚай уақыт ыңғайлы?"
        elif not session.get("contraindications_verdict"):
            answer += "\n\nПеред записью уточню: есть ли у Вас противопоказания?" if lang != "kk" else "\n\nЖазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
        state.save_session(chat_id, session)
        return answer

    # v28: вопросы про цену/стоимость — отвечаем по-русски/по смыслу,
    # не переключаемся на казахский и не игнорируем вопрос.
    if _asks_price(text):
        info = _price_answer(lang, text)
        cont = _safe_continue(lang, session)
        state.save_session(chat_id, session)
        return info + (("\n\n" + cont) if cont else "")

    # v23: вопросы про график/адрес нельзя игнорировать и нельзя вместо ответа
    # повторять список свободного времени.
    if _asks_schedule(text):
        info = get_clinic_info("schedule", lang) or (
            "Врачи принимают с понедельника по пятницу 08:00–20:00, в субботу 09:00–18:00, воскресенье выходной 🌿"
        )
        cont = _safe_continue(lang, session)
        state.save_session(chat_id, session)
        return info + (("\n\n" + cont) if cont else "")

    if _asks_address(text):
        info = get_clinic_info("address", lang) or (
            "Адрес клиники: Кабанбай батыра 28, внутренний двор, подъезд 3, Астана 🌿"
        )
        cont = _safe_continue(lang, session)
        state.save_session(chat_id, session)
        return info + (("\n\n" + cont) if cont else "")

    # v22: пациент спрашивает про лечение/процедуры — сначала отвечаем по смыслу,
    # затем продолжаем текущий шаг записи. Нельзя игнорировать вопрос и сразу слать время.
    if _asks_treatment_methods(text):
        answer = _treatment_methods_answer(lang)
        if not session.get("complaint"):
            answer += "\n\nПодскажите, пожалуйста, что Вас беспокоит?" if lang != "kk" else "\n\nАйтыңызшы, Сізді не мазалайды?"
            state.save_session(chat_id, session)
            return answer
        if not session.get("age"):
            answer += "\n\nПодскажите, пожалуйста, сколько Вам лет?" if lang != "kk" else "\n\nЖасыңыз нешеде?"
            state.save_session(chat_id, session)
            return answer
        if not session.get("preferred_date"):
            answer += "\n\nНа какой день Вам удобно прийти?" if lang != "kk" else "\n\nҚай күн ыңғайлы?"
            state.save_session(chat_id, session)
            return answer
        if not session.get("selected_time"):
            if session.get("last_slots"):
                answer += "\n\n" + ("Какое время из вариантов выше Вам удобно?" if lang != "kk" else "Жоғарыдағы уақыттардың қайсысы ыңғайлы?")
                state.save_session(chat_id, session)
                return answer
            slots_text = await _show_slots(chat_id, session, session["preferred_date"], lang)
            return answer + "\n\n" + slots_text
        if not session.get("contraindications_verdict"):
            answer += "\n\nПеред записью уточню: есть ли у Вас противопоказания?" if lang != "kk" else "\n\nЖазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
            state.save_session(chat_id, session)
            return answer
        state.save_session(chat_id, session)
        return answer

    # v29: если пациент отвечает «ничего», не повторяем один и тот же вопрос.
    # Первый раз мягко уточняем зону. Второй раз ведём как профилактическую консультацию.
    if not session.get("complaint") and _is_no_complaint_answer(text):
        cnt = int(session.get("no_complaint_count") or 0) + 1
        session["no_complaint_count"] = cnt
        if cnt >= 2:
            session["complaint"] = "Профилактическая консультация / диагностика без явной жалобы"
            session["can_help"] = True
            session["questionnaire_step"] = "age"
            state.save_session(chat_id, session)
            return _ru(lang,
                "Поняла Вас. Можно прийти на первичную консультацию на профилактическую проверку 🌿\nПодскажите, пожалуйста, сколько Вам лет?",
                "Түсіндім. Профилактикалық тексеріс үшін алғашқы консультацияға келуге болады 🌿\nЖасыңыз нешеде?"
            )
        session["questionnaire_step"] = "complaint"
        state.save_session(chat_id, session)
        return _ru(lang,
            "Понимаю. Можно прийти и на профилактическую консультацию. Но для записи нам нужно понимать, что хотите проверить: спина, шея, суставы, плечо или колено. Напишите, пожалуйста 🌿",
            "Түсіндім. Профилактикалық консультацияға да келуге болады. Бірақ жазу үшін нені тексергіңіз келетінін түсіну керек: арқа, мойын, буын, иық немесе тізе. Жазып жіберіңізші 🌿"
        )

    # 0) Вопросы/ответы, которые нельзя путать с анкетой.
    # v29: если человек явно отказался от записи, не продолжаем давить вопросом про день.
    if _is_stop_booking(text):
        session["questionnaire_step"] = "stopped"
        state.save_session(chat_id, session)
        return _ru(lang,
            "Хорошо, не буду записывать 🌿 Если решите прийти на консультацию — просто напишите.",
            "Жақсы, жазбаймын 🌿 Консультацияға келемін деп шешсеңіз, жаза салыңыз."
        )

    if 16 <= int(session.get("age") or 0) < 18 and _has_any(text, PARENT_Q):
        session["minor_parent_notice_given"] = True
        state.save_session(chat_id, session)
        return _ru(lang,
            "Да, так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿",
            "Иә, Сіз 18 жасқа толмағандықтан, консультацияға ата-анаңызбен немесе заңды өкіліңізбен келу қажет 🌿"
        )

    if session.get("mobility_check_pending"):
        negative_mobility = ["нет", "не смогу", "не могу", "не получится", "не передвигаюсь", "жоқ", "бара алмаймын"]
        if any(w == low or w in low for w in negative_mobility):
            session["mobility_check_pending"] = False
            state.save_session(chat_id, session)
            return _ru(lang,
                "Поняла Вас. Тогда передам информацию администратору, чтобы он уточнил, как лучше поступить, потому что в клинике есть лестницы и процедуры нужно проходить очно 🌿",
                "Түсіндім. Бұл жағдайда әкімшіге жіберемін, өйткені клиникада баспалдақ бар және процедуралар клиникада жасалады 🌿"
            )
        if any(w == low or w in low for w in YES_WORDS):
            session["mobility_check_pending"] = False
            session["mobility_ok"] = True
            # После ответа «да» не проваливаемся в противопоказания/отказ,
            # а продолжаем текущий шаг записи.
            cont = _safe_continue(lang, session)
            state.save_session(chat_id, session)
            return ("Хорошо 🌿\n" + cont) if cont else "Хорошо 🌿"

    # 1) Собираем факты из любого сообщения, даже если они пришли не по порядку.
    booking_intent = _is_booking_intent(text)
    complaint_seen = _has_complaint(text)
    if complaint_seen and not session.get("complaint"):
        session["complaint"] = text.strip()
        session["can_help"] = True

    age = _extract_age(text, step=step)
    if age is not None and not session.get("age"):
        session["age"] = age

    date_iso = _parse_date(text)
    if date_iso:
        session["preferred_date"] = date_iso

    req_time = _parse_requested_time(text)
    if req_time:
        session["requested_time"] = req_time

    # Имя сохраняем заранее только если есть явный маркер, чтобы не принять «да/завтра» за имя.
    name_match = re.search(r"(?:меня зовут|я\s+|аты[мң]\s*|менің атым\s+)([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,40})", text.strip(), re.IGNORECASE)
    if name_match and not session.get("patient_name"):
        maybe_name = name_match.group(1).strip()
        if _looks_like_name(maybe_name):
            session["patient_name"] = maybe_name

    state.save_session(chat_id, session)

    # 2) Если сообщение вообще не про запись/жалобу, отдаём GPT.
    has_active_questionnaire = bool(session.get("complaint") or session.get("age") or session.get("preferred_date") or step not in {"start", "", None})
    if not has_active_questionnaire and not booking_intent and not complaint_seen and not any(w in low for w in ["здравствуйте", "добрый", "привет", "сәлем", "салем"]):
        return None

    # 3) Ограничение по передвижению — это не стоп сразу, а уточнение.
    if _has_any(text, IMMOBILITY) and not session.get("mobility_ok"):
        session["mobility_check_pending"] = True
        state.save_session(chat_id, session)
        return _ru(lang,
            "Понимаю Вас 🙏🏻 Уточню перед записью: сможете ли Вы самостоятельно прийти в клинику и передвигаться по клинике?",
            "Түсіндім 🙏🏻 Жазбас бұрын нақтылайын: клиникаға өзіңіз келіп, клиника ішінде жүріп-тұра аласыз ба?"
        )

    # 4) Жалоба — первый обязательный пункт.
    if not session.get("complaint"):
        session["questionnaire_step"] = "complaint"
        state.save_session(chat_id, session)
        if booking_intent:
            return _ru(lang,
                "Здравствуйте! Да, можно записаться на консультацию по акции 🌿\nПодскажите, пожалуйста, что Вас беспокоит?",
                "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿\nАйтыңызшы, Сізді не мазалайды?"
            )
        return _ru(lang,
            "Подскажите, пожалуйста, что Вас беспокоит? 🌿",
            "Айтыңызшы, Сізді не мазалайды? 🌿"
        )

    # Если жалоба только что появилась, отвечаем по ней и идём дальше.
    if step in {"start", "complaint", "", None} and complaint_seen:
        session["questionnaire_step"] = "age"
        state.save_session(chat_id, session)
        # Если возраст уже тоже есть в этом же сообщении, не спрашиваем возраст.
        if not session.get("age"):
            return _ru(lang,
                "Понимаю Вас 🙏🏻 С такой жалобой можно прийти на первичную консультацию. Врач осмотрит и подскажет дальнейший план.\nПодскажите, пожалуйста, сколько Вам лет?",
                "Түсіндім 🙏🏻 Мұндай шағыммен алғашқы консультацияға келуге болады. Дәрігер қарап, әрі қарай не істеу керегін айтады.\nЖасыңыз нешеде?"
            )

    # 5) Возраст.
    if not session.get("age"):
        session["questionnaire_step"] = "age"
        state.save_session(chat_id, session)
        return _ru(lang, "Подскажите, пожалуйста, сколько Вам лет?", "Жасыңыз нешеде?")

    age_val = int(session.get("age") or 0)
    if age_val <= 15 or age_val >= 75:
        session["questionnaire_step"] = "escalated"
        state.save_session(chat_id, session)
        try:
            await crm.escalate_to_operator(phone=sanitize_kz_phone(phone) or phone, reason=f"Возраст пациента {age_val}, нужна проверка администратором")
        except Exception:
            pass
        return _ru(lang,
            "Спасибо. По возрасту я передам информацию администратору, чтобы не подсказать неверно 🌿",
            "Рақмет. Жасыңыз бойынша қате айтпау үшін ақпаратты әкімшіге жіберемін 🌿"
        )

    if 16 <= age_val < 18 and not session.get("minor_parent_notice_given"):
        session["minor_parent_required"] = True
        session["minor_parent_notice_given"] = True
        session["questionnaire_step"] = "day"
        state.save_session(chat_id, session)
        # Если день уже указан, не теряем его, но сначала предупреждаем про родителя.
        return _ru(lang,
            "Так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿",
            "Сіз 18 жасқа толмағандықтан, консультацияға ата-анаңызбен немесе заңды өкіліңізбен келу қажет 🌿"
        )

    # 6) День и свободные записи.
    if not session.get("preferred_date"):
        session["questionnaire_step"] = "day"
        state.save_session(chat_id, session)
        return _ru(lang, "Спасибо. На какой день Вам удобно прийти?", "Рақмет. Қай күн ыңғайлы?")

    # Если дата есть, но выбранного времени нет — проверяем CRM и показываем/выбираем слоты.
    if not session.get("selected_time"):
        # Если пациент уже видел слоты и выбрал время — пробуем найти его в last_slots.
        slot = _parse_time(text, session.get("last_slots") or [])
        if slot:
            session["selected_date"] = slot["date"]
            session["selected_time"] = slot["time"]
            session["selected_doctor_login"] = slot["doctor_login"]
            session["selected_doctor_name"] = slot["doctor_name"]
            session["questionnaire_step"] = "contra"
            state.save_session(chat_id, session)
        else:
            # Если пациент попросил ближайший вариант — ищем ближайший.
            if _asks_nearest(text):
                return await _show_nearest_slots(chat_id, session, lang)
            return await _show_slots(chat_id, session, session["preferred_date"], lang)

    # 7) Противопоказания.
    if not session.get("contraindications_verdict"):
        session["questionnaire_step"] = "contra"
        state.save_session(chat_id, session)

        if _is_no_contra(text):
            session["contraindications_verdict"] = "proceed"
            session["contraindications_reason"] = "no contraindications reported"
            session["questionnaire_step"] = "name"
            state.save_session(chat_id, session)
        elif _is_symptom_not_contra(text):
            return _ru(lang,
                "Понимаю. Это больше похоже на жалобу, а не на противопоказание. Уточню именно по противопоказаниям: они у Вас есть?",
                "Түсіндім. Бұл қарсы көрсетілім емес, шағымға көбірек ұқсайды. Нақты қарсы көрсетілімдеріңіз бар ма?"
            )
        elif _is_generic_contra_yes(text):
            session["questionnaire_step"] = "contra_details"
            state.save_session(chat_id, session)
            return _ru(lang,
                "Подскажите, пожалуйста, какие именно противопоказания есть? Это важно перед записью 🌿",
                "Қандай қарсы көрсетілім бар екенін нақтылап жіберіңізші. Бұл жазылар алдында маңызды 🌿"
            )
        elif _is_real_contra(text):
            session["questionnaire_step"] = "escalated"
            state.save_session(chat_id, session)
            try:
                await crm.escalate_to_operator(phone=sanitize_kz_phone(phone) or phone, reason=f"Пациент указал противопоказания: {text[:300]}")
            except Exception:
                pass
            return _ru(lang,
                "Спасибо, что уточнили. Я передам информацию администратору/врачу, чтобы они проверили, можно ли Вам проходить консультацию и процедуры 🌿",
                "Нақтылағаныңызға рақмет. Консультация мен процедуралар бойынша қате айтпау үшін ақпаратты әкімшіге/дәрігерге жіберемін 🌿"
            )
        else:
            return _ru(lang,
                "Перед записью уточню: есть ли у Вас противопоказания?",
                "Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?"
            )

    if session.get("questionnaire_step") == "contra_details":
        if _is_no_contra(text):
            session["contraindications_verdict"] = "proceed"
            session["questionnaire_step"] = "name"
            state.save_session(chat_id, session)
        elif _is_symptom_not_contra(text):
            session["questionnaire_step"] = "contra"
            state.save_session(chat_id, session)
            return _ru(lang,
                "Понимаю. Это больше похоже на жалобу, а не на противопоказание. Уточню именно по противопоказаниям: они у Вас есть?",
                "Түсіндім. Бұл қарсы көрсетілім емес, шағымға көбірек ұқсайды. Нақты қарсы көрсетілімдеріңіз бар ма?"
            )
        else:
            session["questionnaire_step"] = "escalated"
            state.save_session(chat_id, session)
            try:
                await crm.escalate_to_operator(phone=sanitize_kz_phone(phone) or phone, reason=f"Пациент указал противопоказания/неясность: {text[:300]}")
            except Exception:
                pass
            return _ru(lang,
                "Спасибо, я передам информацию администратору/врачу, чтобы они проверили и не подсказать неверно 🌿",
                "Рақмет, қате айтпау үшін ақпаратты әкімшіге/дәрігерге жіберемін 🌿"
            )

    # 8) Имя.
    if not session.get("patient_name"):
        # На финальном шаге любое нормальное имя принимаем.
        if _looks_like_name(text) and session.get("questionnaire_step") == "name":
            session["patient_name"] = text.strip()
            state.save_session(chat_id, session)
        else:
            session["questionnaire_step"] = "name"
            state.save_session(chat_id, session)
            return _ru(lang, "Хорошо. Подскажите, пожалуйста, Ваше имя для записи?", "Жақсы. Жазу үшін аты-жөніңізді жазып жіберіңізші.")

    # 9) Запись в CRM.
    required = ["selected_date", "selected_time", "selected_doctor_login", "patient_name"]
    if all(session.get(k) for k in required) and session.get("contraindications_verdict") == "proceed":
        try:
            booked = await crm.book_appointment(
                patient_name=session["patient_name"],
                phone=sanitize_kz_phone(phone) or phone,
                doctor_login=session["selected_doctor_login"],
                doctor_name=session.get("selected_doctor_name") or None,
                date=session["selected_date"],
                time_start=session["selected_time"],
                notes=session.get("complaint") or "Запись через WhatsApp-бота",
            )
            session["last_booking"] = booked
            session["questionnaire_step"] = "booked"
            state.save_session(chat_id, session)
            try:
                await crm.log_outcome(phone=sanitize_kz_phone(phone) or phone, outcome="booked", appointment_id=booked.get("appointmentId"), note="Записан через гибкий контроллер анкеты")
            except Exception:
                pass
            date_h = _format_date_human(session["selected_date"], lang)
            return _ru(lang,
                f"Готово, Вы записаны на консультацию на {date_h} в {session['selected_time']} 🌿\nБудем ждать Вас!",
                f"Дайын, Сіз {date_h} күні сағат {session['selected_time']} консультацияға жазылдыңыз 🌿\nКүтеміз!"
            )
        except Exception:
            return _ru(lang,
                "Не смогла создать запись в CRM. Передам администратору, чтобы он проверил и помог завершить запись 🌿",
                "CRM-да жазбаны жасай алмадым. Әкімшіге жіберемін, ол тексеріп, жазылуға көмектеседі 🌿"
            )

    return None

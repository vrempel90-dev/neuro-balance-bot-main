from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import crm
import state

try:
    from phone import sanitize_kz_phone
except Exception:
    def sanitize_kz_phone(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        return digits

try:
    from language_guard import detect_language as detect_message_language
except Exception:
    detect_message_language = None

try:
    from config import get_settings
except Exception:
    get_settings = None


# ============================================================
# Neuro Balance dialog.py
# Safe controller:
# complaint -> age -> contraindications -> date -> time -> name -> booking
# ============================================================

KZ_MARKERS = [
    "сәлем", "салем", "қалай", "менің", "меним", "атым",
    "белім", "белим", "ауырады", "ауыр", "аурат", "ертең", "ертен",
    "бүгін", "бугин", "жоқ", "жок", "joq", "ия", "иә",
    "қарсы", "карсы", "көрсетілім", "корсетилим",
    "жазыл", "жазылғым", "келеді", "жас", "жастамын",
    "аяқ", "аяг", "қол", "кол", "омыртқа", "жарық",
]

RU_MARKERS = [
    "здравствуйте", "привет", "добрый", "хочу", "записаться", "консультац",
    "болит", "боль", "спина", "поясница", "шея", "грыжа", "протрузия",
    "сколько", "стоит", "адрес", "завтра", "сегодня", "лет",
]

BOOKING_WORDS = [
    "запис", "консультац", "прием", "приём", "акци", "50%",
    "жазыл", "қабылдау", "кабылдау",
]

COMPLAINT_WORDS = [
    "болит", "боль", "ноет", "тянет", "ломит", "хрустит", "онем",
    "мурашк", "защем", "грыж", "грыжа", "протруз", "протрузия",
    "отдает", "отдаёт", "стреляет", "прострел", "нога", "ногу", "ноге",
    "рука", "руку", "руке", "бедро", "таз", "лопат", "ребр",
    "спина", "спине", "спину", "поясниц", "шея", "шей", "сустав",
    "колен", "плеч", "голова", "артроз", "артрит", "остеохонд", "травм",
    "ауырады", "ауырып", "ауыр", "аурат", "қатты", "катты", "береді", "береди",
    "belim", "белім", "белим", "бел", "арқа", "аркам", "арқам",
    "аяқ", "аяққа", "аяк", "аякка", "аяғым", "аягым",
    "мойын", "мойным", "буын", "тізе", "тизе", "иық", "иык", "қол", "кол",
]

NO_COMPLAINT_WORDS = [
    "ничего", "ничего не беспокоит", "не беспокоит", "ничего не болит",
    "просто консультация", "просто осмотр", "профилактика",
    "профилактический осмотр", "для профилактики", "не знаю",
    "ештеңе", "ештене", "мазаламайды", "ауырмайды",
    "білмеймін", "билмеймин", "жай консультация", "профилактика үшін",
]

PRICE_WORDS = [
    "сколько стоит", "стоимость", "цена", "прайс", "қанша тұрады",
    "канша турады", "бағасы", "багасы", "стоить",
]

ADDRESS_WORDS = [
    "адрес", "где находитесь", "вы в астане", "2gis", "2 гис",
    "мекенжай", "қайда", "кайда", "астанада",
]

SCHEDULE_WORDS = [
    "график", "режим", "работаете", "расписание", "сенбі", "жексенбі",
    "кесте", "жұмыс", "жумыс",
]

MRI_WORDS = [
    "мрт", "кт", "рентген", "узи", "анализ", "снимок", "снимки",
    "диагностика", "диагностик", "томография",
]

CANCEL_WORDS = [
    "отмен", "не приду", "не смогу", "перенес", "перенести", "поменять время",
    "басқа уақыт", "ауыстыр", "келмеймін", "келе алмаймын",
]

LOOKUP_WORDS = [
    "я уже записан", "я уже записана", "уже записан", "уже записана",
    "у меня запись", "моя запись", "мою запись", "когда я записан",
    "когда у меня запись", "напомните", "на какое время", "во сколько",
    "проверить запись", "посмотреть запись", "жазылдым", "жазбам", "қашан",
]

NO_CONTRA_WORDS = [
    "нет", "нету", "не было", "противопоказаний нет", "нет противопоказаний",
    "ничего нет", "все нет", "всё нет", "по всем нет",
    "жоқ", "жок", "joq", "jok", "қарсы көрсетілім жоқ", "карсы корсетилим жок",
]

YES_WORDS = [
    "да", "есть", "бар", "иә", "ия", "есть противопоказ", "имеется",
]

HARD_CONTRA_WORDS = [
    "кардиостимулятор", "имплант", "беремен", "жүктілік", "жукцилик",
    "онколог", "рак", "эпилеп", "тромб", "кровотеч", "қан кет",
    "температура", "инфекц", "острое воспал", "жүрек", "сердеч",
]

NAME_BANNED_WORDS = set(
    "да нет ок окей хорошо приду буду завтра сегодня ертең бугин бүгін жок жоқ хочу записаться консультация болит боль".split()
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _low(text: str) -> str:
    return _clean(text).lower().replace("ё", "е")


def _has_any(text: str, words: list[str]) -> bool:
    low = _low(text)
    return any(w in low for w in words)


def _has_mri_question(text: str) -> bool:
    low = _low(text)
    # ВАЖНО: "узи" проверяем только как отдельное слово.
    # Иначе слово "протрузия" содержит "узи" внутри и ошибочно включает ответ про УЗИ/МРТ.
    if re.search(r"\b(мрт|кт|рентген|снимок|снимки|томография|диагностика|диагностик)\b", low):
        return True
    if re.search(r"(?<![а-яa-z])узи(?![а-яa-z])", low):
        return True
    return False


def _detect_lang(text: str, session: dict[str, Any]) -> str:
    current = session.get("language") or "ru"
    if detect_message_language:
        try:
            return detect_message_language(text, current)
        except Exception:
            pass

    low = _low(text)
    has_kz = any(w in low for w in KZ_MARKERS) or bool(re.search(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]", text or ""))
    has_ru = any(w in low for w in RU_MARKERS)

    # Если есть русская основа и одно казахское слово типа "бел" — оставляем русский.
    if has_ru:
        return "ru"
    if has_kz:
        return "kk"
    return current if current in ("ru", "kk") else "ru"


def _tr(session_or_lang: dict[str, Any] | str, ru: str, kk: str) -> str:
    if isinstance(session_or_lang, dict):
        lang = session_or_lang.get("language") or "ru"
    else:
        lang = session_or_lang or "ru"
    return kk if lang == "kk" else ru


def _price_short_text(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Приём в нашей клинике — 5 000 тг 🌿",
        "Біздің клиникада алғашқы қабылдау — 5 000 тг 🌿",
    )


def _prepend_price_if_needed(text: str, session: dict[str, Any], answer: str) -> str:
    if _has_any(text, PRICE_WORDS):
        price = _price_short_text(session)
        if price not in answer:
            return price + "\n\n" + answer
    return answer


def _is_relative_new_booking_request(text: str) -> bool:
    low = _low(text)

    relatives = [
        "маму", "мама", "папу", "папа", "отца", "отец", "сына", "сын",
        "дочь", "дочку", "дочери", "мужа", "жену", "брата", "сестру",
        "анамды", "анама", "әкемді", "әкем", "баламды", "балам",
        "ұлымды", "қызымды", "жолдасымды", "ағамды", "інімді", "әпкемді",
    ]

    booking_words = [
        "записать", "записаться", "запишите", "хочу записать", "хочу записаться",
        "жазу", "жазғым", "жазыл", "жазып", "жазайын",
    ]

    existing_words = [
        "уже записан", "уже записана", "я записан", "я записана", "был записан",
        "была записана", "бұрын жазылған", "жазылған едім",
        "отмените", "отменить", "перенести", "перенесите",
        "басқа уақыт", "ауыстыру", "ауыстырыңыз",
    ]

    return (
        any(w in low for w in relatives)
        and any(w in low for w in booking_words)
        and any(w in low for w in existing_words)
    )


def _relative_dual_task_answer(session: dict[str, Any], text: str) -> str:
    low = _low(text)

    details = []
    age_match = _extract_age(text, step="age")
    if age_match:
        details.append(f"возраст: {age_match}")

    if any(w in low for w in ["шея", "мойын"]):
        details.append("жалоба: шея")
    elif any(w in low for w in ["спина", "поясница", "бел"]):
        details.append("жалоба: спина/поясница")
    elif any(w in low for w in ["колено", "тізе"]):
        details.append("жалоба: колено")
    elif any(w in low for w in ["нога", "аяқ"]):
        details.append("жалоба: нога")

    if _contra_is_clear_no(text):
        details.append("противопоказаний нет")
    elif _contra_has_hard_stop(text):
        details.append("есть важные ограничения — передать врачу")

    if any(w in low for w in ["завтра", "ертең", "ертен"]):
        details.append("желательная дата: завтра")

    details_text_ru = ""
    details_text_kk = ""
    if details:
        joined = "; ".join(details)
        details_text_ru = f"\n\nПо новой записи передам данные: {joined}."
        details_text_kk = f"\n\nЖаңа жазба бойынша мәліметтерді жіберемін: {joined}."

    return _tr(
        session,
        "Поняла Вас. Передам администратору, чтобы он проверил Вашу запись и помог отменить или перенести её 🌿\n\nТакже администратор отдельно поможет записать родственника на консультацию.",
        "Түсіндім. Әкімшіге жіберемін, ол Сіздің жазбаңызды тексеріп, тоқтатуға немесе ауыстыруға көмектеседі 🌿\n\nСонымен қатар әкімші туысыңызды консультацияға бөлек жазуға көмектеседі.",
    ) + _tr(session, details_text_ru, details_text_kk)


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


def _finalize(chat_id: str, session: dict[str, Any], answer: str) -> str:
    answer = _clean(answer)
    if not answer:
        if session.get("complaint") and not session.get("age"):
            answer = _ask_age(session)
        else:
            answer = _tr(
                session,
                "Подскажите, пожалуйста, что Вас беспокоит? 🌿",
                "Сізді не мазалайды? 🌿",
            )

    # Никогда не возвращаем пустой ответ: если ответ совпал, мягко уточняем.
    session["last_assistant_answer"] = answer
    _safe_save(chat_id, session)
    _safe_add_message(chat_id, "assistant", answer)
    return answer


def _extract_age(text: str, step: str = "") -> int | None:
    low = _low(text)

    # Не путать время с возрастом: "10:30" не возраст.
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", low):
        return None

    # Прямые формы возраста RU/KZ.
    patterns = [
        r"\bмне\s*(\d{1,2})\s*(?:лет|года|год)?\b",
        r"\bмен\s*(\d{1,2})\s*(?:жастамын|жаста|жас)?\b",
        r"\b(\d{1,2})\s*(?:лет|года|год|жас|жастамын|жаста)\b",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            age = int(m.group(1))
            if 1 <= age <= 99:
                return age

    # Не путать длительность боли с возрастом: "3 день болит" не возраст.
    if re.search(r"\b\d{1,2}\s*(день|дня|дней|недел|неделя|месяц|месяцев|сутки)\b", low):
        return None

    # Если мы явно ждём возраст — можно принять просто число.
    nums = re.findall(r"\b(\d{1,2})\b", low)
    if nums and step == "age":
        age = int(nums[0])
        if 1 <= age <= 99:
            return age

    return None


def _age_stop_text(age: int, session: dict[str, Any]) -> str:
    if age < 18:
        return _tr(
            session,
            "Так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿",
            "18 жасқа толмағандықтан, консультацияға ата-анаңызбен немесе заңды өкіліңізбен келу керек 🌿",
        )
    # 75+ НЕ останавливаем автоматически: аккуратно продолжаем запись,
    # но обязательно собираем противопоказания перед датой.
    return ""


def _has_booking_intent(text: str) -> bool:
    return _has_any(text, BOOKING_WORDS)


def _has_complaint(text: str) -> bool:
    return _has_any(text, COMPLAINT_WORDS)


def _has_no_complaint(text: str) -> bool:
    return _has_any(text, NO_COMPLAINT_WORDS)


def _is_positive_confirm(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in [
        "да", "хочу", "запишите", "можно", "ок", "окей", "давайте",
        "иә", "ия", "жаз", "жазылы", "болады", "келісемін", "келисемин",
    ])


def _is_greeting_only(text: str) -> bool:
    low = _low(text)
    words = re.sub(r"[^\wа-яА-ЯәіңғүұқөһӘІҢҒҮҰҚӨҺ]+", " ", low).split()
    return bool(words) and len(words) <= 3 and any(w in words for w in ["здравствуйте", "привет", "салем", "сәлем"])


def _parse_date(text: str) -> str | None:
    low = _low(text)
    today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()

    if any(w in low for w in ["сегодня", "бүгін", "бугин"]):
        return today.isoformat()
    if any(w in low for w in ["завтра", "ертең", "ертен"]):
        return (today + timedelta(days=1)).isoformat()

    weekdays = {
        "понедельник": 0, "в понедельник": 0, "дүйсенбі": 0, "дуйсенби": 0,
        "вторник": 1, "во вторник": 1, "сейсенбі": 1, "сейсенби": 1,
        "среда": 2, "среду": 2, "в среду": 2, "сәрсенбі": 2, "сарсенби": 2,
        "четверг": 3, "в четверг": 3, "бейсенбі": 3, "бейсенби": 3,
        "пятница": 4, "пятницу": 4, "в пятницу": 4, "жұма": 4, "жума": 4,
        "суббота": 5, "субботу": 5, "в субботу": 5, "сенбі": 5, "сенби": 5,
        "воскресенье": 6, "воскресенье": 6, "жексенбі": 6, "жексенби": 6,
    }
    for name, wd in weekdays.items():
        if name in low:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return (today + timedelta(days=delta)).isoformat()

    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", low)
    if m:
        d, mo, y = m.groups()
        year = int(y) if y else today.year
        if year < 100:
            year += 2000
        try:
            return datetime(year, int(mo), int(d)).date().isoformat()
        except ValueError:
            return None

    return None


def _time_from_text(text: str) -> str | None:
    m = re.search(r"\b([01]?\d|2[0-3])[:.\- ]([0-5]\d)\b", text or "")
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    m = re.search(r"\b([8-9]|1\d|20)\s*(?:час|ч|:00)?\b", _low(text))
    if m:
        return f"{int(m.group(1)):02d}:00"

    return None


def _format_slots(slots_data: dict[str, Any], max_count: int = 5) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []

    # CRM format: {"availability":[{"doctorLogin": "...", "doctorName": "...", "date": "...", "availableSlots":["09:00"]}]}
    for item in slots_data.get("availability", []) or []:
        doctor_login = item.get("doctorLogin") or item.get("doctor_login") or ""
        doctor_name = item.get("doctorName") or item.get("doctor_name") or "Врач клиники"
        date = item.get("date") or ""
        for time_start in item.get("availableSlots", []) or item.get("slots", []) or []:
            if isinstance(time_start, dict):
                time_start = time_start.get("timeStart") or time_start.get("time") or ""
            if not time_start:
                continue
            result.append({
                "doctor_login": str(doctor_login),
                "doctor_name": str(doctor_name),
                "date": str(date),
                "time": str(time_start),
            })
            if len(result) >= max_count:
                return result

    # Fallback format: {"slots":[...]}
    for item in slots_data.get("slots", []) or []:
        if isinstance(item, str):
            result.append({"doctor_login": "", "doctor_name": "Врач клиники", "date": "", "time": item})
        elif isinstance(item, dict):
            result.append({
                "doctor_login": str(item.get("doctorLogin") or item.get("doctor_login") or ""),
                "doctor_name": str(item.get("doctorName") or item.get("doctor_name") or "Врач клиники"),
                "date": str(item.get("date") or ""),
                "time": str(item.get("timeStart") or item.get("time") or ""),
            })
        if len(result) >= max_count:
            return result

    return result


def _slots_text(slots: list[dict[str, str]], lang: str) -> str:
    lines = []
    for i, slot in enumerate(slots, 1):
        date = slot.get("date") or ""
        time = slot.get("time") or ""
        doctor = slot.get("doctor_name") or "Врач клиники"
        if lang == "kk":
            lines.append(f"{i}) {date} {time} — {doctor}")
        else:
            lines.append(f"{i}) {date} в {time} — {doctor}")
    return "\n".join(lines)


def _select_slot(text: str, slots: list[dict[str, str]]) -> dict[str, str] | None:
    low = _low(text)

    m = re.search(r"\b([1-9])\b", low)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(slots):
            return slots[idx]

    t = _time_from_text(text)
    if t:
        for slot in slots:
            if slot.get("time") == t:
                return slot

    return None


def _looks_like_name(text: str) -> bool:
    clean = _clean(text)
    low = _low(clean)
    if not clean or len(clean) > 60:
        return False
    if any(ch.isdigit() for ch in clean):
        return False
    if low in NAME_BANNED_WORDS:
        return False
    if _has_any(low, BOOKING_WORDS + COMPLAINT_WORDS + PRICE_WORDS + ADDRESS_WORDS):
        return False
    return bool(re.match(r"^[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,}$", clean))


def _extract_name(text: str) -> str:
    clean = _clean(text)

    patterns = [
        r"\bменя\s+зовут\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bзовут\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bмо[её]\s+имя\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bатым\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bменің\s+атым\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
    ]
    low = clean.lower()
    for p in patterns:
        m = re.search(p, low, flags=re.I)
        if m:
            name = _clean(m.group(1)).strip(" .,!?:;")
            return name.title() if _looks_like_name(name) else ""

    return clean.title() if _looks_like_name(clean) else ""


def _clinic_answer(text: str, session: dict[str, Any]) -> str | None:
    lang = session.get("language") or "ru"

    if _has_any(text, PRICE_WORDS):
        return _tr(
            lang,
            "Приём в нашей клинике — 5 000 тг 🌿\nВ стоимость входит осмотр врача, подробная консультация, индивидуальные назначения и составление плана лечения.\n\nПодскажите, пожалуйста, что Вас беспокоит?",
            "Біздің клиникада алғашқы қабылдау — 5 000 тг 🌿\nҚабылдауға дәрігердің қарауы, толық консультация, жеке ұсыныстар және емдеу жоспарын құру кіреді.\n\nСізді не мазалайды?",
        )

    if _has_any(text, ADDRESS_WORDS):
        return _tr(
            lang,
            "Мы находимся в Астане 🌿\nАдрес: Кабанбай батыра 28, ішкі двор, подъезд 3. Вход со стороны Кунаева, после шлагбаума направо.\n\nПодскажите, пожалуйста, что Вас беспокоит?",
            "Біз Астанадамыз 🌿\nМекенжай: Қабанбай батыр 28, ішкі аула, 3-подъезд. Қонаев жағынан кіріп, шлагбаумнан кейін оңға бұрыласыз.\n\nСізді не мазалайды?",
        )

    if _has_any(text, SCHEDULE_WORDS):
        return _tr(
            lang,
            "Работаем по предварительной записи 🌿 Напишите, пожалуйста, какой день Вам удобен — проверю свободное время.",
            "Алдын ала жазылу бойынша жұмыс істейміз 🌿 Қай күн ыңғайлы екенін жазыңыз — бос уақытты тексеремін.",
        )

    if _has_mri_question(text):
        return _tr(
            lang,
            "Снимки и МРТ у нас не делают. Но заранее делать обследование не обязательно 🌿 Врач на консультации осмотрит Вас и подскажет, нужно ли МРТ/КТ или другое обследование.\n\nПодскажите, пожалуйста, что Вас беспокоит?",
            "Бізде МРТ/снимок жасалмайды. Бірақ алдын ала тексеруден өту міндетті емес 🌿 Дәрігер консультацияда қарап, МРТ/КТ немесе басқа тексеріс керек пе — соны айтады.\n\nСізді не мазалайды?",
        )

    return None


def _ask_complaint(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Подскажите, пожалуйста, что Вас беспокоит? 🌿",
        "Сізді не мазалайды? 🌿",
    )


def _ask_age(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Понимаю Вас 🙏 С такой жалобой можно прийти на первичную консультацию. Врач осмотрит и подскажет дальнейший план.\nПодскажите, пожалуйста, сколько Вам лет?",
        "Түсіндім 🙏 Мұндай шағыммен алғашқы консультацияға келуге болады. Дәрігер қарап, әрі қарай не істеу керегін айтады.\nЖасыңыз нешеде?",
    )


def _ask_age_contextual(session: dict[str, Any], text: str) -> str:
    parts_ru: list[str] = []
    parts_kk: list[str] = []

    if _has_any(text, PRICE_WORDS):
        parts_ru.append("Приём в нашей клинике — 5 000 тг 🌿 В стоимость входит осмотр врача и консультация.")
        parts_kk.append("Біздің клиникада алғашқы қабылдау — 5 000 тг 🌿 Құнына дәрігердің қарауы және консультация кіреді.")

    if _has_mri_question(text):
        parts_ru.append("МРТ заранее делать не обязательно. Врач на консультации осмотрит Вас и подскажет, нужно ли МРТ/КТ или другое обследование.")
        parts_kk.append("МРТ-ны алдын ала жасау міндетті емес. Дәрігер консультацияда қарап, МРТ/КТ немесе басқа тексеріс керек пе — соны айтады.")

    parts_ru.append("Понимаю Вас 🙏 С такой жалобой можно прийти на первичную консультацию. Подскажите, пожалуйста, сколько Вам лет?")
    parts_kk.append("Түсіндім 🙏 Мұндай шағыммен алғашқы консультацияға келуге болады. Жасыңыз нешеде?")

    return _tr(session, "\n\n".join(parts_ru), "\n\n".join(parts_kk))


def _senior_contra_intro(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Спасибо 🌿 Перед записью уточню важный момент: есть ли у Вас противопоказания или ограничения по здоровью?\n\nНапример: онкология, высокая температура, острое воспаление, свежие травмы/переломы, кардиостимулятор?",
        "Рақмет 🌿 Жазылар алдында маңызды сұрақты нақтылайын. Жазбас бұрын қарсы көрсетілімдер немесе денсаулық бойынша шектеулерді нақтылау маңызды.\n\nМысалы: онкология, жоғары температура, жедел қабыну, жаңа жарақат/сыну, кардиостимулятор?",
    )


def _ask_contra(session: dict[str, Any]) -> str:
    # Универсальный текст без беременности, чтобы не писать это мужчинам.
    return _tr(
        session,
        "Спасибо 🌿 Перед записью уточню: есть ли у Вас противопоказания или ограничения по здоровью?\n\nНапример: онкология, высокая температура, острое воспаление, свежие травмы/переломы, кардиостимулятор?",
        "Рақмет 🌿 Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер немесе денсаулық бойынша шектеулер бар ма?\n\nМысалы: онкология, жоғары температура, жедел қабыну, жаңа жарақат/сыну, кардиостимулятор?",
    )


def _ask_date(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Спасибо. На какой день Вам удобно прийти?",
        "Рақмет. Қай күн ыңғайлы?",
    )


def _ask_name(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Перед записью подскажите, пожалуйста, как к Вам обращаться? Напишите только имя.",
        "Жазбас бұрын атыңызды жазыңызшы. Тек атыңызды жазыңыз.",
    )


def _no_slots_text(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "На эту дату свободных окошек не нашла. Напишите, пожалуйста, другую дату — проверю расписание.",
        "Бұл күнге бос уақыт табылмады. Басқа күнді жазыңызшы — кестені тексеремін.",
    )


async def _show_slots(chat_id: str, session: dict[str, Any], date_iso: str) -> str:
    session["preferred_date"] = date_iso
    lang = session.get("language") or "ru"

    try:
        max_slots = 5
        if get_settings:
            try:
                max_slots = int(getattr(get_settings(), "max_slots_to_show", 5) or 5)
            except Exception:
                pass

        data = await crm.check_slots(date_iso)
        slots = _format_slots(data, max_count=max_slots)
    except Exception as exc:
        _safe_log(chat_id, "crm_check_slots_error", {"error": str(exc)[:500]})
        session["step"] = "escalated"
        session["escalated"] = True
        return _tr(
            session,
            "Сейчас не получается проверить свободные окошки. Передам координатору, она подберёт время вручную 🌿",
            "Қазір бос уақыттарды тексере алмадым. Координаторға жіберемін, ол уақытты қолмен таңдап береді 🌿",
        )

    if not slots:
        session["step"] = "date"
        return _no_slots_text(session)

    session["last_slots"] = slots
    session["step"] = "time"
    return _tr(
        session,
        "Есть такие свободные окошки:\n" + _slots_text(slots, lang) + "\n\nКакое Вам удобно? Можно ответить номером варианта.",
        "Мынадай бос уақыттар бар:\n" + _slots_text(slots, lang) + "\n\nҚайсысы ыңғайлы? Нұсқа нөмірімен жауап беруге болады.",
    )


async def _book(chat_id: str, session: dict[str, Any], phone: str) -> str:
    normalized_phone = sanitize_kz_phone(phone or session.get("phone") or "")
    slot = session.get("selected_slot") or {}

    if not normalized_phone:
        session["step"] = "phone"
        return _tr(
            session,
            "Не вижу номер телефона. Напишите, пожалуйста, Ваш номер в формате 77001234567.",
            "Телефон нөмірі көрінбей тұр. Нөміріңізді 77001234567 форматында жазыңызшы.",
        )

    if not slot:
        session["step"] = "date"
        return _prepend_price_if_needed(text, session, _ask_date(session))

    try:
        booked = await crm.book_appointment(
            patient_name=session.get("patient_name") or "Пациент",
            phone=normalized_phone,
            doctor_login=slot.get("doctor_login") or slot.get("doctorLogin") or "",
            doctor_name=slot.get("doctor_name") or slot.get("doctorName") or None,
            date=slot.get("date") or session.get("preferred_date"),
            time_start=slot.get("time") or slot.get("timeStart"),
            notes=f"Жалоба: {session.get('complaint') or ''}; возраст: {session.get('age') or ''}",
        )
        session["booked"] = True
        session["appointment"] = booked
        session["step"] = "done"

        date = booked.get("date") or slot.get("date") or session.get("preferred_date") or ""
        time_start = booked.get("timeStart") or booked.get("time_start") or slot.get("time") or ""
        doctor = booked.get("doctorName") or slot.get("doctor_name") or ""

        details = " ".join(x for x in [date, time_start, doctor] if x)
        return _tr(
            session,
            f"Готово, записала Вас 🌿\n{details}\n\nБудем ждать Вас!",
            f"Дайын, Сізді жаздым 🌿\n{details}\n\nКүтеміз!",
        )
    except Exception as exc:
        _safe_log(chat_id, "crm_book_error", {"error": str(exc)[:500]})
        session["step"] = "escalated"
        session["escalated"] = True
        return _tr(
            session,
            "Не получилось автоматически создать запись. Передам администратору, он закрепит удобное время вручную 🌿",
            "Жазбаны автоматты түрде жасай алмадым. Әкімшіге жіберемін, ол ыңғайлы уақытты қолмен бекітеді 🌿",
        )


async def _handle_existing_lookup(chat_id: str, phone: str, session: dict[str, Any], text: str = "") -> str:
    normalized = sanitize_kz_phone(phone or session.get("phone") or "") or phone
    try:
        lookup = await crm.patient_lookup(normalized)
        session["patient_lookup"] = lookup
        session["patient_lookup_done"] = True

        appt = None
        if isinstance(lookup, dict) and lookup.get("hasActiveAppointment"):
            raw = lookup.get("lastAppointment") or lookup.get("appointment") or {}
            if isinstance(raw, dict) and raw:
                appt = raw

        if appt:
            date = appt.get("date") or appt.get("appointmentDate") or ""
            time = appt.get("timeStart") or appt.get("time_start") or appt.get("time") or ""
            doctor = appt.get("doctorName") or appt.get("doctor_name") or ""
            details = ", ".join(str(x) for x in [date, time, doctor] if x) or "активная запись"
            session["step"] = "done"
            return _tr(session, f"Вы уже записаны: {details} 🌿", f"Сіз жазылғансыз: {details} 🌿")
    except Exception as exc:
        _safe_log(chat_id, "patient_lookup_error", {"error": str(exc)[:500]})

    low = _low(text)
    wants_move = _is_cancel(text) or any(w in low for w in [
        "перенести", "перенес", "поменять время", "на завтра", "завтра",
        "ауыстыр", "басқа уақыт", "ертең", "ертен"
    ])
    has_contra_note = _contra_has_hard_stop(text) or any(w in low for w in ["кардиостимулятор", "кардиостимулятор бар"])

    session["step"] = "escalated"
    session["escalated"] = True

    if wants_move:
        answer = _tr(
            session,
            "Поняла Вас. Передам администратору, чтобы он проверил Вашу запись, напомнил дату и время и помог перенести на удобное время 🌿",
            "Түсіндім. Әкімшіге жіберемін, ол жазбаңызды тексеріп, күні мен уақытын еске салып, ыңғайлы уақытқа ауыстыруға көмектеседі 🌿",
        )
    else:
        answer = _tr(
            session,
            "Поняла Вас. Я передам администратору, чтобы он проверил Вашу запись и напомнил дату и время 🌿",
            "Түсіндім. Жазбаңызды тексеріп, күні мен уақытын еске салу үшін әкімшіге жіберемін 🌿",
        )

    if has_contra_note:
        answer += "\n\n" + _tr(
            session,
            "Информацию про кардиостимулятор обязательно передадим врачу.",
            "Кардиостимулятор туралы ақпаратты дәрігерге міндетті түрде жеткіземіз.",
        )

    return answer

def _wants_existing_lookup(text: str) -> bool:
    low = _low(text)
    if any(w in low for w in LOOKUP_WORDS):
        return True
    has_existing = any(w in low for w in ["уже", "моя", "мою", "у меня", "менің", "меним"])
    has_record = any(w in low for w in ["запис", "запись", "жазыл", "жазба"])
    has_time = any(w in low for w in ["когда", "время", "дат", "во сколько", "напом", "қашан", "уақыт"])
    return has_existing and has_record and has_time


def _is_cancel(text: str) -> bool:
    return _has_any(text, CANCEL_WORDS)


def _contra_has_hard_stop(text: str) -> bool:
    low = _low(text)

    # Негативные конструкции считаем только если "нет/жоқ" относится прямо к этому слову.
    # Важно:
    # "кардиостимулятора нет" -> НЕ важная заметка
    # "нет кардиостимулятора" -> НЕ важная заметка
    # "онкологии нет, но есть кардиостимулятор" -> важная заметка для врача, запись продолжаем
    direct_neg_patterns = [
        r"(?:нет|нету|жоқ|жок)\s+(?:у\s+меня\s+)?{word}",
        r"{word}\s+(?:у\s+меня\s+)?(?:нет|нету|жоқ|жок)",
        r"{word}\s+жоқ",
        r"{word}\s+жок",
    ]

    for word in HARD_CONTRA_WORDS:
        w = re.escape(word)

        if not re.search(w, low):
            continue

        negated = False
        for pat in direct_neg_patterns:
            if re.search(pat.format(word=w), low):
                negated = True
                break

        if negated:
            continue

        # "есть кардиостимулятор", "кардиостимулятор есть", "бар" — важная заметка врачу.
        if re.search(r"(?:есть|имеется|бар)\s+(?:у\s+меня\s+)?[^.!?,]{0,25}" + w, low):
            return True
        if re.search(w + r"[^.!?,]{0,25}(?:есть|имеется|бар)", low):
            return True

        # Если слово есть без отрицания — считаем важной заметкой врачу, но запись НЕ останавливаем.
        return True

    return False

def _contra_is_clear_no(text: str) -> bool:
    low = _low(text)
    return any(w == low or w in low for w in NO_CONTRA_WORDS)


async def _continue_after_collected_age(chat_id: str, session: dict[str, Any], text: str, age: int) -> str:
    """Продолжение сценария, если возраст уже есть в этом же сообщении.

    Нужно для сложных сообщений:
    "мне 56, болит поясница, кардиостимулятора нет, завтра можно?"
    """
    if _contra_has_hard_stop(text):
        session["contraindications_raw"] = text
        session["contraindications_ok"] = False
        session["contraindications_verdict"] = "doctor_note"
        session["doctor_note_required"] = True

        date_iso = _parse_date(text)
        prefix = _tr(
            session,
            "Спасибо, что уточнили 🙏 Это важная информация, врач обязательно учтёт её на консультации. На первичную консультацию можно записаться, а врач после осмотра подскажет безопасный дальнейший план.",
            "Нақтылағаныңызға рақмет 🙏 Бұл маңызды ақпарат, дәрігер консультацияда міндетті түрде ескереді. Алғашқы консультацияға жазылуға болады, дәрігер қарап, қауіпсіз әрі дұрыс жоспарды түсіндіреді.",
        )

        if date_iso:
            slots_answer = await _show_slots(chat_id, session, date_iso)
            return _prepend_price_if_needed(text, session, prefix + "\n\n" + slots_answer)

        session["step"] = "date"
        session["questionnaire_step"] = "date"
        return _prepend_price_if_needed(text, session, prefix + "\n\n" + _ask_date(session))

    # Если пациент сразу написал, что противопоказаний нет — не спрашиваем это повторно.
    if _contra_is_clear_no(text):
        session["contraindications_raw"] = text
        session["contraindications_ok"] = True
        session["contraindications_verdict"] = "proceed"

        date_iso = _parse_date(text)
        if date_iso:
            slots_answer = await _show_slots(chat_id, session, date_iso)
            return _prepend_price_if_needed(text, session, slots_answer)

        session["step"] = "date"
        return _ask_date(session)

    # Если противопоказания ещё не ясны — задаём обязательный вопрос.
    session["step"] = "contraindications"
    session["questionnaire_step"] = "contra"

    if age > 74:
        session["senior_patient"] = True
        return _senior_contra_intro(session)

    stop = _age_stop_text(age, session)
    if age < 18:
        session["minor_parent_required"] = True
        return stop + "\n\n" + _ask_contra(session)

    return _ask_contra(session)


async def handle_message(chat_id: str, phone: str, user_text: str) -> str:
    """Главная функция, которую вызывает main.py.

    main.py ожидает именно такую сигнатуру:
    await handle_message(chat_id=chat_id, phone=phone, user_text=text)
    """
    text = _clean(user_text)
    _safe_add_message(chat_id, "user", text)

    session = state.get_session(chat_id)
    if not isinstance(session, dict):
        session = {}

    session["phone"] = phone or session.get("phone") or ""
    session["language"] = _detect_lang(text, session)

    # 0.5) Две задачи в одном сообщении:
    # "я уже записан/отмените/перенести" + "маму/папу/сына хочу записать".
    # Не запускаем обычную анкету, чтобы не перепутать пациентов.
    if _is_relative_new_booking_request(text):
        session["step"] = "escalated"
        session["escalated"] = True
        answer = _relative_dual_task_answer(session, text)
        return _finalize(chat_id, session, answer)

    if not text:
        return _finalize(chat_id, session, _tr(session, "Напишите, пожалуйста, что Вас беспокоит 🌿", "Сізді не мазалайды? 🌿"))

    # 1) Уже записан / напомнить запись — не запускаем новую запись.
    if _wants_existing_lookup(text):
        answer = await _handle_existing_lookup(chat_id, phone, session, text)
        return _finalize(chat_id, session, answer)

    # 2) Отмена/перенос — не запускаем новую запись.
    if _is_cancel(text):
        session["step"] = "escalated"
        session["escalated"] = True
        answer = _tr(
            session,
            "Поняла Вас 🌿 Передам администратору, он проверит Вашу запись и поможет отменить или перенести её на удобное время.",
            "Түсіндім 🌿 Әкімшіге жіберемін, ол жазбаңызды тексеріп, тоқтатуға немесе ыңғайлы уақытқа ауыстыруға көмектеседі.",
        )
        if _contra_has_hard_stop(text) or "кардиостимулятор" in _low(text):
            answer += "\n\n" + _tr(
                session,
                "Информацию про кардиостимулятор обязательно передадим врачу.",
                "Кардиостимулятор туралы ақпаратты дәрігерге міндетті түрде жеткіземіз.",
            )
        return _finalize(chat_id, session, answer)

    # 2.5) Суперсложный сценарий: жалоба + возраст + противопоказания/дата в одном сообщении.
    # Важно: этот блок стоит ДО FAQ и ДО обычной анкеты.
    # Примеры:
    # "Мне 78 лет, болит спина, противопоказаний нет"
    # "Мне 45, грыжа поясницы, но есть кардиостимулятор"
    # "Мен 62 жастамын, белім ауырады, ертең келуге бола ма?"
    inline_age = _extract_age(text, step="age")
    if (
        inline_age
        and _has_complaint(text)
        and session.get("contraindications_ok") is not True
        and not session.get("contraindications_verdict")
    ):
        session["complaint"] = session.get("complaint") or text
        session["age"] = inline_age
        answer = await _continue_after_collected_age(chat_id, session, text, inline_age)
        return _finalize(chat_id, session, answer)

    # 3) Типовые вопросы.
    # Если в сообщении есть жалоба, жалоба важнее FAQ.
    # Например "Белім ауырады, похоже протрузия" нельзя ошибочно трактовать как вопрос про УЗИ.
    info = None if _has_complaint(text) else _clinic_answer(text, session)
    if info and not session.get("complaint"):
        session["step"] = "complaint"
        return _finalize(chat_id, session, info)

    step = session.get("step") or "start"

    # 4) Если пациент прислал возраст внутри любого сообщения — сохраняем,
    # но НЕ перескакиваем противопоказания.
    age = _extract_age(text, step="age" if step == "age" else "")
    if age and not session.get("age"):
        session["age"] = age

    # ЖЁСТКИЙ ГЕЙТ: если жалоба уже есть и пациент прислал возраст,
    # нельзя перейти к дате, пока не закрыты противопоказания.
    if age and session.get("complaint") and session.get("contraindications_ok") is not True and not session.get("contraindications_verdict"):
        answer = await _continue_after_collected_age(chat_id, session, text, age)
        return _finalize(chat_id, session, answer)

    # 5) Старт / выясняем жалобу.
    if step in ("start", "", None):
        if _has_no_complaint(text):
            count = int(session.get("no_complaint_count") or 0) + 1
            session["no_complaint_count"] = count
            session["step"] = "complaint_no_confirm"
            answer = _tr(
                session,
                "Поняла 🌿 Если конкретной жалобы нет, можно прийти на первичную консультацию для профилактического осмотра. Хотите записаться на консультацию?",
                "Түсіндім 🌿 Егер нақты шағым болмаса, профилактикалық қаралу үшін алғашқы консультацияға келуге болады. Консультацияға жазылайын ба?",
            )
            return _finalize(chat_id, session, answer)

        if _has_complaint(text):
            session["complaint"] = text

            # Если возраст уже есть в этом же сообщении — не спрашиваем его повторно.
            # Сразу проверяем противопоказания/дату.
            if session.get("age"):
                answer = await _continue_after_collected_age(chat_id, session, text, int(session["age"]))
                return _finalize(chat_id, session, answer)

            session["step"] = "age"
            return _finalize(chat_id, session, _ask_age_contextual(session, text))

        if _has_booking_intent(text) or _is_greeting_only(text):
            session["step"] = "complaint"
            answer = _tr(
                session,
                "Здравствуйте! Да, можно записаться на консультацию по акции 🌿\nПодскажите, пожалуйста, что Вас беспокоит?",
                "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿\nСізді не мазалайды?",
            )
            return _finalize(chat_id, session, answer)

        session["step"] = "complaint"
        return _finalize(chat_id, session, _ask_complaint(session))

    if step == "complaint":
        if _has_no_complaint(text):
            count = int(session.get("no_complaint_count") or 0) + 1
            session["no_complaint_count"] = count
            if count == 1:
                session["step"] = "complaint_no_confirm"
                answer = _tr(
                    session,
                    "Поняла 🌿 Если конкретной жалобы нет, можно прийти на первичную консультацию для профилактического осмотра. Хотите записаться на консультацию?",
                    "Түсіндім 🌿 Егер нақты шағым болмаса, профилактикалық қаралу үшін алғашқы консультацияға келуге болады. Консультацияға жазылайын ба?",
                )
                return _finalize(chat_id, session, answer)
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог с записью и подсказал, какая консультация подойдёт.", "Түсіндім 🌿 Әкімшіге жіберемін, ол жазылуға көмектесіп, қандай консультация қолайлы екенін айтады."))

        if not _has_complaint(text):
            if _has_booking_intent(text) or _is_greeting_only(text):
                return _finalize(chat_id, session, _ask_complaint(session))
            return _finalize(chat_id, session, _ask_complaint(session))

        session["complaint"] = text
        session["step"] = "age"
        return _finalize(chat_id, session, _ask_age_contextual(session, text))

    if step == "complaint_no_confirm":
        if _has_no_complaint(text):
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог с записью и подсказал, какая консультация подойдёт.", "Түсіндім 🌿 Әкімшіге жіберемін, ол жазылуға көмектесіп, қандай консультация қолайлы екенін айтады."))
        if _is_positive_confirm(text) or _has_booking_intent(text):
            session["complaint"] = "Профилактическая консультация, без конкретной жалобы"
            session["step"] = "age"
            return _finalize(chat_id, session, _tr(session, "Хорошо 🌿 Подскажите, пожалуйста, сколько Вам лет?", "Жақсы 🌿 Жасыңыз нешеде?"))
        if _has_complaint(text):
            session["complaint"] = text

            # Если возраст уже есть в этом же сообщении — не спрашиваем его повторно.
            # Сразу проверяем противопоказания/дату.
            if session.get("age"):
                answer = await _continue_after_collected_age(chat_id, session, text, int(session["age"]))
                return _finalize(chat_id, session, answer)

            session["step"] = "age"
            return _finalize(chat_id, session, _ask_age_contextual(session, text))
        session["step"] = "escalated"
        session["escalated"] = True
        return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог сориентироваться.", "Түсіндім 🌿 Әкімшіге жіберемін, ол нақтылап көмектеседі."))

    # 6) Возраст: после возраста ВСЕГДА спрашиваем противопоказания.
    if step == "age":
        age = _extract_age(text, step="age")
        if not age:
            return _finalize(chat_id, session, _tr(session, "Подскажите, пожалуйста, сколько Вам лет?", "Жасыңыз нешеде?"))

        session["age"] = age
        stop = _age_stop_text(age, session)
        if age > 74:
            session["senior_patient"] = True
            session["step"] = "contraindications"
            session["questionnaire_step"] = "contra"
            return _finalize(chat_id, session, _senior_contra_intro(session))

        # Для младше 18 не останавливаем насовсем, но фиксируем необходимость родителя.
        if age < 18:
            session["minor_parent_required"] = True
            session["step"] = "contraindications"
            answer = stop + "\n\n" + _ask_contra(session)
            return _finalize(chat_id, session, answer)

        session["step"] = "contraindications"
        session["questionnaire_step"] = "contra"
        return _finalize(chat_id, session, _ask_contra(session))

    # 7) Противопоказания — обязательный гейт перед датой.
    if step == "contraindications":
        session["contraindications_raw"] = text

        if _contra_is_clear_no(text):
            session["contraindications_ok"] = True
            session["contraindications_verdict"] = "proceed"
            session["step"] = "date"
            return _finalize(chat_id, session, _ask_date(session))

        if _contra_has_hard_stop(text):
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "escalate"
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(
                chat_id,
                session,
                _tr(
                    session,
                    "Спасибо, что уточнили 🙏 Это важная информация, врач обязательно учтёт её на консультации. На первичную консультацию можно записаться, а врач после осмотра подскажет безопасный дальнейший план.",
                    "Нақтылағаныңызға рақмет 🙏 Бұл маңызды ақпарат, дәрігер консультацияда міндетті түрде ескереді. Алғашқы консультацияға жазылуға болады, дәрігер қарап, қауіпсіз әрі дұрыс жоспарды түсіндіреді.",
                ),
            )

        # Если пациент написал симптомы вместо ответа по противопоказаниям — не считаем это противопоказанием.
        if _has_complaint(text):
            return _finalize(chat_id, session, _ask_contra(session))

        # Любой явный "есть/бар" без деталей — эскалация, чтобы не рисковать.
        if any(w in _low(text) for w in YES_WORDS):
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "escalate"
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(
                chat_id,
                session,
                _tr(
                    session,
                    "Поняла Вас 🙏 Чтобы не подсказать неверно, передам информацию администратору — он уточнит детали и подскажет по записи 🌿",
                    "Түсіндім 🙏 Қате ақпарат бермеу үшін әкімшіге жіберемін — ол нақтылап, жазылу бойынша көмектеседі 🌿",
                ),
            )

        return _finalize(chat_id, session, _ask_contra(session))

    # 8) Дата.
    if step in ("date", "preferred_time"):
        date_iso = _parse_date(text)
        if not date_iso:
            return _finalize(chat_id, session, _ask_date(session))

        answer = await _show_slots(chat_id, session, date_iso)
        return _finalize(chat_id, session, answer)

    # 9) Выбор времени.
    if step in ("time", "select_slot"):
        slots = session.get("last_slots") or []
        slot = _select_slot(text, slots)
        if not slot:
            return _finalize(
                chat_id,
                session,
                _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
            )

        session["selected_slot"] = slot
        session["selected_date"] = slot.get("date") or session.get("preferred_date")
        session["selected_time"] = slot.get("time")
        session["step"] = "name"
        return _finalize(chat_id, session, _ask_name(session))

    # 10) Имя.
    if step == "name":
        name = _extract_name(text)
        if not name:
            return _finalize(chat_id, session, _ask_name(session))

        session["patient_name"] = name
        answer = await _book(chat_id, session, phone)
        return _finalize(chat_id, session, answer)

    # 11) После записи короткие сообщения не запускают новую анкету.
    if step == "done" or session.get("booked"):
        if _is_cancel(text):
            session["step"] = "escalated"
            answer = _tr(
                session,
                "Поняла Вас 🌿 Передам администратору, он поможет отменить или перенести запись.",
                "Түсіндім 🌿 Әкімшіге жіберемін, ол жазбаны тоқтатуға немесе ауыстыруға көмектеседі.",
            )
            return _finalize(chat_id, session, answer)
        return _finalize(chat_id, session, _tr(session, "Хорошо, приняли 🌿 Будем ждать Вас!", "Жақсы, қабылдадық 🌿 Күтеміз!"))

    # 12) Если состояние непонятное — безопасно продолжаем с ближайшего обязательного шага.
    if not session.get("complaint"):
        session["step"] = "complaint"
        return _finalize(chat_id, session, _ask_complaint(session))

    if not session.get("age"):
        session["step"] = "age"
        return _finalize(chat_id, session, _tr(session, "Подскажите, пожалуйста, сколько Вам лет?", "Жасыңыз нешеде?"))

    if session.get("contraindications_ok") is not True:
        session["step"] = "contraindications"
        return _finalize(chat_id, session, _ask_contra(session))

    session["step"] = "date"
    return _finalize(chat_id, session, _ask_date(session))

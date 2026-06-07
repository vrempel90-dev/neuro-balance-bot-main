from __future__ import annotations

import re


KZ_UNIQUE_LETTERS = set("әғқңөұүһі")

KZ_STRONG_WORDS = {
    "сәлем", "ассалаумағалейку", "менің", "атым", "жасым", "жастамын",
    "ауырады", "ауырсыну", "ертең", "бүгін", "жоқ", "иә", "жазылғым",
    "келеді", "келемін", "барамын", "рахмет", "уақыт", "керек", "емес",
    "дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі",
    "жексенбі", "қарсы", "көрсетілім", "омыртқа", "жарық", "мүмкін",
    "қатты", "қай", "күн", "ыңғайлы",
}

KZ_WEAK_TRANSLIT = {
    "salem", "jok", "joq", "erten", "bugin", "belim", "aurady",
    "kelesi", "uakit", "qarsy", "korsetilim",
}

RU_STRONG_WORDS = {
    "здравствуйте", "добрый", "привет", "хочу", "записаться", "запишите",
    "прием", "приём", "консультация", "акция", "болит", "боль", "спина",
    "шея", "поясница", "плечо", "колено", "нога", "рука", "грыжа",
    "протрузия", "протруз", "артроз", "артрит", "лет", "мне", "завтра",
    "сегодня", "понедельник", "вторник", "среда", "четверг", "пятница",
    "суббота", "воскресенье", "вы", "вас", "астане", "находитесь",
    "сколько", "стоит", "цена", "стоимость", "лечебная", "процедура",
    "адрес", "где", "работаете", "график", "можно", "нужно", "отменить",
    "отмените", "перенести", "запись",
}

WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯәғқңөұүһіӘҒҚҢӨҰҮҺІ]+")


def detect_language(text: str, current: str | None = None) -> str:
    low = (text or "").lower()
    if not low.strip():
        return current if current in {"ru", "kk"} else "ru"

    unique_kz_letters = sum(low.count(ch) for ch in KZ_UNIQUE_LETTERS)
    words = WORD_RE.findall(low)
    word_set = set(words)

    strong_kz = len(word_set & KZ_STRONG_WORDS)
    weak_kz = len(word_set & KZ_WEAK_TRANSLIT)
    strong_ru = len(word_set & RU_STRONG_WORDS)

    if unique_kz_letters == 0 and strong_ru >= 1:
        return "ru"

    if unique_kz_letters >= 1 and strong_ru == 0:
        return "kk"

    has_cyrillic = bool(re.search(r"[а-яА-Я]", low))
    if has_cyrillic and unique_kz_letters == 0 and strong_kz == 0:
        return "ru"

    if strong_kz >= 2 and strong_ru == 0:
        return "kk"

    if weak_kz >= 2 and strong_ru == 0:
        return "kk"

    if strong_ru > strong_kz:
        return "ru"

    if strong_kz > strong_ru and unique_kz_letters > 0:
        return "kk"

    return current if current in {"ru", "kk"} else "ru"

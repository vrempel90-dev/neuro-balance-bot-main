from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any


# ============================================================
# Neuro Balance bilingual language guard (RU / KK)
#
# Две задачи:
#   1. ПОНЯТЬ язык входящего сообщения пациента (ru / kk / mixed / unknown).
#   2. ПРОВЕРИТЬ, что финальный ответ бота написан на языке пациента.
#
# Принципы:
#   - совпадения только по целым словам (подстрока "кол" не должна
#     превращать русское "колено" в казахский);
#   - смешанные RU+KK сообщения распознаются как mixed с доминирующим языком;
#   - явная просьба пациента ("қазақша жазыңыз" / "можно на русском")
#     имеет наивысший приоритет;
#   - имена, адреса, ссылки, телефоны и бренды не считаются языковым сигналом.
# ============================================================

LANG_RU = "ru"
LANG_KK = "kk"
LANG_MIXED = "mixed"
LANG_UNKNOWN = "unknown"

KZ_UNIQUE_LETTERS = set("әғқңөұүһі")
KZ_UNIQUE_RE = re.compile(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]")

# Буквы, которых практически не бывает в исконно казахских словах.
RU_ONLY_LETTERS = set("щъэё")

# Целые слова, однозначно казахские. Русских омонимов здесь быть не должно.
KZ_STRONG_WORDS = {
    # приветствия / вежливость
    "сәлем", "сәлеметсіз", "сәлеметсізбе", "салеметсиз", "салем",
    "ассалаумағалейкум", "ассалаумагалейкум", "рахмет", "рақмет",
    "кешіріңіз", "кешириниз", "өтінемін", "отинемин",
    # да / нет / согласие
    "иә", "ия", "жоқ", "жок", "жарайды", "жақсы", "жаксы", "дұрыс", "дурыс",
    "болады", "бола", "болмайды", "емес", "бар", "керек",
    # местоимения и служебные
    "мен", "сен", "сіз", "сіздер", "сиз", "сиздер", "біз", "биз", "ол",
    "менің", "меним", "сіздің", "сиздин", "біздің", "биздин", "оның",
    "және", "жане", "немесе", "бірақ", "бирак", "үшін", "ушин",
    "ма", "ме", "ба", "бе", "па", "пе",
    # вопросы
    "қалай", "калай", "қанша", "канша", "қайда", "кайда", "қашан", "кашан",
    "неге", "кім", "ким", "қай", "қайсы", "кайсы", "неше", "нешеде",
    # время
    "бүгін", "бугин", "ертең", "ертен", "кеше", "күн", "куни", "күні",
    "уақыт", "уакыт", "уақыты", "сағат", "сагат", "апта", "айда",
    "дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі",
    "дуйсенби", "сейсенби", "сарсенби", "бейсенби", "жума", "сенби", "жексенби",
    # возраст
    "жасым", "жаста", "жастамын", "жасында", "жасыңыз", "жасыныз",
    # тело / жалобы
    "ауырады", "ауырып", "ауыр", "ауырсыну", "сырқат", "сыркат",
    "белім", "белим", "бел", "мойын", "арқа", "арка", "аяқ", "аягым", "аяғым",
    "қол", "қолым", "колым", "буын", "омыртқа", "омыртка", "жарық", "жарык",
    "тізе", "тизе", "иық", "иык", "бас", "жүрек", "журек", "ұйып", "уйып",
    # клиника / запись
    "дәрігер", "дәрігерге", "даригер", "даригерге",
    "емдеу", "емдейсіз", "емдей", "аласыз", "аласыздар",
    "қабылдау", "кабылдау", "жазылу", "жазылғым", "жазылгым", "жазыламын",
    "келемін", "келемин", "келеді", "келеди", "барамын", "тұрады", "турады",
    "көмек", "комек", "көмектесе", "мүмкін", "мумкин",
    "қарсы", "карсы", "көрсетілім", "корсетилим", "көрсетілімдер",
    "атым", "атыңыз", "атыныз", "есімі", "есіміңіз",
    "айтыңыз", "айтыңызшы", "айтыныз", "айтынызшы",
    "түсінемін", "тусинемин", "білгім", "билгим",
}

# Латиница-транслит казахского.
KZ_TRANSLIT_WORDS = {
    "salem", "salemetsiz", "salemetsizbe", "rahmet", "rakhmet", "raqmet",
    "jok", "joq", "zhok", "zhoq", "iya", "ia", "bar", "kerek", "bolady",
    "erten", "bugin", "kun", "uakyt", "uakit", "sagat",
    "belim", "aurady", "auyrady", "ayak", "kol", "moyin", "omyrtka",
    "kansha", "qansha", "kaida", "qaida", "kashan", "qashan", "kalay", "qalay",
    "dariger", "kabyldau", "zhazylu", "jazylu", "kelemin", "baramyn",
    "zhasym", "jasym", "zhasta", "emdeu",
}

# Целые слова, однозначно русские. Слова-заимствования, общие для обоих
# языков (консультация, процедура, клиника, массаж, МРТ), сюда НЕ входят.
RU_STRONG_WORDS = {
    # приветствия / вежливость
    "здравствуйте", "здравствуй", "привет", "добрый", "доброе", "утро",
    "день", "вечер", "спасибо", "пожалуйста", "извините", "простите",
    # да / нет
    "да", "нет", "ага", "угу", "неа", "конечно", "хорошо", "ладно", "ок",
    # местоимения / служебные
    "я", "мы", "вы", "вас", "вам", "мне", "меня", "мой", "моя", "наш", "ваш",
    "он", "она", "они", "это", "этот", "эта", "который", "если", "чтобы",
    "или", "тоже", "также", "очень", "ещё", "еще", "уже", "только",
    # вопросы
    "сколько", "стоит", "стоимость", "цена", "где", "когда", "почему",
    "зачем", "какой", "какая", "какие", "что", "кто", "куда", "как",
    # время
    "сегодня", "завтра", "вчера", "утром", "днем", "днём", "вечером",
    "время", "часов", "неделя", "недели", "месяц",
    "понедельник", "вторник", "среда", "четверг", "пятница",
    "суббота", "воскресенье",
    # возраст
    "лет", "года", "год", "возраст", "исполнилось",
    # тело / жалобы
    "болит", "боль", "боли", "болел", "ноет", "тянет", "ломит", "немеет",
    "онемение", "спина", "спине", "поясница", "пояснице", "шея", "шее",
    "колено", "колене", "плечо", "плече", "нога", "ноге", "рука", "руке",
    "сустав", "суставы", "грыжа", "протрузия", "защемление",
    # клиника / запись
    "хочу", "можно", "нужно", "надо", "буду", "приду", "записаться",
    "запись", "записать", "записи", "прием", "приём", "врач", "врачу",
    "доктор", "лечение", "лечить", "помочь", "помогает", "свободно",
    "адрес", "работаете", "график", "отменить", "перенести",
}

RU_TRANSLIT_WORDS = {
    "privet", "zdravstvuyte", "zdravstvuite", "spasibo", "pozhaluysta",
    "bolit", "spina", "skolko", "stoit", "mozhno", "hochu", "khochu",
    "zapisatsya", "zavtra", "segodnya", "vrach", "koleno",
    "noga", "ruka", "shea", "sheya", "gryzha",
}

# Явная просьба писать на конкретном языке.
_EXPLICIT_RU_PATTERNS = (
    "на русском", "по русски", "по-русски", "русский язык", "русском языке",
    "пишите на русском", "говорите на русском", "можно на русском",
    "орысша", "орыс тілінде", "орысша жазыңыз", "орысша жаз",
)
_EXPLICIT_KK_PATTERNS = (
    "қазақша", "казакша", "казахша", "қазақ тілінде", "казак тилинде",
    "на казахском", "по казахски", "по-казахски", "казахский язык",
    "пишите на казахском", "можно на казахском", "қазақшаға",
)

# Токены, которые нельзя считать языковым сигналом и нельзя переводить:
# ссылки, телефоны, e-mail, латинские бренды, числа.
_URL_RE = re.compile(r"https?://\S+|www\.\S+|\b\S+\.(?:kz|ru|com|org)\b", re.I)
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-()]{6,}\d)")
_EMAIL_RE = re.compile(r"\b[\w.\-]+@[\w.\-]+\b")
_PROTECTED_BRANDS = ("neuro balance", "neuro_balance", "2gis", "instagram",
                     "whatsapp", "kaspi", "red", "prp")

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁәғқңөұүһіӘҒҚҢӨҰҮҺІ]+")


@dataclass(frozen=True)
class LanguageAnalysis:
    """Структурированный разбор языка одного сообщения."""

    detected_language: str          # ru | kk | mixed | unknown
    preferred_response_language: str  # ru | kk
    confidence: float               # 0.0 .. 1.0
    explicit_request: str = ""      # ru | kk | "" — явная просьба пациента
    ru_score: float = 0.0
    kk_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _low(text: str) -> str:
    return (text or "").lower()


def strip_protected_spans(text: str) -> str:
    """Убирает ссылки/телефоны/почты/бренды перед языковым анализом."""
    out = _URL_RE.sub(" ", text or "")
    out = _EMAIL_RE.sub(" ", out)
    out = _PHONE_RE.sub(" ", out)
    low = out.lower()
    for brand in _PROTECTED_BRANDS:
        idx = 0
        while True:
            found = low.find(brand, idx)
            if found < 0:
                break
            out = out[:found] + " " * len(brand) + out[found + len(brand):]
            low = out.lower()
            idx = found + len(brand)
    return out


def explicit_language_request(text: str) -> str:
    """Возвращает 'ru'/'kk', если пациент явно попросил язык, иначе ''."""
    low = _low(text)
    if not low.strip():
        return ""
    # Казахскую просьбу проверяем первой: "можно на казахском" содержит
    # и русские слова тоже, но смысл задаёт именно целевой язык.
    if any(p in low for p in _EXPLICIT_KK_PATTERNS):
        return LANG_KK
    if any(p in low for p in _EXPLICIT_RU_PATTERNS):
        return LANG_RU
    return ""


def _kz_suffix_hits(words: list[str]) -> int:
    """Казахская морфология на словах без уникальных букв.

    Нужна для коротких сообщений вроде «Жасым 44» или «Балама 12 жаста».
    """
    suffixes = (
        "мын", "мін", "мин", "бын", "бін", "пын", "пін",
        "сыз", "сіз", "сиз", "ңыз", "ңіз", "ныз", "низ",
        "дың", "дің", "тың", "тің", "ның", "нің",
        "лар", "лер", "дар", "дер", "тар", "тер",
        "дан", "ден", "тан", "тен", "нан", "нен",
        "ады", "еді", "еди", "йды", "ыңыз", "іңіз",
    )
    hits = 0
    for w in words:
        if len(w) < 5:
            continue
        if w in RU_STRONG_WORDS:
            continue
        if any(w.endswith(s) for s in suffixes):
            hits += 1
    return hits


def analyze_language(text: str, current: str | None = None) -> LanguageAnalysis:
    """Полный языковой разбор сообщения пациента.

    ``current`` — язык, на котором диалог шёл до этого сообщения. Он
    используется только как запасной вариант, когда сигнала в сообщении нет.
    """
    fallback = current if current in (LANG_RU, LANG_KK) else LANG_RU
    cleaned = strip_protected_spans(text or "")
    low = _low(cleaned)

    explicit = explicit_language_request(text or "")

    if not low.strip():
        return LanguageAnalysis(
            detected_language=LANG_UNKNOWN,
            preferred_response_language=explicit or fallback,
            confidence=0.0,
            explicit_request=explicit,
        )

    words = _WORD_RE.findall(low)
    word_set = set(words)

    # --- казахские сигналы ---
    kz_letter_words = sum(1 for w in words if any(ch in KZ_UNIQUE_LETTERS for ch in w))
    kz_strong = len(word_set & KZ_STRONG_WORDS)
    kz_translit = len(word_set & KZ_TRANSLIT_WORDS)
    kz_suffix = _kz_suffix_hits(words)

    kk_score = 2.0 * kz_letter_words + 2.0 * kz_strong + 1.5 * kz_translit + 1.0 * kz_suffix

    # --- русские сигналы ---
    ru_strong = len(word_set & RU_STRONG_WORDS)
    ru_translit = len(word_set & RU_TRANSLIT_WORDS)
    ru_letter_words = sum(1 for w in words if any(ch in RU_ONLY_LETTERS for ch in w))

    ru_score = 2.0 * ru_strong + 1.5 * ru_translit + 1.0 * ru_letter_words

    total = kk_score + ru_score
    if total <= 0:
        return LanguageAnalysis(
            detected_language=LANG_UNKNOWN,
            preferred_response_language=explicit or fallback,
            confidence=0.0,
            explicit_request=explicit,
            ru_score=ru_score,
            kk_score=kk_score,
        )

    margin = abs(kk_score - ru_score) / total
    dominant = LANG_KK if kk_score > ru_score else LANG_RU

    # Оба языка заметно представлены — это смешанное сообщение.
    both_present = kk_score > 0 and ru_score > 0
    weaker = min(kk_score, ru_score)
    if both_present and weaker >= 2.0 and margin < 0.8:
        detected = LANG_MIXED
    elif margin < 0.2 and both_present:
        detected = LANG_MIXED
    else:
        detected = dominant

    # Уверенность = насколько один язык перевесил другой (margin)
    # И насколько вообще много языкового материала в сообщении (strength).
    # Одно короткое слово не должно давать уверенность 1.0.
    strength = min(1.0, total / 8.0)
    if detected == LANG_MIXED:
        # У смешанного сообщения отвечаем на доминирующем языке;
        # при равенстве сигналов держим язык диалога.
        preferred = fallback if margin < 0.2 else dominant
        confidence = round(min(0.6, 0.3 * margin + 0.3 * strength), 2)
    else:
        preferred = detected
        confidence = round(min(1.0, 0.45 * margin + 0.55 * strength), 2)

    if explicit:
        preferred = explicit
        confidence = 1.0

    return LanguageAnalysis(
        detected_language=detected,
        preferred_response_language=preferred,
        confidence=confidence,
        explicit_request=explicit,
        ru_score=ru_score,
        kk_score=kk_score,
    )


def detect_language(text: str, current: str | None = None) -> str:
    """Обратно совместимая обёртка: всегда возвращает 'ru' или 'kk'."""
    return analyze_language(text, current).preferred_response_language


# ============================================================
# Проверка языка исходящего ответа
# ============================================================

def response_language(answer: str) -> LanguageAnalysis:
    """Язык готового ответа бота (по тем же правилам, что и вход)."""
    return analyze_language(answer, None)


def response_language_violation(answer: str, expected: str) -> str:
    """Проверяет, что ответ написан на ожидаемом языке.

    Возвращает код нарушения или '' если всё в порядке:
      - ``wrong_language_response``  — ответ целиком на другом языке;
      - ``mixed_language_response``  — ответ мешает русский и казахский.

    Защищённые фрагменты (ссылки, телефоны, ФИО латиницей, бренды) не
    учитываются, поэтому «Neuro Balance» в казахском ответе нарушением не
    считается.
    """
    if expected not in (LANG_RU, LANG_KK):
        return ""
    text = (answer or "").strip()
    if not text:
        return ""

    analysis = analyze_language(text, None)
    if analysis.detected_language == LANG_UNKNOWN:
        return ""

    if analysis.detected_language == LANG_MIXED:
        return "mixed_language_response"

    if analysis.detected_language != expected:
        return "wrong_language_response"

    return ""


# ============================================================
# Безопасное исправление языка ответа
# ============================================================

def _template_pairs() -> list[tuple[str, str]]:
    """Пары (русский шаблон, казахский шаблон) из утверждённой базы клиники.

    Это единственный разрешённый способ «перевести» ответ: мы не переводим
    текст на лету, а подставляем заранее утверждённый казахский шаблон.
    Длинные шаблоны идут первыми, чтобы короткий шаблон не съел кусок
    длинного.
    """
    try:
        import clinic_info
    except Exception:  # pragma: no cover
        return []
    pairs: list[tuple[str, str]] = []
    for topic, kk in getattr(clinic_info, "CLINIC_INFO_TEMPLATES_KK", {}).items():
        ru = clinic_info.CLINIC_INFO_TEMPLATES.get(topic)
        if ru and kk and ru != kk:
            pairs.append((ru, kk))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def enforce_response_language(answer: str, expected: str) -> tuple[str, str]:
    """Проверяет язык готового ответа и безопасно чинит его.

    Возвращает ``(answer, violation)``:

    - если язык совпадает — текст возвращается без изменений;
    - если в ответе стоит русский шаблон клиники, а ждали казахский, он
      заменяется на утверждённый казахский шаблон той же темы;
    - если утверждённого эквивалента нет, текст НЕ переводится и НЕ
      выбрасывается: он возвращается как есть, а код нарушения уходит в
      логи. Доставить верный факт на другом языке безопаснее, чем
      сочинить казахский текст или промолчать.

    Имена, адреса, ссылки, телефоны и бренды не трогаются никогда.
    """
    text = str(answer or "")
    if expected not in (LANG_RU, LANG_KK) or not text.strip():
        return text, ""

    violation = response_language_violation(text, expected)
    if not violation:
        return text, ""

    if expected == LANG_KK:
        repaired = text
        for ru_template, kk_template in _template_pairs():
            if ru_template in repaired:
                repaired = repaired.replace(ru_template, kk_template)
        if repaired != text:
            still = response_language_violation(repaired, expected)
            return repaired, (still or violation + "_repaired_from_template")

    return text, violation

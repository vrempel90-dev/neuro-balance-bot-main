"""Conservative validation of the medical scope in SYSTEM_PROMPT_rendered.md.

The model interprets conversation. These backend checks bound what its
interpretation may authorize, without diagnosing or expanding clinic scope.
Unknown vocabulary and contradictory active complaints remain UNCLEAR.
"""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any

PROFILE_KEYWORDS = {
    "боль в спине": ["спин", "поясниц", "позвоноч", "грыж", "протруз", "остеохонд", "радикул", "ишиас", "защем", "седалищ", "арқа", "омыртқа", "arkam", "arqam", "belim"],
    "боль в шее": ["шея", "шеи", "шее", "шею", "шейн", "мойын", "мойны", "мойным", "желке", "moynym"],
    "суставы": ["сустав", "колен", "тазобед", "плеч", "локт", "артроз", "артрит", "буын", "тізе", "иық", "шынтақ", "жамбас", "tizem"],
    "неврология": ["невролог", "защем", "головокруж", "онемение", "мурашк", "жүйке", "ұйып", "ұю"],
}
# Relations and contraindications are deliberately not specialty exclusions.
IRRELEVANT_KEYWORDS = ["зуб", "стоматолог", "лор", "ухо", "уши", "горл", "гинеколог", "кож", "дерматолог", "педиатр", "аппендицит", "желуд", "тіс", "құлақ", "тамақ"]
HEEL_NON_PROFILE_KEYWORDS = ["пятк", "пяточ", "шпор", "подошв", "фасциит", "өкше"]


def _low(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е"))
    return re.sub(r"\bспена\b", "спина", text)


def _matches(text: str, roots: list[str]):
    return re.finditer(r"(?<!\w)(?:" + "|".join(map(re.escape, roots)) + r")\w*", text)


def _inactive(clause: str, start: int, end: int) -> bool:
    before, after = clause[:start], clause[end:]
    return bool(
        re.search(r"\b(?:не|нет|без)\b(?:\s+\w+){0,4}\s*$", before)
        or re.search(r"\b(?:был|была|были|раньше)\b", before)
        or re.match(r"\s+(?:не\s+(?:бол|беспоко|име)|нет\b|ауырмай|жоқ|уже\s+прош|прош[её]л|прошла|өтіп|жазыл)", after)
    )


def _active(clause: str, roots: list[str]) -> bool:
    return any(not _inactive(clause, m.start(), m.end()) for m in _matches(clause, roots))


def classify_by_keywords(text: str) -> dict:
    """Compatibility API with a tri-state result for active complaint clauses."""
    low = _low(text)
    clauses = re.split(r"[,;.!?\n]|\b(?:но|а|и|ал|бірақ|және)\b", low)
    current_rehab = bool(re.search(r"реабилитац|оңалту", low) and re.search(r"нужн|необходим|требует|қажет|керек|хочу", low))
    found: list[str] = []
    excluded = False
    for clause in clauses:
        if current_rehab and re.search(r"операци|травм|жарақат", clause):
            # A past MSK operation is the reason for present rehabilitation,
            # not a resolved complaint. Negation still remains authoritative.
            clause = re.sub(r"\b(?:был|была|были|раньше)\b", "", clause)
        for service, roots in PROFILE_KEYWORDS.items():
            if _active(clause, roots):
                found.append(service)
        for m in re.finditer(r"\bбел(?:ім|і|ің|іңіз)?\b|\bбас айнал\w*", clause):
            if not _inactive(clause, m.start(), m.end()):
                found.append("боль в спине" if "бел" in m.group() else "неврология")
        excluded |= _active(clause, IRRELEVANT_KEYWORDS + HEEL_NON_PROFILE_KEYWORDS)
    if found and excluded:
        status, service, reason = "unclear", "смешанные жалобы", "Есть профильная и непрофильная жалобы. Уточни, по какой нужна консультация; не игнорируй вторую."
    elif found:
        status, service, reason = "profile", found[0], "Подтверждён утверждённый профиль; возраст и противопоказания проверяются отдельно."
    elif excluded:
        status, service, reason = "non_profile", "непрофильный запрос", "Активная жалоба вне утверждённого профиля клиники."
    else:
        status, service, reason = "unclear", "не определено", "Нужен один конкретный уточняющий вопрос о жалобе."
    return {"can_help": {"profile": True, "non_profile": False, "unclear": None}[status],
            "profile_status": status, "service": service,
            "confidence": 0.9 if status != "unclear" else 0.0, "reason": reason}


APPROVED_CONTRAINDICATIONS_RU = """Перед записью нужно уточнить противопоказания. Скажите, есть ли у Вас сейчас или были раньше:

— кардиостимулятор, дефибриллятор, инсулиновая помпа или кохлеарный имплант;
— тромбофлебит, тромбозы или серьёзные нарушения свёртываемости крови;
— активное онкологическое заболевание или подозрение на него, которое ещё не исключено;
— эпилепсия или судороги;
— декомпенсированный сахарный диабет или тиреотоксикоз;
— беременность;
— высокая температура, ОРВИ, грипп или другая острая инфекция;
— тяжёлые проблемы с сердцем, дыханием или психическим состоянием.

Есть ли у Вас что-либо из перечисленного?"""

# Exact translation of the eight approved groups, not a separate medical rule.
APPROVED_CONTRAINDICATIONS_KK = """Жазбас бұрын қарсы көрсетілімдерді нақтылау қажет. Қазір немесе бұрын төмендегілердің біреуі болды ма:

— кардиостимулятор, дефибриллятор, инсулин помпасы немесе кохлеарлық имплант;
— тромбофлебит, тромбоздар немесе қан ұюының елеулі бұзылыстары;
— белсенді онкологиялық ауру немесе әлі жоққа шығарылмаған онкологияға күдік;
— эпилепсия немесе құрысулар;
— декомпенсацияланған қант диабеті немесе тиреотоксикоз;
— жүктілік;
— жоғары температура, ЖРВИ, тұмау немесе басқа жедел инфекция;
— жүрек, тыныс алу немесе психикалық жағдайға байланысты ауыр мәселелер.

Осы аталғандардың біреуі бар ма?"""

APPROVED_PROMPT_PATH = Path(__file__).with_name("SYSTEM_PROMPT_rendered.md")


def _checklist_items(text: str) -> tuple[str, ...]:
    return tuple(_low(line.strip()[1:].strip()).rstrip(";.")
                 for line in text.splitlines() if line.strip().startswith("—"))


def approved_rules_version() -> str:
    """Read and validate the owning source on every gate; no stale cached allow.

    The eight Russian groups are pinned to the clinic-approved baseline. Any
    removal, addition or substitution requires explicit clinical review rather
    than silently authorizing a different rule set.
    """
    try:
        text = APPROVED_PROMPT_PATH.read_text(encoding="utf-8")
        section = text.split("### Шаг 3 — противопоказания", 1)[1].split("### Шаг 4", 1)[0]
    except (OSError, UnicodeError, IndexError):
        return ""
    items = _checklist_items(section)
    if len(items) != 8 or items != _checklist_items(APPROVED_CONTRAINDICATIONS_RU):
        return ""
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def _patient_key(session: dict[str, Any]) -> str:
    return f"{session.get('patient_context_version', 0)}:{normalize_patient_relation(str(session.get('patient_relation') or ''))}"


def mark_checklist_presented(session: dict[str, Any], answer: str) -> bool:
    """Call only after the provider accepted the complete outbound checklist."""
    version = approved_rules_version()
    normalized = _low(answer)
    complete = any(all(item in normalized for item in _checklist_items(text))
                   for text in (APPROVED_CONTRAINDICATIONS_RU, APPROVED_CONTRAINDICATIONS_KK))
    if not version or not complete:
        return False
    session["contraindication_presentation"] = {"version": version, "patient": _patient_key(session)}
    return True


def checklist_was_presented(session: dict[str, Any]) -> bool:
    version = approved_rules_version()
    return bool(version and session.get("contraindication_presentation") ==
                {"version": version, "patient": _patient_key(session)})


def mark_clearance(session: dict[str, Any], user_text: str) -> bool:
    """Record explicit patient speech, never model notes or document contents."""
    if not checklist_was_presented(session) or contraindication_evidence(user_text) != "clear":
        return False
    session["contraindication_clearance"] = dict(session["contraindication_presentation"])
    return True


def clearance_is_current(session: dict[str, Any]) -> bool:
    return bool(checklist_was_presented(session)
                and session.get("contraindications_ok") is True
                and session.get("contraindication_clearance") == session.get("contraindication_presentation"))


def contraindication_checklist_text(lang: str = "ru") -> str:
    return APPROVED_CONTRAINDICATIONS_KK if lang == "kk" else APPROVED_CONTRAINDICATIONS_RU


_CONTRA_ROOTS = ["кардиостимулятор", "дефибриллятор", "кохлеар", "тромбофлеб", "тромбоз", "онколог", "эпилеп", "судорог", "тиреотокс", "беремен", "жүкт", "құрысу", "орви", "жрви", "грипп", "тұмау"]
_CONTRA_PHRASES = [r"инсулин\w*\s+помп", r"декомпенс\w*.*диабет", r"(?:высок\w*|жоғары)\s+температур", r"(?:остр\w*|жедел)\s+инфекц", r"(?:серьезн\w*|елеулі).*?(?:сверт|ұю)", r"(?:сверт|қан\s+ұю).*?(?:серьезн\w*|елеулі)", r"тяжел\w*.*?(?:серд|дыхан|псих)", r"ауыр.*?(?:жүрек|тыныс|псих)", r"(?:жүрек|тыныс|псих).*?ауыр"]


def contraindication_evidence(text: str) -> str:
    """Explicit presence overrides a boolean; questions/uncertainty never clear."""
    low = _low(text)
    question = False
    uncertain = bool(re.search(r"не знаю|не уверен|возможно|подозрен|білмей|мүмкін|күдік", low))
    for sentence in re.split(r"[,;.!\n]|\b(?:но|а|бірақ)\b", low):
        inherited_absence = False
        for clause in re.split(r"\b(?:и|және)\b", sentence):
            matches = list(_matches(clause, _CONTRA_ROOTS))
            matches += [m for pattern in _CONTRA_PHRASES for m in re.finditer(pattern, clause)]
            if not matches:
                continue
            if re.search(r"что такое|что значит|деген не|дегеніміз", clause):
                question = True
                continue
            if "?" in clause or re.search(r"не знаю|не уверен|возможно|білмей|мүмкін|күдік", clause):
                uncertain = True
                continue
            affirmative = bool(re.search(r"\b(?:есть|имеется|имею|бар)\b", clause))
            if len(matches) > 1 and affirmative and re.search(r"\b(?:нет|жоқ)\b", clause):
                uncertain = True
                continue
            for m in matches:
                before, after = clause[:m.start()], clause[m.end():]
                denied_before = bool(re.search(r"\b(?:нет|без|не)\b", before))
                denied_after = bool(re.search(r"\b(?:нет|жоқ)\b", after))
                denied = denied_before or denied_after or (inherited_absence and not affirmative)
                if not denied:
                    return "present"
                # Leading 'нет A и B' negates the enumeration. A following
                # explicit 'есть B' always terminates that scope.
                inherited_absence = denied_before and not affirmative
    if uncertain:
        return "unclear"
    if question:
        return "question"
    if "?" in low:
        return "unclear"
    if re.search(r"(?:противопоказани\w*\s+(?:(?:у\s+\w+\s+){0,2})?нет|нет\s+противопоказани|ничего\s+из\s+(?:этого|перечисленного)\s+нет|(?:все|всё)\s+(?:это\s+)?отсутствует|қарсы көрсетілім\w*\s+жоқ|аталғандардың\s+ешқайсысы\s+жоқ)", low):
        return "clear"
    if low.strip(" .!") in {"нет", "ничего нет", "жоқ", "жок", "подтверждаю", "растаймын"}:
        return "clear"
    return "none"


def normalize_patient_relation(value: str) -> str:
    low = _low(value).strip()
    groups = {
        "mother": ("mother", "мама", "маму", "мамы", "мать", "матери", "ана", "анам"),
        "father": ("father", "папа", "папу", "папы", "отец", "отца", "әке", "әкем"),
        "wife": ("wife", "жена", "жену", "жены", "әйелім"),
        "husband": ("husband", "муж", "мужа", "мужу", "күйеуім"),
        "daughter": ("daughter", "дочь", "дочку", "дочка", "дочери", "қызым"),
        "son": ("son", "сын", "сына", "сыну", "ұлым"),
        "child": ("child", "ребенок", "ребенка", "бала", "балам"),
        "grandmother": ("grandmother", "бабушка", "бабушку", "әжем"),
        "grandfather": ("grandfather", "дедушка", "дедушку", "атам"),
        "sister": ("sister", "сестра", "сестру", "сестры", "әпкем", "сіңлім", "қарындасым"),
        "brother": ("brother", "брат", "брата", "ағам", "інім"),
        "": ("", "self", "я", "себя", "себе", "сам", "сама", "мен", "өзім"),
    }
    return next((canonical for canonical, aliases in groups.items() if low in aliases), low)

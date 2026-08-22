
from __future__ import annotations

import json
import re
from typing import Any

import crm
import state


SPINE_KEYWORDS = {
    "спина", "поясница", "шея", "позвоночник", "грыжа", "протрузия", "протруз",
    "остеохондроз", "сколиоз", "радикулит", "бел", "мойын", "омыртқа", "жарық",
}
JOINT_KEYWORDS = {
    "сустав", "колено", "плечо", "локоть", "кисть", "тазобедрен", "стопа",
    "артроз", "артрит", "тізе", "иық", "буын", "қол", "аяқ",
}
REHAB_KEYWORDS = {
    "перелом", "травма", "операц", "реабилитац", "восстанов", "сынған", "жарақат",
}
NEURO_KEYWORDS = {
    "онемение", "немеет", "нерв", "головокруж", "мигрень", "невралг", "ұйып",
}


def _low(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _doctor_items(data: Any) -> list[dict[str, Any]]:
    """Понимает несколько возможных форматов CRM.

    Старый CRM мог вернуть:
    - {"doctors": [...]}
    - {"specializations": [...]}
    - {"data": [...]}
    - [...]
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("doctors", "specializations", "items", "data", "result"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    # Иногда сам объект уже похож на врача.
    if data.get("doctorLogin") or data.get("login"):
        return [data]
    return []


def _doctor_login(item: dict[str, Any]) -> str:
    return str(item.get("doctorLogin") or item.get("login") or item.get("doctor_login") or "").strip()


def _doctor_name(item: dict[str, Any]) -> str:
    return str(
        item.get("doctorName")
        or item.get("fullName")
        or item.get("name")
        or item.get("doctor_name")
        or ""
    ).strip()


def _doctor_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "doctorName", "name", "description", "specialization", "speciality",
        "canTreat", "preferredDiagnoses", "cannotTreat",
    ):
        val = item.get(key)
        if isinstance(val, list):
            parts.extend(str(x) for x in val)
        elif val:
            parts.append(str(val))
    return _low(" ".join(parts))


def _category_words(complaint: str) -> set[str]:
    low = _low(complaint)
    words: set[str] = set()
    if any(w in low for w in SPINE_KEYWORDS):
        words |= SPINE_KEYWORDS
    if any(w in low for w in JOINT_KEYWORDS):
        words |= JOINT_KEYWORDS
    if any(w in low for w in REHAB_KEYWORDS):
        words |= REHAB_KEYWORDS
    if any(w in low for w in NEURO_KEYWORDS):
        words |= NEURO_KEYWORDS
    # Добавляем слова самого пациента, чтобы поймать точные canTreat/preferredDiagnoses.
    words |= set(re.findall(r"[a-zA-Zа-яА-ЯәғқңөұүһіӘҒҚҢӨҰҮҺІ]{4,}", low))
    return words


def score_doctor_for_complaint(item: dict[str, Any], complaint: str) -> int:
    text = _doctor_text(item)
    if not text:
        return 0
    score = 0
    for w in _category_words(complaint):
        if w and w in text:
            score += 3 if w in _low(complaint) else 1
    # preferredDiagnoses/canTreat важнее обычного description.
    for key in ("preferredDiagnoses", "canTreat"):
        val = item.get(key)
        joined = _low(" ".join(str(x) for x in val)) if isinstance(val, list) else _low(str(val or ""))
        for w in _category_words(complaint):
            if w and w in joined:
                score += 4
    return score


async def choose_doctor_for_complaint(chat_id: str, complaint: str) -> dict[str, str] | None:
    """Выбирает врача по жалобе, используя CRM как источник правды.

    Если CRM не вернула врачей или совпадение слабое — возвращаем None.
    Тогда бот берёт общее расписание и не теряет пациента.
    """
    complaint = (complaint or "").strip()
    if not complaint:
        return None

    try:
        data = await crm.get_doctors()
    except Exception as exc:
        try:
            state.log_bot_action(
                chat_id,
                "error",
                "doctor router get_doctors failed",
                tool_name="get_doctor_info",
                tool_args={"complaint": complaint},
                tool_result=str(exc)[:1000],
            )
        except Exception:
            pass
        return None

    best_item: dict[str, Any] | None = None
    best_score = 0
    for item in _doctor_items(data):
        login = _doctor_login(item)
        if not login:
            continue
        score = score_doctor_for_complaint(item, complaint)
        if score > best_score:
            best_score = score
            best_item = item

    if not best_item or best_score < 3:
        try:
            state.log_bot_action(
                chat_id,
                "guard_blocked",
                "doctor router no confident match",
                tool_name="get_doctor_info",
                tool_args={"complaint": complaint},
                tool_result=json.dumps(data, ensure_ascii=False)[:3000],
            )
        except Exception:
            pass
        return None

    result = {
        "doctor_login": _doctor_login(best_item),
        "doctor_name": _doctor_name(best_item),
        "score": str(best_score),
    }
    try:
        state.log_bot_action(
            chat_id,
            "tool_call",
            "doctor selected by complaint",
            tool_name="get_doctor_info",
            tool_args={"complaint": complaint},
            tool_result=json.dumps(result, ensure_ascii=False),
        )
    except Exception:
        pass
    return result


_DOCTOR_PREFERENCE_STOP_WORDS = {
    "врач", "врачу", "врача", "доктор", "доктору", "доктора",
    "записать", "запишите", "записаться", "прием", "консультация",
    "к", "ко", "у", "на", "именно", "хочу", "можно", "пожалуйста",
}


def _preference_tokens(text: str) -> list[str]:
    normalized = re.sub(r"[^a-zа-яәғқңөұүһі0-9_]+", " ", _low(text))
    return [
        token
        for token in normalized.split()
        if len(token) >= 2 and token not in _DOCTOR_PREFERENCE_STOP_WORDS
    ]


def _name_token_matches(preference_token: str, crm_name_token: str) -> bool:
    """Match common case endings without guessing outside a CRM name.

    Patients normally write names in the dative/genitive form (for example,
    ``Кайсару``), while CRM stores the nominative form (``Кайсар``).  A short
    prefix extension covers those endings and still requires a unique doctor.
    """

    if preference_token == crm_name_token:
        return True
    shorter, longer = sorted((preference_token, crm_name_token), key=len)
    return len(shorter) >= 4 and len(longer) - len(shorter) <= 3 and longer.startswith(shorter)


def _preference_match_score(item: dict[str, Any], preference: str) -> int:
    """Score an explicit user preference against CRM-owned doctor identity."""

    login = _low(_doctor_login(item)).strip()
    name = _low(_doctor_name(item)).strip()
    raw = _low(preference).strip()
    if not login or not raw:
        return 0
    if raw == login or raw.replace(" ", "_") == login:
        return 100

    pref_tokens = set(_preference_tokens(raw))
    name_tokens = set(_preference_tokens(name))
    if not pref_tokens or not name_tokens:
        return 0
    matched_tokens = {
        pref_token
        for pref_token in pref_tokens
        if any(_name_token_matches(pref_token, name_token) for name_token in name_tokens)
    }
    if matched_tokens == pref_tokens:
        return 90 + min(len(pref_tokens), 5)
    if len(matched_tokens) >= 2:
        return 70 + len(matched_tokens)
    if len(matched_tokens) == 1 and len(next(iter(matched_tokens))) >= 4:
        return 50
    return 0


async def resolve_doctor_preference(chat_id: str, preference: str) -> dict[str, str] | None:
    """Resolve a requested doctor only against the current CRM doctor list.

    A unique, confident match is required. This prevents the GPT reply or a
    hard-coded Python table from inventing a doctor/login that CRM cannot book.
    """

    preference = (preference or "").strip()
    if not preference:
        return None
    try:
        data = await crm.get_doctors()
    except Exception as exc:
        try:
            state.log_bot_action(
                chat_id,
                "error",
                "explicit doctor resolution failed",
                tool_name="get_doctor_info",
                tool_args={"preference": preference},
                tool_result=str(exc)[:1000],
            )
        except Exception:
            pass
        return None

    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in _doctor_items(data):
        if not _doctor_login(item) or not _doctor_name(item):
            continue
        score = _preference_match_score(item, preference)
        if score >= 50:
            candidates.append((score, item))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if not candidates:
        return None
    best_score, best_item = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == best_score:
        return None

    result = {
        "doctor_login": _doctor_login(best_item),
        "doctor_name": _doctor_name(best_item),
        "score": str(best_score),
    }
    try:
        state.log_bot_action(
            chat_id,
            "tool_call",
            "explicit doctor resolved from CRM",
            tool_name="get_doctor_info",
            tool_args={"preference": preference},
            tool_result=json.dumps(result, ensure_ascii=False),
        )
    except Exception:
        pass
    return result

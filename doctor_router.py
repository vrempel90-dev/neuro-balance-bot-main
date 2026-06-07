
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
    return str(item.get("doctorName") or item.get("name") or item.get("doctor_name") or "").strip()


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

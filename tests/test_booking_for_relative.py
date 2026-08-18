"""Запись за родственника: бот понимает, что пациент — не тот, кто пишет.

Пациенты постоянно пишут за другого человека: "хочу узнать за родственника",
"записать маму", "у бабушки болит спина", "спрашиваю за брата", "для отца".
Раньше распознавались только точные формы из RELATION_ALIASES ("мама/мамы/маме",
"жена", "ребенок"), поэтому даже "хочу записать маму" считалось записью на себя.

Последствие было клиническим, а не косметическим: бот собирал возраст и
противопоказания НЕ ТОГО человека, и возрастное правило клиники (до 16 и более
75 лет) применялось к тому, кто пишет, а не к пациенту.

Кроме распознавания по правилам, поле patient_relation добавлено в схему
решения брейна: до этого GPT физически не мог сообщить, что запись за другого —
такое решение отвергалось как schema_extra_extracted и уходило в fallback.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import ai
import crm
import dialog
import state
from dialog import handle_message


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _crm_new_patient(monkeypatch: pytest.MonkeyPatch):
    state.init_db()

    async def fake_lookup(phone):
        return {"ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
                "lastAppointment": None, "hasActiveAppointment": False, "appointment": None,
                "appointments": []}
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def _seed(chat_id: str, extra: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({
        "ai_lead_started": True,
        "gate_reason": "active_ai_lead",
        "first_touch_info_sent": True,
        "step": "complaint",
    })
    if extra:
        session.update(extra)
    state.save_session(chat_id, session)


# --- распознавание живых формулировок --------------------------------------


@pytest.mark.parametrize("text,relation", [
    ("хочу узнать за родственника", "родственник"),
    ("хочу записать маму", "мама"),
    ("спрашиваю за брата", "брат"),
    ("у бабушки болит спина", "бабушка"),
    ("для отца хочу узнать", "папа"),
    ("сестре нужна консультация", "сестра"),
    ("тёща жалуется на колено", "тёща"),
    ("хочу записать свекровь", "свекровь"),
    ("дедушке 70 лет болит поясница", "дедушка"),
    ("интересуюсь для близкого человека", "родственник"),
    ("жене нужно записаться", "жена"),
    ("хочу записать ребенка", "ребенок"),
    ("мужу нужна консультация", "муж"),
    ("сыну 20 лет, болит колено", "сын"),
    ("дочке больно наклоняться", "дочь"),
    ("внуку нужно к врачу", "внук"),
    ("тёте плохо со спиной", "тётя"),
    ("дяде нужна помощь", "дядя"),
])
def test_relative_phrasings_are_recognized(text: str, relation: str) -> None:
    detected, _gender = dialog._detect_patient_relation(text)
    assert detected == relation, f"{text!r}: ожидали {relation!r}, получили {detected!r}"


@pytest.mark.parametrize("text", [
    "болит спина",
    "у меня болит поясница",
    "хочу записаться",
    "здравствуйте",
    "мне 40 лет",
    "хочу брать талон",
    "другой день можно",
    "есть другая дата",
    "материал есть",
    "детально расскажите",
    "сестринский уход",
    "я сама запишусь",
    "грыжа позвоночника",
    "для себя хочу записаться",
    "немеет рука",
])
def test_self_messages_are_not_misread_as_relative(text: str) -> None:
    """Ложное срабатывание увело бы бота спрашивать про несуществующего родственника."""
    detected, _gender = dialog._detect_patient_relation(text)
    assert detected is None, f"{text!r}: ложно распознано как {detected!r}"


# --- бот спрашивает про пациента, а не про пишущего ------------------------


@pytest.mark.parametrize("text,expected_in_answer", [
    ("хочу узнать за родственника, болит спина", "сколько лет родственнику"),
    ("хочу записать маму, у неё болит поясница", "сколько лет маме"),
    ("хочу записать брата, болит спина", "сколько лет брату"),
    ("у бабушки болит спина, хочу записать", "сколько лет бабушке"),
])
def test_bot_asks_age_of_the_relative(text: str, expected_in_answer: str) -> None:
    chat_id = f"rel_ask_age_{abs(hash(text))}"
    _seed(chat_id)

    answer = run(handle_message(chat_id, "77011234567", text))
    session = state.get_session(chat_id)

    assert session["patient_subject"] == "relative"
    assert expected_in_answer in answer.lower(), f"бот спросил не про пациента: {answer!r}"
    assert "сколько вам лет" not in answer.lower()


def test_self_booking_still_asks_about_the_writer() -> None:
    """Обычная запись на себя не изменилась."""
    chat_id = "rel_self_unchanged"
    _seed(chat_id)

    answer = run(handle_message(chat_id, "77011234567", "болит поясница, хочу записаться"))
    session = state.get_session(chat_id)

    assert session.get("patient_relation") in (None, "")
    assert session.get("patient_subject") == "self"
    assert "сколько вам лет" in answer.lower()


# --- возрастное правило считается по пациенту ------------------------------


def test_age_rule_applies_to_the_relative_not_the_writer() -> None:
    """Бабушке 80 — запись останавливается, хотя пишет молодой родственник."""
    chat_id = "rel_age_over75"
    _seed(chat_id)

    run(handle_message(chat_id, "77011234567", "хочу записать бабушку, болит спина"))
    answer = run(handle_message(chat_id, "77011234567", "80 лет"))
    session = state.get_session(chat_id)

    assert session["patient_relation"] == "бабушка"
    assert session["age"] == 80
    assert "старше 75" in answer
    assert session.get("escalated") is True


def test_relative_of_allowed_age_continues_to_contraindications() -> None:
    chat_id = "rel_age_ok"
    _seed(chat_id)

    run(handle_message(chat_id, "77011234567", "хочу записать брата, болит спина"))
    answer = run(handle_message(chat_id, "77011234567", "40 лет"))
    session = state.get_session(chat_id)

    assert session["age"] == 40
    assert session["step"] == "contraindications"
    assert session.get("escalated") is not True
    assert "противопоказан" in answer.lower()


# --- решение брейна ---------------------------------------------------------


def _decision(extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent": "complaint",
        "action": "ask_age",
        "next_step": "age",
        "patient_meaning": "",
        "reply": "Подскажите, сколько лет маме?",
        "extracted": {
            "complaint": "", "age": None, "contraindications_clear": None,
            "contraindication_confirmed": False, "contraindication_term_asked": "",
            "contraindication_red_flags": [], "preferred_date_text": "", "time_preference": "",
            "slot_choice": None, "patient_name": "", "wants_human": False, "faq_type": "",
            "language": "ru", **extracted,
        },
        "needs_python_tool": "none",
        "safety": {"hard_stop": False, "reason": "", "unsafe_medical_claim": False,
                   "tries_to_book_without_rules": False},
        "safety_flags": {"promised_cure": False, "asked_name_too_early": False,
                         "offered_date_before_contra": False, "medical_diagnosis": False},
    }


def test_brain_schema_accepts_patient_relation() -> None:
    """Без этого поля решение GPT отвергалось как schema_extra_extracted."""
    decision, reason = ai._normalize_dialog_brain_decision(_decision({"patient_relation": "мама"}))

    assert reason == "", f"решение отвергнуто: {reason!r}"
    assert decision["extracted"]["patient_relation"] == "мама"


def test_new_schema_maps_patient_relation_to_extracted() -> None:
    """Новая схема брейна (entities) пробрасывает поле в старую (extracted)."""
    raw = {
        "understood_context": {"patient_meaning": "запись для мамы"},
        "intent": "complaint",
        "entities": {"complaint": "спина", "patient_relation": "мама"},
        "next_required_step": "age",
        "needs_python_tool": "none",
        "reply": "Подскажите, сколько лет маме?",
        "safety": {"hard_stop": False},
    }
    decision, reason = ai._normalize_dialog_brain_decision(raw)

    assert reason == ""
    assert decision["extracted"]["patient_relation"] == "мама"


def test_brain_patient_relation_lands_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Решение брейна переключает бота на вопросы про родственника."""
    chat_id = "rel_from_brain"
    _seed(chat_id)

    async def fake_brain(**kwargs: Any):
        decision = _decision({"patient_relation": "маме", "complaint": "болит спина"})
        debug = {"openai_brain_used": True, "openai_brain_action": "ask_age",
                 "openai_brain_needs_python_tool": "none",
                 "openai_brain_extracted": decision["extracted"]}
        return decision, debug

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(handle_message(chat_id, "77011234567", "здравствуйте, спина беспокоит"))
    session = state.get_session(chat_id)

    assert session["patient_relation"] == "мама"
    assert session["patient_subject"] == "relative"
    assert "сколько лет маме" in answer.lower()


def test_prompt_instructs_brain_about_relatives() -> None:
    """Правило должно быть в промпте, иначе GPT не заполнит поле."""
    prompt = ai.OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT

    assert "patient_relation" in prompt
    assert "за родственника" in prompt
    assert "не для того, кто пишет" in prompt


# --- падежи -----------------------------------------------------------------


@pytest.mark.parametrize("relation,dative,genitive", [
    ("мама", "маме", "мамы"),
    ("бабушка", "бабушке", "бабушки"),
    ("брат", "брату", "брата"),
    ("родственник", "родственнику", "родственника"),
    ("сестра", "сестре", "сестры"),
])
def test_relation_cases_are_correct(relation: str, dative: str, genitive: str) -> None:
    session = {"patient_relation": relation, "patient_subject": "relative"}
    assert dialog._patient_dative(session) == dative
    assert dialog._patient_genitive(session) == genitive


def test_unknown_relation_degrades_to_neutral_wording() -> None:
    session = {"patient_relation": "кум", "patient_subject": "relative"}
    assert dialog._patient_dative(session) == "пациенту"
    assert dialog._patient_genitive(session) == "пациента"

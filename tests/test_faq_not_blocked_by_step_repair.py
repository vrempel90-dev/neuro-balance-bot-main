"""Фаза 3: боковой вопрос не блокируется за "не тот шаг".

_repair_forbidden_required_question блокирует ответ, тип вопроса которого не
совпадает с текущим required-step, и подменяет его заготовкой. Экшен
answer_faq_and_continue спроектирован правильно (ответить по факту + вернуть на
нужный шаг), но подпадал под эту блокировку: _classify_bot_question смотрит ВЕСЬ
текст, поэтому FAQ про цену/МРТ/противопоказания определялся как "повторный
вопрос шага", и фактический ответ выбрасывался целиком.

Критерий готово: пациент задаёт боковой вопрос в середине воронки — получает
содержательный ответ по факту и возврат к нужному шагу в ОДНОМ сообщении, без
потери прогресса и без эскалации на оператора после 1-2 таких вопросов.
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
                "lastAppointment": None, "hasActiveAppointment": False, "appointment": None, "appointments": []}
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def _seed(chat_id: str, preset: dict[str, Any]) -> dict[str, Any]:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead", "first_touch_info_sent": True})
    session.update(preset)
    state.save_session(chat_id, session)
    return state.get_session(chat_id)


def _brain(action: str, reply: str, extracted: dict[str, Any] | None = None, tool: str = "none"):
    decision = {
        "action": action,
        "reply": reply,
        "extracted": {
            "complaint": "", "age": None, "contraindications_clear": None,
            "contraindication_red_flags": [], "preferred_date_text": "", "time_preference": "",
            "slot_choice": None, "patient_name": "", "faq_type": "", "language": "ru",
            **(extracted or {}),
        },
        "needs_python_tool": tool,
        "safety_flags": {"promised_cure": False, "asked_name_too_early": False,
                         "offered_date_before_contra": False, "medical_diagnosis": False},
    }
    debug = {"openai_brain_used": True, "openai_brain_action": action,
             "openai_brain_needs_python_tool": tool, "openai_brain_extracted": decision["extracted"]}
    return decision, debug


# --- фактический ответ переживает починку шага -----------------------------


@pytest.mark.parametrize(
    "label,preset,answer,fact",
    [
        (
            "FAQ про противопоказания + вопрос о дате",
            {"step": "date", "age": 40, "contraindications_ok": True, "complaint": "спина", "preferred_date": "2026-08-20"},
            "В список противопоказаний входит эпилепсия 🌿\n\nНа какой день Вам удобно прийти?",
            "В список противопоказаний входит эпилепсия 🌿",
        ),
        (
            "FAQ про цену + вопрос о дате",
            {"step": "date", "age": 40, "contraindications_ok": True, "complaint": "спина", "preferred_date": "2026-08-20"},
            "Приём — 5 000 тг 🌿\n\nНа какой день Вам удобно прийти?",
            "Приём — 5 000 тг 🌿",
        ),
        (
            "FAQ про МРТ + вопрос о возрасте",
            {"step": "age", "age": 40, "complaint": "спина"},
            "МРТ заранее делать не обязательно 🌿\n\nПодскажите, сколько Вам лет?",
            "МРТ заранее делать не обязательно 🌿",
        ),
    ],
)
def test_faq_fact_survives_step_repair(label: str, preset: dict[str, Any], answer: str, fact: str) -> None:
    chat_id = f"p3_fact_{abs(hash(label))}"
    session = _seed(chat_id, preset)
    session["faq_fact_reply"] = fact

    repaired = dialog._repair_forbidden_required_question(chat_id, session, answer)

    assert fact in repaired, f"{label}: ответ по факту выброшен при починке шага"
    assert repaired.strip(), f"{label}: ответ стал пустым"


def test_step_repair_still_corrects_the_step() -> None:
    """Починка шага сохранена — меняется только то, что факт больше не теряется."""
    chat_id = "p3_step_still_repaired"
    session = _seed(chat_id, {"step": "age", "age": 40, "complaint": "спина"})
    session["faq_fact_reply"] = "МРТ заранее делать не обязательно 🌿"

    dialog._repair_forbidden_required_question(
        chat_id, session, "МРТ заранее делать не обязательно 🌿\n\nПодскажите, сколько Вам лет?"
    )

    assert session["llm_blocked"] is True
    assert session["repair_reason"] == "repeat_required_step_age_blocked"
    assert session["step"] != "age", "шаг должен был продвинуться вперёд"


def test_without_faq_fact_old_behavior_is_unchanged() -> None:
    """Обычный (не FAQ) повторный вопрос блокируется ровно как раньше."""
    chat_id = "p3_no_fact_unchanged"
    session = _seed(chat_id, {"step": "age", "age": 40, "complaint": "спина"})

    repaired = dialog._repair_forbidden_required_question(
        chat_id, session, "Подскажите, сколько Вам лет?"
    )

    assert "сколько Вам лет" not in repaired
    assert session["llm_blocked"] is True


# --- эскалация после 1-2 боковых вопросов ----------------------------------


def test_no_escalation_after_side_questions_with_facts() -> None:
    """Боковые вопросы с ответом по факту — не зацикливание, оператор не нужен."""
    chat_id = "p3_no_escalation"
    session = _seed(chat_id, {
        "step": "date", "age": 40, "contraindications_ok": True, "complaint": "спина",
        "last_required_step": "date", "last_required_question_count": 3,
    })
    session["faq_fact_reply"] = "Приём — 5 000 тг 🌿"

    repaired = dialog._repair_forbidden_required_question(
        chat_id, session, "Приём — 5 000 тг 🌿\n\nНа какой день Вам удобно прийти?"
    )

    assert session.get("escalated") is not True, "эскалация после боковых вопросов с фактами"
    assert "Приём — 5 000 тг" in repaired
    assert "администратору" not in repaired


def test_escalation_still_fires_on_real_repeat_loop() -> None:
    """Защита от зацикливания сохранена: без ответа по факту эскалация работает."""
    chat_id = "p3_escalation_kept"
    session = _seed(chat_id, {
        "step": "date", "age": 40, "contraindications_ok": True, "complaint": "спина",
        "last_required_step": "date", "last_required_question_count": 3,
    })

    repaired = dialog._repair_forbidden_required_question(
        chat_id, session, "На какой день Вам удобно прийти?"
    )

    assert session["escalated"] is True
    assert "администратору" in repaired


# --- сквозной сценарий через handle_message --------------------------------


def test_side_questions_midfunnel_keep_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Критерий готово: факт + возврат к шагу в одном сообщении, прогресс не теряется."""
    chat_id = "p3_end_to_end"
    _seed(chat_id, {"step": "date", "complaint": "болит спина", "age": 40, "contraindications_ok": True})

    facts = {
        "сколько стоит приём": "Первичный приём — 5 000 тг.",
        "а мрт заранее нужно": "Снимки и МРТ заранее делать не обязательно.",
    }

    for question, fact in facts.items():
        async def fake_brain(**kwargs: Any):
            return _brain("answer_faq_and_continue", fact, {"faq_type": "price"})
        monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

        answer = run(handle_message(chat_id, "77011234567", question))
        session = state.get_session(chat_id)

        assert answer.strip(), f"пустой ответ на боковой вопрос {question!r}"
        assert session.get("escalated") is not True, f"эскалация после вопроса {question!r}"
        # Прогресс воронки не потерян.
        assert session.get("age") == 40
        assert session.get("contraindications_ok") is True
        assert session.get("complaint")


def test_faq_fact_does_not_leak_between_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """faq_fact_reply живёт только текущий ход: следующее сообщение начинает с чистого поля.

    Иначе факт из прошлого ответа приклеился бы к чужой починке шага.
    """
    chat_id = "p3_no_leak"
    _seed(chat_id, {
        "step": "date", "complaint": "спина", "age": 40, "contraindications_ok": True,
        "faq_fact_reply": "Первичный приём — 5 000 тг.",
    })

    async def date_brain(**kwargs: Any):
        return _brain("ask_date", "На какой день Вам удобно?")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", date_brain)

    answer = run(handle_message(chat_id, "77011234567", "завтра"))

    assert not state.get_session(chat_id).get("faq_fact_reply"), "факт протёк на следующий ход"
    assert "5 000 тг" not in answer, "факт прошлого хода приклеился к новому ответу"


def test_validate_openai_dialog_decision_still_blocks_invented_slot() -> None:
    """Защита от выдумывания фактов не ослаблена."""
    session = {"step": "date", "last_slots": [], "complaint": "спина", "age": 40, "contraindications_ok": True}
    decision, _ = _brain("select_slot", "Записала Вас на 10:00", {"slot_choice": 1})
    ok, reason = dialog.validate_openai_dialog_decision(decision, session, "давайте 10:00")
    assert ok is False
    assert reason

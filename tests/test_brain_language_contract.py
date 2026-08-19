"""Регрессия: языковой контракт между брейном и Python.

Находки аудита:

- ``build_dialog_context`` не передавал брейну язык диалога вообще, поэтому
  модель не могла ни удержать язык, ни заметить переход пациента;
- ``extracted["language"]`` парсился и валидировался, но нигде не читался —
  мнение модели о языке просто выбрасывалось.

Контракт: модель ПОНИМАЕТ язык, Python РЕШАЕТ. Подсказка модели применяется
только там, где Python сигнала не нашёл вообще.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ai  # noqa: E402
import dialog  # noqa: E402


# ------------------------------------------------------------ контекст


def test_dialog_context_carries_the_dialog_language() -> None:
    ctx = ai.build_dialog_context(user_text="Белім ауырады", session={"language": "kk", "step": "age"})
    assert ctx["session_state"]["language"] == "kk"
    assert ctx["session_state"]["language_locked"] is False


def test_dialog_context_carries_python_language_analysis() -> None:
    ctx = ai.build_dialog_context(
        user_text="Белім ауырады, врач сегодня принимает?",
        session={"language": "ru", "step": "age"},
    )
    analysis = ctx["message_language"]
    assert analysis["detected_language"] == "mixed"
    assert analysis["preferred_response_language"] in ("ru", "kk")
    assert 0.0 <= analysis["confidence"] <= 1.0


def test_dialog_context_survives_empty_message() -> None:
    ctx = ai.build_dialog_context(user_text="", session={"language": "ru"})
    assert ctx["message_language"]["detected_language"] == "unknown"


# ---------------------------------------------------- схема и валидация


NEW_SCHEMA: dict[str, Any] = {
    "understood_context": {"patient_meaning": "жалоба", "is_answer_to_last_question": True},
    "intent": "complaint",
    "entities": {
        "complaint": "белім ауырады",
        "detected_language": "kk",
        "preferred_response_language": "kk",
        "language_confidence": 0.91,
    },
    "next_required_step": "age",
    "needs_python_tool": "none",
    "reply": "Түсінемін. Жасыңыз нешеде?",
    "safety": {"hard_stop": False},
}

OLD_SCHEMA: dict[str, Any] = {
    "intent": "complaint",
    "patient_meaning": "жалоба",
    "reply": "Түсінемін. Жасыңыз нешеде?",
    "next_step": "age",
    "extracted": {
        "complaint": "белім ауырады",
        "detected_language": "kk",
        "preferred_response_language": "kk",
        "language_confidence": 0.9,
    },
    "needs_python_tool": "none",
    "safety": {"hard_stop": False},
}


@pytest.mark.parametrize("raw", [NEW_SCHEMA, OLD_SCHEMA], ids=["new_schema", "old_schema"])
def test_language_fields_are_accepted_in_both_schemas(raw: dict[str, Any]) -> None:
    decision, error = ai._normalize_dialog_brain_decision(raw)
    assert error == ""
    assert decision["extracted"]["detected_language"] == "kk"
    assert decision["extracted"]["preferred_response_language"] == "kk"
    assert decision["extracted"]["language_confidence"] == pytest.approx(0.9, abs=0.02)


def test_decisions_without_language_fields_remain_valid() -> None:
    """Обратная совместимость: старые ответы модели не должны отклоняться."""
    raw = {
        "intent": "complaint", "patient_meaning": "x", "reply": "ок", "next_step": "age",
        "extracted": {"complaint": "спина"}, "needs_python_tool": "none", "safety": {},
    }
    decision, error = ai._normalize_dialog_brain_decision(raw)
    assert error == ""
    assert decision["extracted"]["detected_language"] == ""


def test_garbage_language_values_are_dropped_not_trusted() -> None:
    raw = dict(OLD_SCHEMA)
    raw["extracted"] = dict(
        OLD_SCHEMA["extracted"],
        detected_language="klingon",
        preferred_response_language="en",
        language_confidence="очень уверен",
    )
    decision, error = ai._normalize_dialog_brain_decision(raw)
    assert error == ""
    assert decision["extracted"]["detected_language"] == ""
    assert decision["extracted"]["preferred_response_language"] == ""
    assert decision["extracted"]["language_confidence"] == 0.0


@pytest.mark.parametrize("value, expected", [(1.7, 1.0), (-3, 0.0), (None, 0.0), ("0.5", 0.5)])
def test_confidence_is_clamped(value: Any, expected: float) -> None:
    assert ai._valid_confidence(value) == expected


def test_brain_prompt_documents_the_language_contract() -> None:
    prompt = ai.OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT
    assert "detected_language" in prompt
    assert "preferred_response_language" in prompt
    assert "language_confidence" in prompt


# -------------------------------------------- Python решает, модель подсказывает


CONFIDENT_KK = {"detected_language": "kk", "preferred_response_language": "kk", "language_confidence": 0.9}


def apply(session: dict[str, Any], extracted: dict[str, Any], text: str) -> dict[str, Any]:
    session = dict(session)
    dialog._apply_brain_language_hint(session, extracted, text)
    return session


def test_hint_is_applied_only_when_python_found_no_signal() -> None:
    session = apply({"language": "ru"}, CONFIDENT_KK, "44")
    assert session["language"] == "kk"
    assert session["brain_language_hint_applied"] is True


@pytest.mark.parametrize("text", ["Болит спина, помогите", "Белім ауырады", "сколько стоит приём"])
def test_python_language_decision_beats_the_model(text: str) -> None:
    session = apply({"language": "ru"}, CONFIDENT_KK, text)
    assert session["language"] == "ru"
    assert session["brain_language_hint_applied"] is False


def test_low_confidence_hint_is_ignored() -> None:
    weak = dict(CONFIDENT_KK, language_confidence=0.4)
    session = apply({"language": "ru"}, weak, "44")
    assert session["language"] == "ru"
    assert session["brain_language_hint_applied"] is False


def test_explicitly_locked_language_is_never_overridden_by_the_model() -> None:
    session = apply({"language": "ru", "language_locked": True}, CONFIDENT_KK, "44")
    assert session["language"] == "ru"
    assert session["brain_language_hint_applied"] is False


def test_hint_is_always_recorded_for_observability() -> None:
    session = apply({"language": "ru"}, CONFIDENT_KK, "Болит спина, помогите")
    assert session["brain_detected_language"] == "kk"
    assert session["brain_preferred_response_language"] == "kk"
    assert session["brain_language_confidence"] == 0.9

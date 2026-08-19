from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dialog


def silence_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dialog, "_safe_log", lambda *args, **kwargs: None)


def base_session(language: str = "ru") -> dict:
    return {
        "language": language,
        "step": "age",
        "complaint": "test",
        "age": 0,
        "last_required_step": "age",
        "last_required_question_count": 2,
    }


def test_repeat_limit_escalates_instead_of_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = base_session()
    answer = dialog._repair_forbidden_required_question("loop-ru", session, "Сколько Вам лет?")
    assert session["step"] == "escalated"
    assert session["escalated"] is True
    assert session["repair_reason"] == "repeat_required_step_age_limit"
    assert "администратор" in answer.lower()


def test_repeat_limit_uses_kazakh_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = base_session("kk")
    answer = dialog._repair_forbidden_required_question("loop-kk", session, "Жасыңыз қаншада?")
    assert session["step"] == "escalated"
    assert session["repair_reason"] == "repeat_required_step_age_limit"
    assert "әкімші" in answer.lower()


def test_faq_fact_bypasses_repeat_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = base_session()
    session["last_required_question_count"] = 7
    session["faq_fact_reply"] = "Факт из утверждённого шаблона."
    original = "Факт из утверждённого шаблона.\n\nСколько Вам лет?"
    answer = dialog._repair_forbidden_required_question("loop-faq", session, original)
    assert answer == original
    assert session.get("escalated") is not True


def test_collected_data_forces_forward_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = base_session()
    session["age"] = 35
    session["contraindications_ok"] = None
    answer = dialog._repair_forbidden_required_question("loop-progress", session, "Сколько Вам лет?")
    assert session["step"] == "contraindications"
    assert session["repair_reason"] == "repeat_required_step_age_blocked"
    assert session.get("escalated") is not True
    assert answer


def test_one_repeat_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = base_session()
    session["last_required_question_count"] = 1
    original = "Сколько Вам лет?"
    assert dialog._repair_forbidden_required_question("loop-one", session, original) == original
    assert session.get("escalated") is not True


def test_successful_gate_resets_repeat_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_logs(monkeypatch)
    session = {
        "language": "ru",
        "step": "contraindications",
        "contraindications_ok": True,
        "last_required_step": "contraindications",
        "last_required_question": "question",
        "last_required_question_count": 9,
        "pending_step_after_faq": "contraindications",
    }
    dialog._cleanup_contraindications_after_ok(session)
    assert session["step"] == "date"
    assert session["last_required_step"] == ""
    assert session["last_required_question_count"] == 0
    assert session["pending_step_after_faq"] == ""

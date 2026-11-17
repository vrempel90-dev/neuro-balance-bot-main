from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import agent
import bot_tools
import medical_rules as rules


def ready():
    return {"age": 35, "complaint": "Болит поясница", "profile_status": "profile",
            "complaint_gate": bot_tools.COMPLAINT_OK, "patient_context_version": 1,
            "last_user_text": "Нет", "contraindications_ok": False}


def confirm(session, text="Ничего из перечисленного нет"):
    session["last_user_text"] = text
    return agent._tool_record_patient_facts("synthetic-medical", session,
        {"contraindications_clear": True, "contraindications_note": text})


def test_bare_no_cannot_clear_without_previously_presented_checklist():
    session = ready()
    confirm(session, "Нет")
    assert session.get("contraindications_ok") is not True


@pytest.mark.parametrize("language,text", [("ru", "Ничего из перечисленного нет"),
                                           ("kk", "Аталғандардың ешқайсысы жоқ")])
def test_accepted_full_checklist_then_clearance_allows_gate(language, text):
    session = ready()
    assert rules.mark_checklist_presented(session, rules.contraindication_checklist_text(language))
    confirm(session, text)
    assert rules.clearance_is_current(session)
    assert agent._medical_gate_error(session) == ""


def test_shortened_checklist_is_not_presentation():
    session = ready()
    assert not rules.mark_checklist_presented(session, "Есть ли беременность или эпилепсия?")
    confirm(session)
    assert not rules.clearance_is_current(session)


def test_patient_switch_invalidates_presentation_and_clearance():
    session = ready()
    rules.mark_checklist_presented(session, rules.APPROVED_CONTRAINDICATIONS_RU)
    confirm(session)
    agent._reset_patient_context(session)
    session.update(age=62, complaint="Болит спина", profile_status="profile", complaint_gate=bot_tools.COMPLAINT_OK)
    confirm(session)
    assert not rules.clearance_is_current(session)
    assert session.get("contraindications_ok") is not True


@pytest.mark.parametrize("kind", ["missing", "removed", "additional"])
def test_invalid_source_blocks_previously_cleared_session(tmp_path, monkeypatch, kind):
    session = ready()
    rules.mark_checklist_presented(session, rules.APPROVED_CONTRAINDICATIONS_RU)
    confirm(session)
    source = Path(agent.__file__).with_name("SYSTEM_PROMPT_rendered.md").read_text(encoding="utf-8")
    path = tmp_path / "rules.md"
    if kind == "removed":
        path.write_text(source.replace("— беременность;", ""), encoding="utf-8")
    elif kind == "additional":
        path.write_text(source.replace("— беременность;", "— беременность;\n— лишний пункт;"), encoding="utf-8")
    monkeypatch.setattr(rules, "APPROVED_PROMPT_PATH", path)
    assert not rules.clearance_is_current(session)
    assert agent._medical_gate_error(session) == "medical_rules_unavailable"
    result = asyncio.run(agent._tool_get_available_slots("synthetic-medical", session, {"date_from": "2031-03-07"}))
    assert result["error"] == "medical_rules_unavailable"
    assert result["slots"] == []


def test_model_note_cannot_override_original_question():
    session = ready()
    rules.mark_checklist_presented(session, rules.APPROVED_CONTRAINDICATIONS_RU)
    session["last_user_text"] = "Что такое кардиостимулятор?"
    agent._tool_record_patient_facts("synthetic-medical", session,
        {"contraindications_clear": True, "contraindications_note": "Ничего из перечисленного нет"})
    assert not rules.clearance_is_current(session)


def test_new_positive_evidence_revokes_prior_clearance():
    session = ready()
    rules.mark_checklist_presented(session, rules.APPROVED_CONTRAINDICATIONS_RU)
    confirm(session)
    confirm(session, "Есть кардиостимулятор")
    assert not rules.clearance_is_current(session)


def test_legacy_boolean_cannot_bypass_new_evidence_gate():
    session = ready()
    session.update(contraindications_ok=True, contraindications_verdict=bot_tools.CONTRA_PROCEED)
    assert agent._medical_gate_error(session) == "contraindications_checklist_not_presented"


@pytest.mark.parametrize("text", ["Противопоказаний нет?", "Ничего из перечисленного нет?"])
def test_question_cannot_clear_medical_rules(text):
    session = ready()
    rules.mark_checklist_presented(session, rules.APPROVED_CONTRAINDICATIONS_RU)
    confirm(session, text)
    assert not rules.clearance_is_current(session)

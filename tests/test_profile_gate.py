"""Regression tests for strict clinic profile gating and age boundaries."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent
import ai
import services


def test_isolated_heel_complaint_is_non_profile_and_age_is_not_stored() -> None:
    session: dict = {}

    result = agent._tool_record_patient_facts(
        "profile_heel",
        session,
        {"complaint": "Болит пятка уже несколько месяцев", "age": 70},
    )

    assert result["profile_status"] == "non_profile"
    assert session["profile_status"] == "non_profile"
    assert session["complaint_gate"] == "NON_PROFILE"
    assert "age" not in session
    assert result["age_known"] is False
    assert "не спрашивай возраст" in result["message"]


def test_unknown_complaint_does_not_become_profile_automatically() -> None:
    session: dict = {}

    result = agent._tool_record_patient_facts(
        "profile_unknown",
        session,
        {"complaint": "Уже месяц непонятно тянет кисть", "age": 44},
    )

    assert result["profile_status"] == "unclear"
    assert session["complaint_gate"] == "PROFILE_UNCLEAR"
    assert "age" not in session
    assert "уточняющий вопрос" in result["message"]


def test_joint_complaint_is_profile_and_can_advance_to_age() -> None:
    session: dict = {}

    result = agent._tool_record_patient_facts(
        "profile_joint",
        session,
        {"complaint": "Болит коленный сустав", "age": 70},
    )

    assert result["profile_status"] == "profile"
    assert session["complaint_gate"] == "COMPLAINT_OK"
    assert session["age"] == 70


def test_heel_plus_profile_joint_keeps_profile_complaint() -> None:
    classified = services.classify_by_keywords("Болит пятка и колено")

    assert classified["can_help"] is True
    assert classified["service"] == "суставы"


def test_age_75_is_allowed_by_backend_boundary() -> None:
    session: dict = {}

    result = agent._tool_record_patient_facts(
        "age_75",
        session,
        {"complaint": "Болит колено", "age": 75},
    )

    assert session["age"] == 75
    assert result["age_outside_clinic_limits"] is False
    assert agent._age_block_reason(75) == ""


def test_age_76_is_blocked_by_backend_boundary() -> None:
    session: dict = {}

    result = agent._tool_record_patient_facts(
        "age_76",
        session,
        {"complaint": "Болит колено", "age": 76},
    )

    assert session["age"] == 76
    assert result["age_outside_clinic_limits"] is True
    assert agent._age_block_reason(76) == "over_max_age"


def test_model_cannot_ask_age_after_heel_was_marked_non_profile() -> None:
    session = {"language": "ru", "complaint": "Болит пятка"}
    result = agent.AgentResult(tool_results=[{"profile_status": "non_profile"}])

    reply = agent._enforce_profile_gate_reply(
        session,
        result,
        "Понял. Подскажите, пожалуйста, сколько Вам лет?",
    )

    assert "сколько вам лет" not in reply.lower()
    assert "пятк" in reply.lower()
    assert "ортопед" in reply.lower()


def test_classifier_unknown_stays_unknown() -> None:
    classified = asyncio.run(ai.classify_complaint("Тянет кисть непонятно где"))

    assert classified["can_help"] is None


def test_canonical_prompt_allows_75_and_blocks_from_76() -> None:
    prompt = ai._rendered_system_prompt()

    assert "76 лет и старше — автоматическую запись не делай" in prompt
    assert "75 лет и старше — автоматическую запись не делай" not in prompt
    assert "проверка профиля клиники" in prompt
    assert "боль в пятке" in prompt

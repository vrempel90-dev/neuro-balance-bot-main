"""Patient/relative identity isolation and privacy contract.

No real OpenAI or CRM calls are made by these tests.
"""
from __future__ import annotations

import asyncio

import pytest

import agent
import patient_subject


@pytest.mark.parametrize(("message", "expected"), [
    ("Хочу записать маму на прием", "мама"),
    ("У мужа болит поясница", "муж"),
    ("Маме 64 года", "мама"),
    ("Маму зовут Айгуль, ей 52", "мама"),
    ("Для бабушки нужна консультация", "бабушка"),
    ("анамды жаздырғым келеді", "мама"),
    ("Хочу записаться сама", "self"),
    ("У меня болит спина", "self"),
    ("Мама сказала, что у меня болит спина", None),
])
def test_explicit_subject_is_conservative(message: str, expected: str | None) -> None:
    assert patient_subject.explicit_subject(message) == expected


def test_sender_name_is_not_relative_patient_name() -> None:
    assert patient_subject.explicit_sender_name("Меня зовут Виктор, хочу записать маму") == "Виктор"
    assert patient_subject.explicit_sender_name("Маму зовут Айгуль, ей 52") == ""


def test_switching_relative_invalidates_every_patient_specific_gate() -> None:
    session = {
        "phone": "77010000000",
        "ai_lead_started": True,
        "patient_relation": "мама",
        "patient_subject": "relative",
        "patient_identity_status": "verified_new",
        "sender_name": "Виктор",
        "patient_name": "Айгуль",
        "complaint": "болит поясница",
        "age": 52,
        "complaint_gate": "COMPLAINT_OK",
        "profile_status": "profile",
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "crm_offered_slots": {"one": {"date": "2026-12-01"}},
        "selected_time": "14:00",
        "tool_history": [{"name": "verify_contraindications_check", "verdict": "proceed"}],
        "known_user_facts": {"age": 52, "complaint": "болит поясница", "patient_name": "Айгуль"},
    }
    assert patient_subject.set_subject(session, "папа") is True
    assert session["patient_relation"] == "папа"
    assert session["patient_identity_status"] == "unverified"
    assert session["phone"] == "77010000000"
    assert session["sender_name"] == "Виктор"
    assert session["ai_lead_started"] is True
    for key in ("age", "complaint", "patient_name", "contraindications_ok",
                "contraindications_verdict", "selected_time", "crm_offered_slots", "tool_history"):
        assert not session.get(key), key
    assert "age" not in session["known_user_facts"]
    assert patient_subject.set_subject(session, "папа") is False


def test_tool_capture_does_not_copy_sender_as_relative_name() -> None:
    session = {"sender_name": "Виктор", "patient_relation": "мама", "patient_subject": "relative"}
    agent._tool_record_patient_facts(
        "identity_test", session, {"patient_relation": "мама", "patient_name": "Виктор"}
    )
    assert not session.get("patient_name")


def test_relative_identity_unknown_cannot_post_booking() -> None:
    session = {
        "patient_relation": "мама",
        "patient_subject": "relative",
        "patient_identity_status": "unverified",
        "sender_name": "Виктор",
    }
    result = asyncio.run(agent._tool_book_appointment(
        "identity_test", session, "77010000000",
        {"patient_name": "Айгуль", "patient_relation": "мама",
         "doctor_login": "doctor", "date": "2026-12-01", "time_start": "10:00"},
    ))
    assert result["booking_success"] is False
    assert result["error"] == "relative_identity_unverified"


def test_relative_cannot_lookup_sender_appointments_as_theirs() -> None:
    session = {"patient_relation": "мама", "patient_subject": "relative"}
    result = asyncio.run(agent._tool_find_my_appointment(
        "identity_test", session, "77010000000", {}
    ))
    assert result["found"] is False
    assert result["appointments"] == []
    assert result["error"] == "relative_identity_unverified"


def test_clinic_age_boundary_matches_approved_source() -> None:
    assert agent._age_block_reason(15) == "under_min_age"
    assert agent._age_block_reason(16) == ""
    assert agent._age_block_reason(74) == ""
    assert agent._age_block_reason(75) == "over_max_age"

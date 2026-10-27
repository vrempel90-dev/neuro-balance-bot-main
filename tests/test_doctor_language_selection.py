"""Booking must honor a real CRM clinician and reply in the patient's language."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import agent
import dialog
import pytest


async def fake_slots(date: str, doctor_login: str | None = None) -> dict:
    availability = [
        {"doctorLogin": "bekbolat", "doctorName": "Бекболат Шуринбайулы",
         "date": date, "availableSlots": ["14:40", "15:20", "16:00"]},
        {"doctorLogin": "kaisar_k", "doctorName": "Куанышулы Кайсар Куанышулы",
         "date": date, "availableSlots": ["14:40", "15:20"]},
        {"doctorLogin": "reserve", "doctorName": "Резерв",
         "date": date, "availableSlots": ["15:20"]},
    ]
    if doctor_login:
        availability = [d for d in availability if d["doctorLogin"] == doctor_login]
    return {"availability": availability}


def test_doctor_choice_required_when_patient_has_not_selected(monkeypatch):
    monkeypatch.setattr(agent.crm, "check_slots", fake_slots)
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    session = {"last_user_text": "В пятницу после обеда"}
    result = asyncio.run(agent._tool_get_available_slots("synthetic", session, {
        "date_from": "2026-10-30", "time_preference": "после обеда",
        "doctor_login": "bekbolat",  # Model cannot choose on patient's behalf.
    }))
    assert result["doctor_choice_required"] is True
    assert result["slots"] == []
    assert {d["doctor_login"] for d in result["doctor_options"]} == {"bekbolat", "kaisar_k"}
    assert not session.get("selected_doctor_login")
    assert session["step"] == "doctor"


def test_selected_doctor_and_time_are_persisted(monkeypatch):
    monkeypatch.setattr(agent.crm, "check_slots", fake_slots)
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    session = {"last_user_text": "В пятницу после обеда"}
    doctor_options = asyncio.run(agent._tool_get_available_slots(
        "synthetic", session, {"date_from": "2026-10-30", "time_preference": "после обеда"}))
    assert doctor_options["doctor_choice_required"] is True
    assert agent._resolve_explicit_doctor_choice(session, "К Бекболату")["doctor_login"] == "bekbolat"

    doctor_slots = asyncio.run(agent._tool_get_available_slots(
        "synthetic", session, {"date_from": "2026-10-30", "time_preference": "после обеда"}))
    assert {s["doctor_login"] for s in doctor_slots["slots"]} == {"bekbolat"}
    agent._remember_presented_slots(
        session, "У Бекболата Шуринбайулы 30 октября есть 14:40, 15:20 и 16:00."
    )
    chosen = agent._resolve_explicit_slot_choice(session, "Давайте на 15:20")
    assert chosen["doctor_login"] == "bekbolat"
    assert session["selected_doctor_login"] == "bekbolat"
    assert session["selected_date"] == "2026-10-30"
    assert session["selected_time"] == "15:20"
    assert session["step"] == "name"


def test_same_time_different_doctors_is_not_automatically_chosen():
    session = {}
    slots = [
        {"doctor_login": "bekbolat", "doctor_name": "Бекболат Шуринбайулы",
         "date": "2026-10-30", "time_start": "15:20"},
        {"doctor_login": "kaisar_k", "doctor_name": "Куанышулы Кайсар Куанышулы",
         "date": "2026-10-30", "time_start": "15:20"},
    ]
    agent._remember_offered_slots(session, slots)
    session["last_slots"] = slots
    assert agent._resolve_explicit_slot_choice(session, "15:20") is None
    assert not session.get("selected_doctor_login")


def test_changing_doctor_clears_old_selected_slot():
    session = {
        "doctor_options": [
            {"doctor_login": "bekbolat", "doctor_name": "Бекболат Шуринбайулы"},
            {"doctor_login": "kaisar_k", "doctor_name": "Куанышулы Кайсар Куанышулы"},
        ],
        "preferred_doctor_login": "bekbolat",
        "selected_doctor_login": "bekbolat",
        "selected_date": "2026-10-30",
        "selected_time": "15:20",
        "last_presented_slots": [{"doctor_login": "bekbolat"}],
    }
    picked = agent._resolve_explicit_doctor_choice(session, "Выбираю врача Кайсара")
    assert picked["doctor_login"] == "kaisar_k"
    assert session.get("selected_time") is None
    assert session["last_presented_slots"] == []


@pytest.mark.parametrize("sentence, expected", [
    ("Здравствуйте, хочу записать маму на приём", "ru"),
    ("Сәлеметсіз бе, анамды дәрігерге жазғым келеді", "kk"),
    ("Анамның белі ауырып жүр, қабылдауға жазылғым келеді", "kk"),
    ("У мамы уже месяц болит поясница, ей 62 года", "ru"),
])
def test_first_message_language(sentence, expected):
    assert dialog._detect_lang(sentence, {}) == expected


def test_language_switch_is_bidirectional_and_keeps_short_replies():
    session = {"language": "ru"}
    assert dialog._detect_lang("Анамның белі ауырып жүр, дәрігерге жазылғым келеді", session) == "kk"
    session["language"] = "kk"
    assert dialog._detect_lang("Иә", session) == "kk"
    assert dialog._detect_lang("У мамы болит поясница, хочу записаться", session) == "ru"
    session["language"] = "ru"
    assert dialog._detect_lang("Да", session) == "ru"
    assert dialog._detect_lang("Қазақша жауап беріңізші", session) == "kk"
    session["language"] = "kk"
    assert dialog._detect_lang("Можно ответить на русском?", session) == "ru"


def test_dynamic_language_instruction_for_every_turn():
    assert "казахский" in agent._answer_language_instruction({"language": "kk"})
    assert "русский" in agent._answer_language_instruction({"language": "ru"})
    assert "казахский" in agent._answer_language_instruction({"language": "kk"})
    assert "не теряй" in agent._answer_language_instruction({"language": "ru"})


def test_wrong_language_correction_does_not_invoke_crm(monkeypatch):
    from language_guard import response_language_violation
    assert response_language_violation(
        "Здравствуйте, хочу записаться на приём. Подскажите, пожалуйста.", "kk"
    )
    calls = []
    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="Сәлеметсіз бе, дәрігердің қабылдауына жазылғым келеді."
                ))]
            )
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda *_: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent.ai, "_brain_token_limit_kwargs", lambda *_: {})
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    corrected = asyncio.run(agent._ensure_reply_language(
        client=client, model="test-model", session={"language": "kk"},
        chat_id="synthetic", answer="Здравствуйте, хочу записаться на приём. Подскажите, пожалуйста."
    ))
    assert corrected.startswith("Сәлеметсіз")
    assert len(calls) == 1
    assert "tools" not in calls[0]


def test_language_rewrite_must_preserve_time_and_date(monkeypatch):
    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="Жұма 30 қазан сағат 16:00, дәрігерге жазылғым келеді."
            ))])
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda *_: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent.ai, "_brain_token_limit_kwargs", lambda *_: {})
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    original = "В пятницу 30 октября есть свободное время на 15:20."
    fixed = asyncio.run(agent._ensure_reply_language(
        client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
        model="test-model", session={"language": "kk"},
        chat_id="synthetic", answer=original,
    ))
    assert fixed == original  # Never silently alter CRM time.


def test_initial_named_doctor_request_is_honored(monkeypatch):
    monkeypatch.setattr(agent.crm, "check_slots", fake_slots)
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    session = {"last_user_text": "Запишите маму к Бекболату после обеда"}
    response = asyncio.run(agent._tool_get_available_slots("synthetic", session, {
        "date_from": "2026-10-30", "time_preference": "после обеда",
    }))
    assert response.get("doctor_choice_required") is not True
    assert session["preferred_doctor_login"] == "bekbolat"
    assert {s["doctor_login"] for s in response["slots"]} == {"bekbolat"}


def test_kazakh_ordinal_selects_offered_doctor():
    session = {"doctor_options": [
        {"doctor_login": "bekbolat", "doctor_name": "Бекболат Шуринбайулы"},
        {"doctor_login": "kaisar_k", "doctor_name": "Куанышулы Кайсар Куанышулы"},
    ]}
    chosen = agent._resolve_explicit_doctor_choice(session, "Екінші дәрігерді таңдадым")
    assert chosen["doctor_login"] == "kaisar_k"


def test_cannot_select_or_book_different_doctor(monkeypatch):
    slots = [
        {"doctor_login": "bekbolat", "doctor_name": "Бекболат Шуринбайулы",
         "date": "2026-10-30", "time_start": "15:20"},
        {"doctor_login": "kaisar_k", "doctor_name": "Куанышулы Кайсар Куанышулы",
         "date": "2026-10-30", "time_start": "15:20"},
    ]
    session = {
        "doctor_options": [
            {"doctor_login": "bekbolat", "doctor_name": "Бекболат Шуринбайулы"},
            {"doctor_login": "kaisar_k", "doctor_name": "Куанышулы Кайсар Куанышулы"},
        ],
    }
    agent._remember_offered_slots(session, slots)
    args = {"doctor_login": "kaisar_k", "date": "2026-10-30", "time_start": "15:20"}
    refused = agent._tool_select_booking_slot("synthetic", session, args)
    assert refused["error"] == "doctor_not_selected"
    session["preferred_doctor_login"] = "bekbolat"
    refused = agent._tool_select_booking_slot("synthetic", session, args)
    assert refused["error"] == "different_doctor_without_consent"
    accepted = agent._tool_select_booking_slot(
        "synthetic", session, dict(args, doctor_login="bekbolat")
    )
    assert accepted["selected"] is True
    assert session["selected_doctor_login"] == "bekbolat"


def test_internal_reserve_never_shown_as_doctor(monkeypatch):
    monkeypatch.setattr(agent.crm, "check_slots", fake_slots)
    monkeypatch.setattr(agent, "_log", lambda *_: None)
    session = {"last_user_text": "Запишите к Бекболату"}
    result = asyncio.run(agent._tool_get_available_slots("synthetic", session, {
        "date_from": "2026-10-30"
    }))
    assert "reserve" not in {s["doctor_login"] for s in result["slots"]}

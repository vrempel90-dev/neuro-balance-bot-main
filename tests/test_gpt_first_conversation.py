"""Conversational behaviour GPT now owns instead of Python keyword branches.

These cases used to be handled — or mishandled — by regex/keyword branches:
free-form phrasing instead of a button value, several parameters in one
message, changing one's mind mid-booking, and booking for a relative. The
point of each test is that the *structured state* handed to GPT already
contains what the patient said, so the model is never forced to re-ask, and
that Python does not veto what the model understood.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import ai
import crm
import dialog
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

PHONE = "77012223344"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
DATE = "2026-09-03"
NEXT_DATE = "2026-09-04"


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NEW_LEADS_ONLY", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})

    calls: dict[str, list[Any]] = {"slots": [], "book": []}

    async def check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "ok": True,
            "date": date,
            "availability": [
                {
                    "doctorLogin": DOCTOR_LOGIN,
                    "doctorName": DOCTOR_NAME,
                    "date": date,
                    "availableSlots": ["09:20", "14:00", "16:40"],
                }
            ],
        }

    async def book(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        return {"ok": True, "id": 555, "status": "Записан"}

    async def doctors(force: bool = False) -> dict[str, Any]:
        return {"ok": True, "doctors": [{"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME}]}

    async def lookup(phone: str) -> dict[str, Any]:
        return {"ok": True, "found": False, "isNew": True, "appointments": [], "appointment": None}

    monkeypatch.setattr(crm, "check_slots", check_slots)
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "get_doctors", doctors)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)

    yield calls, monkeypatch
    get_settings.cache_clear()


def _session(chat_id: str, **extra: Any) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update(
        {
            "ai_lead_started": True,
            "gate_reason": "active_ai_lead",
            "first_touch_info_sent": True,
            "phone": PHONE,
            "complaint": "болит поясница",
            "profile_status": "profile",
            "complaint_gate": "COMPLAINT_OK",
            "age": 38,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "contraindications_raw": "нет",
            "step": "date",
        }
    )
    session.update(extra)
    state.save_session(chat_id, session)


def _script(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeOpenAIClient:
    client = FakeOpenAIClient(script)
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    return client


def _turn(chat_id: str, text: str) -> str:
    return asyncio.run(dialog.handle_message(chat_id, PHONE, text))


def _agent_context(client: FakeOpenAIClient) -> dict[str, Any]:
    """The structured state the loop handed to the model on its first call."""
    for message in client.calls[0]["messages"]:
        content = str(message.get("content") or "")
        if message.get("role") == "system" and content.startswith("Структурированное состояние"):
            return json.loads(content.split("\n", 1)[1])
    raise AssertionError("structured dialog state was not sent to the model")


# ---------------------------------------------------------------------------
# Known facts are handed to GPT so it never re-asks
# ---------------------------------------------------------------------------


def test_known_facts_are_given_to_the_model(agent_env) -> None:
    calls, monkeypatch = agent_env
    client = _script(monkeypatch, [assistant_text("Поняла Вас 🌿 На какой день удобно?")])
    chat_id = "conv_known_facts"
    _session(chat_id, patient_name="Асель")

    _turn(chat_id, "когда можно прийти?")

    context = _agent_context(client)
    assert context["patient"]["complaint"] == "болит поясница"
    assert context["patient"]["age"] == 38
    assert context["patient"]["patient_name"] == "Асель"
    assert context["booking_state"]["contraindications_confirmed_clear"] is True
    assert "today" in context and "tomorrow" in context


def test_relative_booking_marks_patient_separately_from_sender(agent_env) -> None:
    calls, monkeypatch = agent_env
    client = _script(monkeypatch, [assistant_text("Поняла, записываем маму 🌿 На какой день удобно ей?")])
    chat_id = "conv_relative_context"
    _session(chat_id, patient_relation="мама")

    _turn(chat_id, "это для мамы")

    context = _agent_context(client)
    assert context["patient"]["booking_for_self"] is False
    assert context["patient"]["relation"] == "мама"
    assert context["sender"]["phone_masked"].endswith("3344")
    assert PHONE not in json.dumps(context, ensure_ascii=False), "raw phone must not be sent verbatim"


# ---------------------------------------------------------------------------
# Free-form phrasing instead of a button value
# ---------------------------------------------------------------------------


def test_conversational_slot_choice_is_not_vetoed_by_python(agent_env) -> None:
    """"давайте в обед" — GPT resolves it; Python must not reject the choice.

    In the legacy path ``_explicit_slot_selection_text`` refused anything that
    was not a literal time or an ordinal, answered with a repeat of the slot
    list, and the duplicate guard then silenced the turn.
    """
    calls, monkeypatch = agent_env
    _script(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 16:40. Какое удобно?"),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_text(f"Записала на {DATE} в 14:00 🌿"),
        ],
    )
    chat_id = "conv_freeform_slot"
    _session(chat_id)

    _turn(chat_id, f"давайте {DATE}")
    answer = _turn(chat_id, "ага, в обед удобнее, меня Асель зовут")

    assert len(calls["book"]) == 1
    assert calls["book"][0]["time_start"] == "14:00"
    assert answer.strip()


def test_several_parameters_in_one_message(agent_env) -> None:
    calls, monkeypatch = agent_env
    _script(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE, "time_preference": "утром"}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "09:20"},
                call_id="b1",
            ),
            assistant_text(f"Записала Вас на {DATE} в 09:20 🌿"),
        ],
    )
    chat_id = "conv_multi_entity"
    _session(chat_id)

    answer = _turn(chat_id, f"меня Асель зовут, хочу {DATE} утром к неврологу")

    assert len(calls["book"]) == 1
    assert calls["book"][0]["time_start"] == "09:20"
    assert answer.strip()


def test_patient_changes_their_mind_about_the_date(agent_env) -> None:
    calls, monkeypatch = agent_env
    _script(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("На 3 сентября свободно 09:20, 14:00 и 16:40."),
            assistant_tool_call("get_available_slots", {"date_from": NEXT_DATE}, call_id="s2"),
            assistant_text("Хорошо, на 4 сентября свободно 09:20, 14:00 и 16:40."),
        ],
    )
    chat_id = "conv_change_mind"
    _session(chat_id)

    _turn(chat_id, f"давайте {DATE}")
    answer = _turn(chat_id, "ой нет, лучше на следующий день")

    assert [c["date"] for c in calls["slots"]] == [DATE, NEXT_DATE]
    assert calls["book"] == [], "changing the date must not book anything"
    assert answer.strip()


def test_patient_pauses_the_booking(agent_env) -> None:
    """"подумаю" is a legitimate terminal state — and still gets an answer."""
    calls, monkeypatch = agent_env
    _script(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 16:40."),
            assistant_text("Хорошо, подумайте 🌿 Напишите, когда будете готовы."),
        ],
    )
    chat_id = "conv_paused"
    _session(chat_id)

    _turn(chat_id, f"что есть {DATE}?")
    answer = _turn(chat_id, "спасибо, я подумаю")

    assert answer.strip(), "a paused booking still needs a reply"
    assert calls["book"] == []


def test_no_slots_offers_another_period_without_inventing_one(agent_env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, mp = agent_env

    async def empty_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {"ok": True, "date": date, "availability": []}

    mp.setattr(crm, "check_slots", empty_slots)
    _script(
        mp,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE, "days_ahead": 3}),
            assistant_text("На эти дни окошек нет 🌿 Посмотреть следующую неделю?"),
        ],
    )
    chat_id = "conv_no_slots"
    _session(chat_id)

    answer = _turn(chat_id, f"что свободно с {DATE}?")

    assert answer.strip()
    assert calls["book"] == []
    assert state.get_session(chat_id)["agent_outcome"] == agent.OUTCOME_NO_SLOTS


@pytest.mark.parametrize(
    ("relation", "patient_name", "message"),
    [
        ("мама", "Гульнара", "хочу записать маму"),
        ("сын", "Ерлан", "нужно записать сына"),
        ("муж", "Бауыржан", "пишу за мужа"),
    ],
)
def test_booking_for_a_relative_sends_the_patient_not_the_sender(
    agent_env, relation: str, patient_name: str, message: str
) -> None:
    calls, monkeypatch = agent_env
    _script(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {
                    "patient_name": patient_name,
                    "patient_relation": relation,
                    "doctor_login": DOCTOR_LOGIN,
                    "date": DATE,
                    "time_start": "16:40",
                },
                call_id="b1",
            ),
            assistant_text(f"Записала {relation} на {DATE} в 16:40 🌿"),
        ],
    )
    chat_id = f"conv_relative_{relation}"
    _session(chat_id)

    answer = _turn(chat_id, f"{message}, {DATE} в 16:40, зовут {patient_name}")

    assert len(calls["book"]) == 1
    payload = calls["book"][0]
    assert payload["patient_name"] == patient_name, "the CRM record must carry the patient's name"
    assert payload["phone"] == PHONE, "the sender's phone stays the contact number"
    assert relation in payload["notes"]
    assert state.get_session(chat_id)["patient_relation"] == relation
    assert answer.strip()

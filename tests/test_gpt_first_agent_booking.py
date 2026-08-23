"""GPT-first agent loop: real CRM tools, real booking, no fake success.

Covers the production contract required by the task:

* GPT drives the dialog and calls tools; Python executes them.
* Availability and doctors come from the existing CRM endpoints only.
* ``book_appointment`` reaches the real CRM booking endpoint.
* A confirmation is possible only after the CRM confirms the booking.
* Booking is idempotent — one CRM POST per confirmed appointment.
* A slot the CRM never offered, or a doctor the CRM does not know, is refused.
* The loop is bounded and never ends a turn in silence.

CRM and OpenAI are stubbed so the suite is reproducible and never creates a
real patient or holds a real slot — but the stubs speak the *production*
contract (``availability[].availableSlots``, ``POST /api/bot/book`` fields).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
# NB: OPENAI_API_KEY is set per-test via monkeypatch, never at import time —
# a module-level assignment leaks into every other test module and would send
# the rest of the suite at the real OpenAI API.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest

import agent
import ai
import crm
import dialog
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

PHONE = "77011234567"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
OTHER_LOGIN = "aibek_md"
OTHER_NAME = "Айбек Серикович"
DATE = "2026-09-01"
SLOT_TIMES = ["09:20", "14:00", "15:40"]


@pytest.fixture(autouse=True)
def _openai_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    monkeypatch.setenv("NEW_LEADS_ONLY", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


class CRMStub:
    """Stub of the existing CRM contract, recording every call."""

    def __init__(
        self,
        *,
        slots: dict[str, list[str]] | None = None,
        book_response: dict[str, Any] | None = None,
        book_error: Exception | None = None,
        doctors: list[dict[str, Any]] | None = None,
    ):
        self.slots = slots if slots is not None else {DOCTOR_LOGIN: list(SLOT_TIMES)}
        self.book_response = book_response
        self.book_error = book_error
        self.doctors = doctors
        self.check_slots_calls: list[dict[str, Any]] = []
        self.book_calls: list[dict[str, Any]] = []
        self.doctors_calls = 0

    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        self.check_slots_calls.append({"date": date, "doctor_login": doctor_login})
        availability = []
        for login, times in self.slots.items():
            if doctor_login and login != doctor_login:
                continue
            availability.append(
                {
                    "doctorLogin": login,
                    "doctorName": DOCTOR_NAME if login == DOCTOR_LOGIN else OTHER_NAME,
                    "date": date,
                    "availableSlots": list(times),
                }
            )
        return {"ok": True, "date": date, "availability": availability}

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.book_calls.append(kwargs)
        if self.book_error is not None:
            raise self.book_error
        if self.book_response is not None:
            return dict(self.book_response)
        return {
            "ok": True,
            "id": 9001,
            "status": "Записан",
            "date": kwargs.get("date"),
            "timeStart": kwargs.get("time_start"),
            "doctorName": kwargs.get("doctor_name"),
        }

    async def get_doctors(self, force: bool = False) -> dict[str, Any]:
        self.doctors_calls += 1
        if self.doctors is not None:
            return {"ok": True, "doctors": self.doctors}
        return {
            "ok": True,
            "doctors": [
                {"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME, "specialization": "невролог"},
                {"doctorLogin": OTHER_LOGIN, "doctorName": OTHER_NAME, "specialization": "реабилитолог"},
            ],
        }

    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        return {"ok": True, "found": False, "isNew": True, "appointments": [], "appointment": None}


def install_crm(monkeypatch: pytest.MonkeyPatch, stub: CRMStub) -> CRMStub:
    monkeypatch.setattr(crm, "check_slots", stub.check_slots)
    monkeypatch.setattr(crm, "book_appointment", stub.book_appointment)
    monkeypatch.setattr(crm, "get_doctors", stub.get_doctors)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", stub.lookup_active_appointments_by_phone)
    return stub


def install_openai(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeOpenAIClient:
    client = FakeOpenAIClient(script)
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    return client


def ready_session(chat_id: str, **extra: Any) -> dict[str, Any]:
    """A session that already passed complaint/age/contraindications."""
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
            "age": 34,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "contraindications_raw": "нет",
            "step": "date",
        }
    )
    session.update(extra)
    state.save_session(chat_id, session)
    return session


def run_turn(chat_id: str, text: str) -> str:
    return asyncio.run(dialog.handle_message(chat_id, PHONE, text))


# ---------------------------------------------------------------------------
# E2E: the full production booking flow
# ---------------------------------------------------------------------------


def test_e2e_booking_flow_uses_real_crm_and_confirms_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """USER → WAZZUP → GPT → CRM availability → GPT → CRM booking → confirmation."""
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            # Turn 1: patient asks for a day → GPT calls the availability tool.
            assistant_tool_call("get_available_slots", {"date_from": DATE, "days_ahead": 1}),
            assistant_text("На 1 сентября свободно 09:20, 14:00 и 15:40. Какое время удобно?"),
            # Turn 2: patient picks a time conversationally → GPT books it.
            assistant_tool_call(
                "book_appointment",
                {
                    "patient_name": "Асель",
                    "doctor_login": DOCTOR_LOGIN,
                    "date": DATE,
                    "time_start": "14:00",
                },
                call_id="call_book",
            ),
            assistant_text(f"Запись подтверждена 🌿 {DATE} в 14:00, врач {DOCTOR_NAME}."),
        ],
    )
    chat_id = "agent_e2e"
    ready_session(chat_id)

    first = run_turn(chat_id, "Хочу записаться на 1 сентября")
    assert "14:00" in first
    assert stub.check_slots_calls, "availability must come from a real CRM check-slots call"
    assert stub.check_slots_calls[0]["date"] == DATE
    assert not stub.book_calls, "nothing may be booked before the patient chooses"

    second = run_turn(chat_id, "давайте в обед, меня зовут Асель")

    # Booking really happened, exactly once, against the existing CRM contract.
    assert len(stub.book_calls) == 1, "booking endpoint must be called exactly once"
    payload = stub.book_calls[0]
    assert payload["doctor_login"] == DOCTOR_LOGIN
    assert payload["date"] == DATE
    assert payload["time_start"] == "14:00"
    assert payload["patient_name"] == "Асель"
    assert payload["phone"] == PHONE

    # Confirmation reached the patient only after CRM success.
    assert "подтвержд" in second.lower()
    session = state.get_session(chat_id)
    assert session["booking_confirmed"] is True
    assert session["crm_result"] == "success"
    assert session["appointment_id"] == "9001"

    # The loop really fed the CRM result back into the model.
    tool_results = client.tool_results()
    assert any(r.get("booking_success") is True for r in tool_results)


def test_confirmation_requires_crm_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRM failure must never become a booking confirmation."""
    stub = install_crm(
        monkeypatch,
        CRMStub(book_error=crm.CRMError("CRM book error: connection reset")),
    )
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40."),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
            ),
            assistant_text("Пока не смогла оформить запись, уточню у администратора и вернусь к Вам."),
        ],
    )
    chat_id = "agent_crm_failure"
    ready_session(chat_id)

    run_turn(chat_id, f"Хочу записаться {DATE}")
    answer = run_turn(chat_id, "давайте 14:00, я Асель")

    assert answer.strip(), "a failed booking must still answer the patient"
    assert "записаны" not in answer.lower()
    assert "подтвержд" not in answer.lower()
    session = state.get_session(chat_id)
    assert session.get("booking_confirmed") is not True
    assert session.get("crm_result") == "failed"


def test_timeout_does_not_create_false_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(book_error=httpx.ReadTimeout("timed out")))
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40."),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "09:20"},
            ),
            assistant_text("Не получилось оформить прямо сейчас — уточню у администратора."),
        ],
    )
    chat_id = "agent_timeout"
    ready_session(chat_id)

    run_turn(chat_id, f"Запишите на {DATE}")
    answer = run_turn(chat_id, "09:20, Асель")

    assert answer.strip()
    assert state.get_session(chat_id).get("booking_confirmed") is not True
    assert len(stub.book_calls) == 1


def test_slot_conflict_is_not_success_and_offers_real_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another patient took the slot between availability and booking."""
    response = httpx.Response(
        409,
        json={"error": "slot_taken", "code": "slot_taken"},
        request=httpx.Request("POST", "https://crm.test/api/bot/book"),
    )
    conflict = crm.CRMResponseError("book", response, {"error": "slot_taken", "code": "slot_taken"})
    stub = install_crm(monkeypatch, CRMStub(book_error=conflict))
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40."),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="call_book",
            ),
            assistant_tool_call(
                "get_available_slots", {"date_from": DATE}, call_id="call_slots_again"
            ),
            assistant_text("Это время только что заняли 🌿 Свободны 09:20 и 15:40 — какое подойдёт?"),
        ],
    )
    chat_id = "agent_conflict"
    ready_session(chat_id)

    run_turn(chat_id, f"Хочу {DATE}")
    stub.slots = {DOCTOR_LOGIN: ["09:20", "15:40"]}
    answer = run_turn(chat_id, "14:00, Асель")

    assert state.get_session(chat_id).get("booking_confirmed") is not True
    assert "заняли" in answer.lower()
    assert len(stub.check_slots_calls) >= 2, "conflict must trigger a fresh CRM availability call"


# ---------------------------------------------------------------------------
# Anti-hallucination: CRM is the only source of truth
# ---------------------------------------------------------------------------


def test_booking_rejects_slot_crm_never_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPT cannot invent a slot: the tool refuses before any CRM POST."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "21:00"},
            ),
            assistant_text("Сейчас посмотрю реальные свободные окошки 🌿"),
        ],
    )
    chat_id = "agent_fake_slot"
    ready_session(chat_id)

    answer = run_turn(chat_id, "запишите на девять вечера")

    assert stub.book_calls == [], "an unoffered slot must never reach the CRM booking endpoint"
    assert state.get_session(chat_id).get("booking_confirmed") is not True
    assert answer.strip()


def test_booking_rejects_unknown_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": "ivanov_md", "date": DATE, "time_start": "14:00"},
                call_id="call_book",
            ),
            assistant_text("Такого врача у нас нет, подберу из наших специалистов 🌿"),
        ],
    )
    chat_id = "agent_fake_doctor"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"запишите к Иванову на {DATE} в 14:00")

    assert stub.book_calls == [], "a doctor CRM does not know must never be booked"
    assert answer.strip()


def test_agent_never_books_without_contraindications_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Medical safety gates stay deterministic even in GPT-first mode."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="call_book",
            ),
            assistant_text("Перед записью уточню про противопоказания."),
        ],
    )
    chat_id = "agent_no_contra"
    # NB: contraindications_ok=True is deliberately sticky in state.save_session,
    # so the gate must be left unset from the start rather than cleared later.
    ready_session(chat_id, contraindications_ok=None, contraindications_verdict="", step="contraindications")

    answer = run_turn(chat_id, f"запишите на {DATE} 14:00, я Асель")

    assert stub.book_calls == [], "booking gate must block a CRM POST before contraindications"
    assert answer.strip()


# ---------------------------------------------------------------------------
# Idempotency / duplicate protection
# ---------------------------------------------------------------------------


def test_duplicate_booking_prevented_for_same_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b2",
            ),
            assistant_text(f"Запись подтверждена: {DATE} 14:00, {DOCTOR_NAME}."),
        ],
    )
    chat_id = "agent_idempotent"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"запишите на {DATE} в 14:00, я Асель")

    assert len(stub.book_calls) == 1, "a repeated booking tool call must not create a second CRM record"
    assert state.get_session(chat_id)["booking_confirmed"] is True
    assert answer.strip()


# ---------------------------------------------------------------------------
# Loop protection and silent-turn protection
# ---------------------------------------------------------------------------


def test_tool_iteration_limit_stops_loop_and_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    script = [
        assistant_tool_call("get_available_slots", {"date_from": DATE}, call_id=f"c{i}")
        for i in range(agent.MAX_TOOL_ITERATIONS + 4)
    ]
    install_openai(monkeypatch, script)
    chat_id = "agent_loop_limit"
    ready_session(chat_id)

    answer = run_turn(chat_id, "а когда есть время?")

    session = state.get_session(chat_id)
    assert session["agent_iterations"] <= agent.MAX_TOOL_ITERATIONS
    assert answer.strip(), "hitting the tool budget must not silence the turn"


def test_model_returning_empty_text_never_silences_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("")])
    chat_id = "agent_empty_reply"
    ready_session(chat_id)

    answer = run_turn(chat_id, "здравствуйте")

    assert answer.strip(), "an empty model reply must be replaced, never sent as silence"


def test_openai_failure_falls_back_to_python_and_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI outage must degrade to the deterministic funnel, not to silence."""
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [RuntimeError("openai is down")])
    chat_id = "agent_openai_down"
    ready_session(chat_id)

    answer = run_turn(chat_id, "хочу записаться")

    session = state.get_session(chat_id)
    assert session["agent_used"] is False
    assert session["agent_skip_reason"] == "openai_error"
    assert answer.strip(), "OpenAI failure must still produce an outbound answer"


def test_agent_skipped_when_openai_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    chat_id = "agent_no_key"
    ready_session(chat_id)

    answer = run_turn(chat_id, "хочу записаться")

    session = state.get_session(chat_id)
    assert session["agent_used"] is False
    assert session["agent_skip_reason"] == "openai_key_missing"
    assert answer.strip()


def test_ai_budget_exhausted_falls_back_without_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (False, "monthly_budget_exceeded"))
    chat_id = "agent_budget"
    ready_session(chat_id)

    answer = run_turn(chat_id, "хочу записаться завтра")

    session = state.get_session(chat_id)
    assert session["agent_skip_reason"] == "monthly_budget_exceeded"
    assert answer.strip(), "an exhausted AI budget must never abandon a patient mid-booking"


# ---------------------------------------------------------------------------
# Doctor selection and relative booking
# ---------------------------------------------------------------------------


def test_specific_doctor_availability_and_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting a named doctor must query and book that exact doctorLogin."""
    stub = install_crm(
        monkeypatch,
        CRMStub(slots={DOCTOR_LOGIN: ["09:20", "14:00"], OTHER_LOGIN: ["11:00"]}),
    )
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_doctors", {}),
            assistant_tool_call(
                "get_available_slots",
                {"date_from": DATE, "doctor_login": OTHER_LOGIN},
                call_id="call_slots",
            ),
            assistant_text(f"У {OTHER_NAME} свободно 11:00. Записываем?"),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": OTHER_LOGIN, "date": DATE, "time_start": "11:00"},
                call_id="call_book",
            ),
            assistant_text(f"Записала к {OTHER_NAME} на {DATE} в 11:00 🌿"),
        ],
    )
    chat_id = "agent_doctor_choice"
    ready_session(chat_id)

    run_turn(chat_id, f"хочу к Айбеку Сериковичу на {DATE}")
    assert stub.doctors_calls >= 1, "doctor must be resolved against the CRM doctor list"
    assert stub.check_slots_calls[-1]["doctor_login"] == OTHER_LOGIN

    run_turn(chat_id, "да, 11:00, я Асель")

    assert len(stub.book_calls) == 1
    assert stub.book_calls[0]["doctor_login"] == OTHER_LOGIN, "must never book a different doctor"
    assert stub.book_calls[0]["time_start"] == "11:00"


def test_doctor_without_free_slots_offers_real_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(slots={DOCTOR_LOGIN: SLOT_TIMES, OTHER_LOGIN: []}))
    install_openai(
        monkeypatch,
        [
            assistant_tool_call(
                "get_available_slots", {"date_from": DATE, "doctor_login": OTHER_LOGIN}
            ),
            assistant_tool_call("get_available_slots", {"date_from": DATE}, call_id="call_any"),
            assistant_text(f"У {OTHER_NAME} на этот день окон нет, но у {DOCTOR_NAME} свободно 09:20 и 14:00."),
        ],
    )
    chat_id = "agent_doctor_no_slots"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"хочу к Айбеку {DATE}")

    assert answer.strip()
    assert len(stub.check_slots_calls) >= 2
    assert stub.book_calls == []


def test_relative_booking_uses_patient_name_not_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Запиши маму" — the CRM record must carry the patient, not the sender."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40. Какое время удобно маме?"),
            assistant_tool_call(
                "book_appointment",
                {
                    "patient_name": "Гульнара Сериковна",
                    "patient_relation": "мама",
                    "doctor_login": DOCTOR_LOGIN,
                    "date": DATE,
                    "time_start": "09:20",
                },
                call_id="call_book",
            ),
            assistant_text(f"Записала маму на {DATE} в 09:20 🌿"),
        ],
    )
    chat_id = "agent_relative"
    ready_session(chat_id)

    run_turn(chat_id, f"хочу записать маму на {DATE}")
    run_turn(chat_id, "09:20, её зовут Гульнара Сериковна")

    assert len(stub.book_calls) == 1
    payload = stub.book_calls[0]
    assert payload["patient_name"] == "Гульнара Сериковна"
    assert payload["phone"] == PHONE, "the sender's phone stays the CRM contact"
    assert "мама" in payload["notes"]
    session = state.get_session(chat_id)
    assert session["patient_relation"] == "мама"


# ---------------------------------------------------------------------------
# CRM response handling
# ---------------------------------------------------------------------------


def test_malformed_crm_availability_is_handled_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = CRMStub()

    async def malformed(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        stub.check_slots_calls.append({"date": date, "doctor_login": doctor_login})
        return {"ok": True, "availability": "not-a-list", "unexpected": {"nested": 1}}

    install_crm(monkeypatch, stub)
    monkeypatch.setattr(crm, "check_slots", malformed)
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Сейчас не вижу свободных окошек на этот день, посмотрим другой?"),
        ],
    )
    chat_id = "agent_malformed"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"что свободно {DATE}?")

    assert answer.strip()
    assert stub.book_calls == []


def test_crm_ok_false_response_is_not_a_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response that says ok=false must not be read as success."""
    stub = install_crm(
        monkeypatch,
        CRMStub(book_response={"ok": False, "error": "validation_failed"}),
    )
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="call_book",
            ),
            assistant_text("Не удалось оформить запись, уточню у администратора."),
        ],
    )
    chat_id = "agent_ok_false"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"{DATE} 14:00, Асель")

    assert len(stub.book_calls) == 1
    assert state.get_session(chat_id).get("booking_confirmed") is not True
    assert "записан" not in answer.lower()


def test_availability_tool_result_is_fed_back_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop must continue after a tool call, not stop at the first one."""
    install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40."),
        ],
    )
    chat_id = "agent_tool_result"
    ready_session(chat_id)

    run_turn(chat_id, f"что свободно {DATE}?")

    results = client.tool_results()
    assert results, "tool result must be returned to GPT"
    assert results[0]["slot_count"] == len(SLOT_TIMES)
    assert {s["time_start"] for s in results[0]["slots"]} == set(SLOT_TIMES)
    assert len(client.calls) == 2, "GPT must be called again with the tool result"


def test_multiple_sequential_tool_calls_in_one_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_clinic_info", {"topic": "price_first_visit"}, call_id="c1"),
            assistant_tool_call("get_available_slots", {"date_from": DATE}, call_id="c2"),
            assistant_text("Первичный приём 5 000 тг. Свободно 09:20, 14:00 и 15:40."),
        ],
    )
    chat_id = "agent_multi_tool"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"сколько стоит и что свободно {DATE}?")

    assert answer.strip()
    assert len(client.calls) == 3
    assert state.get_session(chat_id)["agent_tool_calls"] == ["get_clinic_info", "get_available_slots"]


def test_escalation_tool_delivers_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("escalate_to_operator", {"reason": "patient asked for a human"}),
            assistant_text("Передаю Ваш вопрос администратору, он свяжется с Вами 🌿"),
        ],
    )
    chat_id = "agent_escalate"
    ready_session(chat_id)

    answer = run_turn(chat_id, "позовите живого человека")

    assert answer.strip(), "escalation is a terminal state that must still answer the patient"
    assert state.get_session(chat_id)["agent_outcome"] == agent.OUTCOME_OPERATOR_ESCALATION


# ---------------------------------------------------------------------------
# Hardening found during self-review
# ---------------------------------------------------------------------------


def test_one_response_with_many_tool_calls_cannot_bypass_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single response carrying N tool calls must not run N CRM requests.

    The budget counts round-trips, so a per-round cap is also needed: without
    it one confused response could execute an unbounded number of CRM calls
    inside a single "iteration".
    """
    stub = install_crm(monkeypatch, CRMStub())
    many = agent._MAX_TOOL_CALLS_PER_ROUND + 5
    message = assistant_tool_call("get_available_slots", {"date_from": DATE}, call_id="c0")
    message.tool_calls = [
        __import__("fake_openai").FakeToolCall(f"c{i}", "get_available_slots", {"date_from": DATE})
        for i in range(many)
    ]
    install_openai(monkeypatch, [message, assistant_text("Свободно 09:20, 14:00 и 15:40.")])
    chat_id = "agent_many_calls"
    ready_session(chat_id)

    answer = run_turn(chat_id, "когда свободно?")

    assert len(stub.check_slots_calls) <= agent._MAX_TOOL_CALLS_PER_ROUND
    assert answer.strip()


def test_stale_no_slots_state_does_not_leak_into_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Здравствуйте 🌿 Чем могу помочь?")])
    chat_id = "agent_stale_outcome"
    ready_session(chat_id, crm_availability_empty=True)

    run_turn(chat_id, "здравствуйте")

    session = state.get_session(chat_id)
    assert session["agent_outcome"] != agent.OUTCOME_NO_SLOTS, (
        "a previous turn's empty-availability flag must not classify this turn"
    )


def test_booking_without_a_phone_is_refused_before_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty phone would also make the idempotency key collide."""
    stub = install_crm(monkeypatch, CRMStub())
    session: dict[str, Any] = {
        "complaint": "спина",
        "complaint_gate": "COMPLAINT_OK",
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
    }
    agent._remember_offered_slots(
        session,
        [{"doctor_login": DOCTOR_LOGIN, "doctor_name": DOCTOR_NAME, "date": DATE, "time_start": "14:00"}],
    )
    session["crm_known_doctors"] = {DOCTOR_LOGIN: DOCTOR_NAME}

    result = asyncio.run(
        agent._tool_book_appointment(
            "agent_no_phone",
            session,
            "",
            {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
        )
    )

    assert result["booking_success"] is False
    assert result["error"] == "missing_phone"
    assert stub.book_calls == []


def test_wide_search_stops_early_instead_of_hammering_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 21-day search is 21 sequential CRM calls; it must stop once it has enough."""
    stub = install_crm(monkeypatch, CRMStub())
    session: dict[str, Any] = {}

    result = asyncio.run(
        agent._tool_get_available_slots(
            "agent_wide", session, {"date_from": DATE, "days_ahead": 21}
        )
    )

    assert result["ok"] is True
    assert len(stub.check_slots_calls) < 21, "wide search must stop once enough slots are collected"
    assert result["slot_count"] <= agent._MAX_SLOTS_IN_TOOL_RESULT


# ---------------------------------------------------------------------------
# Review findings: facts, age limits, strict CRM success, telemetry privacy
# ---------------------------------------------------------------------------


def test_gpt_collected_facts_are_persisted_for_the_booking_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-new patient must become bookable through the agent alone.

    Without record_patient_facts the complaint/age/contraindication answers
    existed only as chat history, so booking_gate_status saw an empty session
    and the age limits had nothing to check.
    """
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call(
                "record_patient_facts",
                {
                    "complaint": "болит поясница",
                    "age": 34,
                    "contraindications_clear": True,
                    "contraindications_note": "ничего нет",
                    "patient_name": "Асель",
                },
                call_id="f1",
            ),
            assistant_tool_call("get_available_slots", {"date_from": DATE}, call_id="s1"),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_text(f"Записала на {DATE} в 14:00 🌿"),
        ],
    )
    chat_id = "agent_facts_persisted"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead", "phone": PHONE, "step": "complaint"})
    state.save_session(chat_id, session)

    answer = run_turn(chat_id, f"болит поясница, мне 34, противопоказаний нет, хочу {DATE} в 14:00, я Асель")

    saved = state.get_session(chat_id)
    assert saved["complaint"] == "болит поясница"
    assert saved["age"] == 34
    assert saved["contraindications_ok"] is True
    assert len(stub.book_calls) == 1
    assert answer.strip()


def test_age_boundaries_are_inclusive() -> None:
    """Pin the exact comparison: 16 and 75 are allowed, 15 and 76 are not."""
    assert agent._age_block_reason(agent.MIN_PATIENT_AGE) == ""
    assert agent._age_block_reason(agent.MAX_PATIENT_AGE) == ""
    assert agent._age_block_reason(agent.MIN_PATIENT_AGE - 1) == "under_min_age"
    assert agent._age_block_reason(agent.MAX_PATIENT_AGE + 1) == "over_max_age"
    # Unknown / unusable values are not a block on their own — the separate
    # "missing age" check refuses those, with a different message.
    assert agent._age_block_reason(None) == ""
    assert agent._age_block_reason(0) == ""
    assert agent._age_block_reason("сорок") == ""


def test_rejected_age_is_reported_back_to_the_model() -> None:
    """Silently dropping an unusable age would leave the model misinformed."""
    session: dict[str, Any] = {}

    result = agent._tool_record_patient_facts("agent_bad_age", session, {"age": 500})

    assert result["age_rejected"] == "out_of_plausible_range"
    assert result["age_known"] is False
    assert "age" not in result["stored"]
    assert "Переспроси возраст" in result["message"]

    text_result = agent._tool_record_patient_facts("agent_bad_age", session, {"age": "сорок"})
    assert text_result["age_rejected"] == "not_a_number"


@pytest.mark.parametrize("age", [12, 15, 76, 81])
def test_age_outside_clinic_limits_blocks_the_crm_booking(
    monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    """Clinic age limits are enforced in Python, not only in the prompt."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_text("Передам администратору."),
        ],
    )
    chat_id = f"agent_age_{age}"
    ready_session(chat_id, age=age)

    answer = run_turn(chat_id, f"запишите на {DATE} в 14:00")

    assert stub.book_calls == [], f"age {age} is outside clinic limits and must never reach the CRM"
    assert answer.strip()


def test_booking_without_a_recorded_age_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_text("Подскажите, пожалуйста, возраст пациента."),
        ],
    )
    chat_id = "agent_missing_age"
    ready_session(chat_id, age=0)

    answer = run_turn(chat_id, f"запишите на {DATE} в 14:00")

    assert stub.book_calls == [], "an unknown age must not reach the CRM booking endpoint"
    assert answer.strip()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"ok": True, "id": 1}, True),
        ({"success": True}, True),
        ({"id": 42}, True),
        ({"appointmentId": "a-1"}, True),
        # A CRM may return "created" as a timestamp rather than a flag; that is
        # not a confirmation on its own, but an id alongside it is.
        ({"created": "2026-09-01T10:00:00Z"}, False),
        ({"created": "2026-09-01T10:00:00Z", "id": 7}, False),
        ({"booked": "yes"}, False),
        ({}, False),
        ({"message": "queued"}, False),
        ({"ok": False}, False),
        ({"success": False, "id": 5}, False),
        ({"error": "slot_taken"}, False),
        ({"status": "failed"}, False),
        (None, False),
        ("ok", False),
    ],
)
def test_crm_success_needs_positive_confirmation(response: Any, expected: bool) -> None:
    """An unconfirmed response must never be read as a created appointment."""
    assert agent._crm_booking_succeeded(response) is expected


def test_unconfirmed_crm_response_is_not_a_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(book_response={"message": "queued"}))
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="b1",
            ),
            assistant_text("Уточню статус записи у администратора."),
        ],
    )
    chat_id = "agent_unconfirmed"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"{DATE} 14:00, Асель")

    assert len(stub.book_calls) == 1
    assert state.get_session(chat_id).get("booking_confirmed") is not True
    assert "записан" not in answer.lower()


def test_telemetry_never_records_free_text_from_the_model() -> None:
    """Escalation reasons and names are patient data; only shape is logged."""
    safe = agent._safe_args(
        {
            "patient_name": "Гульнара Сериковна",
            "patient_relation": "мама",
            "complaint": "болит поясница",
            "reason": "пациентка просит перезвонить на 87011234567",
            "date_from": "2026-09-01",
            "days_ahead": 3,
        }
    )

    assert safe["patient_name"] is True
    assert safe["patient_relation"] is True
    assert safe["complaint"] is True
    assert safe["reason"] is True
    # Structured values stay — they are what makes the telemetry useful.
    assert safe["date_from"] == "2026-09-01"
    assert safe["days_ahead"] == 3


def test_escalation_reason_is_reduced_to_a_category() -> None:
    assert agent._escalation_category("пациент просит живого оператора") == "human_requested"
    assert agent._escalation_category("сомнение по противопоказаниям") == "medical_doubt"
    assert agent._escalation_category("хочет возврат денег") == "refund_or_claim"
    assert agent._escalation_category("нечто необычное") == "other"


def test_relative_dates_use_the_clinic_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot works across UTC midnight, so 'today' must be Astana's.

    Comparing against ``astana_now()`` alone would also pass on a host that
    happens to share the clinic's timezone, so the clinic clock is moved to a
    date the host clock cannot produce.
    """
    import datetime as _dt
    import schedule

    fixed = _dt.datetime(2031, 3, 7, 1, 30, tzinfo=_dt.timezone(_dt.timedelta(hours=5)))
    monkeypatch.setattr(schedule, "astana_now", lambda: fixed)

    context = agent.build_agent_context(session={}, phone=PHONE)

    assert context["today"] == "2031-03-07", "relative dates must come from the clinic clock"
    assert context["tomorrow"] == "2031-03-08"
    assert context["today"] != _dt.datetime.now().date().isoformat()


# ---------------------------------------------------------------------------
# Third review round: false confirmation, concurrency, budget, search bounds
# ---------------------------------------------------------------------------


def test_empty_crm_200_is_not_a_confirmed_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``200 {}`` must never be reported as a created appointment.

    crm.book_appointment applies convenience defaults (ok=True,
    status="Записан") to any 2xx body, so an empty CRM response used to look
    exactly like a real confirmation.
    """
    import httpx as _httpx

    class _EmptyOk:
        async def handle_async_request(self, request: "_httpx.Request") -> "_httpx.Response":
            await request.aread()
            return _httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(crm, "_client", lambda: _httpx.AsyncClient(transport=_EmptyOk()))

    response = asyncio.run(
        crm.book_appointment(
            patient_name="Асель",
            phone=PHONE,
            doctor_login=DOCTOR_LOGIN,
            date=DATE,
            time_start="14:00",
        )
    )

    assert response["ok"] is True, "the legacy default is still applied for existing callers"
    assert response[crm.CRM_RESPONSE_KEYS_FIELD] == [], "no key actually came from the CRM"
    assert agent._crm_booking_succeeded(response) is False, (
        "a defaulted ok=True must not be read as a CRM confirmation"
    )


def test_crm_confirmation_is_accepted_when_really_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    class _RealOk:
        async def handle_async_request(self, request: "_httpx.Request") -> "_httpx.Response":
            await request.aread()
            return _httpx.Response(200, json={"ok": True, "id": 4242}, request=request)

    monkeypatch.setattr(crm, "_client", lambda: _httpx.AsyncClient(transport=_RealOk()))

    response = asyncio.run(
        crm.book_appointment(
            patient_name="Асель",
            phone=PHONE,
            doctor_login=DOCTOR_LOGIN,
            date=DATE,
            time_start="14:00",
        )
    )

    assert set(response[crm.CRM_RESPONSE_KEYS_FIELD]) >= {"ok", "id"}
    assert agent._crm_booking_succeeded(response) is True


def test_concurrent_turns_create_only_one_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different messages in one chat must not create two appointments.

    claim_message only de-duplicates a repeated delivery of the *same* message.
    Two distinct messages are handled concurrently, and both would read
    booking_confirmed=False from their own copy of the session.
    """
    stub = install_crm(monkeypatch, CRMStub())
    started = asyncio.Event()

    async def slow_book(**kwargs: Any) -> dict[str, Any]:
        stub.book_calls.append(kwargs)
        started.set()
        await asyncio.sleep(0.05)  # widen the window between check and write
        return {"ok": True, "id": 9001, crm.CRM_RESPONSE_KEYS_FIELD: ["ok", "id"]}

    monkeypatch.setattr(crm, "book_appointment", slow_book)

    def _session() -> dict[str, Any]:
        session: dict[str, Any] = {
            "complaint": "спина",
            "complaint_gate": "COMPLAINT_OK",
            "age": 40,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "crm_known_doctors": {DOCTOR_LOGIN: DOCTOR_NAME},
        }
        agent._remember_offered_slots(
            session,
            [{"doctor_login": DOCTOR_LOGIN, "doctor_name": DOCTOR_NAME, "date": DATE, "time_start": "14:00"}],
        )
        return session

    args = {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"}

    async def both() -> list[dict[str, Any]]:
        # Separate session objects: exactly what two concurrent webhook
        # handlers get, since each loads its own copy from SQLite.
        return await asyncio.gather(
            agent._tool_book_appointment("concurrent", _session(), PHONE, dict(args)),
            agent._tool_book_appointment("concurrent", _session(), PHONE, dict(args)),
        )

    results = asyncio.run(both())

    assert len(stub.book_calls) == 1, "concurrent turns must produce exactly one CRM booking"
    assert sum(1 for r in results if r.get("booking_success")) == 1
    refused = [r for r in results if not r.get("booking_success")]
    assert refused and refused[0]["error"] == "booking_already_in_progress"


def test_claim_is_released_only_on_an_unambiguous_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout keeps the claim: the CRM may have booked anyway."""
    import state as _state

    _state.claim_booking("timeout-key", "chat")
    agent._settle_booking_claim("chat", "timeout-key", rejected=False)
    assert _state.booking_claim_status("timeout-key") == "uncertain"
    assert _state.claim_booking("timeout-key", "chat") is False, "an uncertain claim must not be reusable"

    _state.claim_booking("rejected-key", "chat")
    agent._settle_booking_claim("chat", "rejected-key", rejected=True)
    assert _state.claim_booking("rejected-key", "chat") is True, "a rejected slot may be claimed again"


def test_budget_is_rechecked_before_every_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One turn can make several calls; the cap must apply to all of them."""
    install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40."),
        ],
    )

    calls = {"n": 0}

    def budget(purpose: str) -> tuple[bool, str]:
        calls["n"] += 1
        # Call 1 is agent_skip_reason's admission check, call 2 is the first
        # round-trip. From the second round-trip on, the budget is exhausted.
        allowed = calls["n"] <= 2
        return (allowed, "" if allowed else "monthly_budget_exceeded")

    monkeypatch.setattr(agent.ai_budget, "check_allowed", budget)
    chat_id = "agent_budget_midturn"
    ready_session(chat_id)

    answer = run_turn(chat_id, f"что свободно {DATE}?")

    assert len(client.calls) == 1, "the loop must stop once the budget is exhausted"
    assert answer.strip(), "an exhausted budget must still answer the patient"


def test_all_empty_period_is_bounded_by_request_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty CRM never triggers the "enough slots" early exit."""
    stub = CRMStub()

    async def empty(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        stub.check_slots_calls.append({"date": date, "doctor_login": doctor_login})
        return {"ok": True, "date": date, "availability": []}

    install_crm(monkeypatch, stub)
    monkeypatch.setattr(crm, "check_slots", empty)

    result = asyncio.run(
        agent._tool_get_available_slots("agent_empty_wide", {}, {"date_from": DATE, "days_ahead": 21})
    )

    assert result["slot_count"] == 0
    assert len(stub.check_slots_calls) <= agent._MAX_AVAILABILITY_REQUESTS, (
        "an all-empty search must still be bounded by the request ceiling"
    )

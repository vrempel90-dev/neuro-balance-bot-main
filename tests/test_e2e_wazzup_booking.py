"""End-to-end: Wazzup webhook → GPT → CRM → confirmation delivered to Wazzup.

This is the full production path the task describes:

    USER -> WAZZUP HANDLER -> GPT -> CRM AVAILABILITY TOOL -> CRM SLOTS
         -> GPT -> USER CHOOSES -> GPT -> CRM BOOKING TOOL -> CRM SUCCESS
         -> GPT -> CONFIRMATION SENT TO USER

It goes through the *existing* webhook endpoint with the *existing* payload
shape, so it also proves the GPT-first change needs no new webhook, callback
or Wazzup configuration.

The two invariants asserted end to end:

    booking_call_count == 1
    confirmation_sent_only_after_crm_success == True
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("WAZZUP_API_KEY", "test-key")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient

import agent
import ai
import crm
import dialog
import main
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

PHONE = "77010005555"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
DATE = "2026-09-02"


class Outbound:
    """Captures everything the bot would send back through Wazzup."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> dict[str, Any]:
        self.messages.append(kwargs)
        return {"ok": True, "status_code": 200}

    @property
    def texts(self) -> list[str]:
        return [str(m.get("text") or "") for m in self.messages]


class CRMRecorder:
    def __init__(self, *, slot_times: list[str] | None = None) -> None:
        self.slot_times = slot_times or ["09:20", "14:00", "15:40"]
        self.check_slots_calls: list[dict[str, Any]] = []
        self.book_calls: list[dict[str, Any]] = []
        self.booking_confirmed_at_call: int | None = None

    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        self.check_slots_calls.append({"date": date, "doctor_login": doctor_login})
        return {
            "ok": True,
            "date": date,
            "availability": [
                {
                    "doctorLogin": DOCTOR_LOGIN,
                    "doctorName": DOCTOR_NAME,
                    "date": date,
                    "availableSlots": list(self.slot_times),
                }
            ],
        }

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.book_calls.append(kwargs)
        return {
            "ok": True,
            "id": 7777,
            "status": "Записан",
            "date": kwargs.get("date"),
            "timeStart": kwargs.get("time_start"),
        }

    async def get_doctors(self, force: bool = False) -> dict[str, Any]:
        return {"ok": True, "doctors": [{"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME}]}

    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        return {"ok": True, "found": False, "isNew": True, "appointments": [], "appointment": None}


@pytest.fixture
def e2e(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NEW_LEADS_ONLY", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)

    recorder = CRMRecorder()
    monkeypatch.setattr(crm, "check_slots", recorder.check_slots)
    monkeypatch.setattr(crm, "book_appointment", recorder.book_appointment)
    monkeypatch.setattr(crm, "get_doctors", recorder.get_doctors)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", recorder.lookup_active_appointments_by_phone)

    outbound = Outbound()
    monkeypatch.setattr(main, "send_wazzup_message", outbound.send)

    yield recorder, outbound, monkeypatch
    get_settings.cache_clear()


def _post(client: TestClient, chat_id: str, text: str, message_id: str) -> dict[str, Any]:
    response = client.post(
        "/webhook/wazzup",
        json={
            "chatId": chat_id,
            "phone": chat_id,
            "text": text,
            "messageId": message_id,
            "timestamp": "2026-09-01T21:00:00+05:00",
            "direction": "incoming",
            "channelId": "test-channel",
        },
    )
    assert response.status_code == 200
    return response.json()


def _prepare_session(chat_id: str) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update(
        {
            "ai_lead_started": True,
            "gate_reason": "active_ai_lead",
            "first_touch_info_sent": True,
            "phone": chat_id,
            "complaint": "болит поясница",
            "profile_status": "profile",
            "complaint_gate": "COMPLAINT_OK",
            "age": 41,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "contraindications_raw": "нет",
            "step": "date",
        }
    )
    state.save_session(chat_id, session)


def test_full_booking_journey_through_the_existing_webhook(e2e) -> None:
    recorder, outbound, monkeypatch = e2e
    client = FakeOpenAIClient(
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE, "days_ahead": 1}),
            assistant_text(f"На {DATE} свободно 09:20, 14:00 и 15:40 🌿 Какое время удобно?"),
            assistant_tool_call(
                "book_appointment",
                {
                    "patient_name": "Асель Кайратовна",
                    "doctor_login": DOCTOR_LOGIN,
                    "date": DATE,
                    "time_start": "14:00",
                },
                call_id="call_book",
            ),
            assistant_text(f"Готово 🌿 Записала Вас на {DATE} в 14:00 к врачу {DOCTOR_NAME}."),
        ]
    )
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    http = TestClient(main.app)
    _prepare_session(PHONE)

    # --- turn 1: patient asks for a day -------------------------------------
    first = _post(http, PHONE, f"Хочу записаться {DATE}", "e2e-1")

    assert first["should_send_wazzup"] is True
    assert recorder.check_slots_calls, "slots must come from a real CRM call"
    assert recorder.book_calls == [], "nothing may be booked before the patient chooses"
    assert "14:00" in outbound.texts[-1]

    # --- turn 2: patient picks a time and gives a name -----------------------
    second = _post(http, PHONE, "давайте в обед, Асель Кайратовна", "e2e-2")

    assert second["should_send_wazzup"] is True

    # INVARIANT 1: exactly one CRM booking call.
    assert len(recorder.book_calls) == 1, "booking_call_count must be exactly 1"
    payload = recorder.book_calls[0]
    assert payload["doctor_login"] == DOCTOR_LOGIN
    assert payload["date"] == DATE
    assert payload["time_start"] == "14:00"
    assert payload["patient_name"] == "Асель Кайратовна"

    # INVARIANT 2: the confirmation reached Wazzup only after CRM success.
    confirmation = outbound.texts[-1]
    assert DATE in confirmation and "14:00" in confirmation
    session = state.get_session(PHONE)
    assert session["booking_confirmed"] is True
    assert session["crm_result"] == "success"

    # Every accepted turn produced an outbound message.
    assert len(outbound.messages) == 2


def test_confirmation_is_not_sent_when_crm_booking_fails(e2e) -> None:
    recorder, outbound, monkeypatch = e2e

    async def failing_book(**kwargs: Any) -> dict[str, Any]:
        recorder.book_calls.append(kwargs)
        raise crm.CRMError("CRM book error: upstream down")

    monkeypatch.setattr(crm, "book_appointment", failing_book)

    client = FakeOpenAIClient(
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20, 14:00 и 15:40. Какое время удобно?"),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "09:20"},
                call_id="call_book",
            ),
            assistant_text("Пока не получилось оформить запись — уточню у администратора и напишу Вам 🌿"),
        ]
    )
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    http = TestClient(main.app)
    chat_id = "77010005556"
    _prepare_session(chat_id)

    _post(http, chat_id, f"Запишите на {DATE}", "e2e-f1")
    _post(http, chat_id, "09:20, Асель", "e2e-f2")

    assert len(recorder.book_calls) == 1
    session = state.get_session(chat_id)
    assert session.get("booking_confirmed") is not True

    last = outbound.texts[-1].lower()
    assert last.strip(), "a failed booking must still answer the patient"
    assert "записаны" not in last
    assert "запись подтверждена" not in last


def test_every_accepted_turn_produces_an_outbound_message(e2e) -> None:
    """No accepted active turn may end with outbound_count == 0."""
    recorder, outbound, monkeypatch = e2e
    client = FakeOpenAIClient(
        [
            assistant_text("Расскажите, пожалуйста, что именно беспокоит?"),
            assistant_tool_call("get_clinic_info", {"topic": "price_first_visit"}),
            assistant_text("Первичный приём 5 000 тг 🌿"),
            assistant_text("Хорошо, посмотрю свободные окошки."),
        ]
    )
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    http = TestClient(main.app)
    chat_id = "77010005557"
    _prepare_session(chat_id)

    messages = ["здравствуйте", "сколько стоит?", "ага"]
    for index, text in enumerate(messages):
        result = _post(http, chat_id, text, f"e2e-turn-{index}")
        assert result.get("should_send_wazzup") is True, f"turn {index} ({text!r}) produced no outbound"

    assert len(outbound.messages) == len(messages)
    assert all(str(t).strip() for t in outbound.texts)


def test_wazzup_retry_of_the_same_message_does_not_double_book(e2e) -> None:
    recorder, outbound, monkeypatch = e2e
    client = FakeOpenAIClient(
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_tool_call(
                "book_appointment",
                {"patient_name": "Асель", "doctor_login": DOCTOR_LOGIN, "date": DATE, "time_start": "14:00"},
                call_id="call_book",
            ),
            assistant_text(f"Записала на {DATE} в 14:00 🌿"),
        ]
    )
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    http = TestClient(main.app)
    chat_id = "77010005558"
    _prepare_session(chat_id)

    payload_id = "retry-book-1"
    first = _post(http, chat_id, f"запишите на {DATE} в 14:00, я Асель", payload_id)
    second = _post(http, chat_id, f"запишите на {DATE} в 14:00, я Асель", payload_id)

    assert first["should_send_wazzup"] is True
    assert second.get("no_reply_reason") == "duplicate_message"
    assert len(recorder.book_calls) == 1, "a Wazzup retry must never create a second CRM booking"

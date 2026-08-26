"""Reschedule and cancel are agent tools, not improvised text.

Before this, the agent had six tools and none of them could touch an existing
appointment: ``crm.reschedule_appointment`` / ``crm.cancel_appointment`` were
reachable only from a legacy ``dialog.py`` branch that the GPT-first path never
executes. A patient who wrote "перенесите запись" therefore got whatever the
model made up, because Python had handed it no way to do the real thing.

These tests run whole dialogs through the real ``agent.run_agent_turn`` loop
with OpenAI and the CRM stubbed, and assert the two properties that matter:

* the tool the scenario needs is really called, against the real CRM contract;
* every date, time and doctor the patient is told came out of a ``tool_result``
  of this dialog — never out of the model's head.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
# NB: OPENAI_API_KEY is set per-test via monkeypatch, never at import time — a
# module-level assignment leaks into every other test module.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import ai
import crm
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

PHONE = "77011234567"
OTHER_PHONE = "77019998877"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
OTHER_LOGIN = "aibek_md"
OTHER_NAME = "Айбек Серикович"
APPOINTMENT_ID = "5501"
OLD_DATE = "2026-09-01"
OLD_TIME = "09:20"
NEW_DATE = "2026-09-03"
NEW_TIMES = ["11:00", "15:40"]

_DATE_TOKEN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME_TOKEN = re.compile(r"\b\d{1,2}:\d{2}\b")


@pytest.fixture(autouse=True)
def _openai_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


class CRMStub:
    """The existing CRM contract for appointments, recording every call."""

    def __init__(
        self,
        *,
        appointments: list[dict[str, Any]] | None = None,
        slots: dict[str, list[str]] | None = None,
        reschedule_response: dict[str, Any] | None = None,
        reschedule_error: Exception | None = None,
        cancel_response: dict[str, Any] | None = None,
        cancel_error: Exception | None = None,
    ):
        self.appointments = appointments if appointments is not None else [
            {
                "id": int(APPOINTMENT_ID),
                "date": OLD_DATE,
                "timeStart": OLD_TIME,
                "doctorLogin": DOCTOR_LOGIN,
                "doctorName": DOCTOR_NAME,
                "status": "Записан",
            }
        ]
        self.slots = slots if slots is not None else {DOCTOR_LOGIN: list(NEW_TIMES)}
        self.reschedule_response = reschedule_response
        self.reschedule_error = reschedule_error
        self.cancel_response = cancel_response
        self.cancel_error = cancel_error
        self.lookup_calls: list[str] = []
        self.check_slots_calls: list[dict[str, Any]] = []
        self.reschedule_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []

    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        self.lookup_calls.append(phone)
        active = list(self.appointments)
        return {
            "ok": True,
            "appointments": active,
            "appointment": active[0] if active else None,
            "active_count": len(active),
        }

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

    async def reschedule_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.reschedule_calls.append(kwargs)
        if self.reschedule_error is not None:
            raise self.reschedule_error
        if self.reschedule_response is not None:
            return dict(self.reschedule_response)
        return {
            "ok": True,
            "rescheduled": True,
            "id": int(APPOINTMENT_ID),
            "date": kwargs.get("new_date"),
            "timeStart": kwargs.get("new_time_start"),
        }

    async def cancel_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.cancel_calls.append(kwargs)
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_response is not None:
            return dict(self.cancel_response)
        return {"ok": True, "cancelled": True, "id": int(APPOINTMENT_ID)}


def install_crm(monkeypatch: pytest.MonkeyPatch, stub: CRMStub) -> CRMStub:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", stub.lookup_active_appointments_by_phone)
    monkeypatch.setattr(crm, "check_slots", stub.check_slots)
    monkeypatch.setattr(crm, "reschedule_appointment", stub.reschedule_appointment)
    monkeypatch.setattr(crm, "cancel_appointment", stub.cancel_appointment)
    return stub


def install_openai(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeOpenAIClient:
    client = FakeOpenAIClient(script)
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    return client


def booked_patient_session(chat_id: str, **extra: Any) -> dict[str, Any]:
    """A patient who already has an appointment and writes about it."""
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"phone": PHONE, "language": "ru", "ai_lead_started": True})
    session.update(extra)
    state.save_session(chat_id, session)
    return session


def run_turn(session: dict[str, Any], text: str, chat_id: str) -> agent.AgentResult:
    return asyncio.run(
        agent.run_agent_turn(chat_id=chat_id, phone=PHONE, session=session, user_text=text)
    )


def tool_names(result: agent.AgentResult) -> list[str]:
    return [call["tool"] for call in result.tool_calls]


def assert_facts_came_from_tools(reply: str, client: FakeOpenAIClient) -> None:
    """Every date/time/doctor in the reply must exist in a tool_result."""
    facts = json.dumps(client.tool_results(), ensure_ascii=False)
    for token in sorted(set(_DATE_TOKEN.findall(reply)) | set(_TIME_TOKEN.findall(reply))):
        assert token in facts, f"{token!r} нет ни в одном tool_result — модель это придумала"
    for doctor in (DOCTOR_NAME, OTHER_NAME):
        if doctor in reply:
            assert doctor in facts, f"{doctor!r} нет ни в одном tool_result"


# ---------------------------------------------------------------------------
# 1. Reschedule: find → offer real slots → move the real appointment
# ---------------------------------------------------------------------------


def test_full_dialog_reschedules_the_real_appointment(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}, call_id="call_find"),
            assistant_text(f"Ваша запись: {OLD_DATE} в {OLD_TIME}, врач {DOCTOR_NAME}. На какой день перенести?"),
            assistant_tool_call(
                "get_available_slots",
                {"date_from": NEW_DATE, "days_ahead": 1, "doctor_login": DOCTOR_LOGIN},
                call_id="call_slots",
            ),
            assistant_text(f"На {NEW_DATE} свободно 11:00 и 15:40. Какое время удобно?"),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "15:40"},
                call_id="call_move",
            ),
            assistant_text(f"Перенесла 🌿 Теперь {NEW_DATE} в 15:40, врач {DOCTOR_NAME}."),
        ],
    )
    chat_id = "agent_reschedule_ok"
    session = booked_patient_session(chat_id)

    first = run_turn(session, "Здравствуйте, хочу перенести запись", chat_id)
    assert tool_names(first) == ["find_my_appointment"]
    assert stub.lookup_calls == [PHONE], "запись должна прийти из реального CRM-поиска"
    assert not stub.reschedule_calls, "до выбора нового слота переносить нечего"

    second = run_turn(session, "давайте на третье сентября", chat_id)
    assert tool_names(second) == ["get_available_slots"]
    assert stub.check_slots_calls == [{"date": NEW_DATE, "doctor_login": DOCTOR_LOGIN}]
    assert not stub.reschedule_calls, "перенос только после выбора конкретного окошка"

    third = run_turn(session, "давайте в 15:40", chat_id)
    assert tool_names(third) == ["reschedule_appointment"]

    # The CRM really moved the appointment, exactly once, on the existing contract.
    assert len(stub.reschedule_calls) == 1
    payload = stub.reschedule_calls[0]
    assert payload["appointment_id"] == APPOINTMENT_ID
    assert payload["new_date"] == NEW_DATE
    assert payload["new_time_start"] == "15:40"
    assert payload["phone"] == PHONE

    # Every fact the patient was told came from a tool_result of this dialog.
    for reply in (first.reply, second.reply, third.reply):
        assert_facts_came_from_tools(reply, client)
    results = client.tool_results()
    assert any(r.get("reschedule_success") is True for r in results)
    assert session["appointment_date"] == NEW_DATE
    assert session["appointment_time"] == "15:40"
    assert session["appointment_status"] == "rescheduled"


def test_reschedule_is_not_repeated_for_the_same_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated tool call must not POST a second move to the CRM."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}, call_id="call_find"),
            assistant_text("Нашла Вашу запись."),
            assistant_tool_call("get_available_slots", {"date_from": NEW_DATE, "doctor_login": DOCTOR_LOGIN}),
            assistant_text("Свободно 11:00 и 15:40."),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "11:00"},
            ),
            assistant_text(f"Перенесла на {NEW_DATE} в 11:00 🌿"),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "11:00"},
            ),
            assistant_text(f"Запись стоит на {NEW_DATE} в 11:00 🌿"),
        ],
    )
    chat_id = "agent_reschedule_twice"
    session = booked_patient_session(chat_id)

    run_turn(session, "перенесите запись", chat_id)
    run_turn(session, "на 3 сентября", chat_id)
    run_turn(session, "в 11:00", chat_id)
    repeat = run_turn(session, "в 11:00, я же написал", chat_id)

    assert len(stub.reschedule_calls) == 1, "повторный вызов не должен снова дёргать CRM"
    assert tool_names(repeat) == ["reschedule_appointment"]


# ---------------------------------------------------------------------------
# 2. Cancel: find → cancel the real appointment
# ---------------------------------------------------------------------------


def test_full_dialog_cancels_the_real_appointment(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}, call_id="call_find"),
            assistant_text(f"Вижу запись {OLD_DATE} в {OLD_TIME}. Отменить её?"),
            assistant_tool_call(
                "cancel_appointment",
                {"appointment_id": APPOINTMENT_ID, "reason": "пациент не сможет прийти"},
                call_id="call_cancel",
            ),
            assistant_text(f"Отменила запись на {OLD_DATE} в {OLD_TIME} 🌿 Если понадобится — подберём новое время."),
        ],
    )
    chat_id = "agent_cancel_ok"
    session = booked_patient_session(chat_id)

    first = run_turn(session, "не смогу прийти, отмените запись", chat_id)
    assert tool_names(first) == ["find_my_appointment"]
    assert not stub.cancel_calls, "отмена только после подтверждения записи в CRM"

    second = run_turn(session, "да, отменяйте", chat_id)
    assert tool_names(second) == ["cancel_appointment"]

    assert len(stub.cancel_calls) == 1
    payload = stub.cancel_calls[0]
    assert payload["appointment_id"] == APPOINTMENT_ID
    assert payload["phone"] == PHONE
    assert payload["reason"] == "пациент не сможет прийти"

    for reply in (first.reply, second.reply):
        assert_facts_came_from_tools(reply, client)
    assert any(r.get("cancel_success") is True for r in client.tool_results())
    assert session["appointment_status"] == "cancelled"
    assert session["booking_confirmed"] is False


def test_cancel_frees_the_slot_claim_for_a_new_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a real cancel the patient must be able to book that slot again.

    The durable booking claim is what stops a second CRM POST for the same
    slot; if the cancelled appointment kept it, a patient who cancels could
    never re-book from the same chat.
    """
    install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_text("Нашла Вашу запись."),
            assistant_tool_call("cancel_appointment", {"appointment_id": APPOINTMENT_ID, "reason": "передумал"}),
            assistant_text("Запись отменена 🌿"),
        ],
    )
    chat_id = "agent_cancel_claim"
    claim_key = agent._slot_key(OLD_DATE, OLD_TIME, DOCTOR_LOGIN) + "|" + PHONE
    assert state.claim_booking(claim_key, chat_id) is True
    session = booked_patient_session(
        chat_id,
        booking_confirmed=True,
        booking_idempotency_key=claim_key,
        appointment_id=APPOINTMENT_ID,
        appointment_date=OLD_DATE,
        appointment_time=OLD_TIME,
    )

    run_turn(session, "хочу отменить запись", chat_id)
    run_turn(session, "да, отменяйте", chat_id)

    assert state.claim_booking(claim_key, chat_id) is True, "claim отменённой записи должен освобождаться"
    assert session.get("appointment_date") is None, "отменённая запись не должна оставаться в состоянии"


def test_cancelling_another_appointment_keeps_this_dialogs_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim protects one slot: another appointment's cancel must not free it."""
    other_id = "6602"
    install_crm(
        monkeypatch,
        CRMStub(
            appointments=[
                {
                    "id": int(other_id),
                    "date": OLD_DATE,
                    "timeStart": "17:00",
                    "doctorLogin": DOCTOR_LOGIN,
                    "doctorName": DOCTOR_NAME,
                    "status": "Записан",
                }
            ]
        ),
    )
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("cancel_appointment", {"appointment_id": other_id, "reason": "не приду"}),
            assistant_text("Запись отменена 🌿"),
        ],
    )
    chat_id = "agent_cancel_other"
    claim_key = agent._slot_key(OLD_DATE, OLD_TIME, DOCTOR_LOGIN) + "|" + PHONE
    assert state.claim_booking(claim_key, chat_id) is True
    session = booked_patient_session(
        chat_id,
        booking_confirmed=True,
        booking_idempotency_key=claim_key,
        appointment_id=APPOINTMENT_ID,
        appointment_date=OLD_DATE,
        appointment_time=OLD_TIME,
    )

    run_turn(session, "отмените запись на 17:00", chat_id)

    assert state.claim_booking(claim_key, chat_id) is False, "чужой claim освобождать нельзя"
    assert session.get("appointment_date") == OLD_DATE


# ---------------------------------------------------------------------------
# 3. No active appointment: escalate, never invent one
# ---------------------------------------------------------------------------


def test_reschedule_without_active_appointment_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(appointments=[]))
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}, call_id="call_find"),
            # The model tries to move an appointment it invented anyway.
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": "99999", "new_date": NEW_DATE, "new_time": "15:40"},
                call_id="call_move",
            ),
            assistant_tool_call(
                "escalate_to_operator",
                {"reason": "активной записи в CRM нет, нужен администратор"},
                call_id="call_esc",
            ),
            assistant_text("Не вижу активной записи на Ваш номер — передаю администратору, он проверит и вернётся к Вам 🌿"),
        ],
    )
    chat_id = "agent_reschedule_missing"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "перенесите мою запись на другой день", chat_id)

    assert stub.lookup_calls == [PHONE]
    assert not stub.reschedule_calls, "CRM нельзя дёргать записью, которой она не возвращала"
    assert tool_names(result) == ["find_my_appointment", "reschedule_appointment", "escalate_to_operator"]
    assert result.escalate is True
    assert result.outcome == agent.OUTCOME_OPERATOR_ESCALATION

    results = client.tool_results()
    assert any(r.get("found") is False for r in results)
    assert any(r.get("error") == "appointment_not_found" for r in results)
    # Nothing was invented for the patient: no date and no time in the answer.
    assert not _DATE_TOKEN.findall(result.reply)
    assert not _TIME_TOKEN.findall(result.reply)
    assert_facts_came_from_tools(result.reply, client)


def test_cancel_refuses_an_appointment_crm_never_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(appointments=[]))
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("cancel_appointment", {"appointment_id": "77777", "reason": "не приду"}),
            assistant_tool_call("escalate_to_operator", {"reason": "записи нет в CRM"}),
            assistant_text("Активной записи на Ваш номер не вижу, передаю администратору 🌿"),
        ],
    )
    chat_id = "agent_cancel_missing"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "отмените мою запись", chat_id)

    assert not stub.cancel_calls
    assert result.escalate is True
    assert any(r.get("error") == "appointment_not_found" for r in client.tool_results())


# ---------------------------------------------------------------------------
# Deterministic gates around the reschedule tool
# ---------------------------------------------------------------------------


def test_reschedule_refuses_a_slot_crm_did_not_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A time nobody was offered is not a slot, even if the patient named it."""
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "18:30"},
            ),
            assistant_text("Сейчас посмотрю свободные окошки на этот день 🌿"),
        ],
    )
    chat_id = "agent_reschedule_unknown_slot"
    session = booked_patient_session(chat_id)

    run_turn(session, "перенесите на 3 сентября в 18:30", chat_id)

    assert not stub.reschedule_calls
    assert any(r.get("error") == "slot_not_offered_by_crm" for r in client.tool_results())


def test_reschedule_refuses_a_window_of_another_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CRM keeps the appointment's doctor, so another doctor's window is not a slot."""
    stub = install_crm(monkeypatch, CRMStub(slots={OTHER_LOGIN: ["15:40"]}))
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("get_available_slots", {"date_from": NEW_DATE}),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "15:40"},
            ),
            assistant_text("Проверю окошки Вашего врача и напишу 🌿"),
        ],
    )
    chat_id = "agent_reschedule_other_doctor"
    session = booked_patient_session(chat_id)

    run_turn(session, "перенесите на 3 сентября в 15:40", chat_id)

    assert not stub.reschedule_calls
    assert any(r.get("error") == "slot_not_offered_by_crm" for r in client.tool_results())


def test_crm_failure_is_never_reported_as_a_move(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(
        monkeypatch,
        CRMStub(reschedule_error=crm.CRMError("CRM appointment/reschedule error: connection reset")),
    )
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("get_available_slots", {"date_from": NEW_DATE, "doctor_login": DOCTOR_LOGIN}),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "11:00"},
            ),
            assistant_text("Пока не получилось перенести, уточню у администратора и вернусь к Вам 🌿"),
        ],
    )
    chat_id = "agent_reschedule_crm_down"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "перенесите на 3 сентября в 11:00", chat_id)

    assert len(stub.reschedule_calls) == 1
    results = client.tool_results()
    assert not any(r.get("reschedule_success") is True for r in results)
    assert any(r.get("error") == "crm_unavailable" for r in results)
    # The session must not claim a move that never happened.
    assert session.get("appointment_date") != NEW_DATE
    assert "перенесл" not in result.reply.lower()


def test_crm_rejection_is_never_reported_as_a_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub(cancel_response={"ok": False, "error": "appointment_locked"}))
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("cancel_appointment", {"appointment_id": APPOINTMENT_ID, "reason": "не приду"}),
            assistant_text("Не смогла отменить сама, передаю администратору 🌿"),
        ],
    )
    chat_id = "agent_cancel_rejected"
    session = booked_patient_session(chat_id)

    run_turn(session, "отмените запись", chat_id)

    assert len(stub.cancel_calls) == 1
    results = client.tool_results()
    assert not any(r.get("cancel_success") is True for r in results)
    assert any(r.get("error") == "crm_rejected" for r in results)
    assert session.get("appointment_status") != "cancelled"


def test_lookup_is_scoped_to_the_senders_own_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """One patient must never read another patient's appointments."""
    stub = install_crm(monkeypatch, CRMStub())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {"phone": OTHER_PHONE}),
            assistant_tool_call("escalate_to_operator", {"reason": "запись на другой номер"}),
            assistant_text("Запись на другой номер проверит администратор 🌿"),
        ],
    )
    chat_id = "agent_lookup_foreign"
    session = booked_patient_session(chat_id)

    run_turn(session, f"посмотрите запись на номер {OTHER_PHONE}", chat_id)

    assert not stub.lookup_calls, "CRM нельзя опрашивать по чужому номеру"
    assert any(r.get("error") == "foreign_phone" for r in client.tool_results())


def test_lookup_accepts_the_senders_number_in_any_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """8XXX / +7XXX are the same number: that is a format, not another patient."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {"phone": "8 (701) 123-45-67"}),
            assistant_text(f"Ваша запись {OLD_DATE} в {OLD_TIME} 🌿"),
        ],
    )
    chat_id = "agent_lookup_8_format"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "когда я записан?", chat_id)

    assert stub.lookup_calls == [PHONE]
    assert tool_names(result) == ["find_my_appointment"]


def test_crm_unavailable_lookup_does_not_invent_an_appointment(monkeypatch: pytest.MonkeyPatch) -> None:
    class DownCRM(CRMStub):
        async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
            self.lookup_calls.append(phone)
            raise crm.CRMError("CRM lookup failed")

    stub = install_crm(monkeypatch, DownCRM())
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("escalate_to_operator", {"reason": "CRM недоступна"}),
            assistant_text("Не могу сейчас проверить запись, передаю администратору 🌿"),
        ],
    )
    chat_id = "agent_lookup_crm_down"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "когда я записан?", chat_id)

    assert stub.lookup_calls == [PHONE]
    assert any(r.get("error") == "crm_unavailable" for r in client.tool_results())
    assert result.escalate is True
    assert not _DATE_TOKEN.findall(result.reply)
    assert not _TIME_TOKEN.findall(result.reply)


def test_reschedule_never_names_a_doctor_the_appointment_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reschedule endpoint takes no doctor, so a window's owner is not a fact.

    When the CRM returns an appointment without a doctor, the free window used
    for the move says nothing about who will see the patient — reporting that
    window's doctor would be exactly the kind of invented fact these tools exist
    to prevent.
    """
    stub = install_crm(
        monkeypatch,
        CRMStub(
            appointments=[{"id": int(APPOINTMENT_ID), "date": OLD_DATE, "timeStart": OLD_TIME, "status": "Записан"}],
            slots={OTHER_LOGIN: ["11:00"]},
        ),
    )
    client = install_openai(
        monkeypatch,
        [
            assistant_tool_call("find_my_appointment", {}),
            assistant_tool_call("get_available_slots", {"date_from": NEW_DATE}),
            assistant_tool_call(
                "reschedule_appointment",
                {"appointment_id": APPOINTMENT_ID, "new_date": NEW_DATE, "new_time": "11:00"},
            ),
            assistant_text(f"Перенесла на {NEW_DATE} в 11:00 🌿"),
        ],
    )
    chat_id = "agent_reschedule_no_doctor"
    session = booked_patient_session(chat_id)

    result = run_turn(session, "перенесите на 3 сентября в 11:00", chat_id)

    assert len(stub.reschedule_calls) == 1
    moved = [r for r in client.tool_results() if r.get("reschedule_success") is True]
    assert moved and moved[0]["date"] == NEW_DATE and moved[0]["time_start"] == "11:00"
    assert not moved[0]["doctor_name"], "врач чужого окошка не является фактом об этой записи"
    assert OTHER_NAME not in result.reply
    assert_facts_came_from_tools(result.reply, client)

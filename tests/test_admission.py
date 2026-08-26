"""Допуск лида: единственное место, где решается, отвечает ли бот вообще.

Правило клиники: ИИ-консультант работает только с новыми обращениями.
Пациент, который уже есть в CRM, пациент с действующей записью и молчащая
CRM — это работа администратора, и бот в таких диалогах молчит.

Раньше это правило жило в монкипатчах (new_leads_only_policy,
returning_patient_policy), которые подменяли приватные функции dialog.py на
импорте и противоречили друг другу: один переводил вернувшегося пациента в
NO REPLY, другой — обратно в нового. Здесь оно проверяется как чистая функция,
случай за случаем, включая пограничные ответы CRM.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import admission
import crm
import dialog
import state

state.init_db()

PHONE = "77011234567"


def _clinic_day(offset_days: int) -> str:
    return ((datetime.now(timezone.utc) + timedelta(hours=5)) + timedelta(days=offset_days)).date().isoformat()


def new_patient_lookup(**extra: Any) -> dict[str, Any]:
    payload = {
        "ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
        "lastAppointment": None, "hasActiveAppointment": False,
        "appointment": None, "appointments": [],
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Новый лид
# ---------------------------------------------------------------------------


def test_unknown_number_is_a_new_lead() -> None:
    verdict = admission.classify(PHONE, new_patient_lookup())

    assert verdict.state == admission.NEW
    assert verdict.bot_may_reply is True


def test_new_lead_with_a_fresh_crm_lead_record() -> None:
    """CRM уже завела лид со статусом «новая» — обращение всё ещё новое."""
    verdict = admission.classify(PHONE, new_patient_lookup(found=True, lead={"id": "l1", "status": "НОВАЯ"}))

    assert verdict.state == admission.NEW


def test_phone_formats_are_the_same_patient() -> None:
    """+7 / 8 / без кода — это один номер, а не три разных пациента."""
    verdicts = [
        admission.classify(value, new_patient_lookup())
        for value in ("+7 701 123 45 67", "87011234567", "7011234567", "+77011234567")
    ]

    assert {v.phone for v in verdicts} == {PHONE}
    assert {v.state for v in verdicts} == {admission.NEW}


def test_a_number_crm_cannot_use_is_never_treated_as_new() -> None:
    verdict = admission.classify("", new_patient_lookup())

    assert verdict.state == admission.CRM_UNAVAILABLE
    assert verdict.bot_may_reply is False


# ---------------------------------------------------------------------------
# Вернувшийся пациент
# ---------------------------------------------------------------------------


def test_patient_in_crm_without_appointments_is_returning() -> None:
    verdict = admission.classify(PHONE, new_patient_lookup(found=True, isNew=False, patient={"name": "Алия"}))

    assert verdict.state == admission.RETURNING
    assert verdict.bot_may_reply is False


def test_past_appointment_is_returning_not_active() -> None:
    lookup = new_patient_lookup(
        found=True,
        isNew=False,
        lastAppointment={"id": "a1", "date": _clinic_day(-3), "timeStart": "10:00", "status": "Записан"},
    )
    verdict = admission.classify(PHONE, lookup)

    assert verdict.state == admission.RETURNING
    assert verdict.appointment is None


def test_cancelled_appointment_is_returning_not_active() -> None:
    lookup = new_patient_lookup(
        found=True,
        isNew=False,
        appointments=[{"id": "a2", "date": _clinic_day(3), "timeStart": "10:00", "status": "Отменена"}],
        lastAppointment={"id": "a2", "date": _clinic_day(3), "status": "Отменена"},
    )
    verdict = admission.classify(PHONE, lookup)

    assert verdict.state == admission.RETURNING
    assert verdict.appointment is None


def test_lead_already_in_progress_is_returning() -> None:
    verdict = admission.classify(PHONE, new_patient_lookup(found=True, lead={"id": "l2", "status": "В работе"}))

    assert verdict.state == admission.RETURNING


# ---------------------------------------------------------------------------
# Действующая запись
# ---------------------------------------------------------------------------


def test_future_appointment_is_an_active_booking() -> None:
    appointment = {"id": 77, "date": _clinic_day(2), "timeStart": "12:30", "status": "booked"}
    verdict = admission.classify(PHONE, new_patient_lookup(found=True, isNew=False, appointments=[appointment]))

    assert verdict.state == admission.ACTIVE_BOOKING
    assert verdict.appointment == appointment
    assert verdict.bot_may_reply is False


def test_appointment_today_still_counts_as_active() -> None:
    verdict = admission.classify(
        PHONE,
        new_patient_lookup(found=True, isNew=False, appointments=[{"id": 78, "date": _clinic_day(0), "timeStart": "19:00", "status": "Записан"}]),
    )

    assert verdict.state == admission.ACTIVE_BOOKING


def test_crm_flag_alone_is_enough_for_an_active_booking() -> None:
    """hasActiveAppointment=true без тела записи — всё равно не новый лид."""
    verdict = admission.classify(PHONE, new_patient_lookup(found=True, isNew=False, hasActiveAppointment=True))

    assert verdict.state == admission.ACTIVE_BOOKING


# ---------------------------------------------------------------------------
# CRM недоступна — fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lookup", [None, {}, {"ok": False}, {"error": "timeout"}])
def test_unusable_crm_answer_is_never_a_new_lead(lookup: Any) -> None:
    verdict = admission.classify(PHONE, lookup)

    assert verdict.state == admission.CRM_UNAVAILABLE
    assert verdict.bot_may_reply is False


# ---------------------------------------------------------------------------
# Тот же контракт через реальный ход диалога
# ---------------------------------------------------------------------------


def _turn(chat_id: str, text: str, monkeypatch: pytest.MonkeyPatch, lookup: Any, phone: str = PHONE) -> str:
    async def fake_lookup(value: str) -> dict[str, Any]:
        if isinstance(lookup, Exception):
            raise lookup
        return dict(lookup)

    async def fake_escalate(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(crm, "escalate_to_operator", fake_escalate)
    state.reset_session(chat_id)
    return asyncio.run(dialog.handle_message(chat_id, phone, text))


def test_returning_patient_never_reaches_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _turn(
        "admission_returning", "Хочу записаться", monkeypatch,
        new_patient_lookup(found=True, isNew=False, patient={"name": "Алия"}),
    )

    assert answer == ""
    session = state.get_session("admission_returning")
    assert session["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    assert session["silent_old_lead"] is True


def test_active_booking_never_reaches_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _turn(
        "admission_active", "Здравствуйте", monkeypatch,
        new_patient_lookup(found=True, isNew=False, appointments=[{"id": 9, "date": _clinic_day(1), "timeStart": "10:00", "status": "booked"}]),
    )

    assert answer == ""
    assert state.get_session("admission_active")["crm_patient_state"] == "ACTIVE_BOOKING"


def test_crm_outage_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _turn("admission_outage", "Здравствуйте", monkeypatch, crm.CRMError("crm down"))

    assert answer == ""
    session = state.get_session("admission_outage")
    assert session["crm_patient_state"] == "CRM_UNAVAILABLE"
    assert session["no_reply_reason"] == "crm_lookup_failed"


def test_admission_is_the_only_place_that_classifies_a_lead() -> None:
    """Решение принимает admission.classify, а не второй разбор ответа CRM.

    Проверяется именно функция хода: сырой ответ CRM dialog.py по-прежнему
    раскладывает в отладочную выдачу (``_store_crm_lookup_debug``), но ни одно
    поле оттуда не должно влиять на вердикт.
    """
    import inspect

    source = inspect.getsource(dialog._classify_lead)
    assert "admission.classify" in source
    # Поля сырого ответа CRM в ходе диалога не разбираются: их читает
    # admission.classify (решение) и _store_crm_lookup_debug (диагностика).
    for gone in ("hasActiveAppointment", "isNew", "lastAppointment", '"patient"', '"lead"'):
        assert gone not in source, f"разбор ответа CRM не должен жить в dialog._classify_lead: {gone}"


@pytest.mark.parametrize(
    "lookup, expected",
    [
        (new_patient_lookup(), "NEW_PATIENT"),
        (new_patient_lookup(found=True, isNew=False, patient={"name": "Алия"}), "RETURNING_PATIENT_NO_ACTIVE_BOOKING"),
        (new_patient_lookup(found=True, isNew=False, hasActiveAppointment=True), "ACTIVE_BOOKING"),
        (RuntimeError("crm down"), "CRM_UNAVAILABLE"),
    ],
)
def test_dialog_verdict_always_matches_admission(monkeypatch: pytest.MonkeyPatch, lookup: Any, expected: str) -> None:
    """Ход диалога не имеет собственного мнения о пациенте."""
    chat_id = f"admission_match_{expected}"
    _turn(chat_id, "Здравствуйте", monkeypatch, lookup)

    assert state.get_session(chat_id)["crm_patient_state"] == expected

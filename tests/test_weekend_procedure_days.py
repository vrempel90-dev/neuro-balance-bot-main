"""Суббота и воскресенье — процедурные дни: консультаций в них нет.

Правило клиники раньше жило в weekend_booking_policy.py, который на импорте
подменял приватную функцию dialog.py и приклеивал объяснение текстом перед
ответом бота — из-за чего поведение зависело от порядка импортов, а
формулировку писал Python. Теперь правило живёт там, где берутся даты: в
инструменте доступности. Модель получает факт и объясняет его своими словами.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import crm
import state

state.init_db()

DOCTOR_LOGIN = "zhuma_md"
OTHER_LOGIN = "aibek_md"
OTHER_NAME = "Айбек Серикович"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
SATURDAY = "2026-09-05"
SUNDAY = "2026-09-06"
MONDAY = "2026-09-07"
TUESDAY = "2026-09-08"


class SlotsStub:
    def __init__(self, times: dict[str, list[str]] | None = None):
        self.times = times or {}
        self.calls: list[str] = []

    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        self.calls.append(date)
        by_doctor = self.times.get(date, [])
        if not isinstance(by_doctor, dict):
            by_doctor = {DOCTOR_LOGIN: list(by_doctor)}
        availability = [
            {"doctorLogin": login, "doctorName": DOCTOR_NAME if login == DOCTOR_LOGIN else OTHER_NAME,
             "date": date, "availableSlots": list(times)}
            for login, times in by_doctor.items()
            if not doctor_login or login == doctor_login
        ]
        return {"ok": True, "date": date, "availability": availability}


def run_tool(monkeypatch: pytest.MonkeyPatch, stub: SlotsStub, args: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr(crm, "check_slots", stub.check_slots)
    return asyncio.run(agent._tool_get_available_slots("weekend_chat", {}, args))


def test_saturday_request_is_answered_with_the_next_working_day(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = SlotsStub({MONDAY: ["10:00", "15:00"]})

    result = run_tool(monkeypatch, stub, {"date_from": SATURDAY, "days_ahead": 3})

    assert SATURDAY not in stub.calls and SUNDAY not in stub.calls
    assert stub.calls[0] == MONDAY
    assert result["weekend_procedure_day_requested"] is True
    assert result["requested_date_from"] == SATURDAY
    assert result["date_from"] == MONDAY
    assert {slot["date"] for slot in result["slots"]} == {MONDAY}
    assert "процедурные дни" in result["note"]


def test_the_model_is_told_to_explain_it_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python отдаёт факт, а не готовую фразу пациенту."""
    stub = SlotsStub({MONDAY: ["10:00"]})

    result = run_tool(monkeypatch, stub, {"date_from": SUNDAY})

    assert "своими словами" in result["note"]


def test_a_weekday_request_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = SlotsStub({TUESDAY: ["11:00"]})

    result = run_tool(monkeypatch, stub, {"date_from": TUESDAY, "days_ahead": 1})

    assert stub.calls == [TUESDAY]
    assert result["weekend_procedure_day_requested"] is False
    assert result["date_from"] == TUESDAY


def test_a_multi_day_search_never_spends_a_request_on_a_weekend(monkeypatch: pytest.MonkeyPatch) -> None:
    """days_ahead считается в рабочих днях, а не в календарных."""
    stub = SlotsStub()

    run_tool(monkeypatch, stub, {"date_from": "2026-09-03", "days_ahead": 4})

    assert stub.calls == ["2026-09-03", "2026-09-04", MONDAY, TUESDAY]


# ---------------------------------------------------------------------------
# Врач в запросе доступности
# ---------------------------------------------------------------------------


def test_unknown_doctor_login_does_not_hide_free_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прод 27.08.2026: в doctor_login пришёл «<PRIVATE_PERSON>».

    CRM на такой фильтр вернула availability=[], и это выглядит как «у врача
    нет окошек», хотя окошки есть у всех. Логин, которого CRM не давала, не
    должен сужать поиск.
    """
    stub = SlotsStub({TUESDAY: ["11:00", "15:00"]})

    result = run_tool(monkeypatch, stub, {"date_from": TUESDAY, "doctor_login": "<PRIVATE_PERSON>"})

    assert stub.calls == [TUESDAY]
    assert result["slot_count"] == 2, "окошки всех врачей должны остаться видны"
    assert result["unknown_doctor_login_ignored"] == "<PRIVATE_PERSON>"
    assert result["requested_doctor_login"] == ""
    assert "не опознан" in result["note"]


def test_doctor_name_instead_of_login_is_not_used_as_a_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Имя врача — не doctorLogin, даже если модель прислала его в это поле."""
    stub = SlotsStub({TUESDAY: ["11:00"]})

    result = run_tool(monkeypatch, stub, {"date_from": TUESDAY, "doctor_login": DOCTOR_NAME})

    assert stub.calls[0] == TUESDAY
    assert result["unknown_doctor_login_ignored"] == DOCTOR_NAME
    assert result["slot_count"] == 1


def test_real_doctor_login_still_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий логин по-прежнему сужает поиск до этого врача."""
    stub = SlotsStub({TUESDAY: {DOCTOR_LOGIN: ["11:00"], OTHER_LOGIN: ["12:00"]}})

    result = run_tool(monkeypatch, stub, {"date_from": TUESDAY, "doctor_login": DOCTOR_LOGIN})

    assert stub.calls == [TUESDAY]
    assert result["requested_doctor_login"] == DOCTOR_LOGIN
    assert result["unknown_doctor_login_ignored"] == ""
    assert {slot["doctor_login"] for slot in result["slots"]} == {DOCTOR_LOGIN}

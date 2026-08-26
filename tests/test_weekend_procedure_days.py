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
        return {
            "ok": True,
            "date": date,
            "availability": [
                {"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME, "date": date,
                 "availableSlots": list(self.times.get(date, []))}
            ],
        }


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

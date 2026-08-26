"""Двадцать живых диалогов целиком — главный критерий готовности бота.

Юнит-тесты остаются зелёными при сломанном проде, если проверяют мёртвую
ветку. Здесь проверяется другое: реплика за репликой, через тот же код, что
работает в проде (``dialog.handle_message`` → ``agent.run_agent_turn`` →
инструменты CRM), с фейковыми OpenAI и CRM.

Сценарии описаны в ``scenarios.yaml`` — по одному блоку на диалог, чтобы
добавить новый случай можно было, не трогая код. Модель в сценарии
скриптованная: это нужно не для того, чтобы «проверить GPT», а чтобы задать
модели поведение — в том числе плохое (выдумать время, объявить запись,
которой нет, повторить тот же вопрос) — и убедиться, что Python его ловит.

По каждому сценарию проверяется три вещи:

(а) финальное состояние: запись создана / эскалация / молчание / обычный ответ;
(б) ни один вопрос бота не повторяется два хода подряд — это и есть баг
    зацикливания, ради которого всё переписывалось;
(в) каждая дата, время и имя врача в ответах бота встречается в результате
    инструмента этого диалога или в словах самого пациента.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import yaml

import agent
import ai
import crm
import dialog
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

SCENARIOS_FILE = Path(__file__).parent / "scenarios.yaml"
SCENARIOS: list[dict[str, Any]] = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8"))["scenarios"]

PHONE = "77011234567"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
OTHER_LOGIN = "aibek_md"
OTHER_NAME = "Айбек Серикович"


@pytest.fixture(autouse=True)
def _openai_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Фейковая CRM: тот же контракт, что у настоящей
# ---------------------------------------------------------------------------


class ScenarioCRM:
    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.slots: dict[str, list[str]] = dict(self.config.get("slots") or {})
        self.appointments: list[dict[str, Any]] = list(self.config.get("appointments") or [])
        self.calls: dict[str, list[Any]] = {
            "lookup": [], "check_slots": [], "book": [], "reschedule": [], "cancel": [], "escalate": [],
        }

    # --- допуск лида -------------------------------------------------------
    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        self.calls["lookup"].append(phone)
        kind = str(self.config.get("lookup") or "new")
        if kind == "error":
            raise crm.CRMError("crm down")
        if kind == "returning":
            return {"ok": True, "found": True, "isNew": False, "patient": {"name": "Алия"},
                    "lead": {"id": "l1", "status": "В работе"}, "lastAppointment": {"id": "a0", "status": "Завершён"},
                    "hasActiveAppointment": False, "appointment": None, "appointments": []}
        if kind == "active":
            return {"ok": True, "found": True, "isNew": False, "hasActiveAppointment": True,
                    "appointment": self.appointments[0] if self.appointments else None,
                    "appointments": list(self.appointments)}
        return {"ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
                "lastAppointment": None, "hasActiveAppointment": False, "appointment": None, "appointments": []}

    # --- запись ------------------------------------------------------------
    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        self.calls["check_slots"].append({"date": date, "doctor_login": doctor_login})
        availability = []
        for login, times in self._slots_for(date).items():
            if doctor_login and login != doctor_login:
                continue
            availability.append({
                "doctorLogin": login,
                "doctorName": DOCTOR_NAME if login == DOCTOR_LOGIN else OTHER_NAME,
                "date": date,
                "availableSlots": list(times),
            })
        return {"ok": True, "date": date, "availability": availability}

    def _slots_for(self, date: str) -> dict[str, list[str]]:
        value = self.slots.get(date)
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {DOCTOR_LOGIN: list(value)}

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["book"].append(kwargs)
        mode = str(self.config.get("book") or "ok")
        if mode == "error":
            raise crm.CRMError("CRM book error: connection reset")
        if mode == "conflict":
            import httpx

            raise crm.CRMResponseError(
                "book",
                httpx.Response(409, text="slot_taken", request=httpx.Request("POST", "https://crm.test/api/bot/book")),
                {"code": "slot_taken"},
            )
        if mode == "unconfirmed":
            return {}
        return {"ok": True, "id": 9001, "status": "Записан", "date": kwargs.get("date"),
                "timeStart": kwargs.get("time_start"), "doctorName": kwargs.get("doctor_name")}

    async def get_doctors(self, force: bool = False) -> dict[str, Any]:
        return {"ok": True, "doctors": [
            {"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME, "specialization": "невролог"},
            {"doctorLogin": OTHER_LOGIN, "doctorName": OTHER_NAME, "specialization": "реабилитолог"},
        ]}

    # --- существующая запись ----------------------------------------------
    async def reschedule_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["reschedule"].append(kwargs)
        if str(self.config.get("reschedule") or "ok") == "error":
            raise crm.CRMError("CRM reschedule error")
        return {"ok": True, "rescheduled": True, "id": kwargs.get("appointment_id")}

    async def cancel_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["cancel"].append(kwargs)
        if str(self.config.get("cancel") or "ok") == "error":
            raise crm.CRMError("CRM cancel error")
        return {"ok": True, "cancelled": True, "id": kwargs.get("appointment_id")}

    async def escalate_to_operator(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["escalate"].append(kwargs)
        return {"ok": True}


def install_crm(monkeypatch: pytest.MonkeyPatch, stub: ScenarioCRM) -> ScenarioCRM:
    for name in ("lookup_active_appointments_by_phone", "check_slots", "book_appointment",
                 "get_doctors", "reschedule_appointment", "cancel_appointment", "escalate_to_operator"):
        monkeypatch.setattr(crm, name, getattr(stub, name))
    return stub


# ---------------------------------------------------------------------------
# Скрипт модели
# ---------------------------------------------------------------------------


def build_script(scenario: dict[str, Any]) -> list[Any]:
    script: list[Any] = []
    for index, turn in enumerate(scenario["turns"]):
        for step in turn.get("model") or []:
            if "error" in step:
                script.append(RuntimeError(str(step["error"])))
            elif "tool" in step:
                script.append(assistant_tool_call(step["tool"], step.get("args") or {}, call_id=f"call_{index}_{len(script)}"))
            else:
                script.append(assistant_text(str(step.get("text") or "")))
    return script


# ---------------------------------------------------------------------------
# Проверки, общие для всех сценариев
# ---------------------------------------------------------------------------


_QUESTION = re.compile(r"[^.!?\n]*\?")


def _questions(answer: str) -> set[str]:
    return {re.sub(r"\s+", " ", q).strip().lower() for q in _QUESTION.findall(answer or "") if q.strip()}


def assert_no_repeated_question(name: str, answers: list[str]) -> None:
    """(б) Один и тот же вопрос два хода подряд — это и есть зацикливание."""
    spoken = [a for a in answers if str(a or "").strip()]
    for previous, current in zip(spoken, spoken[1:]):
        assert previous.strip() != current.strip(), f"{name}: бот повторил ответ дословно"
        repeated = _questions(previous) & _questions(current)
        assert not repeated, f"{name}: бот задал тот же вопрос два хода подряд: {sorted(repeated)}"


def assert_facts_from_tools(name: str, answers: list[str], client: FakeOpenAIClient, user_texts: list[str]) -> None:
    """(в) Дата, время и врач в ответе обязаны быть фактом из инструмента."""
    import json

    sources = json.dumps(client.tool_results(), ensure_ascii=False) + "\n" + "\n".join(user_texts)
    known_times = dialog._times_in(sources)
    known_dates = dialog._dates_in(sources)
    for answer in answers:
        if not str(answer or "").strip():
            continue
        for value in dialog._times_in(answer):
            assert value in known_times, f"{name}: время {value} не встречается ни в одном tool_result"
        for value in dialog._dates_in(answer):
            assert value in known_dates, f"{name}: дата {value} не встречается ни в одном tool_result"
        for doctor in (DOCTOR_NAME, OTHER_NAME):
            if doctor in answer:
                assert doctor in sources, f"{name}: врач {doctor} не встречается ни в одном tool_result"


def assert_outcome(name: str, scenario: dict[str, Any], answers: list[str], session: dict[str, Any], stub: ScenarioCRM) -> None:
    """(а) Финальное состояние диалога."""
    expect = scenario.get("expect") or {}
    outcome = str(expect.get("outcome") or "answered")
    last = str(answers[-1] or "") if answers else ""

    if outcome == "booked":
        assert session.get("booking_confirmed") is True, f"{name}: запись не создана"
        assert len(stub.calls["book"]) == 1, f"{name}: ожидался ровно один POST записи"
    elif outcome == "escalated":
        assert session.get("escalated") is True or session.get("manual_takeover") is True, f"{name}: эскалации не было"
    elif outcome == "silent":
        assert last == "", f"{name}: бот ответил там, где обязан молчать: {last!r}"
    elif outcome == "answered":
        assert last.strip(), f"{name}: последний ход остался без ответа"
    else:  # pragma: no cover - опечатка в сценарии
        raise AssertionError(f"{name}: неизвестный ожидаемый исход {outcome!r}")

    if expect.get("no_booking"):
        assert session.get("booking_confirmed") is not True, f"{name}: запись не должна была появиться"
    if "crm_book_calls" in expect:
        assert len(stub.calls["book"]) == int(expect["crm_book_calls"])
    for call in expect.get("crm_calls") or []:
        assert stub.calls[call], f"{name}: не было вызова CRM {call}"
    for call in expect.get("no_crm_calls") or []:
        assert not stub.calls[call], f"{name}: CRM {call} вызывать было нельзя"
    if expect.get("answer_is_handoff"):
        assert last == dialog.OPERATOR_HANDOFF_RU, f"{name}: ожидался ответ про администратора, получено {last!r}"
    if expect.get("answer_is_emergency"):
        assert last == dialog.EMERGENCY_103_RU, f"{name}: ожидался шаблон 103, получено {last!r}"
    for fragment in expect.get("answer_contains") or []:
        assert str(fragment) in last, f"{name}: в ответе нет {fragment!r}: {last!r}"
    for fragment in expect.get("answer_not_contains") or []:
        assert str(fragment) not in " ".join(answers), f"{name}: в ответах не должно быть {fragment!r}"


# ---------------------------------------------------------------------------
# Прогон сценария
# ---------------------------------------------------------------------------


def prepare_session(chat_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"phone": PHONE, "language": scenario.get("language") or "ru"})
    session.update(scenario.get("session") or {})
    state.save_session(chat_id, session)
    return session


def run_scenario(scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    name = scenario["name"]
    stub = install_crm(monkeypatch, ScenarioCRM(scenario.get("crm") or {}))
    client = FakeOpenAIClient(build_script(scenario))
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    budget_from = scenario.get("budget_exhausted_from_turn")
    turn_counter = {"index": 0}

    def check_allowed(purpose: str) -> tuple[bool, str]:
        if budget_from is not None and turn_counter["index"] >= int(budget_from):
            return False, "monthly_budget_exceeded"
        return True, ""

    monkeypatch.setattr(agent.ai_budget, "check_allowed", check_allowed)

    chat_id = f"scenario_{name}"
    session = prepare_session(chat_id, scenario)
    mode = str(scenario.get("mode") or "dialog")
    phone = str(scenario.get("phone") or PHONE)

    answers: list[str] = []
    user_texts: list[str] = []
    for index, turn in enumerate(scenario["turns"], start=1):
        turn_counter["index"] = index
        if turn.get("session"):
            session = state.get_session(chat_id)
            session.update(turn["session"])
            state.save_session(chat_id, session)
        text = str(turn.get("user") or "")
        user_texts.append(text)

        if turn.get("concurrent_with_previous"):
            # Пациент дослал второе сообщение, пока бот ещё думает над первым.
            # Оба хода обрабатываются одновременно, поэтому одинаковый ответ
            # здесь — не зацикливание: пациенту уйдёт один, дубль режет
            # исходящий guard в main.py (test_identical_answer_is_sent_once).
            previous = str(scenario["turns"][index - 2].get("user") or "")
            both = asyncio.run(_two_messages_at_once(chat_id, phone, previous, text))
            answers[-1:] = [max(both, key=len)]
            continue

        if mode == "agent":
            session = state.get_session(chat_id)
            result = asyncio.run(agent.run_agent_turn(
                chat_id=chat_id, phone=phone, session=session, user_text=text,
            ))
            state.save_session(chat_id, session)
            answers.append(result.reply if result.used else "")
        else:
            answers.append(asyncio.run(dialog.handle_message(chat_id, phone, text)))

    session = state.get_session(chat_id)
    assert_outcome(name, scenario, answers, session, stub)
    assert_no_repeated_question(name, answers)
    assert_facts_from_tools(name, answers, client, user_texts)


async def _two_messages_at_once(chat_id: str, phone: str, first: str, second: str) -> list[str]:
    return list(await asyncio.gather(
        dialog.handle_message(chat_id, phone, first),
        dialog.handle_message(chat_id, phone, second),
    ))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scenario(scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    run_scenario(scenario, monkeypatch)


def test_every_required_scenario_is_covered() -> None:
    """Двадцать сценариев из задачи — не меньше и все с уникальными именами."""
    names = [s["name"] for s in SCENARIOS]
    assert len(names) == len(set(names)), "имена сценариев должны быть уникальны"
    assert len(SCENARIOS) >= 20, f"ожидалось 20 сценариев, найдено {len(SCENARIOS)}"

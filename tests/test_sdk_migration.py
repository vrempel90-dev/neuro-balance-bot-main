"""Focused regression checks for the optional Agents SDK transport and output guard."""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import agent
import agents_sdk_runtime as runtime
from response_policy import violation


class _Result:
    def __init__(self, *, booked=False, tools=None):
        self.booked = booked
        self.tool_results = tools or []


@pytest.mark.parametrize("answer, expected", [
    ("Вы записаны на приём.", "unconfirmed_booking_claim"),
    ("Ваша запись подтверждена.", "unconfirmed_booking_claim"),
    ("Запись отменена.", "unconfirmed_cancellation_claim"),
    ("Мы перенесли вашу запись.", "unconfirmed_reschedule_claim"),
    ("Instagram: https://instagram.com/not_neurobalance", "unapproved_url"),
    ("Наш адрес: улица Другая 17, Астана.", "unapproved_address"),
    ("Вы можете записаться на консультацию?", ""),
    ("Запись уточнит администратор.", ""),
])
def test_outbound_policy_rejects_unverified_claims(answer, expected):
    assert violation(answer) == expected


def test_verified_crm_confirmation_is_allowed():
    assert violation("Вы записаны на приём.", _Result(booked=True)) == ""
    assert violation("Запись отменена.", _Result(tools=[{"cancel_success": True}])) == ""
    assert violation("Запись перенесена.", _Result(tools=[{"reschedule_success": True}])) == ""


def test_approved_instagram_and_address():
    assert violation(
        "Instagram: https://www.instagram.com/neuro_balance_ast?vrfl=NmttZWZoeWF2MGs1"
    ) == ""
    assert violation("📍 Адрес: Кабанбай батыра 28, Астана.") == ""


def test_sdk_history_has_no_duplicate_current_user_message(monkeypatch):
    monkeypatch.setattr(agent, "build_agent_context", lambda **_kw: {"patient": {"age": 62}})
    messages = runtime._context_messages(
        session={}, phone="77001234567", user_text="Мне 62",
        recent_history=[{"role": "assistant", "text": "Сколько лет?"},
                        {"role": "user", "text": "Мне 62"}],
    )
    # dialog._recent_history_for_brain already excludes current text; the
    # runtime must be robust when a caller accidentally includes it.
    assert messages[-1]["content"] == "Мне 62"
    assert sum(1 for m in messages if m["role"] == "user" and m["content"] == "Мне 62") == 1


def _fake_agents(monkeypatch, runner):
    fake = types.ModuleType("agents")

    class Container:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake.Agent = Container
    fake.ModelSettings = Container
    fake.RunConfig = Container
    fake.FunctionTool = Container
    fake.Runner = types.SimpleNamespace(run=runner)
    monkeypatch.setitem(sys.modules, "agents", fake)


def _stub_legacy(monkeypatch):
    settings = types.SimpleNamespace(
        ai_brain_model="gpt-5.6-terra",
        openai_model="gpt-5.6-luna",
        agents_sdk_max_turns=6, agents_sdk_max_tool_calls=4,
    )
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(agent, "agent_skip_reason", lambda _session, _text: "")
    monkeypatch.setattr(agent, "_resolve_explicit_slot_choice", lambda *_: None)
    monkeypatch.setattr(agent, "_simple_patient_name_candidate", lambda *_: "")
    monkeypatch.setattr(agent, "build_agent_context", lambda **_: {"patient": {"age": 62}})
    monkeypatch.setattr(agent, "agent_system_prompt", lambda: "Approved clinic rules")
    monkeypatch.setattr(agent, "_classify_outcome", lambda *_: agent.OUTCOME_CONTINUE)
    monkeypatch.setattr(agent, "_safe_args", lambda x: x)
    monkeypatch.setattr(runtime.ai_budget, "check_allowed", lambda *_: (True, ""))
    monkeypatch.setattr(runtime.state, "log_event", lambda *_: None)
    monkeypatch.setattr(runtime.state, "record_ai_usage", lambda **_: None)

    async def no_preflight(*_):
        return None

    monkeypatch.setattr(agent, "_try_auto_complete_booking", no_preflight)
    monkeypatch.setattr(agent, "TOOL_SCHEMAS", [{
        "function": {
            "name": "record_patient_facts",
            "description": "Persist facts",
            "parameters": {
                "type": "object",
                "properties": {"age": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
    }])


def test_sdk_invokes_existing_backend_once(monkeypatch):
    _stub_legacy(monkeypatch)
    executed = []

    async def execute_tool(**kwargs):
        executed.append(kwargs)
        return {"ok": True, "stored": ["age"]}

    monkeypatch.setattr(agent, "execute_tool", execute_tool)

    async def fake_runner(sdk_agent, input, max_turns, run_config):
        assert max_turns == 6
        assert run_config.tracing_disabled is True
        assert sdk_agent.model_settings.parallel_tool_calls is False
        tool_result = await sdk_agent.tools[0].on_invoke_tool(None, json.dumps({"age": 62}))
        assert json.loads(tool_result)["stored"] == ["age"]
        return types.SimpleNamespace(
            final_output="Подскажите удобный день.",
            context_wrapper=types.SimpleNamespace(
                usage=types.SimpleNamespace(input_tokens=100, output_tokens=25)),
        )

    _fake_agents(monkeypatch, fake_runner)
    result = asyncio.run(runtime.run_agent_turn(
        chat_id="synthetic", phone="77001234567",
        session={}, user_text="Маме 62",
        recent_history=[{"role": "assistant", "text": "Сколько лет пациенту?"}],
    ))
    assert result.used is True
    assert result.reply == "Подскажите удобный день."
    assert len(executed) == 1
    assert executed[0]["name"] == "record_patient_facts"
    assert result.tool_calls[0]["tool"] == "record_patient_facts"


def test_sdk_failure_after_tool_does_not_restart_booking(monkeypatch):
    _stub_legacy(monkeypatch)
    calls = []

    async def execute_tool(**kwargs):
        calls.append(kwargs["name"])
        return {"ok": True, "stored": ["age"]}

    monkeypatch.setattr(agent, "execute_tool", execute_tool)
    monkeypatch.setattr(agent, "_safety_net_reply", lambda *_: "Подключаю администратора.")

    async def failing_runner(sdk_agent, **_):
        await sdk_agent.tools[0].on_invoke_tool(None, '{"age":62}')
        raise RuntimeError("simulated sdk transport failure")

    _fake_agents(monkeypatch, failing_runner)
    result = asyncio.run(runtime.run_agent_turn(
        chat_id="synthetic", phone="77001234567",
        session={}, user_text="Маме 62",
    ))
    assert calls == ["record_patient_facts"]
    assert result.escalate is True
    assert result.reply == "Подключаю администратора."

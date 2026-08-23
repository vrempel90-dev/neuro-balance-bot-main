"""Scriptable stand-in for the OpenAI chat-completions client.

Tests must be reproducible and must never create real patients or hold real
slots, so OpenAI and the CRM are stubbed. The stub mirrors the production
response shape exactly (``choices[0].message.tool_calls`` with
``function.name`` / ``function.arguments``), so the agent loop under test is
the same code that runs against the real API.
"""
from __future__ import annotations

import json
from typing import Any


class FakeFunction:
    def __init__(self, name: str, arguments: dict[str, Any] | str):
        self.name = name
        self.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any] | str):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls: list[FakeToolCall] | None = None):
        self.content = content
        self.tool_calls = tool_calls or None
        self.role = "assistant"


class FakeChoice:
    def __init__(self, message: FakeMessage, finish_reason: str = "stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 40):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.prompt_tokens_details = None


class FakeResponse:
    def __init__(self, message: FakeMessage, finish_reason: str = "stop"):
        self.choices = [FakeChoice(message, finish_reason)]
        self.usage = FakeUsage()
        self.model = "fake-model"


class ScriptedCompletions:
    """Replays a scripted list of assistant turns, recording what it received."""

    def __init__(self, script: list[FakeMessage | Exception]):
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._script:
            # Nothing scripted left: behave like a model that just answers.
            return FakeResponse(FakeMessage(content="Хорошо 🌿"))
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)


class FakeOpenAIClient:
    def __init__(self, script: list[FakeMessage | Exception]):
        self.completions = ScriptedCompletions(script)
        self.chat = self

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.completions.calls

    def tool_messages(self) -> list[dict[str, Any]]:
        """Every ``role=tool`` message the loop fed back into the model."""
        messages: list[dict[str, Any]] = []
        for call in self.completions.calls:
            for message in call.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "tool":
                    messages.append(message)
        return messages

    def tool_results(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for message in self.tool_messages():
            try:
                results.append(json.loads(message.get("content") or "{}"))
            except Exception:
                continue
        return results


def assistant_tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1", content: str = "") -> FakeMessage:
    return FakeMessage(content=content, tool_calls=[FakeToolCall(call_id, name, arguments)])


def assistant_text(content: str) -> FakeMessage:
    return FakeMessage(content=content)

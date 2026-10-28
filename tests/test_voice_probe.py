"""Synthetic integration-probe orchestration tests; SDK and ASR are mocked."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import production_voice_probe as probe


_WAV = b"RIFF" + (4).to_bytes(4, "little") + b"WAVE" + b"synthetic"


@pytest.fixture
def rig(monkeypatch):
    calls = {"tts": [], "asr": [], "budget": [], "closed": 0, "response_closed": 0}

    class Response:
        async def aread(self):
            return _WAV

        async def aclose(self):
            calls["response_closed"] += 1

    async def create(**kwargs):
        calls["tts"].append(kwargs)
        return Response()

    async def close():
        calls["closed"] += 1

    def client(**kwargs):
        calls["client"] = kwargs
        return SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)), close=close)

    async def transcribe(data, **kwargs):
        calls["asr"].append((data, kwargs))
        language = "ru" if len(calls["asr"]) == 1 else "kk"
        return SimpleNamespace(text=probe._PHRASES[language])

    def budget(purpose):
        calls["budget"].append(purpose)
        return True, ""

    monkeypatch.setattr(probe, "get_settings", lambda: SimpleNamespace(openai_api_key="synthetic-private-key"))
    monkeypatch.setattr(probe, "AsyncOpenAI", client)
    monkeypatch.setattr(probe.voice, "transcribe_bytes", transcribe)
    monkeypatch.setattr(probe.ai_budget, "check_allowed", budget)
    return calls


def test_controlled_bilingual_probe_uses_auto_asr_once_and_returns_no_payload(rig):
    result = asyncio.run(probe.synthetic_voice_probe())
    assert result["executed"] and result["ok"]
    assert result["reason"] == "verified"
    assert result["native_quality_verified"] is False
    assert result["channel_delivery_verified"] is False
    assert result["audio_cost_accounting_verified"] is False
    assert result["patient_messages_sent"] == result["crm_writes"] == 0
    assert rig["client"]["max_retries"] == 0
    assert rig["client"]["timeout"] == 30
    assert len(rig["tts"]) == len(rig["asr"]) == 2
    assert rig["response_closed"] == 2 and rig["closed"] == 1
    assert rig["budget"] == ["production_voice_probe_tts", "production_voice_probe_asr"] * 2
    assert all(call["model"] == "tts-1" and call["voice"] == "alloy" and call["response_format"] == "wav" for call in rig["tts"])
    assert all(kwargs["language"] is None for _, kwargs in rig["asr"])
    serialized = json.dumps(result, ensure_ascii=False)
    assert "synthetic-private-key" not in serialized and "RIFF" not in serialized
    assert all(phrase not in serialized for phrase in probe._PHRASES.values())


def test_missing_key_never_constructs_client_or_calls_budget(monkeypatch, rig):
    monkeypatch.setattr(probe, "get_settings", lambda: SimpleNamespace(openai_api_key=""))
    result = asyncio.run(probe.synthetic_voice_probe())
    assert result["executed"] is False and result["ok"] is False
    assert result["reason"] == "openai_key_unavailable"
    assert rig["tts"] == rig["asr"] == rig["budget"] == []
    assert "client" not in rig


def test_blocked_tts_budget_makes_no_api_calls(monkeypatch, rig):
    monkeypatch.setattr(probe.ai_budget, "check_allowed", lambda _: (False, "monthly_budget_exceeded"))
    result = asyncio.run(probe.synthetic_voice_probe())
    assert result["executed"] is False
    assert all(item["reason"] == "ai_budget_blocked" for item in result["languages"].values())
    assert rig["tts"] == rig["asr"] == []
    assert rig["closed"] == 1


def test_asr_budget_is_checked_after_each_tts(monkeypatch, rig):
    monkeypatch.setattr(probe.ai_budget, "check_allowed", lambda purpose: (purpose.endswith("tts"), ""))
    result = asyncio.run(probe.synthetic_voice_probe())
    assert result["executed"] and not result["ok"]
    assert len(rig["tts"]) == 2 and not rig["asr"]
    assert all(item["tts_executed"] and not item["asr_executed"] for item in result["languages"].values())


@pytest.mark.parametrize("payload", [b"", b"not a wav", _WAV + b"x" * 250_000])
def test_invalid_or_oversize_audio_never_reaches_asr(monkeypatch, rig, payload):
    class Response:
        async def aread(self):
            return payload

        async def aclose(self):
            rig["response_closed"] += 1

    async def create(**kwargs):
        return Response()

    async def close():
        pass

    monkeypatch.setattr(probe, "AsyncOpenAI", lambda **_: SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)), close=close))
    result = asyncio.run(probe.synthetic_voice_probe())
    assert not result["ok"] and not rig["asr"]
    assert all(item["reason"] == "invalid_synthetic_audio" for item in result["languages"].values())
    assert rig["response_closed"] == 2


@pytest.mark.parametrize("text", ["", "Спасибо, до свидания", "Хочу записаться к кардиологу."])
def test_nonmatching_transcript_is_not_a_success(monkeypatch, rig, text):
    async def transcribe(*args, **kwargs):
        return SimpleNamespace(text=text)

    monkeypatch.setattr(probe.voice, "transcribe_bytes", transcribe)
    result = asyncio.run(probe.synthetic_voice_probe())
    assert not result["ok"]
    assert all(not item["ok"] and item["reason"] == "transcription_mismatch" for item in result["languages"].values())


def test_api_error_is_sanitized_and_never_retried(monkeypatch, rig):
    async def transcribe(*args, **kwargs):
        rig["asr"].append("attempt")
        raise RuntimeError("private transcript and synthetic-private-key")

    monkeypatch.setattr(probe.voice, "transcribe_bytes", transcribe)
    result = asyncio.run(probe.synthetic_voice_probe())
    assert len(rig["tts"]) == len(rig["asr"]) == 2
    assert not result["ok"] and rig["closed"] == 1
    assert "private" not in json.dumps(result)
    assert all(item["reason"] == "asr_api_error" for item in result["languages"].values())


def test_per_call_timeout_is_bounded_without_retry(monkeypatch, rig):
    monkeypatch.setattr(probe, "CALL_TIMEOUT_SECONDS", 0.005)

    async def transcribe(*args, **kwargs):
        rig["asr"].append("attempt")
        await asyncio.sleep(10)

    monkeypatch.setattr(probe.voice, "transcribe_bytes", transcribe)
    result = asyncio.run(probe.synthetic_voice_probe())
    assert not result["ok"] and len(rig["asr"]) == 2
    assert all(item["reason"] == "asr_timeout" for item in result["languages"].values())


def test_global_timeout_cancels_current_stage_and_prevents_next_language(monkeypatch, rig):
    monkeypatch.setattr(probe, "TOTAL_TIMEOUT_SECONDS", 0.005)

    async def transcribe(*args, **kwargs):
        rig["asr"].append("attempt")
        await asyncio.sleep(10)

    monkeypatch.setattr(probe.voice, "transcribe_bytes", transcribe)
    result = asyncio.run(probe.synthetic_voice_probe())
    assert result["reason"] == "total_timeout"
    assert not result["ok"] and len(rig["asr"]) == len(rig["tts"]) == 1
    assert result["languages"]["ru"]["reason"] == "total_timeout"
    assert result["languages"]["kk"]["executed"] is False
    assert rig["closed"] == 1

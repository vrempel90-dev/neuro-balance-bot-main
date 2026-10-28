"""Bounded synthetic TTS → multilingual ASR check, never a patient message.

This is an explicitly invoked integration probe, not a recurring health check.
Only fixed test phrases leave the service; neither audio nor transcripts are
persisted or included in its result. A successful keyword check does not prove
native speaker quality or the channel's end-to-end media delivery.
"""
from __future__ import annotations

import asyncio
from typing import Any

from openai import AsyncOpenAI

import ai_budget
from config import get_settings
from language_guard import detect_language
import voice

MAX_WAV_BYTES = 250_000
TOTAL_TIMEOUT_SECONDS = 60.0
CALL_TIMEOUT_SECONDS = 30.0
_PHRASES = {
    "ru": "Хочу записаться к неврологу.",
    "kk": "Невролог дәрігерге жазылғым келеді.",
}


def _empty_language_result() -> dict[str, Any]:
    return {"executed": False, "tts_executed": False, "asr_executed": False,
            "detected": False, "matches_phrase": False, "ok": False,
            "reason": "not_executed"}


def _matches_phrase(text: str, language: str) -> bool:
    normalized = text.lower()
    if language == "ru":
        return "запис" in normalized and "неврол" in normalized
    return "жазыл" in normalized and ("дәрігер" in normalized or "неврол" in normalized)


def _allowed(purpose: str) -> bool:
    allowed, _ = ai_budget.check_allowed(purpose)
    return bool(allowed)


async def _generate_wav(client: Any, phrase: str) -> bytes:
    response = await client.audio.speech.create(
        model="tts-1", voice="alloy", input=phrase, response_format="wav",
    )
    try:
        data = await response.aread()
    finally:
        await response.aclose()
    if not isinstance(data, bytes) or not data or len(data) > MAX_WAV_BYTES:
        raise ValueError("invalid_synthetic_audio_size")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("invalid_synthetic_audio_format")
    return data


async def synthetic_voice_probe() -> dict[str, Any]:
    """Generate fixed RU/KK audio and transcribe once with auto language.

    Uses the service's existing credential. There are at most two TTS and two
    ASR requests, no retries, each guarded by the existing AI budget. Existing
    audio APIs do not supply token usage, so token/cost accounting is explicitly
    unverified instead of recording a fabricated zero-cost success.
    """
    results = {language: _empty_language_result() for language in _PHRASES}
    output: dict[str, Any] = {
        "executed": False, "ok": False, "reason": "not_executed",
        "native_quality_verified": False, "channel_delivery_verified": False,
        "audio_cost_accounting_verified": False, "languages": results,
        "patient_messages_sent": 0, "crm_writes": 0,
    }
    if not str(getattr(get_settings(), "openai_api_key", "") or "").strip():
        output["reason"] = "openai_key_unavailable"
        return output

    client = AsyncOpenAI(api_key=get_settings().openai_api_key,
                         timeout=CALL_TIMEOUT_SECONDS, max_retries=0)
    current: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            for language, phrase in _PHRASES.items():
                current = results[language]
                if not _allowed("production_voice_probe_tts"):
                    current["reason"] = "ai_budget_blocked"
                    continue
                output["executed"] = current["executed"] = current["tts_executed"] = True
                try:
                    async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                        data = await _generate_wav(client, phrase)
                except TimeoutError:
                    current["reason"] = "tts_timeout"
                    continue
                except ValueError:
                    current["reason"] = "invalid_synthetic_audio"
                    continue
                except Exception:
                    current["reason"] = "tts_api_error"
                    continue

                if not _allowed("production_voice_probe_asr"):
                    current["reason"] = "ai_budget_blocked"
                    continue
                current["asr_executed"] = True
                try:
                    async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                        transcript = await voice.transcribe_bytes(
                            data, filename="synthetic-probe.wav", language=None,
                        )
                    text = str(transcript.text or "").strip()
                    current["detected"] = bool(text) and detect_language(text) == language
                    current["matches_phrase"] = _matches_phrase(text, language)
                    current["ok"] = current["detected"] and current["matches_phrase"]
                    current["reason"] = "verified" if current["ok"] else "transcription_mismatch"
                except TimeoutError:
                    current["reason"] = "asr_timeout"
                except Exception:
                    current["reason"] = "asr_api_error"
    except TimeoutError:
        if current is not None and not current["ok"]:
            current["reason"] = "total_timeout"
        output["reason"] = "total_timeout"
    finally:
        try:
            async with asyncio.timeout(2):
                await client.close()
        except Exception:
            pass
    output["ok"] = all(result["ok"] for result in results.values())
    if output["reason"] != "total_timeout":
        output["reason"] = "verified" if output["ok"] else "synthetic_voice_probe_incomplete"
    return output

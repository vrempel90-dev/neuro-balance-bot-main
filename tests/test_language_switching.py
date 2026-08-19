"""Regression tests for mid-dialog RU <-> KK language switching.

Audit finding: ``_detect_lang`` returned the frozen session language as soon as
``step`` left ``"start"``, so a patient who switched languages mid-conversation
kept getting answers in the previous language forever.

The replacement must switch on a confident signal without flip-flopping on
short confirmations like «иә» / «да» / «44».
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import dialog  # noqa: E402


def detect(text: str, **session: Any) -> str:
    return dialog._detect_lang(text, dict(session))


# ------------------------------------------------- first message sets the language


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Болит спина", "ru"),
        ("Здравствуйте, хочу записаться", "ru"),
        ("Белім ауырады", "kk"),
        ("Сәлеметсіз бе, жазылғым келеді", "kk"),
    ],
)
def test_first_message_establishes_dialog_language(text: str, expected: str) -> None:
    assert detect(text, step="start") == expected


@pytest.mark.parametrize("text", ["Колено", "колено ноет", "Плечо и колено болит"])
def test_russian_knee_complaint_does_not_start_a_kazakh_dialog(text: str) -> None:
    """Regression: «кол» matched inside «колено» and locked the chat to Kazakh."""
    assert detect(text, step="start") == "ru"


# ------------------------------------------------- switching mid-dialog


def test_patient_switches_russian_to_kazakh() -> None:
    assert detect("Белім қатты ауырып тұр, жазылғым келеді", language="ru", step="age") == "kk"


def test_patient_switches_kazakh_to_russian() -> None:
    assert detect("Здравствуйте, а сколько стоит консультация?", language="kk", step="age") == "ru"


@pytest.mark.parametrize("step", ["complaint", "age", "contraindications", "date", "time", "name"])
def test_switching_works_on_every_step_not_only_start(step: str) -> None:
    """The old code froze the language for every step except ``start``."""
    assert detect("Белім қатты ауырып тұр, жазылғым келеді", language="ru", step=step) == "kk"


# ------------------------------------------------- stickiness


@pytest.mark.parametrize("text", ["иә", "ия", "жоқ", "жок", "рахмет", "44", "жарайды"])
def test_short_kazakh_answers_do_not_switch_a_russian_dialog(text: str) -> None:
    assert detect(text, language="ru", step="contraindications") == "ru"


@pytest.mark.parametrize("text", ["да", "нет", "ага", "угу", "неа", "42", "спасибо"])
def test_short_russian_answers_do_not_switch_a_kazakh_dialog(text: str) -> None:
    assert detect(text, language="kk", step="contraindications") == "kk"


@pytest.mark.parametrize("text", ["", "   ", "+", "🌿", "???"])
def test_signal_free_messages_keep_the_language(text: str) -> None:
    assert detect(text, language="kk", step="age") == "kk"
    assert detect(text, language="ru", step="age") == "ru"


# ------------------------------------------------- mixed messages


@pytest.mark.parametrize("current", ["ru", "kk"])
def test_mixed_message_does_not_switch_the_dialog_language(current: str) -> None:
    assert detect("Белім ауырады, врач сегодня принимает?", language=current, step="age") == current


# ------------------------------------------------- explicit requests


def test_explicit_kazakh_request_switches_even_a_locked_dialog() -> None:
    assert detect("қазақша жазыңыз", language="ru", language_locked=True, step="age") == "kk"


def test_explicit_russian_request_switches_even_a_locked_dialog() -> None:
    assert detect("можно на русском", language="kk", language_locked=True, step="age") == "ru"


def test_locked_language_is_not_changed_by_plain_text() -> None:
    """After an explicit request the choice sticks until the patient asks again."""
    assert detect(
        "Белім қатты ауырып тұр, жазылғым келеді",
        language="ru",
        language_locked=True,
        step="age",
    ) == "ru"

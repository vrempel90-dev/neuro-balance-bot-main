"""Отрицание противопоказания не должно приводить к отказу в записи.

HARD_CONTRA_WORDS хранит ОСНОВЫ слов ("эпилеп", "онколог", "беремен"), а шаблоны
отрицания требовали, чтобы сразу за основой шёл пробел. Пациент пишет склонённую
форму, поэтому "эпилепсии нет" не распознавалось как отрицание и превращалось в
hard-stop: человеку БЕЗ эпилепсии приходил отказ в записи. При этом обратный
порядок слов ("нет эпилепсии") отрабатывал верно — баг зависел от формулировки.

До правки ложный отказ давали 8 из 18 типичных отрицаний.

Здесь закреплено и обратное: подтверждённые противопоказания по-прежнему
останавливают запись, включая смешанные фразы вида
"эпилепсии нет, но есть кардиостимулятор".
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("OPENAI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import dialog


@pytest.mark.parametrize("text", [
    "эпилепсии нет",
    "онкологии нет",
    "беременности нет",
    "тромбоза нет",
    "кардиостимулятора нет",
    "судорог нет",
    "температуры нет",
    "эпилепсия нет",
    "онкология нет",
    "беременность нет",
    "нет эпилепсии",
    "нет онкологии",
    "нет беременности",
    "нет тромбоза",
    "нет кардиостимулятора",
    "нет судорог",
    "эпилепсии у меня нет",
    "онкологии у меня нет",
    "кардиостимулятора нету",
    "тромбоза нету",
])
def test_denied_contraindication_does_not_stop_booking(text: str) -> None:
    assert dialog._contra_has_hard_stop(text) is False, f"{text!r}: ложный отказ пациенту без противопоказания"


@pytest.mark.parametrize("text", [
    "у меня эпилепсия",
    "есть кардиостимулятор",
    "я беременна",
    "у меня онкология",
    "есть тромбоз",
    "кардиостимулятор",
    "эпилепсия",
    "беременность есть",
    "у меня судороги",
])
def test_confirmed_contraindication_still_stops_booking(text: str) -> None:
    assert dialog._contra_has_hard_stop(text) is True, f"{text!r}: противопоказание пропущено"


@pytest.mark.parametrize("text", [
    "эпилепсии нет, но есть кардиостимулятор",
    "онкологии нет, беременность есть",
    "нет эпилепсии, но у меня тромбоз",
])
def test_denial_of_one_does_not_hide_confirmation_of_another(text: str) -> None:
    """Отрицание одного пункта не должно прятать подтверждение другого."""
    assert dialog._contra_has_hard_stop(text) is True, f"{text!r}: подтверждённое противопоказание пропущено"


@pytest.mark.parametrize("text", [
    "эпилепсии нет и онкологии нет",
    "кардиостимулятора нет, тромбоза нет",
    "ничего из этого нет",
    "нет",
])
def test_multiple_denials_do_not_stop_booking(text: str) -> None:
    assert dialog._contra_has_hard_stop(text) is False, f"{text!r}: ложный отказ"


@pytest.mark.parametrize("text", [
    "что такое эпилепсия?",
    "что значит кохлеарный имплант",
    "что это тромбоз",
])
def test_term_questions_are_not_hard_stops(text: str) -> None:
    """Защищённое поведение: вопрос о термине — не подтверждение противопоказания."""
    assert dialog._contra_has_hard_stop(text) is False

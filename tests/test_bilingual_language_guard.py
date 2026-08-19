"""Regression tests for the RU/KK language guard.

Covers the audit findings:
  - substring matching turned Russian "Колено" into a Kazakh dialog;
  - pure Kazakh sentences without a Russian-absent marker were read as Russian;
  - mixed RU/KK messages had no representation at all;
  - the outgoing answer was never checked against the patient's language.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from language_guard import (  # noqa: E402
    LANG_KK,
    LANG_MIXED,
    LANG_RU,
    LANG_UNKNOWN,
    analyze_language,
    detect_language,
    explicit_language_request,
    response_language_violation,
)


# ---------------------------------------------------------------- RU input


@pytest.mark.parametrize(
    "text",
    [
        "Болит поясница, можно к вам?",
        "Здравствуйте",
        "кароч болит спина че делать",
        "скок стоит",
        "ну вроде нет",
        "завтра можно часиков в 5",
        "Мне 44",
        "ага",
        "неа",
    ],
)
def test_russian_messages_are_detected_as_russian(text: str) -> None:
    analysis = analyze_language(text, LANG_RU)
    assert analysis.detected_language == LANG_RU
    assert analysis.preferred_response_language == LANG_RU


@pytest.mark.parametrize("text", ["Колено", "колено ноет", "около недели", "Плечо и колено"])
def test_russian_body_words_are_not_mistaken_for_kazakh(text: str) -> None:
    """Regression: substring "кол" used to match Kazakh «қол» inside «колено».

    A Russian patient complaining about a knee was switched to Kazakh for the
    whole conversation.
    """
    analysis = analyze_language(text, LANG_RU)
    assert analysis.detected_language == LANG_RU
    assert analysis.preferred_response_language == LANG_RU


# ---------------------------------------------------------------- KK input


@pytest.mark.parametrize(
    "text",
    [
        "Белім ауырып жүр, сіздерге баруға бола ма?",
        "Ал консультация қанша тұрады?",
        "Сәлеметсіз бе",
        "Ертең бос уақыт бар ма?",
        "Жасыңыз нешеде",
        "Қай күн ыңғайлы?",
    ],
)
def test_kazakh_messages_are_detected_as_kazakh(text: str) -> None:
    analysis = analyze_language(text, LANG_RU)
    assert analysis.detected_language == LANG_KK
    assert analysis.preferred_response_language == LANG_KK


@pytest.mark.parametrize("text", ["Жасым 44", "Ия", "Иә", "Жоқ", "Жок", "Рахмет", "Жарайды"])
def test_short_kazakh_answers_are_detected_without_unique_letters(text: str) -> None:
    """«Жасым 44» / «Ия» carry no unique Kazakh letters but are still Kazakh."""
    analysis = analyze_language(text, LANG_RU)
    assert analysis.preferred_response_language == LANG_KK


@pytest.mark.parametrize("text", ["salem", "rahmet", "joq", "belim aurady"])
def test_kazakh_translit_is_detected(text: str) -> None:
    assert detect_language(text, LANG_RU) == LANG_KK


# ---------------------------------------------------------------- mixed


@pytest.mark.parametrize(
    "text",
    [
        "Белім ауырады, врач сегодня принимает?",
        "завтра бос уақыт бар ма?",
    ],
)
def test_mixed_messages_are_reported_as_mixed(text: str) -> None:
    analysis = analyze_language(text, LANG_RU)
    assert analysis.detected_language == LANG_MIXED
    # A mixed message still has to produce exactly one reply language.
    assert analysis.preferred_response_language in (LANG_RU, LANG_KK)


def test_mixed_message_answers_in_the_dominant_language() -> None:
    analysis = analyze_language("Белім ауырып жүр, сіздерге баруға бола ма, сколько стоит?", LANG_RU)
    assert analysis.detected_language == LANG_MIXED
    assert analysis.preferred_response_language == LANG_KK


def test_loanwords_shared_by_both_languages_do_not_force_russian() -> None:
    """«консультация» is used in Kazakh too and must not outvote Kazakh words."""
    analysis = analyze_language("консультация қанша тұрады?", LANG_RU)
    assert analysis.preferred_response_language == LANG_KK


# ---------------------------------------------------------------- explicit


@pytest.mark.parametrize("text", ["қазақша жазыңыз", "можно на казахском", "казакша жазыныз"])
def test_explicit_kazakh_request_wins(text: str) -> None:
    assert explicit_language_request(text) == LANG_KK
    analysis = analyze_language(text, LANG_RU)
    assert analysis.preferred_response_language == LANG_KK
    assert analysis.confidence == 1.0


@pytest.mark.parametrize("text", ["можно на русском", "пишите на русском", "орысша жазыңыз"])
def test_explicit_russian_request_wins(text: str) -> None:
    assert explicit_language_request(text) == LANG_RU
    analysis = analyze_language(text, LANG_KK)
    assert analysis.preferred_response_language == LANG_RU


# ---------------------------------------------------------------- unknown


@pytest.mark.parametrize("text", ["", "   ", "44", "+", "???", "🌿"])
def test_signal_free_messages_keep_the_current_language(text: str) -> None:
    assert analyze_language(text, LANG_KK).preferred_response_language == LANG_KK
    assert analyze_language(text, LANG_RU).preferred_response_language == LANG_RU


def test_links_phones_and_brands_are_not_a_language_signal() -> None:
    text = "Neuro Balance https://2gis.kz/astana +7 777 067 93 80"
    analysis = analyze_language(text, LANG_KK)
    assert analysis.detected_language == LANG_UNKNOWN
    assert analysis.preferred_response_language == LANG_KK


# ---------------------------------------------------- outgoing answer check


def test_correct_language_answers_are_not_flagged() -> None:
    ru = "Здравствуйте! Да, можно записаться на консультацию по акции 🌿\nПодскажите, что Вас беспокоит?"
    kk = "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿\nАйтыңызшы, Сізді не мазалайды?"
    assert response_language_violation(ru, LANG_RU) == ""
    assert response_language_violation(kk, LANG_KK) == ""


def test_answer_in_the_wrong_language_is_flagged() -> None:
    ru = "Здравствуйте! Да, можно записаться на консультацию по акции 🌿"
    kk = "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿"
    assert response_language_violation(ru, LANG_KK) == "wrong_language_response"
    assert response_language_violation(kk, LANG_RU) == "wrong_language_response"


def test_russian_template_with_kazakh_tail_is_flagged_as_mixed() -> None:
    """The exact shape response_guard used to emit for Kazakh patients."""
    answer = "Приём в нашей клинике — 5 000 тг.\n\nАйтыңызшы, Сізді не мазалайды?"
    assert response_language_violation(answer, LANG_KK) == "mixed_language_response"


def test_address_and_links_do_not_break_a_kazakh_answer() -> None:
    answer = (
        "Мекенжай: Астана, Қабанбай батыр 28, ішкі аула, 3-подъезд.\n"
        "👉 2GIS: https://2gis.kz/astana/firm/70000001105992248"
    )
    assert response_language_violation(answer, LANG_KK) == ""


def test_empty_answer_is_never_a_violation() -> None:
    assert response_language_violation("", LANG_RU) == ""
    assert response_language_violation("   ", LANG_KK) == ""

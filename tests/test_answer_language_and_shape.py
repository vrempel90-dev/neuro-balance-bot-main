"""Регрессия: язык финального ответа и сохранение вопроса сценария.

Две находки аудита:

1. ``ai._humanize_guard_ok`` не проверял язык вообще. Проверки искали вопрос
   про возраст/имя/дату сразу на двух языках, поэтому перевод русского
   ответа на казахский (и наоборот) проходил как корректный, и пациент
   получал ответ не на своём языке.

2. ``strict_prompt_guard._limit_length`` резал ответ до двух предложений,
   если он не попал в жёсткий список маркеров. Ответ в стиле answer-first
   («ответили на вопрос → вернулись к шагу») терял последний вопрос, и
   воронка вставала: бот отвечал на FAQ и больше ничего не спрашивал.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ai  # noqa: E402
import strict_prompt_guard  # noqa: E402


BASE_RU = "Понимаю Вас. С такой жалобой можно прийти на консультацию.\nПодскажите, пожалуйста, сколько Вам лет?"
BASE_KK = "Түсінемін. Мұндай шағыммен консультацияға келуге болады.\nЖасыңыз нешеде?"


# ------------------------------------------------------- humanize и язык


def test_humanize_rejects_russian_answer_translated_to_kazakh() -> None:
    assert ai._humanize_guard_ok(BASE_RU, BASE_KK) is False


def test_humanize_rejects_kazakh_answer_translated_to_russian() -> None:
    assert ai._humanize_guard_ok(BASE_KK, BASE_RU) is False


def test_humanize_rejects_mixed_language_rewrite() -> None:
    mixed = "Понимаю Вас. С такой жалобой можно прийти на консультацию.\nЖасыңыз нешеде?"
    assert ai._humanize_guard_ok(BASE_RU, mixed) is False


def test_humanize_accepts_same_language_rewrite_ru() -> None:
    livelier = "Понимаю Вас 🌿 С такой жалобой к нам можно прийти на консультацию.\nПодскажите, сколько Вам лет?"
    assert ai._humanize_guard_ok(BASE_RU, livelier) is True


def test_humanize_accepts_same_language_rewrite_kk() -> None:
    livelier = "Түсінемін 🌿 Мұндай шағыммен консультацияға келуге болады.\nЖасыңыз нешеде?"
    assert ai._humanize_guard_ok(BASE_KK, livelier) is True


def test_humanize_prompt_states_the_language_rule() -> None:
    prompt = ai.STRICT_HUMANIZE_REPLY_PROMPT.lower()
    assert "язык" in prompt
    assert "казахск" in prompt


def test_language_neutral_base_answer_is_not_blocked() -> None:
    """Если в базовом ответе нет языкового сигнала, блокировать нечего."""
    assert ai._humanize_language_ok("5 000 тг 🌿", "5 000 тг 🌿") is True


# ---------------------------------------------- answer-first не теряет вопрос


@pytest.mark.parametrize(
    "answer",
    [
        "Да, конечно. Процедура занимает около 30 минут. Врач осмотрит и составит план. Когда Вам удобно подойти?",
        "Иә, әрине. Процедура шамамен 30 минут алады. Дәрігер қарап, жоспар құрады. Қай күні ыңғайлы?",
        "Рахмет. Біздің клиника Астанада орналасқан. Дәрігерлер жұмыс істейді. Сізді не мазалайды?",
        "Понятно. Мы работаем без выходных. Всё уточним на месте. Что именно Вас беспокоит?",
    ],
)
def test_trailing_scenario_question_survives_length_trim(answer: str) -> None:
    out = strict_prompt_guard.enforce_prompt_only(answer, {"language": "ru"})
    assert out.rstrip().endswith("?"), f"вопрос сценария потерян: {out!r}"
    assert answer.rstrip().split(". ")[-1].strip() in out


def test_long_answer_without_question_is_still_trimmed() -> None:
    answer = "Понятно. Спасибо за информацию. Мы всё записали. Всё в порядке."
    out = strict_prompt_guard.enforce_prompt_only(answer, {"language": "ru"})
    assert out == "Понятно. Спасибо за информацию."


def test_short_answers_are_untouched() -> None:
    answer = "Хорошо 🌿 Подскажите, сколько Вам лет?"
    assert strict_prompt_guard.enforce_prompt_only(answer, {"language": "ru"}) == answer


def test_allowlisted_long_answers_are_not_trimmed() -> None:
    answer = (
        "Перед записью мне нужно уточнить несколько моментов. "
        "Есть ли у Вас кардиостимулятор? Онкология? Беременность? "
        "Эпилепсия? Ответьте, пожалуйста, по каждому пункту."
    )
    assert strict_prompt_guard.enforce_prompt_only(answer, {"language": "ru"}) == answer

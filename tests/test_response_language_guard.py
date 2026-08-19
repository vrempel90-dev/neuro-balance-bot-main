"""Регрессия: язык финального ответа проверяется перед отправкой в Wazzup.

Находка аудита: response_guard.validate_answer в проде вообще не вызывался —
это мёртвый код, доступный только из тестов. Единственная реальная точка
перед отправкой пациенту — main._guard_answer, и языковой проверки в ней
не было. Казахоязычный пациент мог получить полностью русский ответ, и это
никем не фиксировалось.

Правило исправления жёсткое: переводить текст на лету нельзя. Разрешена
только подстановка заранее утверждённого казахского шаблона клиники.
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

import clinic_info  # noqa: E402
import main  # noqa: E402
import state  # noqa: E402
from language_guard import enforce_response_language, response_language_violation  # noqa: E402

state.init_db()


def make_session(chat_id: str, language: str) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session["language"] = language
    state.save_session(chat_id, session)


# ------------------------------------------------- enforce_response_language


def test_russian_clinic_template_is_swapped_for_the_kazakh_one() -> None:
    answer, violation = enforce_response_language(clinic_info.CLINIC_INFO_TEMPLATES["price_first_visit"], "kk")
    assert answer == clinic_info.CLINIC_INFO_TEMPLATES_KK["price_first_visit"]
    assert violation.endswith("_repaired_from_template")


def test_russian_template_with_kazakh_tail_is_fully_repaired() -> None:
    mixed = clinic_info.CLINIC_INFO_TEMPLATES["address"] + "\n\nАйтыңызшы, Сізді не мазалайды?"
    answer, violation = enforce_response_language(mixed, "kk")
    assert violation.startswith("mixed_language_response")
    assert response_language_violation(answer, "kk") == ""
    # Ссылка и номер дома пережили замену.
    assert "2gis.kz" in answer
    assert "28" in answer


def test_correct_language_answer_is_returned_untouched() -> None:
    ok = "Сәлеметсіз бе! Иә, жазылуға болады 🌿"
    assert enforce_response_language(ok, "kk") == (ok, "")
    ru = "Здравствуйте! Да, можно записаться 🌿"
    assert enforce_response_language(ru, "ru") == (ru, "")


def test_answer_without_approved_equivalent_is_kept_and_only_reported() -> None:
    """Выдумывать казахский текст нельзя — факт важнее языка."""
    text = "Свободного времени на эту дату нет."
    answer, violation = enforce_response_language(text, "kk")
    assert answer == text
    assert violation == "wrong_language_response"


def test_unknown_expected_language_is_a_no_op() -> None:
    text = "Здравствуйте!"
    assert enforce_response_language(text, "") == (text, "")
    assert enforce_response_language(text, "en") == (text, "")


def test_empty_answer_is_a_no_op() -> None:
    assert enforce_response_language("", "kk") == ("", "")


# --------------------------------------------------- production choke point


def test_guard_answer_repairs_russian_template_for_kazakh_patient() -> None:
    chat_id = "lang-guard-kk"
    make_session(chat_id, "kk")
    out = main._guard_answer(chat_id, clinic_info.CLINIC_INFO_TEMPLATES["price_first_visit"])
    assert response_language_violation(out, "kk") == ""
    assert state.get_session(chat_id)["language_guard_result"].endswith("_repaired_from_template")


def test_guard_answer_keeps_correct_russian_answer_for_russian_patient() -> None:
    chat_id = "lang-guard-ru"
    make_session(chat_id, "ru")
    answer = "Здравствуйте! Да, можно записаться на консультацию по акции 🌿"
    assert main._guard_answer(chat_id, answer) == answer
    assert state.get_session(chat_id)["language_guard_result"] == "ok"


def test_guard_answer_records_violation_when_it_cannot_repair() -> None:
    chat_id = "lang-guard-report"
    make_session(chat_id, "kk")
    answer = "Свободного времени на эту дату нет."
    assert main._guard_answer(chat_id, answer) == answer
    assert state.get_session(chat_id)["language_guard_result"] == "wrong_language_response"


@pytest.mark.parametrize("language", ["ru", "kk"])
def test_guard_answer_never_breaks_links_phones_or_brands(language: str) -> None:
    chat_id = f"lang-guard-protected-{language}"
    make_session(chat_id, language)
    answer = (
        "Neuro Balance\n"
        "https://2gis.kz/astana/firm/70000001105992248\n"
        "8 777 067 93 80 — Асем"
    )
    out = main._guard_answer(chat_id, answer)
    assert "Neuro Balance" in out
    assert "https://2gis.kz/astana/firm/70000001105992248" in out
    assert "8 777 067 93 80" in out
    assert "Асем" in out


def test_guard_answer_without_session_language_is_a_no_op() -> None:
    chat_id = "lang-guard-nolang"
    state.reset_session(chat_id)
    answer = "Здравствуйте! Чем могу помочь?"
    assert main._guard_answer(chat_id, answer) == answer

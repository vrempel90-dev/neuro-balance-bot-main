"""Регрессия: guard'ы галлюцинаций работают и на казахском.

Находка аудита: критичные проверки сопоставляли только русские слова, а
_low (через _normalize_typos) приводит казахские буквы к русским
(«қарсы» -> «карсы», «уақыт» -> «уакыт»). Из-за этого:

- «Қай дәрігер қабылдайды?» проходило мимо guard'а выдуманных врачей;
- казахские формулировки выдуманных слотов не ловились;
- условие `"қарсы" not in low_reply` было мёртвым кодом, и корректный
  казахский ответ про противопоказания блокировался как отсутствие
  обязательного вопроса.
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


def decision(reply: str, action: str = "answer_faq_and_continue", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": action,
        "needs_python_tool": "none",
        "reply": reply,
        "extracted": {},
        "safety": {},
        "safety_flags": {},
    }
    base.update(over)
    return base


BASE_SESSION: dict[str, Any] = {"step": "complaint", "last_slots": []}


# ------------------------------------------------------------ _kkl хелпер


def test_kkl_normalizes_kazakh_letters_like_low_does() -> None:
    assert dialog._kkl("қарсы") == ("карсы",)
    assert dialog._kkl("уақыт", "жүреді") == ("уакыт", "журеди")


def test_kkl_phrases_actually_match_normalized_text() -> None:
    """Смысл хелпера: литерал обязан совпасть с _low(сообщение)."""
    for phrase in ("қарсы көрсетілім", "бос уақыт бар", "дәрігер"):
        assert dialog._kkl(phrase)[0] in dialog._low(f"Сәлеметсіз бе, {phrase} ме?")


# ------------------------------------------------------- выдуманный врач


@pytest.mark.parametrize(
    "user_text, reply",
    [
        ("Какой врач принимает?", "Вас примет врач Иванов"),
        ("Кто принимает?", "Принимает доктор Петров"),
        ("Қай дәрігер қабылдайды?", "Сізді дәрігер Иванов қабылдайды"),
        ("Қандай маман бар?", "Бізде тәжірибелі маман бар"),
    ],
)
def test_doctor_hallucination_is_blocked_in_both_languages(user_text: str, reply: str) -> None:
    allowed, reason = dialog.validate_openai_dialog_decision(decision(reply), dict(BASE_SESSION), user_text)
    assert allowed is False
    assert reason == "doctor_hallucination"


# ------------------------------------------------------- выдуманные слоты


@pytest.mark.parametrize(
    "reply",
    [
        "Есть свободные слоты: 10:00, 11:00",
        "Показываю свободные слоты, выберите подходящий",
        "Ертең бос уақыт бар, ыңғайлысын таңдаңыз",
        "Мына уақыттар бар, бірін таңдаңыз",
    ],
)
def test_slot_hallucination_without_crm_is_blocked_in_both_languages(reply: str) -> None:
    allowed, reason = dialog.validate_openai_dialog_decision(decision(reply), dict(BASE_SESSION), "когда можно?")
    assert allowed is False
    assert reason == "slot_hallucination"


# ---------------------------------------------------- длительность процедуры


@pytest.mark.parametrize(
    "reply",
    [
        "Процедура длится 30 минут",
        "Процедура 30 минут созылады",
    ],
)
def test_duration_hallucination_is_blocked_in_both_languages(reply: str) -> None:
    allowed, reason = dialog.validate_openai_dialog_decision(decision(reply), dict(BASE_SESSION), "сколько идёт?")
    assert allowed is False
    assert reason == "duration_hallucination"


# ------------------------------------- казахский вопрос про противопоказания


def test_valid_kazakh_contraindications_question_is_not_blocked() -> None:
    """Regression: `"қарсы" not in low_reply` был мёртвым кодом.

    Корректный казахский ответ на шаге противопоказаний отклонялся как
    contra_question_missing, потому что русского слова «противопоказ» в нём
    нет, а казахский литерал никогда не совпадал.
    """
    session = {"step": "contraindications", "contraindications_ok": None, "last_slots": []}
    allowed, reason = dialog.validate_openai_dialog_decision(
        decision("Жазбас бұрын нақтылайын: Сізде қарсы көрсетілімдер бар ма?", "ask_contraindications"),
        session,
        "жарайды",
    )
    assert allowed is True, f"казахский вопрос про противопоказания заблокирован: {reason}"


def test_russian_contraindications_question_still_passes() -> None:
    session = {"step": "contraindications", "contraindications_ok": None, "last_slots": []}
    allowed, _ = dialog.validate_openai_dialog_decision(
        decision("Перед записью уточню: есть ли у Вас противопоказания?", "ask_contraindications"),
        session,
        "хорошо",
    )
    assert allowed is True


def test_reply_without_any_contraindications_question_is_still_blocked() -> None:
    session = {"step": "contraindications", "contraindications_ok": None, "last_slots": []}
    allowed, reason = dialog.validate_openai_dialog_decision(
        decision("Хорошо, записываю Вас.", "ask_contraindications"), session, "хорошо"
    )
    assert allowed is False
    assert reason == "contra_question_missing"

"""Многошаговые регрессии на русском и казахском.

Это не unit-тесты отдельных функций, а полные цепочки
пациент → бот → пациент → бот через handle_message.

Покрывают находки аудита:

- FAQ посреди воронки не должен ломать шаг: бот отвечает на вопрос и
  возвращается к незавершённому шагу (answer-first);
- казахоязычный пациент должен получать казахские тексты, а не русские;
- пациент может перейти на другой язык посреди разговора, и бот обязан
  перейти вместе с ним;
- короткие подтверждения язык диалога не переключают;
- явная просьба о языке фиксирует его.

CRM здесь замокан: подключаться к боевому CRM из тестов нельзя.
"""

from __future__ import annotations

import asyncio
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

import crm  # noqa: E402
import state  # noqa: E402
from dialog import handle_message  # noqa: E402
from language_guard import response_language_violation  # noqa: E402

state.init_db()

PHONE = "77011234567"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _offline_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Новый пациент без активных записей — без обращения к боевому CRM."""

    async def fake_lookup(phone: str) -> dict[str, Any]:
        return {
            "ok": True, "status_code": 200, "phone_raw": phone,
            "phone_normalized": phone, "phone_variants": [phone],
            "endpoint_url": "test", "query_params": {},
            "appointments_raw_count": 0, "appointments": [], "appointment": None,
            "appointments_cache": [], "active_count": 0, "raw": {"found": False},
        }

    async def fake_patient_lookup(phone: str) -> dict[str, Any]:
        return {"found": False}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(crm, "patient_lookup", fake_patient_lookup)


class Dialog:
    """Один разговор: отправляет сообщения и хранит ответы бота."""

    def __init__(self, chat_id: str, **preset: Any) -> None:
        self.chat_id = chat_id
        state.reset_session(chat_id)
        session = state.get_session(chat_id)
        session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead"})
        session.update(preset)
        state.save_session(chat_id, session)

    def say(self, text: str) -> str:
        return run(handle_message(self.chat_id, PHONE, text)) or ""

    @property
    def session(self) -> dict[str, Any]:
        return state.get_session(self.chat_id)

    @property
    def language(self) -> str:
        return str(self.session.get("language") or "")

    @property
    def step(self) -> str:
        return str(self.session.get("step") or "")


def assert_language(answer: str, expected: str) -> None:
    violation = response_language_violation(answer, expected)
    assert violation == "", f"ожидался {expected}, нарушение {violation}: {answer[:160]!r}"


# ============================================================ русская цепочка


def test_russian_complaint_faq_age_chain() -> None:
    """жалоба → вопрос о цене посреди воронки → возраст."""
    chat = Dialog("mt-ru-chain", step="complaint")

    first = chat.say("Болит спина")
    assert chat.step == "age"
    assert "лет" in first.lower()
    assert_language(first, "ru")

    # FAQ не должен ломать шаг: отвечаем на цену и возвращаемся к возрасту.
    price = chat.say("А сколько стоит приём?")
    assert "5 000" in price
    assert chat.step == "age", "FAQ сбил шаг воронки"
    assert "лет" in price.lower(), "бот не вернулся к незавершённому шагу"
    assert_language(price, "ru")

    contra = chat.say("Мне 42")
    assert chat.session["age"] == 42
    assert chat.step == "contraindications"
    assert "противопоказ" in contra.lower()
    assert_language(contra, "ru")


# ========================================================== казахская цепочка


def test_kazakh_complaint_faq_age_chain() -> None:
    """Та же цепочка целиком на казахском, без русских вставок."""
    chat = Dialog("mt-kk-chain", step="complaint")

    first = chat.say("Белім ауырады")
    assert chat.language == "kk"
    assert chat.step == "age"
    assert_language(first, "kk")

    price = chat.say("Ал консультация қанша тұрады?")
    assert "5 000" in price
    assert chat.step == "age", "FAQ сбил шаг воронки"
    assert_language(price, "kk")

    contra = chat.say("Жасым 42")
    assert chat.session["age"] == 42
    assert chat.step == "contraindications"
    assert_language(contra, "kk")
    # Чек-лист противопоказаний должен быть казахским, а не русским.
    assert "кардиостимулятор" in contra.lower()
    assert "қарсы көрсетілім" in contra.lower()


# ==================================================== переключение языка


def test_patient_switches_to_kazakh_mid_funnel() -> None:
    chat = Dialog("mt-switch-kk", step="complaint")
    chat.say("Болит спина")
    chat.say("Мне 42")
    assert chat.language == "ru"

    answer = chat.say("Жоқ, ондай нәрсе жоқ. Мекенжайыңыз қайда?")
    assert chat.language == "kk", "бот не пошёл за пациентом на казахский"
    assert_language(answer, "kk")
    # Адрес — факт из базы клиники, ссылка и номер дома не переводятся.
    assert "28" in answer
    assert "2gis.kz" in answer.lower()


def test_patient_switches_to_russian_mid_funnel() -> None:
    chat = Dialog("mt-switch-ru", step="complaint")
    chat.say("Белім ауырады")
    chat.say("Жасым 42")
    assert chat.language == "kk"

    answer = chat.say("Нет, ничего такого нет. А какой у вас адрес?")
    assert chat.language == "ru", "бот не пошёл за пациентом на русский"
    assert_language(answer, "ru")


@pytest.mark.parametrize("short_answer", ["жоқ", "иә", "рахмет"])
def test_short_kazakh_answers_do_not_switch_a_russian_dialog(short_answer: str) -> None:
    chat = Dialog(f"mt-short-{short_answer}", step="complaint")
    chat.say("Болит спина")
    chat.say("Мне 42")
    chat.say(short_answer)
    assert chat.language == "ru"


@pytest.mark.parametrize("short_answer", ["да", "нет", "спасибо"])
def test_short_russian_answers_do_not_switch_a_kazakh_dialog(short_answer: str) -> None:
    chat = Dialog(f"mt-short-kk-{short_answer}", step="complaint")
    chat.say("Белім ауырады")
    chat.say("Жасым 42")
    chat.say(short_answer)
    assert chat.language == "kk"


def test_explicit_language_request_sticks_across_turns() -> None:
    chat = Dialog("mt-explicit", step="complaint")
    chat.say("Болит спина")

    switched = chat.say("қазақша жазыңыз")
    assert chat.language == "kk"
    assert_language(switched, "kk")

    # После явной просьбы русская реплика язык не возвращает.
    answer = chat.say("Мне 42")
    assert chat.language == "kk"
    assert_language(answer, "kk")


# ================================================= разговорная речь и опечатки


def test_colloquial_russian_is_understood() -> None:
    chat = Dialog("mt-colloquial-ru", step="complaint")
    answer = chat.say("кароч болит спина че делать")
    assert chat.step == "age"
    assert chat.language == "ru"
    assert_language(answer, "ru")


def test_russian_typo_complaint_is_understood() -> None:
    chat = Dialog("mt-typo-ru", step="complaint")
    chat.say("поясница бкспокоит")
    assert chat.step == "age"


def test_mixed_language_message_does_not_flip_the_dialog() -> None:
    chat = Dialog("mt-mixed", step="complaint")
    chat.say("Болит спина")
    before = chat.language
    chat.say("Белім ауырады, врач сегодня принимает?")
    assert chat.language == before, "смешанное сообщение переключило язык"

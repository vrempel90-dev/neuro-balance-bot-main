"""Regression: на шаге contraindications бот не молчит на текстовые сообщения.

Прод-баг 17.08.2026 (Railway, chat_id 77478875259, канал WhatsApp, ТЕКСТ):
пациент на шаге "contraindications" написал "Все равно болеет", затем "81 лет" —
бот не ответил ни разу (answer_empty: true, humanize_skipped reason=locked_template).

Причина: duplicate_answer_guard в dialog._finalize возвращал "" раньше, чем
отрабатывал final_no_empty_guard. Ответ шага совпадал с прошлым вопросом о
противопоказаниях, и вместо повторного вопроса пациент получал тишину.

Здесь же закреплено, что старое поведение сохранено там, где оно должно
сохраниться: голосовые по-прежнему без ответа, а duplicate_answer_guard
продолжает глушить дубликаты на остальных шагах.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import crm
import dialog
import state
from dialog import handle_message


def setup_function():
    state.init_db()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _crm_new_patient(monkeypatch: pytest.MonkeyPatch):
    """CRM отвечает "новый пациент" — тест не ходит в сеть."""

    async def fake_lookup(phone):
        return {
            "ok": True,
            "found": False,
            "isNew": True,
            "patient": None,
            "lead": None,
            "lastAppointment": None,
            "hasActiveAppointment": False,
            "appointment": None,
            "appointments": [],
        }

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def _seed_contraindications_step(chat_id: str) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update(
        {
            "step": "contraindications",
            "complaint": "болит спина",
            "ai_lead_started": True,
            "first_touch_info_sent": True,
        }
    )
    state.save_session(chat_id, session)


def test_production_case_77478875259_never_silent() -> None:
    """Точный сценарий из прод-логов: оба текстовых сообщения получают ответ."""
    chat_id = "contra_never_silent_prod_case"
    _seed_contraindications_step(chat_id)

    first = run(handle_message(chat_id, "77478875259", "Все равно болеет"))
    second = run(handle_message(chat_id, "77478875259", "81 лет"))

    assert first.strip(), "первое текстовое сообщение осталось без ответа"
    assert second.strip(), "второе текстовое сообщение осталось без ответа (прод-баг)"

    session = state.get_session(chat_id)
    assert session.get("outbound_duplicate_guard_blocked") is not True
    assert dialog.CONTRAINDICATIONS_MESSAGE_RU in second


def test_debounced_combined_text_never_silent() -> None:
    """Дебаунс-воркер склеивает быстрые сообщения в один текст — ответ обязателен."""
    chat_id = "contra_never_silent_debounced"
    _seed_contraindications_step(chat_id)

    run(handle_message(chat_id, "77478875259", "Все равно болеет"))
    combined = run(handle_message(chat_id, "77478875259", "Все равно болеет\n81 лет"))

    assert combined.strip(), "склеенное дебаунсом сообщение осталось без ответа"


@pytest.mark.parametrize(
    "user_text",
    [
        "Все равно болеет",
        "81 лет",
        "мне 81 лет",
        "не знаю",
        "а",
        "?",
        "Все равно болеет",
        "ничего нет",
        "хорошо",
        "ок",
        "что?",
        "🙂",
        "asdfgh",
        "Все равно болеет",
    ],
)
def test_no_text_input_leaves_contraindications_step_silent(user_text: str) -> None:
    """Ни один текстовый ввод на шаге contraindications не даёт пустой ответ."""
    chat_id = f"contra_never_silent_{abs(hash(user_text))}"
    _seed_contraindications_step(chat_id)

    # Первый прогон задаёт вопрос о противопоказаниях, второй раньше молчал
    # из-за duplicate_answer_guard.
    run(handle_message(chat_id, "77478875259", "Все равно болеет"))
    answer = run(handle_message(chat_id, "77478875259", user_text))

    assert answer.strip(), f"пустой ответ на текст {user_text!r} на шаге contraindications"


def test_repeated_unclear_answers_never_go_silent_while_step_is_contraindications() -> None:
    """Пока шаг остаётся contraindications и сессия не эскалирована — ответ есть всегда.

    Дальше включается штатная эскалация на оператора (защищённое состояние), и
    молчание там ожидаемо — этот сценарий закреплён отдельным тестом ниже.
    """
    chat_id = "contra_never_silent_streak"
    _seed_contraindications_step(chat_id)

    for _ in range(5):
        session = state.get_session(chat_id)
        if str(session.get("step") or "") != "contraindications" or session.get("escalated"):
            break
        answer = run(handle_message(chat_id, "77478875259", "Все равно болеет"))
        assert answer.strip(), "бот замолчал на повторяющемся непонятном ответе"


def test_escalated_session_still_silent() -> None:
    """Защищённое поведение: после эскалации на оператора AI молчит — не регрессия."""
    chat_id = "contra_escalated_still_silent"
    _seed_contraindications_step(chat_id)
    session = state.get_session(chat_id)
    session.update({"step": "escalated", "escalated": True})
    state.save_session(chat_id, session)

    assert run(handle_message(chat_id, "77478875259", "Все равно болеет")) == ""


def test_voice_message_still_gets_no_reply() -> None:
    """Защищённое поведение: голосовые остаются без ответа — это не регрессия."""
    chat_id = "contra_voice_still_silent"
    _seed_contraindications_step(chat_id)
    session = state.get_session(chat_id)
    session["last_ignored_message_type"] = "voice"
    state.save_session(chat_id, session)

    session = state.get_session(chat_id)
    assert dialog._openai_brain_skip_reason(session, "Все равно болеет") == "voice_or_audio"


def test_duplicate_guard_still_silences_other_steps() -> None:
    """Старое поведение duplicate_answer_guard вне contraindications сохранено."""
    chat_id = "contra_dup_guard_other_step"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update(
        {
            "step": "name",
            "complaint": "болит спина",
            "age": 40,
            "contraindications_ok": True,
            "ai_lead_started": True,
            "first_touch_info_sent": True,
            "last_assistant_answer": "Как Вас зовут? 🌿",
        }
    )
    state.save_session(chat_id, session)

    answer = dialog._finalize(chat_id, session, "Как Вас зовут? 🌿")

    assert answer == ""
    assert state.get_session(chat_id).get("outbound_duplicate_guard_blocked") is True

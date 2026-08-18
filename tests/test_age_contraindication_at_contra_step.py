"""Возраст как противопоказание на шаге contraindications.

Прод 17.08.2026 (Railway, chat_id 77478875259, WhatsApp, текст): пациент на шаге
"contraindications" написал "81 лет". Возраст не сохранялся (age оставался 0), и
правило клиники "возраст ... более 75 лет" не срабатывало, хотя то же сообщение
на шаге age корректно останавливает запись. Бот просто переспрашивал про
противопоказания.

Правило over_75 теперь применяется и на шаге contraindications.

under_16 на этом шаге намеренно НЕ применяется: пациенты описывают здесь анамнез
длительностями ("инсульт был 5 лет назад", "болею 3 года"), из которых
_extract_age достаёт 5 и 3. Под "младше 16" такие сообщения попадали бы ошибочно,
а это жёсткий отказ в приёме — ошибка хуже той, которую чиним.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import crm
import dialog
import state
from dialog import handle_message


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _crm_new_patient(monkeypatch: pytest.MonkeyPatch):
    state.init_db()

    async def fake_lookup(phone):
        return {"ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
                "lastAppointment": None, "hasActiveAppointment": False, "appointment": None,
                "appointments": []}
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def _seed_contra(chat_id: str, extra: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({
        "ai_lead_started": True,
        "gate_reason": "active_ai_lead",
        "first_touch_info_sent": True,
        "complaint": "болит спина",
        "step": "contraindications",
    })
    if extra:
        session.update(extra)
    state.save_session(chat_id, session)


def _stopped(session: dict[str, Any]) -> bool:
    return bool(session.get("escalated")) or str(session.get("step") or "") in {"escalated", "stopped"}


# --- прод-кейс -------------------------------------------------------------


def test_production_case_81_years_at_contra_step_hard_stops() -> None:
    """Точный сценарий из прод-логов: 81 год останавливает запись."""
    chat_id = "age_contra_prod_case"
    _seed_contra(chat_id)

    run(handle_message(chat_id, "77478875259", "Все равно болеет"))
    answer = run(handle_message(chat_id, "77478875259", "81 лет"))
    session = state.get_session(chat_id)

    assert "старше 75" in answer
    assert session["age"] == 81
    assert session["contraindications_ok"] is False
    assert session["contraindications_verdict"] == "admin_contact"
    assert _stopped(session)


@pytest.mark.parametrize("text,age", [
    ("81 лет", 81),
    ("мне 81", 81),
    ("81 год", 81),
    ("мне 90 лет", 90),
    ("76 лет", 76),
    ("мама 80 лет", 80),
])
def test_over_75_stops_at_contra_step(text: str, age: int) -> None:
    chat_id = f"age_contra_stop_{abs(hash(text))}"
    _seed_contra(chat_id)

    answer = run(handle_message(chat_id, "77478875259", text))
    session = state.get_session(chat_id)

    assert "старше 75" in answer, f"{text!r}: правило >75 не сработало"
    assert session["age"] == age
    assert _stopped(session)


def test_contra_step_matches_age_step_behavior() -> None:
    """На обоих шагах одно и то же сообщение даёт одинаковый вердикт."""
    _seed_contra("age_contra_parity_contra")
    _seed_contra("age_contra_parity_age", {"step": "age"})

    from_contra = run(handle_message("age_contra_parity_contra", "77478875259", "81 лет"))
    from_age = run(handle_message("age_contra_parity_age", "77478875259", "81 лет"))

    assert "старше 75" in from_contra
    assert "старше 75" in from_age
    for chat_id in ("age_contra_parity_contra", "age_contra_parity_age"):
        session = state.get_session(chat_id)
        assert session["age"] == 81
        assert session["contraindications_verdict"] == "admin_contact"


# --- защита от ложного отказа ---------------------------------------------


@pytest.mark.parametrize("text", [
    "инсульт был 5 лет назад",
    "болею 3 года",
    "операция 10 лет назад",
    "операция была 80 лет назад",
    "травма 20 лет назад",
    "курю 30 лет",
])
def test_medical_history_durations_do_not_stop_booking(text: str) -> None:
    """Анамнез с длительностями — не возраст: ложного отказа быть не должно."""
    chat_id = f"age_contra_duration_{abs(hash(text))}"
    _seed_contra(chat_id)

    answer = run(handle_message(chat_id, "77478875259", text))
    session = state.get_session(chat_id)

    assert not _stopped(session), f"{text!r}: ложная остановка записи"
    assert "старше 75" not in answer
    # "младше 16" встречается и в обычном шаблоне-списке, поэтому отказ
    # определяем по его собственной фразе.
    assert "Запись оформить не смогу" not in answer
    assert answer.strip(), "бот не должен молчать (гарантия Фазы 0)"


@pytest.mark.parametrize("text", [
    "давление 180 на 100",
    "вес 80 кг",
    "рост 180",
    "сахар 90",
    "пульс 90",
])
def test_medical_measurements_are_not_treated_as_age(text: str) -> None:
    chat_id = f"age_contra_measure_{abs(hash(text))}"
    _seed_contra(chat_id)

    answer = run(handle_message(chat_id, "77478875259", text))

    assert not _stopped(state.get_session(chat_id)), f"{text!r}: показатель принят за возраст"
    assert answer.strip()


@pytest.mark.parametrize("text", ["мне 40 лет", "мне 75", "мне 16 лет"])
def test_allowed_ages_do_not_stop(text: str) -> None:
    """75 включительно и 16 — в пределах правила, запись не останавливается."""
    chat_id = f"age_contra_allowed_{abs(hash(text))}"
    _seed_contra(chat_id)

    answer = run(handle_message(chat_id, "77478875259", text))

    assert not _stopped(state.get_session(chat_id))
    assert answer.strip()


def test_under_16_is_not_applied_at_contra_step() -> None:
    """under_16 сознательно не применяется здесь — слишком много ложных срабатываний."""
    chat_id = "age_contra_no_under16"
    _seed_contra(chat_id)

    answer = run(handle_message(chat_id, "77478875259", "болею 3 года"))

    assert "Запись оформить не смогу" not in answer
    assert not _stopped(state.get_session(chat_id))


def test_under_16_still_works_at_age_step() -> None:
    """При этом на шаге age правило "младше 16" по-прежнему действует."""
    chat_id = "age_step_under16_kept"
    _seed_contra(chat_id, {"step": "age"})

    answer = run(handle_message(chat_id, "77478875259", "мне 12 лет"))

    assert "Запись оформить не смогу" in answer
    assert _stopped(state.get_session(chat_id))


def test_normal_contra_answers_unchanged() -> None:
    """Обычные ответы про противопоказания работают как раньше."""
    chat_id = "age_contra_normal_flow"
    _seed_contra(chat_id, {"age": 40})

    answer = run(handle_message(chat_id, "77478875259", "нет"))
    session = state.get_session(chat_id)

    assert answer.strip()
    assert not _stopped(session)
    assert session.get("contraindications_ok") is True

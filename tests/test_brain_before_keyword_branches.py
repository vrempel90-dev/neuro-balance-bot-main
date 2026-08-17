"""Фаза 2: GPT понимает пациента первым, а keyword-ветки — только fallback.

Раньше _try_openai_dialog_brain вызывался ПОСЛЕ ~50 hardcoded keyword-веток,
которые отвечали заготовленными фразами и завершали обработку раньше, чем текст
доходил до GPT. Именно поэтому бот "не понимал" нестандартные формулировки.

Перенесены ПОСЛЕ брейна (интерпретационные ветки):
  * _is_greeting_only            — "просто поздоровался"
  * DOCTOR_WORDS                 — вопрос про врача
  * city/Астана                  — ответ на вопрос о городе
  * ADDRESS_WORDS                — вопрос про адрес (обе ветки)
  * _combined_profile_booking_answer — профильность жалобы

Остались ДО брейна (deterministic/safety-critical): парсинг телефона, исполнение
готовой брони, hard-stop противопоказания, защищённые состояния и активация лида.

Здесь закреплено, что при живом брейне заготовки не срабатывают, а при
недоступном брейне ведут себя ровно как раньше.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Именно setdefault: conftest сбрасывает кэш настроек на каждом тесте, поэтому
# жёсткая перезапись SQLITE_PATH увела бы остальные тест-модули на пустую БД.
os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import crm
import dialog
import state
from dialog import handle_message

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _crm_new_patient(monkeypatch: pytest.MonkeyPatch):
    # Таблицы могут отсутствовать, если путь к БД переопределил другой модуль.
    state.init_db()

    async def fake_lookup(phone):
        return {"ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
                "lastAppointment": None, "hasActiveAppointment": False, "appointment": None, "appointments": []}
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def _seed(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({
        "ai_lead_started": True,
        "gate_reason": "active_ai_lead",
        "first_touch_info_sent": True,
    })
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


def _brain_decision(action: str, reply: str, extracted: dict[str, Any] | None = None, tool: str = "none"):
    decision = {
        "action": action,
        "reply": reply,
        "extracted": {
            "complaint": "", "age": None, "contraindications_clear": None,
            "contraindication_red_flags": [], "preferred_date_text": "", "time_preference": "",
            "slot_choice": None, "patient_name": "", "faq_type": "", "language": "ru",
            **(extracted or {}),
        },
        "needs_python_tool": tool,
        "safety_flags": {"promised_cure": False, "asked_name_too_early": False,
                         "offered_date_before_contra": False, "medical_diagnosis": False},
    }
    debug = {"openai_brain_used": True, "openai_brain_action": action,
             "openai_brain_needs_python_tool": tool, "openai_brain_extracted": decision["extracted"]}
    return decision, debug


def _install_brain(monkeypatch: pytest.MonkeyPatch, action: str, reply: str, extracted: dict[str, Any] | None = None):
    calls: list[str] = []

    async def fake_brain(**kwargs: Any):
        calls.append(str(kwargs.get("user_text") or ""))
        return _brain_decision(action, reply, extracted)

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    return calls


def _disable_brain(monkeypatch: pytest.MonkeyPatch):
    """Брейн недоступен — как при отсутствии ключа или ошибке OpenAI."""
    async def dead_brain(**kwargs: Any):
        decision, _ = _brain_decision("fallback_rule_based", "")
        return decision, {"openai_brain_used": False, "openai_brain_fallback_used": True,
                          "openai_brain_skip_reason": "openai_error"}
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", dead_brain)


# --- брейн получает сообщение раньше keyword-веток -------------------------


def test_greeting_reaches_brain_not_canned_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_greeting_brain"
    _seed(chat_id, {"step": "complaint"})
    calls = _install_brain(monkeypatch, "ask_complaint", "Здравствуйте 🌿 Что именно Вас беспокоит?")

    answer = run(handle_message(chat_id, "77011234567", "Здравствуйте"))

    assert calls, "брейн не получил сообщение — keyword-ветка перехватила его раньше"
    assert "Доброе утро" not in answer, "сработала заготовка _is_greeting_only вместо GPT"


def test_doctor_question_reaches_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_doctor_brain"
    _seed(chat_id, {"step": "date", "complaint": "спина", "age": 40, "contraindications_ok": True})
    calls = _install_brain(monkeypatch, "ask_date", "Приём ведёт врач клиники. На какой день Вам удобно?")

    run(handle_message(chat_id, "77011234567", "а кто меня будет смотреть вообще"))

    assert calls, "вопрос про врача перехвачен веткой DOCTOR_WORDS до GPT"


def test_address_question_reaches_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_address_brain"
    _seed(chat_id, {"step": "complaint"})
    calls = _install_brain(monkeypatch, "answer_faq_and_continue", "Мы на Кабанбай батыра. Что Вас беспокоит?")

    run(handle_message(chat_id, "77011234567", "а где вы находитесь"))

    assert calls, "вопрос про адрес перехвачен веткой ADDRESS_WORDS до GPT"


def test_city_answer_reaches_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_city_brain"
    _seed(chat_id, {"step": "complaint", "last_bot_question_type": "city"})
    calls = _install_brain(monkeypatch, "ask_complaint", "Поняла Вас 🌿 Что именно беспокоит?")

    run(handle_message(chat_id, "77011234567", "Караганда"))

    assert calls, "ответ про город перехвачен keyword-веткой до GPT"


# --- keyword-ветки продолжают работать как fallback ------------------------


def test_greeting_fallback_still_works_without_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Брейн недоступен — заготовка отвечает ровно как раньше."""
    chat_id = "phase2_greeting_fallback"
    _seed(chat_id, {"step": "complaint"})
    _disable_brain(monkeypatch)

    answer = run(handle_message(chat_id, "77011234567", "Здравствуйте"))

    assert answer.strip(), "без брейна бот не должен молчать"
    assert "беспокоит" in answer.lower()


def test_address_fallback_still_works_without_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_address_fallback"
    _seed(chat_id, {"step": "complaint"})
    _disable_brain(monkeypatch)

    answer = run(handle_message(chat_id, "77011234567", "какой у вас адрес"))

    assert answer.strip(), "без брейна ответ про адрес пропал"


def test_doctor_fallback_still_works_without_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "phase2_doctor_fallback"
    _seed(chat_id, {"step": "date", "complaint": "спина", "age": 40, "contraindications_ok": True})
    _disable_brain(monkeypatch)

    answer = run(handle_message(chat_id, "77011234567", "какой врач принимает"))

    assert answer.strip(), "без брейна ответ про врача пропал"


# --- гейты брейна ----------------------------------------------------------


@pytest.mark.parametrize("gate_reason", sorted(dialog.OPENAI_BRAIN_ALLOWED_GATE_REASONS))
def test_brain_allowed_for_every_permitted_gate_reason(gate_reason: str) -> None:
    session = {"step": "complaint", "gate_reason": gate_reason, "ai_lead_started": False}
    assert dialog._openai_brain_skip_reason(session, "болит спина") == ""


def test_brain_still_blocked_without_permitted_gate_reason() -> None:
    session = {"step": "complaint", "gate_reason": "not_new_lead", "ai_lead_started": False}
    assert dialog._openai_brain_skip_reason(session, "болит спина") == "not_ai_lead"


def test_gate_reasons_do_not_exceed_guard_permissions() -> None:
    """Брейну не открыт доступ шире, чем разрешает _should_ai_handle_new_lead."""
    assert dialog.OPENAI_BRAIN_ALLOWED_GATE_REASONS == {
        "new_lead", "new_lead_like_message", "active_conversation_reply", "active_ai_lead",
    }


# --- защищённые состояния не тронуты ---------------------------------------


@pytest.mark.parametrize(
    "preset,expected",
    [
        ({"last_ignored_message_type": "voice"}, "voice_or_audio"),
        ({"escalated": True}, "manual_or_muted"),
        ({"manual_takeover": True}, "manual_or_muted"),
        ({"ai_muted": True}, "manual_or_muted"),
        ({"booked": True}, "booked_or_handoff"),
        ({"step": "booked"}, "booked_or_handoff"),
        ({"refund_claim_admin_required": True}, "refund_or_claim"),
        ({"old_chat_ai_disabled": True}, "old_chat_ai_disabled"),
        ({"hard_contraindication_stop": True}, "hard_contraindication_stop"),
    ],
)
def test_protected_states_still_skip_brain(preset: dict[str, Any], expected: str) -> None:
    session = {"step": "complaint", "gate_reason": "active_ai_lead", "ai_lead_started": True}
    session.update(preset)
    assert dialog._openai_brain_skip_reason(session, "болит спина") == expected


def test_validate_openai_dialog_decision_untouched() -> None:
    """Защита от выдумывания фактов на месте и не ослаблена."""
    session = {"step": "date", "last_slots": [], "complaint": "спина", "age": 40, "contraindications_ok": True}
    decision, _ = _brain_decision("select_slot", "Записала Вас на 10:00", {"slot_choice": 1})
    ok, reason = dialog.validate_openai_dialog_decision(decision, session, "давайте 10:00")
    assert ok is False, "слот выдуман без реальных слотов из CRM — должен блокироваться"
    assert reason

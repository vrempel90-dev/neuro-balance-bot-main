"""Инварианты золотого набора диалогов.

Проверяем не точный текст (он намеренно меняется, когда бот становится
живее), а свойства, которые обязаны держаться всегда:

  - бот отвечает по существу заданного вопроса, а не повторяет предыдущий
    ответ дословно;
  - шаг воронки не теряется: после ответа на боковой вопрос бот возвращает
    пациента к незавершённому шагу;
  - факты берутся только из базы клиники: время приёма не называется без
    слотов из CRM;
  - язык ответа совпадает с языком пациента;
  - locked-шаблон противопоказаний уходит пациенту в утверждённом виде.

Находка аудита, ради которой этот файл существует: на шаге contraindications
любой ответ пациента перезаписывался дословным чек-листом. «дорого»,
«подумаю» и вопрос про цену получали одинаковую простыню, а
duplicate_answer_guard затем превращал следующий такой ответ в молчание —
воронка вставала.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from run_golden import FAKE_SLOT_TIMES, install_offline_crm, load_cases, run_case  # noqa: E402

import state  # noqa: E402
from language_guard import response_language_violation  # noqa: E402

state.init_db()

# install_offline_crm намеренно НЕ вызывается на уровне модуля: подмена
# crm при импорте протекала бы на остальные тесты сессии. Мок ставится
# только внутри фикстуры, на время прогона золотого набора.
CASES = {case["name"]: case for case in load_cases()}
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b")


@pytest.fixture(scope="module")
def results() -> dict[str, dict]:
    install_offline_crm()
    return {name: run_case(case) for name, case in CASES.items()}


# ------------------------------------------------- общие инварианты набора


def test_golden_set_covers_the_required_scenarios() -> None:
    assert len(CASES) >= 30, "золотой набор должен покрывать минимум 30 сценариев"


def test_no_answer_is_a_verbatim_repeat_of_the_previous_one(results: dict[str, dict]) -> None:
    """Дословный повтор — признак того, что слой починки затёр ответ."""
    offenders = []
    for name, result in results.items():
        answers = [turn["bot"] for turn in result["turns"]]
        for i in range(1, len(answers)):
            if answers[i] and answers[i] == answers[i - 1]:
                offenders.append(f"{name}: ход {i + 1}")
    assert not offenders, "бот дословно повторил предыдущий ответ: " + "; ".join(offenders)


def test_times_are_shown_only_from_crm_slots(results: dict[str, dict]) -> None:
    """Время приёма называется только когда слоты реально пришли из CRM,
    и только те значения, которые CRM вернул."""
    offenders = []
    for name, result in results.items():
        for turn in result["turns"]:
            found = set(TIME_RE.findall(turn["bot"] or ""))
            if not found:
                continue
            if not turn.get("slots_count"):
                offenders.append(f"{name}: время без слотов CRM — {sorted(found)}")
                continue
            invented = found - set(FAKE_SLOT_TIMES)
            if invented:
                offenders.append(f"{name}: время не из CRM — {sorted(invented)}")
    assert not offenders, "; ".join(offenders)


def test_every_answer_matches_the_patient_language(results: dict[str, dict]) -> None:
    offenders = []
    for name, result in results.items():
        for turn in result["turns"]:
            if not turn["bot"] or turn["language"] not in ("ru", "kk"):
                continue
            violation = response_language_violation(turn["bot"], turn["language"])
            if violation:
                offenders.append(f"{name}: {violation}")
    assert not offenders, "ответ не на языке пациента: " + "; ".join(offenders)


def test_no_answer_asks_about_contraindications_twice(results: dict[str, dict]) -> None:
    """Возврат к шагу не должен добавляться к ответу, который сам уже спросил.

    Такое случалось на вопросе про термин из чек-листа: пациент получал
    «...у Вас этого нет?» и сразу следом «...противопоказаний из списка
    выше нет?» — два вопроса подряд читаются как автоответчик.
    """
    markers = ("у вас этого нет?", "противопоказаний из списка выше нет?",
               "у вас ничего из этого нет?")
    offenders = []
    for name, result in results.items():
        for turn in result["turns"]:
            low = (turn["bot"] or "").lower()
            hits = sum(1 for marker in markers if marker in low)
            if hits > 1:
                offenders.append(f"{name}: {hits} вопроса про противопоказания")
    assert not offenders, "; ".join(offenders)


# ------------------------------------- боковой вопрос не ломает воронку


@pytest.mark.parametrize(
    "case_name, expected_fragment",
    [
        ("13_objection_expensive", "рассрочка"),
        ("14_objection_will_think", "отзывы"),
        ("33_faq_during_contraindications", "5 000"),
        ("34_address_during_contraindications", "28"),
    ],
)
def test_side_question_gets_a_real_answer(results: dict[str, dict], case_name: str, expected_fragment: str) -> None:
    """Возражение и вопрос получают ответ по существу, а не чек-лист."""
    last = results[case_name]["turns"][-1]["bot"]
    assert expected_fragment.lower() in last.lower(), f"{case_name}: ответ не по существу — {last[:120]!r}"


@pytest.mark.parametrize(
    "case_name",
    ["13_objection_expensive", "14_objection_will_think", "33_faq_during_contraindications",
     "34_address_during_contraindications"],
)
def test_side_question_keeps_the_unfinished_step(results: dict[str, dict], case_name: str) -> None:
    """После ответа на боковой вопрос шаг воронки не теряется."""
    assert results[case_name]["final_step"] == "contraindications"


@pytest.mark.parametrize(
    "case_name",
    ["13_objection_expensive", "33_faq_during_contraindications", "34_address_during_contraindications"],
)
def test_side_question_returns_the_patient_to_the_step(results: dict[str, dict], case_name: str) -> None:
    """Ответ заканчивается возвратом к незавершённому шагу, а не повисает."""
    last = results[case_name]["turns"][-1]["bot"]
    assert last.rstrip().endswith("?"), f"{case_name}: нет возврата к шагу — {last[-90:]!r}"


def test_kazakh_objection_answered_in_kazakh(results: dict[str, dict]) -> None:
    last = results["28_kk_objection"]["turns"][-1]["bot"]
    assert response_language_violation(last, "kk") == ""
    assert "kaspi red" in last.lower()


# ------------------------------------------------- инварианты безопасности


@pytest.mark.parametrize(
    "case_name",
    ["03_age_under_16", "04_age_over_74", "05_hard_contraindication"],
)
def test_safety_stops_never_reach_booking(results: dict[str, dict], case_name: str) -> None:
    assert results[case_name]["final_step"] not in ("date", "time", "name", "booked")


def test_admin_takeover_keeps_the_bot_silent(results: dict[str, dict]) -> None:
    assert all(not turn["bot"] for turn in results["19_admin_takeover"]["turns"])


def test_booked_session_does_not_restart_the_funnel(results: dict[str, dict]) -> None:
    """После брони бот не начинает анкету заново.

    Полное молчание здесь не требуется: вежливый ответ допустим, но шаг
    обязан остаться booked, а вопросов воронки быть не должно.
    """
    result = results["18_already_booked_thanks"]
    assert result["final_step"] == "booked"
    for turn in result["turns"]:
        low = (turn["bot"] or "").lower()
        assert "сколько вам лет" not in low
        assert "что вас беспокоит" not in low
        assert "противопоказ" not in low


def test_locked_contraindications_template_is_delivered_verbatim(results: dict[str, dict]) -> None:
    """Утверждённый чек-лист уходит пациенту с сохранением абзацев."""
    import dialog

    first_checklist = results["33_faq_during_contraindications"]["turns"][1]["bot"]
    assert dialog.CONTRAINDICATIONS_MESSAGE_RU in first_checklist


def test_profile_complaint_reaches_slot_selection(results: dict[str, dict]) -> None:
    """Главный сценарий: профильная жалоба доходит до выбора времени."""
    result = results["01_profile_complaint_to_booking"]
    assert result["final_step"] == "time"
    last = result["turns"][-1]["bot"]
    assert any(t in last for t in FAKE_SLOT_TIMES), f"слоты не показаны: {last[:120]!r}"
    assert result["turns"][-1]["slots_count"] > 0

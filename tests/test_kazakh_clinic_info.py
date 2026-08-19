"""Регрессия: операторские шаблоны клиники на казахском.

Находка аудита: ``clinic_info.get_clinic_info`` принимал аргумент ``lang``,
но полностью его игнорировал, а ``bot_tools.get_clinic_info`` язык сессии
вообще не передавал. Из-за этого казахоязычный пациент на любой FAQ —
цена, адрес, график, МРТ, рассрочка — получал русский текст.

Отдельно проверяем, что казахская ветка response_guard больше не склеивает
русский шаблон с казахским хвостом.
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

import bot_tools  # noqa: E402
import clinic_info  # noqa: E402
import response_guard  # noqa: E402
from language_guard import response_language_violation  # noqa: E402


KK_TOPICS = sorted(clinic_info.CLINIC_INFO_TEMPLATES_KK)


def test_every_kazakh_topic_exists_in_russian_too() -> None:
    """Казахских тем-«сирот» быть не должно — это был бы выдуманный факт."""
    assert set(KK_TOPICS) <= set(clinic_info.CLINIC_INFO_TEMPLATES)


@pytest.mark.parametrize("topic", KK_TOPICS)
def test_kazakh_template_differs_from_russian(topic: str) -> None:
    ru = clinic_info.get_clinic_info(topic, "ru")
    kk = clinic_info.get_clinic_info(topic, "kk")
    assert kk and ru
    assert kk != ru, f"{topic}: казахский шаблон совпадает с русским"


@pytest.mark.parametrize("topic", KK_TOPICS)
def test_kazakh_template_is_actually_kazakh(topic: str) -> None:
    kk = clinic_info.get_clinic_info(topic, "kk")
    assert response_language_violation(kk, "kk") == "", f"{topic}: шаблон не на казахском"


@pytest.mark.parametrize("topic", sorted(clinic_info.CLINIC_INFO_TEMPLATES))
def test_russian_template_is_actually_russian(topic: str) -> None:
    ru = clinic_info.get_clinic_info(topic, "ru")
    violation = response_language_violation(ru, "ru")
    if topic == "refund_complaint":
        # Утверждённый шаблон намеренно двуязычный: русский текст и его
        # казахское повторение в одном сообщении.
        assert violation == "mixed_language_response"
        return
    assert violation == "", f"{topic}: русский шаблон не на русском"


def test_missing_kazakh_topic_falls_back_to_russian_and_never_invents() -> None:
    assert not clinic_info.has_kazakh_template("refund_complaint")
    assert clinic_info.get_clinic_info("refund_complaint", "kk") == clinic_info.CLINIC_INFO_TEMPLATES["refund_complaint"]


def test_unknown_topic_returns_none_in_both_languages() -> None:
    assert clinic_info.get_clinic_info("no_such_topic", "ru") is None
    assert clinic_info.get_clinic_info("no_such_topic", "kk") is None


# ------------------------------------------------------------ протянутый язык


def test_bot_tools_passes_session_language() -> None:
    ru_session: dict = {"language": "ru"}
    kk_session: dict = {"language": "kk"}
    assert bot_tools.get_clinic_info(ru_session, "address") == clinic_info.CLINIC_INFO_TEMPLATES["address"]
    assert bot_tools.get_clinic_info(kk_session, "address") == clinic_info.CLINIC_INFO_TEMPLATES_KK["address"]
    assert kk_session["last_clinic_info_lang"] == "kk"
    assert kk_session["last_clinic_info_lang_fallback"] is False


def test_bot_tools_marks_russian_fallback_for_observability() -> None:
    session: dict = {"language": "kk"}
    bot_tools.get_clinic_info(session, "refund_complaint")
    assert session["last_clinic_info_lang_fallback"] is True


def test_bot_tools_defaults_to_russian_without_session_language() -> None:
    session: dict = {}
    assert bot_tools.get_clinic_info(session, "schedule") == clinic_info.CLINIC_INFO_TEMPLATES["schedule"]


# ------------------------------------------------------- защищённые сущности


@pytest.mark.parametrize(
    "topic, fragment",
    [
        ("address", "28"),
        ("address", "2gis.kz"),
        ("objection_will_think", "instagram.com/neuro_balance_ast"),
        ("installment", "Kaspi RED"),
        ("price_first_visit", "5 000 тг"),
        ("kaspi_prepay_saturday", "2 000 тг"),
    ],
)
def test_kazakh_templates_keep_links_brands_and_numbers(topic: str, fragment: str) -> None:
    """Ссылки, бренды, номера домов и цены переводить нельзя."""
    kk = clinic_info.get_clinic_info(topic, "kk")
    assert fragment.lower() in kk.lower()


# ------------------------------------------------- response_guard без смеси


@pytest.mark.parametrize(
    "user_text, ru_answer",
    [
        ("Қабылдау қанша тұрады?", "Приём в нашей клинике — 5 000 тг. В стоимость входит осмотр врача."),
        ("Мекенжайыңыз қайда?", "📍 Адрес: Кабанбай батыра 28, внутренний двор, подъезд 3, Астана."),
        ("Қандай график бойынша жұмыс істейсіздер?", "🕒 График приёма. Понедельник – Пятница: 08:00 – 20:00"),
    ],
)
def test_kazakh_patient_never_gets_russian_template_with_kazakh_tail(user_text: str, ru_answer: str) -> None:
    """Regression: guard отдавал русский шаблон + казахский вопрос в одном сообщении."""
    answer, violations = response_guard.validate_answer("chat-kk", user_text, ru_answer, {"language": "ru"})
    assert "wrong_language_ru_template_on_kk" in violations
    assert response_language_violation(answer, "kk") == ""

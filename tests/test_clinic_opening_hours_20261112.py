"""Clinic reception opening hours are independent of the AI night shift.

Only the weekday clinic consultation/procedure hours changed to 08:00–18:00.
The AI remains active strictly 20:00–08:00 by its own runtime guard.
"""
from pathlib import Path

from clinic_info import get_clinic_info


def test_clinic_opening_hours_russian_and_kazakh() -> None:
    for lang, label in (("ru", "Понедельник – Пятница"), ("kk", "Дүйсенбі – Жұма")):
        schedule = get_clinic_info("schedule", lang)
        assert schedule is not None
        assert schedule.count(f"{label}: 08:00 – 18:00") == 2
        assert "08:00 – 20:00" not in schedule
        assert "09:00 – 18:00" in schedule  # unchanged Saturday procedure hours
        assert "Воскресенье: выходной" in schedule or "Жексенбі: демалыс" in schedule


def test_legacy_prompt_matches_clinic_hours() -> None:
    root = Path(__file__).resolve().parent.parent
    legacy = (root / "system_prompt_original_FIXED-2.md").read_text(encoding="utf-8")
    assert legacy.count("Пн–Пт 08:00–18:00") == 2
    assert "Пн–Пт 08:00–20:00" not in legacy


def test_ai_night_schedule_is_independent_of_clinic_opening_hours() -> None:
    root = Path(__file__).resolve().parent.parent
    prompt = (root / "SYSTEM_PROMPT_rendered.md").read_text(encoding="utf-8")
    assert "20:00 до 08:00" in prompt
    assert "WORK_HOURS_GUARD_ENABLED=true" in prompt

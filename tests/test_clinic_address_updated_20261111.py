"""Regression checks for the NeuroBalance address returned by get_clinic_info.

The address is maintained in factual templates; never rely on model memory.
"""
from pathlib import Path

from clinic_info import get_clinic_info

MAP_URL = "https://2gis.kz/astana/geo/70000001105992248"


def test_latest_location_in_russian_and_kazakh_templates() -> None:
    ru = get_clinic_info("address", lang="ru")
    kk = get_clinic_info("address", lang="kk")
    assert ru and kk
    assert "Туран, 59/2" in ru
    assert "Тұран даңғылы, 59/2" in kk
    assert MAP_URL in ru and MAP_URL in kk
    for text in (ru, kk):
        assert "Кабанбай" not in text
        assert "Қабанбай" not in text
        assert "подъезд 3" not in text
        assert "3-кіреберіс" not in text


def test_legacy_prompt_is_not_contaminated_with_old_clinic_address() -> None:
    prompt = (Path(__file__).resolve().parents[1] / "system_prompt_original_FIXED-2.md").read_text(encoding="utf-8")
    assert "Туран, 59/2" in prompt
    assert MAP_URL in prompt
    assert "Кабанбай батыра 28" not in prompt

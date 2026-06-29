from __future__ import annotations

import asyncio, os, sys, tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import crm, state
from dialog import handle_message

state.init_db()

def run(coro: Any) -> Any:
    return asyncio.run(coro)

def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    s = state.get_session(chat_id)
    s.update({"ai_lead_started": True, "phone": "77011234567"})
    if preset:
        s.update(preset)
    state.save_session(chat_id, s)

def answer(chat_id: str, text: str) -> str:
    return run(handle_message(chat_id, "77011234567", text))

def patch_crm(monkeypatch: Any, slots: list[str] | None = None) -> dict[str, list[Any]]:
    calls = {"slots": [], "book": []}
    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {"availability": [{"doctorLogin": doctor_login or "zhuma_md", "doctorName": "Жумабек Мади Мухтарович", "date": date, "availableSlots": slots or ["09:20", "14:00"]}]}
    async def fake_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        return {"ok": True, "date": kwargs.get("date"), "timeStart": kwargs.get("time_start"), "doctorName": kwargs.get("doctor_name")}
    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(crm, "book_appointment", fake_book)
    return calls

def test_new_lead_complaint_age_contra_flow() -> None:
    reset("prod_flow")
    r = answer("prod_flow", "Боль в спине")
    s = state.get_session("prod_flow")
    assert s["step"] == "age" and "сколько Вам лет" in r
    r = answer("prod_flow", "32")
    s = state.get_session("prod_flow")
    assert s["age"] == 32 and s["step"] == "contraindications" and "противопоказания" in r.lower()
    r = answer("prod_flow", "нет")
    s = state.get_session("prod_flow")
    assert s["contraindications_ok"] is True and s["step"] == "date" and "день" in r.lower()

def test_doctor_lock_and_before_noon_filter(monkeypatch: Any) -> None:
    calls = patch_crm(monkeypatch, ["09:20", "14:40", "15:20", "16:40", "17:20"])
    reset("prod_doctor", {"step": "date", "complaint": "спина", "age": 32, "contraindications_ok": True})
    r = answer("prod_doctor", "Могу завтра до обеда к Мади Мухтаровичу")
    s = state.get_session("prod_doctor")
    assert s["selected_doctor_login"] == "zhuma_md"
    assert calls["slots"][-1]["doctor_login"] == "zhuma_md"
    assert "09:20" in r and all(t not in r for t in ["14:40", "15:20", "16:40", "17:20"])

def test_before_noon_request_with_only_afternoon_slots_does_not_label_them_morning(monkeypatch: Any) -> None:
    patch_crm(monkeypatch, ["14:40", "15:20", "16:40", "17:20"])
    reset(
        "prod_before_noon_only_afternoon",
        {
            "step": "date",
            "complaint": "спина",
            "age": 32,
            "contraindications_ok": True,
            "selected_doctor_login": "zhuma_md",
            "selected_doctor_name": "Жумабек Мади Мухтарович",
            "preferred_date": "2026-06-30",
        },
    )
    r = answer("prod_before_noon_only_afternoon", "Когда есть время к Мади Мухтаровичу завтра до обеда?")
    assert "до обеда свободных окошек не вижу" in r
    assert "Есть после обеда" in r
    assert "14:40" in r
    assert "До обеда есть 14:40" not in r

def test_before_noon_request_shows_only_morning_slots(monkeypatch: Any) -> None:
    patch_crm(monkeypatch, ["09:20", "14:40", "15:20"])
    reset("prod_before_noon_mixed", {"step": "date", "complaint": "спина", "age": 32, "contraindications_ok": True})
    r = answer("prod_before_noon_mixed", "Могу завтра до обеда")
    assert "09:20" in r
    assert "14:40" not in r
    assert "15:20" not in r

def test_slot_status_name_booking_success(monkeypatch: Any) -> None:
    calls = patch_crm(monkeypatch)
    reset("prod_book", {"step": "time", "complaint": "спина", "age": 32, "contraindications_ok": True, "preferred_date": "2026-07-02", "last_slots": [{"doctorLogin":"zhuma_md","doctorName":"Жумабек Мади Мухтарович","date":"2026-07-02","timeStart":"09:20","doctor_login":"zhuma_md","doctor_name":"Жумабек Мади Мухтарович","time":"09:20"}]})
    r = answer("prod_book", "9:20")
    s = state.get_session("prod_book")
    assert s["selected_time"] == "09:20" and s["step"] == "name" and "имя" in r.lower()
    r = answer("prod_book", "Записали?")
    assert "Пока ещё нет" in r and "имя" in r
    r = answer("prod_book", "Дана")
    s = state.get_session("prod_book")
    assert calls["book"] and s["booking_confirmed"] is True
    assert "Отлично, запись оформлена" in r and "Кабанбай батыра 28" in r and "2ГИС" in r

def test_faqs_and_guards(monkeypatch: Any) -> None:
    patch_crm(monkeypatch)
    reset("prod_faq", {"step": "age", "complaint": "спина"})
    r = answer("prod_faq", "Заодно хотел узнать цены")
    s = state.get_session("prod_faq")
    assert "5 000 тг" in r and "сколько Вам лет" in r and not s.get("escalated")
    r = answer("prod_faq", "Хотел узнать лечится")
    assert "после осмотра" in r.lower() and "сколько Вам лет" in r
    reset("prod_addr")
    r = answer("prod_addr", "В каком городе, адрес")
    assert "Кабанбай батыра 28" in r and "что Вас беспокоит" not in r
    reset("prod_mri")
    r = answer("prod_mri", "Я в другом городе, снимка нет")
    assert "Снимок заранее не обязателен" in r

def test_name_date_time_last_slots_and_kazakh(monkeypatch: Any) -> None:
    patch_crm(monkeypatch, ["09:20", "14:00"])
    reset("prod_thanks", {"step": "time", "last_slots": [{"time":"09:20","timeStart":"09:20"}], "preferred_date": "2026-07-02"})
    r = answer("prod_thanks", "спасибо")
    assert state.get_session("prod_thanks").get("patient_name", "") == "" and "09:20" in r
    reset("prod_date", {"step": "date", "complaint":"спина", "age":32, "contraindications_ok": True})
    answer("prod_date", "на четверг")
    s = state.get_session("prod_date")
    assert s.get("preferred_date") and s.get("patient_name", "") == ""
    reset("prod_recover", {"step":"time", "preferred_date":"2026-07-02", "last_slots": [], "complaint":"спина", "age":32, "contraindications_ok": True})
    r = answer("prod_recover", "9:20")
    assert state.get_session("prod_recover")["selected_time"] == "09:20" and r
    reset("prod_kk", {"step":"age", "complaint":"белім ауырады", "language":"kk"})
    r = answer("prod_kk", "25те")
    assert state.get_session("prod_kk")["age"] == 25 and "Қарсы көрсетілім" in r
    reset("prod_kk_complaint")
    r = answer("prod_kk_complaint", "Мені мазалайтыны белім ауырады, саным ауырады")
    assert "Түсіндім" in r and "Жасыңыз" in r

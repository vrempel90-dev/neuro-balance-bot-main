from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import crm
import dialog
import main
import state


def test_debug_crm_lookup_endpoint_exposes_raw_lookup(monkeypatch):
    async def fake_lookup(phone: str):
        return {
            "ok": True,
            "status_code": 200,
            "endpoint_url": "https://crm.test/api/bot/patient-lookup",
            "query_params": {"phone": "77008984505"},
            "raw": {"appointments": [{"id": 1}]},
            "appointments": [{"id": 1, "date": "2099-01-01", "timeStart": "10:00", "doctorName": "Тестовый врач"}],
            "appointment": {"id": 1, "date": "2099-01-01", "timeStart": "10:00", "doctorName": "Тестовый врач"},
            "active_count": 1,
            "appointments_raw_count": 1,
        }

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    response = TestClient(main.app).get("/debug/crm/lookup", params={"phone": "+77008984505"})
    data = response.json()

    assert response.status_code == 200
    assert data["phone_raw"] == "+77008984505"
    assert data["phone_normalized"] == "77008984505"
    assert data["phone_lookup_variants"] == ["77008984505", "+77008984505", "87008984505", "7008984505"]
    assert data["crm_lookup_called"] is True
    assert data["crm_endpoint_url"] == "https://crm.test/api/bot/patient-lookup"
    assert data["crm_status_code"] == 200
    assert data["crm_raw_response"] == {"appointments": [{"id": 1}]}
    assert data["active_appointment_found"] is True
    assert data["active_appointment_count"] == 1


def test_debug_chat_booked_client_blocks_first_touch(monkeypatch):
    async def fake_lookup(phone: str):
        return {
            "ok": True,
            "status_code": 200,
            "endpoint_url": "https://crm.test/api/bot/patient-lookup",
            "phone_variants": crm.phone_lookup_variants(phone),
            "raw": {"appointments": [{"id": 7}]},
            "appointments": [{"id": 7, "date": "2099-01-01", "timeStart": "10:00", "doctorName": "Тестовый врач"}],
            "appointment": {"id": 7, "date": "2099-01-01", "timeStart": "10:00", "doctorName": "Тестовый врач"},
            "active_count": 1,
            "appointments_raw_count": 1,
        }

    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    chat_id = "debug_chat_booked_regression"
    state.reset_session(chat_id)

    response = TestClient(main.app).post("/debug/chat", json={"chat_id": chat_id, "phone": "+77008984505", "text": "Здравствуйте"})
    data = response.json()

    assert response.status_code == 200
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in data["answer"]
    assert "Вы уже записаны" in data["answer"]
    assert data["crm_lookup_called"] is True
    assert data["active_appointment_found"] is True
    assert data["first_touch_allowed"] is False


def test_debug_chat_crm_failure_fails_closed(monkeypatch):
    async def broken(phone: str):
        raise crm.CRMError("down")

    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", broken)
    chat_id = "debug_chat_crm_failure_regression"
    state.reset_session(chat_id)

    response = TestClient(main.app).post("/debug/chat", json={"chat_id": chat_id, "phone": "+77008984505", "text": "Здравствуйте"})
    data = response.json()

    assert response.status_code == 200
    assert dialog.FIRST_TOUCH_CLINIC_INFO_RU not in data["answer"]
    assert data["answer"] == "Сейчас уточню Вашу запись у администратора 🌿"
    assert data["session"]["manual_takeover"] is True
    assert data["first_touch_allowed"] is False
    assert data["first_touch_blocked_reason"] == "crm_lookup_failed"

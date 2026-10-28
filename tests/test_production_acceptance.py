"""Synthetic local SQLite + fake readback; no network or CRM writes."""
from __future__ import annotations

import asyncio
import json

import pytest

import production_acceptance as acceptance
import state
from config import get_settings


@pytest.fixture(autouse=True)
def acceptance_database(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "acceptance.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


def save(chat_id="synthetic-ai-booking", **changes):
    session = {
        "booking_confirmed": True, "created_by_ai": True,
        "crm_booking_id": "synthetic-booking", "phone": "77001230001",
        "appointment_date": "2031-01-03", "appointment_time": "10:00",
        "appointment_doctor_login": "doctor.test",
        "appointment_doctor_name": "Synthetic Doctor",
    }
    session.update(changes)
    state.save_session(chat_id, session)
    return session


def run():
    return asyncio.run(acceptance.verify_existing_ai_booking())


def test_no_booking_is_explicitly_not_executed(monkeypatch):
    async def forbidden(**kwargs):
        raise AssertionError("no record should be queried")
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", forbidden)
    assert run() == {"executed": False, "verified": False, "attempted_records": 0, "reason": "no_confirmed_ai_booking_available"}


@pytest.mark.parametrize("changes", [
    {"booking_confirmed": False}, {"booking_confirmed": "true"},
    {"created_by_ai": False, "crm_booking_id": ""},
    {"crm_booking_id": "", "appointment_id": ""},
    {"phone": "123"}, {"appointment_date": "2031-99-03"},
    {"appointment_time": "25:00"}, {"appointment_doctor_login": ""},
])
def test_unconfirmed_or_incomplete_session_is_not_used(monkeypatch, changes):
    save(**changes)
    calls = []
    async def verify(**kwargs):
        calls.append(kwargs)
        return {"verified": True}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    assert run()["executed"] is False
    assert not calls


def test_actual_helper_receives_saved_fields_and_output_is_sanitized(monkeypatch):
    save()
    calls = []
    async def verify(**kwargs):
        calls.append(kwargs)
        return {"verified": True, "appointment": {"phone": "77001230001", "name": "Synthetic Doctor"}}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    result = run()
    assert calls[0] == {
        "response": {"appointmentId": "synthetic-booking"}, "phone": "77001230001",
        "doctor_login": "doctor.test", "doctor_name": "Synthetic Doctor",
        "date": "2031-01-03", "time_start": "10:00",
    }
    assert result == {"executed": True, "verified": True, "attempted_records": 1, "reason": "existing_ai_booking_readback_verified"}
    for private in ("77001230001", "Synthetic Doctor", "synthetic-booking", "2031-01-03"):
        assert private not in json.dumps(result)


def test_saved_payload_is_not_mutated(monkeypatch):
    original = save()
    async def verify(**kwargs):
        return {"verified": True}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    run()
    saved = state.get_session("synthetic-ai-booking")
    assert all(saved[key] == value for key, value in original.items())


def test_maximum_three_distinct_bookings_are_read(monkeypatch):
    for index in range(6):
        save(f"synthetic-{index}", crm_booking_id=f"booking-{index}")
    calls = []
    async def verify(**kwargs):
        calls.append(kwargs)
        return {"verified": False}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    result = run()
    assert len(calls) == result["attempted_records"] == 3
    assert result["verified"] is False
    assert result["reason"] == "booking_readback_unverified"


def test_duplicate_session_appointments_are_only_read_once(monkeypatch):
    save("synthetic-a")
    save("synthetic-b")
    calls = []
    async def verify(**kwargs):
        calls.append(kwargs)
        return {"verified": False}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    assert run()["attempted_records"] == len(calls) == 1


def test_global_timeout_reports_unverified_without_leaking_exception(monkeypatch):
    save()
    async def verify(**kwargs):
        await asyncio.sleep(1)
        return {"verified": True}
    monkeypatch.setattr(acceptance, "_READBACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    result = run()
    assert result == {"executed": True, "verified": False, "attempted_records": 1, "reason": "readback_timeout"}


def test_private_network_exception_is_not_returned(monkeypatch):
    save()
    async def verify(**kwargs):
        raise RuntimeError("private phone 77001230001")
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    result = run()
    assert result["reason"] == "booking_readback_unavailable"
    assert "77001230001" not in json.dumps(result)


def test_bad_session_json_does_not_hide_valid_candidate(monkeypatch):
    save()
    with state._connect() as conn:
        conn.execute("INSERT INTO sessions(chat_id,data_json,updated_at) VALUES (?,?,?)", ("malformed-synthetic", "{", state.now_iso()))
    async def verify(**kwargs):
        return {"verified": True}
    monkeypatch.setattr(acceptance.crm, "verify_saved_booking", verify)
    assert run()["verified"] is True


def test_database_failure_is_explicitly_not_executed(monkeypatch):
    def broken():
        raise RuntimeError("private session failure")
    monkeypatch.setattr(acceptance, "_load_candidates", broken)
    assert run() == {"executed": False, "verified": False, "attempted_records": 0, "reason": "session_read_unavailable"}

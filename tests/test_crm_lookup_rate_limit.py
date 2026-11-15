"""Safe, isolated CRM rate-limit regression checks.

No production credentials, live network access, booking writes or patient data.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import crm


PHONE = "77001234567"


def fresh() -> dict:
    return {"ok": True, "found": False, "isNew": True, "hasActiveAppointment": False}


def response_error(status_code: int, retry_after: str = "") -> crm.CRMResponseError:
    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://example.invalid/api/bot/patient-lookup"),
        headers={"Retry-After": retry_after} if retry_after else {},
        json={"code": "synthetic_rate_limit"},
    )
    return crm.CRMResponseError("patient-lookup", response)


def test_alias_lookups_are_sequential_not_bursts(monkeypatch):
    current = peak = 0
    called: list[str] = []

    async def lookup(value: str) -> dict:
        nonlocal current, peak
        current += 1
        peak = max(current, peak)
        called.append(value)
        await asyncio.sleep(0)
        current -= 1
        return fresh()

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))

    assert peak == 1
    assert called == crm.phone_lookup_variants(PHONE)
    assert result["ok"] is True
    assert len(result["identity_variants"]) == len(called)


def test_read_only_429_is_retried_once_then_all_aliases_verified(monkeypatch):
    called: list[str] = []
    target = crm.phone_lookup_variants(PHONE)[0]

    async def lookup(value: str) -> dict:
        called.append(value)
        if value == target and called.count(target) == 1:
            raise response_error(429, "0")
        return fresh()

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))

    assert called.count(target) == 2
    assert called[2:] == crm.phone_lookup_variants(PHONE)[1:]
    assert result["ok"] is True


def test_exhausted_429_fails_closed_and_stops_alias_burst(monkeypatch):
    called: list[str] = []

    async def lookup(value: str) -> dict:
        called.append(value)
        raise response_error(429, "0")

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    with pytest.raises(crm.CRMError, match="rate limited"):
        asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))

    assert called == [crm.phone_lookup_variants(PHONE)[0]] * 2


def test_non_rate_limit_error_is_not_retried(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(crm, "phone_lookup_variants", lambda phone: ["only"])

    async def lookup(value: str) -> dict:
        called.append(value)
        raise response_error(503)

    monkeypatch.setattr(crm, "patient_lookup", lookup)
    with pytest.raises(crm.CRMError, match="all phone formats"):
        asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))

    assert called == ["only"]


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("0", 0.25), ("0.75", 0.75), ("300", 2.0), ("not-a-number", 0.5)],
)
def test_retry_after_is_bounded_and_does_not_echo_body(retry_after, expected):
    error = response_error(429, retry_after)
    assert error.retry_after_seconds == expected
    assert "synthetic_rate_limit" not in str(error)

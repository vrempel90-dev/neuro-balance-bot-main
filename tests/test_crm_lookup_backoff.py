"""Offline transport tests of bounded, read-only CRM lookup recovery."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gc
import logging
import weakref

import httpx
import pytest

import admission
import crm


PHONE = "77001230001"
NEW = {"ok": True, "found": False, "isNew": True, "hasActiveAppointment": False}


def _no_wait(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(crm.asyncio, "sleep", sleep)
    return delays


async def _lookup(monkeypatch, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(crm, "_client", lambda: client)
        return await crm.patient_lookup_raw(PHONE)


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_read_status_retries_once_then_returns_verified_json(monkeypatch, status):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(status if len(attempts) == 1 else 200, json=NEW)

    result = asyncio.run(_lookup(monkeypatch, handler))
    assert result["isNew"] is True
    assert len(attempts) == 2 and all(request.method == "GET" for request in attempts)
    assert delays == [0.25]
    assert attempts[0].url == attempts[1].url


def test_retry_after_within_cap_is_respected_without_early_retry(monkeypatch):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) == 1 else 200,
                              headers={"Retry-After": "1.5"}, json=NEW)

    asyncio.run(_lookup(monkeypatch, handler))
    assert len(attempts) == 2 and delays == [1.5]


@pytest.mark.parametrize("retry_after", ["120", "nan", "inf", "not-a-delay"])
def test_unbounded_or_unparseable_retry_after_fails_closed_without_retry(monkeypatch, retry_after):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after}, json={"error": "private body"})

    with pytest.raises(crm.CRMResponseError) as error:
        asyncio.run(_lookup(monkeypatch, handler))
    assert error.value.status_code == 429
    assert len(attempts) == 1 and not delays


def test_future_http_date_retry_after_over_cap_is_not_retried(monkeypatch):
    retry_after = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=2), usegmt=True)
    assert crm._lookup_retry_delay(httpx.Response(429, headers={"Retry-After": retry_after})) is None


def test_past_http_date_is_allowed_with_bounded_minimum_backoff():
    retry_after = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=2), usegmt=True)
    assert crm._lookup_retry_delay(httpx.Response(429, headers={"Retry-After": retry_after})) == 0.25


@pytest.mark.parametrize("error_class", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_transient_network_timeout_retries_once(monkeypatch, error_class):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise error_class("private URL and phone must not be logged", request=request)
        return httpx.Response(200, json=NEW)

    assert asyncio.run(_lookup(monkeypatch, handler))["ok"] is True
    assert len(attempts) == 2 and delays == [0.25]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_permanent_or_unsupported_status_never_retries(monkeypatch, status):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, json={"error": "private body"})

    with pytest.raises(crm.CRMResponseError):
        asyncio.run(_lookup(monkeypatch, handler))
    assert len(attempts) == 1 and not delays


def test_repeated_transient_failure_is_bounded_and_sanitized(monkeypatch, caplog):
    attempts = []
    _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, json={"error": "synthetic private complaint", "phone": PHONE})

    with caplog.at_level(logging.INFO, logger="crm"):
        with pytest.raises(crm.CRMResponseError) as error:
            asyncio.run(_lookup(monkeypatch, handler))
    assert len(attempts) == 2
    assert "status=429" in caplog.text
    assert PHONE not in caplog.text + str(error.value)
    assert "synthetic private complaint" not in caplog.text + str(error.value)


def test_repeated_network_timeout_is_bounded_and_sanitized(monkeypatch, caplog):
    attempts = []
    _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("synthetic private complaint " + PHONE, request=request)

    with caplog.at_level(logging.INFO, logger="crm"):
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(_lookup(monkeypatch, handler))
    assert len(attempts) == 2
    assert "error_type=ReadTimeout" in caplog.text
    assert PHONE not in caplog.text and "synthetic private complaint" not in caplog.text


def test_invalid_success_json_is_not_retried(monkeypatch):
    attempts = []
    _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, json=["not identity data"])

    with pytest.raises(crm.CRMError):
        asyncio.run(_lookup(monkeypatch, handler))
    assert len(attempts) == 1


def test_all_clients_and_phone_variants_share_two_http_request_limit(monkeypatch):
    active = peak = total = 0

    async def handler(request):
        nonlocal active, peak, total
        active += 1
        total += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.005)
            return httpx.Response(200, json=NEW)
        finally:
            active -= 1

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(crm, "_client", lambda: client)
            # Legacy test modules may replace this wrapper; restore a wrapper
            # whose defining module is crm, so the production raw path is used.
            async def original(phone):
                return await crm.patient_lookup_raw(crm.normalize_phone(phone))
            original.__module__ = "crm"
            monkeypatch.setattr(crm, "patient_lookup", original)
            return await asyncio.gather(*(crm.CRMClient().lookup_active_appointments_by_phone(
                f"7700123000{number}") for number in range(1, 5)))

    results = asyncio.run(run())
    assert peak == 2 and total == 16 and active == 0
    assert all(admission.classify(PHONE, result).state == admission.NEW for result in results)


def test_one_exhausted_variant_still_excludes_new_patient(monkeypatch):
    _no_wait(monkeypatch)
    attempts = {}

    def handler(request):
        phone = request.url.params["phone"]
        attempts[phone] = attempts.get(phone, 0) + 1
        return httpx.Response(429 if phone.startswith("8") else 200, json=NEW)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(crm, "_client", lambda: client)
            async def original(phone):
                return await crm.patient_lookup_raw(phone)
            original.__module__ = "crm"
            monkeypatch.setattr(crm, "patient_lookup", original)
            return await crm.CRMClient().lookup_active_appointments_by_phone(PHONE)

    with pytest.raises(crm.CRMError, match="incomplete"):
        asyncio.run(run())
    assert set(attempts) == set(crm.phone_lookup_variants(PHONE))
    assert attempts["8" + PHONE[1:]] == 2


def test_monkeypatched_raw_lookup_contract_remains_supported(monkeypatch):
    calls = []

    async def raw(phone):
        calls.append(phone)
        return NEW

    async def wrapper(phone):
        return await raw(phone)

    wrapper.__module__ = "crm"
    monkeypatch.setattr(crm, "patient_lookup", wrapper)
    monkeypatch.setattr(crm, "patient_lookup_raw", raw)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))
    assert set(calls) == set(crm.phone_lookup_variants(PHONE)) and result["ok"]


def test_booking_post_is_never_retried_by_lookup_recovery(monkeypatch):
    attempts = []
    delays = _no_wait(monkeypatch)

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(crm, "_client", lambda: client)
            return await crm.book_appointment(patient_name="Synthetic", phone=PHONE,
                doctor_login="synthetic-doctor", date="2031-01-03", time_start="10:00")

    with pytest.raises(crm.CRMResponseError):
        asyncio.run(run())
    assert len(attempts) == 1 and attempts[0].method == "POST" and not delays


def test_finished_event_loops_are_not_retained_by_limiters():
    references = []

    async def run():
        references.append(weakref.ref(asyncio.get_running_loop()))
        limiter = crm._lookup_limiter()
        async with limiter:
            async def compete():
                async with limiter:
                    await asyncio.sleep(0)
            await asyncio.gather(*(compete() for _ in range(4)))

    asyncio.run(run())
    gc.collect()
    assert all(reference() is None for reference in references)

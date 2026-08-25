from __future__ import annotations

import pytest

@pytest.fixture(autouse=True)
def _new_leads_only_test_default(monkeypatch: pytest.MonkeyPatch):
    """Продакшен работает только с новыми лидами — тесты тоже.

    Раньше здесь стояло NEW_LEADS_ONLY=false для всех файлов, кроме одного:
    легаси-воронка иначе не проходила свои же тесты. Воронки больше нет, и
    режим тестов совпадает с продакшеном.
    """
    monkeypatch.setenv("NEW_LEADS_ONLY", "true")
    _reload_settings()
    yield
    _reload_settings()


@pytest.fixture(autouse=True)
def _no_real_openai_calls(monkeypatch: pytest.MonkeyPatch):
    """The suite must never reach the real OpenAI API.

    ``OPENAI_API_KEY`` is empty by default so the GPT-first agent loop skips
    itself and tests exercise the deterministic path. Tests that do cover the
    agent loop set the key themselves via monkeypatch *and* stub the client,
    so they never open a socket either. Without this guard one module setting
    the key at import time would silently send every other module's turns to
    the network — hanging the suite and burning budget.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")
    _reload_settings()
    yield
    _reload_settings()


def _reload_settings() -> None:
    """Drop the cached Settings and make sure the active SQLite file has tables.

    Test modules each point ``SQLITE_PATH`` at their own temp file at import
    time, while ``get_settings`` is an ``lru_cache``. Clearing that cache can
    therefore switch ``state`` onto a database whose ``init_db()`` ran against
    a different path, which surfaces as "no such table: sessions". Re-running
    the idempotent ``init_db()`` after every cache clear keeps the schema and
    the active path in sync no matter which subset of tests is executed.
    """
    try:
        from config import get_settings

        get_settings.cache_clear()
    except Exception:
        return
    try:
        import state

        state.init_db()
    except Exception:
        pass


# NB: deliberately no fixture that restores the ``crm`` module between tests.
# Two legacy runners (run_golden.py, run_dialog_tests.py) install offline fakes
# straight onto ``crm``, and several older tests rely on those fakes still
# being in place. Restoring the real functions makes those tests attempt live
# HTTP calls to the CRM, which doubles the suite runtime and makes it depend on
# an external service. Tests that must assert against the real client check the
# module source instead of the runtime attribute (see
# test_production_readiness.py).


@pytest.fixture(autouse=True)
def _clear_booking_claims():
    """Start every test with an empty booking-claim store.

    Booking claims are deliberately durable in production — that is what stops
    two concurrent messages from creating two appointments. Tests reuse the
    same slot repeatedly, so without this the second test in a file would be
    refused with "booking already in progress".
    """
    def _clear() -> None:
        # Deliberately unguarded: a swallowed failure here would leave a durable
        # claim behind and the *next* test would be refused with "booking
        # already in progress" — a confusing failure far from its cause.
        import state

        with state._connect() as conn:
            conn.execute(state._BOOKING_CLAIMS_DDL)
            conn.execute("DELETE FROM booking_claims")

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _no_real_crm_http(monkeypatch: pytest.MonkeyPatch):
    """The suite must never reach the real CRM over the network.

    Most tests stub ``crm.check_slots`` / ``crm.book_appointment`` directly, but
    a few older ones exercise code paths that also call ``patient_lookup``
    without stubbing it. Those requests used to be absorbed by an offline fake
    another module had leaked onto ``crm``; once that is not the case they hit
    the production CRM host, which is slow, flaky and depends on an external
    service being up.

    Failing fast keeps the observable behaviour identical (the calling code
    already handles a CRM connection error) without opening a socket. Tests that
    want their own transport simply override ``crm._client`` themselves.
    """
    import httpx

    class _RefuseTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "outbound CRM HTTP is disabled in tests; stub the crm function you need",
                request=request,
            )

    client = httpx.AsyncClient(transport=_RefuseTransport())
    monkeypatch.setattr("crm._client", lambda: client, raising=False)
    yield

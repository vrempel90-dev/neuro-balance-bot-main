"""Executed durability/ordering tests against an isolated real SQLite database.

External CRM, AI and message providers are intentionally not called here.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import threading

import pytest

import production_inbox as inbox
import state
from config import get_settings


@pytest.fixture(autouse=True)
def isolated_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "inbox.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    inbox.init_db()
    yield
    get_settings.cache_clear()


def add(key: str, chat: str = "synthetic-chat", text: str = "test"):
    return inbox.enqueue(
        message_key=key, chat_id=chat,
        payload={"messages": [{"text": text}]},
        raw_msg={"id": key, "text": text}, parse_meta={"ok": True},
    )


def test_acknowledged_job_is_committed_before_processing():
    result = add("durable-one")
    assert result["accepted"] and not result["duplicate"]
    # A fresh connection, not an in-memory cache, sees the accepted body.
    with state._connect() as conn:
        row = conn.execute("SELECT * FROM webhook_inbox").fetchone()
    assert row["status"] == "pending"
    assert json.loads(row["payload_json"])["raw_msg"]["text"] == "test"


def test_duplicate_webhook_preserves_original_job_and_body():
    first = add("same-provider-message", text="original")
    second = add("same-provider-message", text="unexpected changed retry")
    assert first["job_id"] == second["job_id"]
    assert second["duplicate"] is True
    assert inbox.status_summary()["counts"]["pending"] == 1
    assert inbox.claim_next()["raw_msg"]["text"] == "original"


def test_concurrent_enqueue_accepts_exactly_one_duplicate():
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: add("racing-webhook"), range(12)))
    assert sum(not result["duplicate"] for result in results) == 1
    assert inbox.status_summary()["counts"]["pending"] == 1


def test_fifo_per_chat_allows_other_chat_to_progress():
    a1 = add("a-1", "a")
    a2 = add("a-2", "a")
    b1 = add("b-1", "b")
    assert inbox.claim_next()["job_id"] == a1["job_id"]
    assert inbox.claim_next()["job_id"] == b1["job_id"]
    assert inbox.claim_next() is None
    inbox.finish(a1["job_id"])
    assert inbox.claim_next()["job_id"] == a2["job_id"]


def test_restart_resumes_pending_and_quarantines_interrupted_turn():
    interrupted = add("interrupted")
    pending = add("not-started", "another-chat")
    inbox.claim_next()
    assert inbox.recover_interrupted() == 1
    counts = inbox.status_summary()["counts"]
    assert counts["uncertain"] == 1 and counts["pending"] == 1
    assert add("interrupted")["duplicate"] is True
    assert inbox.claim_next()["job_id"] == pending["job_id"]
    assert inbox.recover_interrupted() == 1
    assert inbox.claim_next() is None
    assert interrupted["job_id"] != pending["job_id"]


def test_finished_body_is_removed_but_duplicate_key_remains():
    item = add("finished-sensitive", text="synthetic private data")
    inbox.claim_next()
    inbox.finish(item["job_id"])
    with state._connect() as conn:
        row = conn.execute("SELECT payload_json,status FROM webhook_inbox").fetchone()
    assert row["payload_json"] == "{}" and row["status"] == "done"
    assert add("finished-sensitive")["duplicate"] is True


def test_failed_or_uncertain_jobs_are_never_automatically_retried():
    item = add("uncertain-request")
    inbox.claim_next()
    inbox.finish(item["job_id"], "uncertain", error_type="ReadTimeout")
    assert inbox.claim_next() is None
    assert add("uncertain-request")["status"] == "uncertain"
    assert inbox.status_summary()["counts"]["uncertain"] == 1


def test_uncertain_chat_is_quarantined_until_reconciliation_other_chats_continue():
    first = add("uncertain-first", "chat-a")
    second = add("new-message-after-uncertainty", "chat-a")
    other = add("unrelated-chat", "chat-b")
    assert inbox.claim_next()["job_id"] == first["job_id"]
    inbox.finish(first["job_id"], "uncertain")
    assert inbox.status_summary()["quarantined_chats"] == 1
    assert inbox.status_summary()["pending_blocked_by_uncertain"] == 1
    assert inbox.claim_next()["job_id"] == other["job_id"]
    assert inbox.claim_next() is None
    assert inbox.resolve_uncertain(first["job_id"], resolution="done")
    assert inbox.status_summary()["quarantined_chats"] == 0
    assert inbox.status_summary()["pending_blocked_by_uncertain"] == 0
    assert inbox.claim_next()["job_id"] == second["job_id"]
    assert inbox.resolve_uncertain(first["job_id"], resolution="done") is False


def test_uncertain_resolution_cannot_requeue_or_replay_job():
    first = add("uncertain-no-requeue")
    inbox.claim_next()
    inbox.finish(first["job_id"], "uncertain")
    with pytest.raises(ValueError, match="final reconciliation"):
        inbox.resolve_uncertain(first["job_id"], resolution="pending")
    assert inbox.claim_next() is None


def test_aggregate_status_excludes_sensitive_identifiers_and_text():
    add("private-provider-id", chat="synthetic-private-chat", text="synthetic patient complaint")
    status = json.dumps(inbox.status_summary())
    assert "synthetic-private-chat" not in status
    assert "private-provider-id" not in status
    assert "complaint" not in status


def test_rejects_unstable_or_oversized_message_before_acknowledgement():
    with pytest.raises(ValueError, match="stable message"):
        add("")
    with pytest.raises(ValueError, match="size limit"):
        add("oversized", text="x" * (2 * 1024 * 1024))
    assert inbox.status_summary()["counts"]["pending"] == 0


def test_completed_retention_preserves_unresolved_jobs():
    old_done = add("old-done", "done-chat")
    old_uncertain = add("old-uncertain", "uncertain-chat")
    inbox.claim_next()
    inbox.finish(old_done["job_id"])
    inbox.claim_next()
    inbox.finish(old_uncertain["job_id"], "uncertain")
    with state._connect() as conn:
        conn.execute("UPDATE webhook_inbox SET updated_at='2000-01-01T00:00:00+00:00'")
    assert inbox.purge_completed() == 1
    assert inbox.status_summary()["counts"]["uncertain"] == 1


async def wait_until(predicate, timeout: float = 3.0):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("timed out waiting for worker state")
        await asyncio.sleep(0.02)


def test_worker_processes_pending_jobs_after_new_instance_starts():
    add("restart-pending")
    calls = []

    async def scenario():
        async def process(job):
            calls.append(job["raw_msg"]["id"])
            return {"ok": True}
        worker = inbox.InboxWorker(process, poll_seconds=0.05)
        worker.start()
        await wait_until(lambda: inbox.status_summary()["counts"]["done"] == 1)
        assert worker.status()["running"]
        await worker.stop()
        assert not worker.status()["running"]
    asyncio.run(scenario())
    assert calls == ["restart-pending"]


def test_worker_parallel_clients_and_ordered_same_client():
    add("a-first", "a")
    add("a-second", "a")
    add("b-first", "b")
    order = []

    async def scenario():
        first_started = asyncio.Event()
        other_started = asyncio.Event()
        release_first = asyncio.Event()
        async def process(job):
            key = job["raw_msg"]["id"]
            order.append(key)
            if key == "a-first":
                first_started.set()
                await release_first.wait()
            if key == "b-first":
                other_started.set()
            return {"ok": True}
        worker = inbox.InboxWorker(process, concurrency=4, poll_seconds=0.05)
        worker.start()
        await asyncio.wait_for(first_started.wait(), 2)
        await asyncio.wait_for(other_started.wait(), 2)
        assert "a-second" not in order
        release_first.set()
        await wait_until(lambda: inbox.status_summary()["counts"]["done"] == 3)
        await worker.stop()
    asyncio.run(scenario())
    assert order.index("a-first") < order.index("a-second")


@pytest.mark.parametrize("outcome", ["exception", "timeout", "cancelled", "send_failed", "crm_lookup_failed", "malformed_result"])
def test_worker_quarantines_ambiguous_operations_without_repeat(outcome):
    add("ambiguous-operation")
    calls = []

    async def scenario():
        entered = asyncio.Event()
        async def process(job):
            calls.append(job["job_id"])
            entered.set()
            if outcome == "exception":
                raise RuntimeError("synthetic failure after possible remote side effect")
            if outcome in {"timeout", "cancelled"}:
                await asyncio.sleep(10)
            if outcome == "malformed_result":
                return None
            return {"ok": True, "no_reply_reason": outcome}
        worker = inbox.InboxWorker(process, poll_seconds=0.05, job_timeout=0.1 if outcome == "timeout" else 180)
        worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == "cancelled":
            await worker.stop(timeout=0.1)
        else:
            await wait_until(lambda: sum(inbox.status_summary()["counts"][s] for s in ["uncertain", "failed"]) == 1)
            await worker.stop()
        assert len(calls) == 1
        counts = inbox.status_summary()["counts"]
        assert counts["processing"] == 0
        assert counts["failed" if outcome == "crm_lookup_failed" else "uncertain"] == 1
        assert inbox.claim_next() is None
    asyncio.run(scenario())


def enable_durable_ingress(monkeypatch):
    import main
    monkeypatch.setenv("DURABLE_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    return main


def webhook_payload(message_id="http-message", chat_id="synthetic-chat"):
    return {
        "channelId": "synthetic-channel", "chatType": "whatsapp",
        "messages": [{"messageId": message_id, "chatId": chat_id,
                      "text": "synthetic message", "status": "inbound"}],
    }


def sync_wait_until(predicate, timeout=3):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("timed out waiting for HTTP worker")
        time.sleep(0.02)


def test_http_ack_is_fast_while_actual_lifespan_worker_processor_is_blocked(monkeypatch):
    from fastapi.testclient import TestClient
    main = enable_durable_ingress(monkeypatch)
    started, released, finished = threading.Event(), threading.Event(), threading.Event()

    async def blocked_processor(job):
        started.set()
        until = time.monotonic() + 5
        while not released.is_set() and time.monotonic() < until:
            await asyncio.sleep(0.01)
        finished.set()
        return {"ok": True}

    monkeypatch.setattr(main, "_process_inbox_job", blocked_processor)
    with TestClient(main.app) as client:
        try:
            before = time.monotonic()
            response = client.post("/webhook/wazzup", json=webhook_payload())
            elapsed = time.monotonic() - before
            assert response.status_code == 200
            assert response.json()["accepted_count"] == 1
            assert elapsed < 1.0, "HTTP acknowledgement waited for conversation processing"
            assert not finished.is_set()
            assert sum(inbox.status_summary()["counts"][s] for s in ("pending", "processing")) == 1
            assert started.wait(timeout=2), "lifespan did not start the durable worker"
        finally:
            released.set()
        sync_wait_until(lambda: inbox.status_summary()["counts"]["done"] == 1)
    assert main.app.state.inbox_worker is None


def test_http_duplicate_across_all_aliases_commits_one_job(monkeypatch):
    from fastapi.testclient import TestClient
    main = enable_durable_ingress(monkeypatch)
    # Without lifespan: exercise persistent acceptance independently of worker
    # progress, then verify every alias addresses the very same durable key.
    with_client = TestClient(main.app)
    results = [with_client.post(path, json=webhook_payload()).json() for path in main.WAZZUP_WEBHOOK_PATHS]
    assert [item["duplicate_count"] for item in results] == [0, 1, 1, 1]
    assert all(item["accepted_count"] == 1 for item in results)
    assert inbox.status_summary()["counts"]["pending"] == 1
    with_client.close()


def test_lifespan_processes_previously_committed_pending_request_after_restart(monkeypatch):
    from fastapi.testclient import TestClient
    main = enable_durable_ingress(monkeypatch)
    client_before_restart = TestClient(main.app)
    result = client_before_restart.post("/webhook", json=webhook_payload("committed-before-restart"))
    assert result.status_code == 200
    client_before_restart.close()
    assert inbox.status_summary()["counts"]["pending"] == 1
    seen = []

    async def process(job):
        seen.append(job["raw_msg"]["messageId"])
        return {"ok": True}

    monkeypatch.setattr(main, "_process_inbox_job", process)
    with TestClient(main.app):
        sync_wait_until(lambda: inbox.status_summary()["counts"]["done"] == 1)
        assert main.app.state.inbox_worker.status()["running"]
    assert seen == ["committed-before-restart"]
    assert main.app.state.inbox_worker is None


def test_http_batch_preserves_each_message_identity_and_envelope_routing(monkeypatch):
    from fastapi.testclient import TestClient
    main = enable_durable_ingress(monkeypatch)
    payload = {
        "channelId": "synthetic-envelope-channel", "chatType": "instagram",
        "timestamp": "2026-10-27T18:00:00Z",
        "messages": [
            {"messageId": "batch-a", "chatId": "synthetic-a", "text": "first", "status": "inbound"},
            {"messageId": "batch-b", "chatId": "synthetic-b", "text": "second", "status": "inbound"},
        ],
    }
    seen = []

    async def process(job):
        assert "messages" not in job["payload"]
        normalized = main._normalize_wazzup_message(job["payload"], job["raw_msg"])
        seen.append(normalized)
        return {"ok": True}

    monkeypatch.setattr(main, "_process_inbox_job", process)
    with TestClient(main.app) as client:
        result = client.post("/api/wazzup/webhook", json=payload)
        assert result.json()["accepted_count"] == 2
        sync_wait_until(lambda: inbox.status_summary()["counts"]["done"] == 2)
    expected = [main._normalize_wazzup_message(payload, raw) for raw in payload["messages"]]
    assert sorted(seen, key=lambda m: m["message_id"]) == sorted(expected, key=lambda m: m["message_id"])
    assert len({item["message_key"] for item in seen}) == 2


def test_http_provider_retry_after_completed_job_does_not_run_processor_again(monkeypatch):
    from fastapi.testclient import TestClient
    main = enable_durable_ingress(monkeypatch)
    seen = []

    async def process(job):
        seen.append(job["job_id"])
        return {"ok": True}

    monkeypatch.setattr(main, "_process_inbox_job", process)
    with TestClient(main.app) as client:
        client.post("/wazzup/webhook", json=webhook_payload("completed-provider-retry"))
        sync_wait_until(lambda: inbox.status_summary()["counts"]["done"] == 1)
        retry = client.post("/api/wazzup/webhook", json=webhook_payload("completed-provider-retry"))
        assert retry.json()["duplicate_count"] == 1
    assert len(seen) == 1

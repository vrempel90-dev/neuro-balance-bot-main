"""Отправка в WhatsApp блокировалась из-за расхождения каналов.

Прод 18.08.2026: почти все входящие приходили с канала
15e83f65-253b-4c16-a894-effd547d6b76, а бот был сконфигурирован на
7e4fc1db-0061-481c-9e67-309455af8aeb. На каждое сообщение срабатывал
wazzup_send_start со skipped=true и silent_reason="channel_id_mismatch":
ответ генерировался корректно, но не уходил пациенту.

Причина — конфигурация, а не код: в Railway задан WAZZUP_CHANNEL_ID, но НЕ задан
WAZZUP_ALLOWED_CHANNEL_IDS, хотя механизм списка каналов в коде уже есть и в
env.example перечислены оба канала. Без списка main.py сравнивает входящий канал
с единственным WAZZUP_CHANNEL_ID.

Здесь закреплено: репродукция блокировки, работа списка каналов, и алерт,
который делает такое расхождение заметным сразу, а не постфактум по логам.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("WAZZUP_API_KEY", "test-key")
os.environ.setdefault("BOT_AUTO_REPLY_ENABLED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main
import state
from config import get_settings

PRIMARY = "7e4fc1db-0061-481c-9e67-309455af8aeb"
SECONDARY = "15e83f65-253b-4c16-a894-effd547d6b76"


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "channel.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    # Буфер последних событий — модульный и общий для всех тестов: без сброса
    # алерты из соседних тестов протекали бы в проверки этого.
    state._LAST_EVENTS_MEMORY.clear()
    monkeypatch.setattr(main, "is_bot_work_time", lambda *a, **k: True)
    yield
    get_settings.cache_clear()


def _payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "chatId": "77010000001",
        "phone": "77010000001",
        "text": "Здравствуйте, болит спина",
        "messageId": "msg-channel-1",
        "timestamp": "2026-08-18T21:00:00+05:00",
        "direction": "incoming",
        "channelId": PRIMARY,
    }
    data.update(overrides)
    return data


def _install_handler(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def handler(message):
        return {"answer": "Здравствуйте! Сколько Вам лет?", "should_send_wazzup": True}

    async def sender(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    return sent


# --- репродукция прод-конфигурации -----------------------------------------


def test_production_config_blocks_secondary_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прод-конфигурация 18.08.2026: список каналов не задан — отправка блокируется."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.delenv("WAZZUP_ALLOWED_CHANNEL_IDS", raising=False)
    get_settings.cache_clear()
    sent = _install_handler(monkeypatch)

    response = TestClient(main.app).post(
        "/wazzup/webhook", json=_payload(channelId=SECONDARY, messageId="msg-repro-block")
    )

    assert response.status_code == 200
    assert response.json()["should_send_wazzup"] is False
    assert response.json()["no_reply_reason"] == "channel_id_mismatch"
    assert sent == [], "ответ не должен был уйти при заблокированном канале"


def test_allowed_list_unblocks_secondary_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """С WAZZUP_ALLOWED_CHANNEL_IDS ответ уходит — и именно в канал пациента."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", f"{PRIMARY},{SECONDARY}")
    get_settings.cache_clear()
    sent = _install_handler(monkeypatch)

    response = TestClient(main.app).post(
        "/wazzup/webhook", json=_payload(channelId=SECONDARY, messageId="msg-unblocked")
    )

    assert response.status_code == 200
    assert response.json()["should_send_wazzup"] is True
    assert len(sent) == 1
    assert sent[0]["channel_id"] == SECONDARY, "ответ ушёл не в тот канал, откуда написал пациент"


def test_primary_channel_still_works_with_allowed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", f"{PRIMARY},{SECONDARY}")
    get_settings.cache_clear()
    sent = _install_handler(monkeypatch)

    response = TestClient(main.app).post(
        "/wazzup/webhook", json=_payload(channelId=PRIMARY, messageId="msg-primary")
    )

    assert response.json()["should_send_wazzup"] is True
    assert sent[0]["channel_id"] == PRIMARY


def test_unknown_channel_still_blocked_with_allowed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Список каналов не превращается в "разрешить всё"."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", f"{PRIMARY},{SECONDARY}")
    get_settings.cache_clear()
    sent = _install_handler(monkeypatch)

    response = TestClient(main.app).post(
        "/wazzup/webhook", json=_payload(channelId="00000000-dead-beef-0000-000000000000", messageId="msg-unknown")
    )

    assert response.json()["should_send_wazzup"] is False
    assert response.json()["no_reply_reason"] == "channel_id_mismatch"
    assert sent == []


# --- список каналов --------------------------------------------------------


def test_primary_channel_is_added_to_allowed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Добавление второго канала не должно молча отключать основной."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", SECONDARY)
    get_settings.cache_clear()

    allowed = main._allowed_channel_ids(get_settings())

    assert SECONDARY in allowed
    assert PRIMARY in allowed, "основной канал выпал из списка — бот замолчал бы на нём"


def test_empty_allowed_list_keeps_single_channel_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.delenv("WAZZUP_ALLOWED_CHANNEL_IDS", raising=False)
    get_settings.cache_clear()

    assert main._allowed_channel_ids(get_settings()) == []


def test_allowed_list_tolerates_spaces_and_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", f" {SECONDARY} , , {PRIMARY} ")
    get_settings.cache_clear()

    allowed = main._allowed_channel_ids(get_settings())

    assert allowed.count(PRIMARY) == 1
    assert SECONDARY in allowed


# --- алерт на расхождение каналов ------------------------------------------


def _mismatch_alerts() -> list[dict[str, Any]]:
    return [e for e in state.recent_memory_events(limit=200) if e["event_type"] == "wazzup_channel_mismatch_alert"]


def test_alert_fires_when_mismatch_share_is_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "5")
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_RATIO", "0.5")
    get_settings.cache_clear()

    for i in range(6):
        main._track_channel_check(f"chat{i}", SECONDARY, False, [], PRIMARY)

    alerts = _mismatch_alerts()
    assert len(alerts) == 1
    payload = alerts[0]["payload"]
    assert payload["level"] == "critical"
    assert payload["unexpected_channel_id"] == SECONDARY
    assert payload["mismatch_ratio"] >= 0.5
    assert "WAZZUP_ALLOWED_CHANNEL_IDS" in payload["note"]


def test_alert_is_not_repeated_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Один алерт на окно — иначе логи Railway залило бы шумом."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "3")
    get_settings.cache_clear()

    for i in range(20):
        main._track_channel_check(f"spam{i}", SECONDARY, False, [], PRIMARY)

    assert len(_mismatch_alerts()) == 1


def test_no_alert_below_minimum_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пара расхождений — не повод будить: ждём статистически значимого объёма."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "10")
    get_settings.cache_clear()

    for i in range(3):
        main._track_channel_check(f"few{i}", SECONDARY, False, [], PRIMARY)

    assert _mismatch_alerts() == []


def test_no_alert_when_channels_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "3")
    get_settings.cache_clear()

    for i in range(10):
        main._track_channel_check(f"ok{i}", PRIMARY, True, [PRIMARY], PRIMARY)

    assert _mismatch_alerts() == []


def test_no_alert_when_mismatch_share_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """Единичное расхождение на фоне нормального трафика — не алерт."""
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", PRIMARY)
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "5")
    monkeypatch.setenv("CHANNEL_MISMATCH_ALERT_RATIO", "0.5")
    get_settings.cache_clear()

    for i in range(9):
        main._track_channel_check(f"good{i}", PRIMARY, True, [PRIMARY], PRIMARY)
    main._track_channel_check("bad", SECONDARY, False, [PRIMARY], PRIMARY)

    assert _mismatch_alerts() == []


def test_tracking_never_breaks_message_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Наблюдаемость не должна ронять обработку сообщения пациента."""
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(state, "count_events_since", boom)
    main._track_channel_check("chat-x", SECONDARY, False, [], PRIMARY)  # не должно бросить

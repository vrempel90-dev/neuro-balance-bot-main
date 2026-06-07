from __future__ import annotations

from datetime import datetime, timedelta, timezone
from config import get_settings


def astana_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=get_settings().timezone_offset_hours)


def is_bot_work_time(now: datetime | None = None) -> bool:
    """Бот работает только вне рабочего времени КЦ.

    По умолчанию: с 20:00 до 08:00 по времени Астаны.
    Поддерживает интервал через полночь: 20 -> 8.
    """
    settings = get_settings()
    if not settings.work_hours_guard_enabled:
        return True

    now = now or astana_now()
    hour = now.hour
    start = settings.bot_active_from_hour
    end = settings.bot_active_until_hour

    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def daytime_handoff_text() -> str:
    settings = get_settings()
    return settings.daytime_handoff_message


def active_window_id(now: datetime | None = None) -> str:
    """ID текущего ночного окна работы бота.

    Нужно, чтобы бот НЕ продолжал дневной диалог админов.
    На первое сообщение пациента в новом ночном окне история этого чата очищается,
    дальше в пределах этой же ночи бот помнит только ночную переписку.
    """
    settings = get_settings()
    now = now or astana_now()
    start = settings.bot_active_from_hour
    end = settings.bot_active_until_hour

    # Если интервал через полночь, например 20:00–08:00,
    # то сообщения после 00:00 относятся к ночному окну предыдущей даты.
    window_date = now.date()
    if start > end and now.hour < end:
        window_date = (now - timedelta(days=1)).date()

    return f"{window_date.isoformat()}_{start:02d}-{end:02d}"

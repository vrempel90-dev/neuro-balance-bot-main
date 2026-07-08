from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from config import get_settings


def bot_timezone() -> ZoneInfo:
    settings = get_settings()
    return ZoneInfo(getattr(settings, "bot_timezone", "Asia/Almaty") or "Asia/Almaty")


def astana_now() -> datetime:
    return datetime.now(bot_timezone())


def _parse_hhmm(value: str) -> time:
    hour, minute = str(value or "00:00").strip().split(":", 1)
    return time(int(hour), int(minute))


def _time_in_window(current: time, start_t: time, end_t: time) -> bool:
    current = current.replace(second=0, microsecond=0)
    if start_t == end_t:
        return True
    if start_t < end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t


def work_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    settings = get_settings()
    local = (now or astana_now()).astimezone(bot_timezone())
    start_t = _parse_hhmm(getattr(settings, "bot_work_start", "20:00"))
    end_t = _parse_hhmm(getattr(settings, "bot_work_end", "08:00"))
    start_dt = local.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
    end_dt = local.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
    if start_t > end_t:
        if local.time() < end_t:
            start_dt -= timedelta(days=1)
        else:
            end_dt += timedelta(days=1)
    return start_dt, end_dt


def next_work_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    settings = get_settings()
    local = (now or astana_now()).astimezone(bot_timezone())
    start_t = _parse_hhmm(getattr(settings, "bot_work_start", "20:00"))
    end_t = _parse_hhmm(getattr(settings, "bot_work_end", "08:00"))
    start_dt, end_dt = work_bounds(local)
    if start_dt <= local < end_dt:
        return start_dt, end_dt
    if local >= end_dt:
        start_dt += timedelta(days=1)
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def is_normal_work_time(now: datetime | None = None) -> bool:
    """Configured night AI window in business timezone, including midnight wrap."""
    settings = get_settings()
    local = (now or astana_now()).astimezone(bot_timezone())
    start_t = _parse_hhmm(getattr(settings, "bot_work_start", "20:00"))
    end_t = _parse_hhmm(getattr(settings, "bot_work_end", "08:00"))
    return _time_in_window(local.time(), start_t, end_t)


def is_test_window_time(now: datetime | None = None) -> bool:
    """One-day daytime test window that never changes the normal night schedule."""
    settings = get_settings()
    if not bool(getattr(settings, "bot_test_window_enabled", False)):
        return False
    local = (now or astana_now()).astimezone(bot_timezone())
    if local.date().isoformat() != str(getattr(settings, "bot_test_window_date", "")):
        return False
    start_t = _parse_hhmm(getattr(settings, "bot_test_window_start", "14:00"))
    end_t = _parse_hhmm(getattr(settings, "bot_test_window_end", "14:30"))
    return _time_in_window(local.time(), start_t, end_t)


def time_gate_status(now: datetime | None = None) -> dict[str, bool | str]:
    settings = get_settings()
    if not settings.work_hours_guard_enabled:
        return {
            "normal_work_time_now": True,
            "test_window_now": False,
            "bot_work_time_now": True,
            "time_gate_reason": "work_hours_guard_disabled",
        }
    normal = is_normal_work_time(now)
    test = is_test_window_time(now)
    active = normal or test
    if test and not normal:
        reason = "test_window_active"
    elif normal:
        reason = "inside_night_window"
    else:
        reason = "outside_bot_work_time"
    return {
        "normal_work_time_now": normal,
        "test_window_now": test,
        "bot_work_time_now": active,
        "time_gate_reason": reason,
    }


def is_bot_work_time(now: datetime | None = None) -> bool:
    return bool(time_gate_status(now)["bot_work_time_now"])


def daytime_handoff_text() -> str:
    settings = get_settings()
    return settings.daytime_handoff_message

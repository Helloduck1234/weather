"""Local-day windows and the rounding rules that settle a market."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def day_window_utc(day: date, tz: str, clock: str) -> tuple[int, int]:
    """Half-open [start, end) unix window for a local calendar day.

    local_dst follows the wall clock, so a day can run 23 or 25 hours.
    local_standard ignores DST and is always 24 hours (NWS climate day).
    """
    zone = ZoneInfo(tz)
    next_day = day + timedelta(days=1)
    if clock == "local_dst":
        start = datetime.combine(day, time.min, zone)
        end = datetime.combine(next_day, time.min, zone)
    elif clock == "local_standard":
        offset = _standard_offset(day, zone)
        fixed = timezone(offset)
        start = datetime.combine(day, time.min, fixed)
        end = datetime.combine(next_day, time.min, fixed)
    else:
        raise ValueError(f"unknown day clock: {clock}")
    return int(start.timestamp()), int(end.timestamp())


def _standard_offset(day: date, zone: ZoneInfo) -> timedelta:
    noon = datetime.combine(day, time(12), zone)
    return noon.utcoffset() - noon.dst()


def clock_date(unix: int, tz: str, clock: str) -> date:
    """The calendar date a moment falls on under this day clock."""
    t = datetime.fromtimestamp(unix, ZoneInfo(tz))
    return (t - (t.dst() or timedelta(0))).date() if clock == "local_standard" else t.date()


def local_hour(unix: int, tz: str) -> float:
    t = datetime.fromtimestamp(unix, ZoneInfo(tz))
    return t.hour + t.minute / 60


def round_half_up(x: float) -> int:
    """Nearest integer, exact halves go up, so -8.5 becomes -8."""
    return math.floor(round(x, 6) + 0.5)


def c_to_unit(c: float, unit: str) -> float:
    return c if unit == "C" else c * 9 / 5 + 32


def market_degree(c: float, unit: str) -> int:
    """The whole degree a Celsius reading counts as in a market of this unit."""
    return round_half_up(c_to_unit(c, unit))

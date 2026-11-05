from datetime import date, datetime, timedelta, timezone

import pytest

from weatherbot.timeutil import (
    c_to_unit,
    day_window_utc,
    clock_date,
    local_hour,
    market_degree,
    round_half_up,
)


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize(
    "day, tz, clock, start, end",
    [
        # 25-hour day: US DST ends 2026-11-01.
        (date(2026, 11, 1), "America/New_York", "local_dst",
         utc(2026, 11, 1, 4), utc(2026, 11, 2, 5)),
        # 23-hour day: US DST starts 2026-03-08.
        (date(2026, 3, 8), "America/New_York", "local_dst",
         utc(2026, 3, 8, 5), utc(2026, 3, 9, 4)),
        # European DST ends a week earlier than the US.
        (date(2026, 10, 25), "Europe/London", "local_dst",
         utc(2026, 10, 24, 23), utc(2026, 10, 26, 0)),
        # Half-hour offset with no DST.
        (date(2026, 10, 31), "Asia/Kolkata", "local_dst",
         utc(2026, 10, 30, 18, 30), utc(2026, 10, 31, 18, 30)),
        # Southern hemisphere summer is on DST.
        (date(2026, 1, 15), "Pacific/Auckland", "local_dst",
         utc(2026, 1, 14, 11), utc(2026, 1, 15, 11)),
        # Kalshi and NWS style: standard time all year, always 24 hours.
        (date(2026, 7, 4), "America/New_York", "local_standard",
         utc(2026, 7, 4, 5), utc(2026, 7, 5, 5)),
        (date(2026, 11, 1), "America/New_York", "local_standard",
         utc(2026, 11, 1, 5), utc(2026, 11, 2, 5)),
        (date(2026, 1, 15), "Pacific/Auckland", "local_standard",
         utc(2026, 1, 14, 12), utc(2026, 1, 15, 12)),
    ],
)
def test_day_window(day, tz, clock, start, end):
    assert day_window_utc(day, tz, clock) == (start, end)


def test_day_window_rejects_unknown_clock():
    with pytest.raises(ValueError):
        day_window_utc(date(2026, 1, 1), "UTC", "solar")


def test_clock_date_differs_from_utc_date_across_midnight():
    # 03:00Z on Oct 31 is still the evening of Oct 30 in Chicago.
    assert clock_date(utc(2026, 10, 31, 3), "America/Chicago", "local_dst") == date(2026, 10, 30)


def test_local_hour_includes_minutes():
    assert local_hour(utc(2026, 7, 1, 16, 30), "America/New_York") == 12.5


def test_clock_date_ignores_dst_on_the_standard_clock():
    # 00:30 EDT on July 2 is still 23:30 EST on July 1.
    moment = utc(2026, 7, 2, 4, 30)
    assert clock_date(moment, "America/New_York", "local_dst") == date(2026, 7, 2)
    assert clock_date(moment, "America/New_York", "local_standard") == date(2026, 7, 1)


@pytest.mark.parametrize("clock", ["local_dst", "local_standard"])
@pytest.mark.parametrize(
    "day, tz",
    [
        (date(2026, 7, 1), "America/New_York"),
        (date(2026, 3, 8), "America/New_York"),  # 23-hour DST day
        (date(2026, 11, 1), "America/New_York"),  # 25-hour DST day
        (date(2026, 4, 5), "Australia/Lord_Howe"),  # 30-minute fall back
        (date(2026, 10, 4), "Australia/Lord_Howe"),  # 30-minute spring forward
        (date(2026, 10, 31), "Asia/Kolkata"),  # Half-hour zone without DST
    ],
)
def test_clock_date_matches_day_window_boundaries(clock, day, tz):
    start, end = day_window_utc(day, tz, clock)
    assert clock_date(start - 1, tz, clock) == day - timedelta(days=1)
    assert clock_date(start, tz, clock) == day
    assert clock_date(end - 1, tz, clock) == day
    assert clock_date(end, tz, clock) == day + timedelta(days=1)


@pytest.mark.parametrize(
    "x, want",
    [
        (64.94, 65), (64.04, 64), (66.2, 66),
        (-8.5, -8), (13.9, 14), (17.4, 17), (16.5, 17), (-1.7, -2),
        # Float noise must not push a value across the half.
        (2.4999999999, 3), (64.49999999999999, 65),
    ],
)
def test_round_half_up(x, want):
    assert round_half_up(x) == want


@pytest.mark.parametrize(
    "c, unit, want",
    [
        # Worked examples from the market rules.
        (18.3, "F", 65), (17.8, "F", 64), (19.0, "F", 66), (-22.5, "F", -8),
        (13.9, "C", 14), (17.4, "C", 17), (16.5, "C", 17),
        (-1.7, "C", -2), (-8.5, "C", -8),
    ],
)
def test_market_degree_matches_rule_examples(c, unit, want):
    assert market_degree(c, unit) == want


def test_c_to_unit_leaves_celsius_alone():
    assert c_to_unit(21.4, "C") == 21.4


def test_c_to_unit_converts_fahrenheit():
    assert c_to_unit(100.0, "F") == 212.0

"""METAR report parsing and the qualifying daily high for each rule family."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .core import Rule
from .timeutil import day_window_utc

_T_GROUP = re.compile(r"(?:^|\s)T([01])(\d{3})[01]\d{3}(?=\s|$)")
_BODY_GROUP = re.compile(r"(?:^|\s)(M?\d{2})/(?:M?\d{2}|XX|//)?(?=\s|$)")
_SIX_HOURS = 6 * 3600
_DAY = 24 * 3600
_MAX_HOLE = 3 * 3600  # every configured station stayed under 2.6 hours over 204 station-days in October 2026
_MAX_OPENING_GAP = 2 * 3600


@dataclass(frozen=True)
class Obs:
    icao: str
    obs_time: int
    temp_c: float | None
    kind: str  # routine | speci
    raw: str
    max6h_c: float | None = None
    max24h_c: float | None = None


def parse_temp_c(raw: str) -> float | None:
    """Temperature from a report: the T remark in tenths, else the body group."""
    body, _, remarks = raw.partition(" RMK")
    m = _T_GROUP.search(remarks)
    if m:
        tenths = int(m.group(2))
        return -tenths / 10 if m.group(1) == "1" else tenths / 10
    m = _BODY_GROUP.search(body)
    if m:
        value = m.group(1)
        return -float(value[1:]) if value.startswith("M") else float(value)
    return None


def obs_from_json(row: dict) -> Obs:
    raw = row.get("rawOb") or ""
    kind = "speci" if raw.lstrip().startswith("SPECI") else "routine"
    return Obs(
        icao=row["icaoId"],
        obs_time=int(row["obsTime"]),
        temp_c=parse_temp_c(raw),
        kind=kind,
        raw=raw,
        max6h_c=row.get("maxT"),
        max24h_c=row.get("maxT24"),
    )


def qualifying_high_c(reports: Iterable[Obs], rule: Rule, day: date, tz: str) -> float | None:
    """Highest reading that counts under this rule so far, in Celsius."""
    start, end = day_window_utc(day, tz, rule.day_clock)
    values: list[float] = []
    for r in reports:
        if rule.obs_policy == "nws_cli":
            if r.max6h_c is not None and _spans_inside(r.obs_time, start, end):
                values.append(r.max6h_c)
            if r.max24h_c is not None and _is_full_day(r.obs_time, start):
                values.append(r.max24h_c)
        if r.temp_c is None or not start <= r.obs_time < end:
            continue
        if _counts(r, rule):
            values.append(r.temp_c)
    return max(values) if values else None


def covers_day(reports: Iterable[Obs], rule: Rule, day: date, tz: str, until: int | None = None) -> bool:
    """Whether reports leave no hole that could hide the high, up to `until`. A thin day reads falsely low."""
    start, end = day_window_utc(day, tz, rule.day_clock)
    reports = list(reports)
    if rule.obs_policy == "nws_cli" and _has_full_day(reports, start):
        return True
    stop = end if until is None else min(until, end)
    times = sorted(r.obs_time for r in reports if r.temp_c is not None and start <= r.obs_time < stop and _counts(r, rule))
    points = [start, *times, stop]
    return all(later - earlier <= _MAX_HOLE for earlier, later in zip(points, points[1:]))


def opening_temp_c(reports: Iterable[Obs], rule: Rule, day: date, tz: str) -> float | None:
    """Estimated temperature as an NWS day opens. When a front drops the temperature overnight, that first
    moment is the day's high: the sensor keeps it and no report lands on it. None once a sensor maximum
    shows that moment can't beat the high."""
    if rule.obs_policy != "nws_cli":
        return None
    start, _ = day_window_utc(day, tz, rule.day_clock)
    reports = list(reports)
    closed = _closed_spans(reports, qualifying_high_c(reports, rule, day, tz))
    if _has_full_day(reports, start) or any(first < start <= last for first, last in closed):
        return None
    usable = sorted((r for r in reports if r.temp_c is not None), key=lambda r: r.obs_time)
    before = [r for r in usable if r.obs_time < start]
    after = [r for r in usable if r.obs_time >= start]
    if not before or not after:
        return None
    earlier, later = before[-1], after[0]
    gap = later.obs_time - earlier.obs_time
    if gap > _MAX_OPENING_GAP:
        return None
    return earlier.temp_c + (start - earlier.obs_time) / gap * (later.temp_c - earlier.temp_c)


def unverified_high_c(reports: Iterable[Obs], rule: Rule, day: date, tz: str) -> float | None:
    """Highest NWS reading that no sensor maximum has settled. The true peak can sit above it, between reports."""
    if rule.obs_policy != "nws_cli":
        return None
    start, end = day_window_utc(day, tz, rule.day_clock)
    reports = list(reports)
    if _has_full_day(reports, start):
        return None
    closed = _closed_spans(reports, qualifying_high_c(reports, rule, day, tz))
    loose = [r.temp_c for r in reports
             if r.temp_c is not None and start <= r.obs_time < end
             and not any(first < r.obs_time <= last for first, last in closed)]
    return max(loose) if loose else None


def _counts(report: Obs, rule: Rule) -> bool:
    return rule.obs_policy != "routine" or report.kind == "routine"


def _spans_inside(report_time: int, start: int, end: int) -> bool:
    # A 6h group is only the day's own if every hour it spans belongs to the day.
    return start <= report_time - _SIX_HOURS and report_time < end


def _closed_spans(reports: Iterable[Obs], high: float | None) -> list[tuple[int, int]]:
    """Stretches a 6h group shows never topped the high. A group reaching outside the day can't raise the
    high, but it still caps it."""
    if high is None:
        return []
    return [(r.obs_time - _SIX_HOURS, r.obs_time) for r in reports if r.max6h_c is not None and r.max6h_c <= high]


def _has_full_day(reports: Iterable[Obs], start: int) -> bool:
    return any(r.max24h_c is not None and _is_full_day(r.obs_time, start) for r in reports)


def _is_full_day(report_time: int, start: int) -> bool:
    # ASOS files the 24h group shortly before midnight LST.
    return abs(report_time - _DAY - start) <= 3600


def latest_obs(reports: Iterable[Obs]) -> Obs | None:
    """Newest report with a temperature. SPECIs count: this anchors, it doesn't settle."""
    usable = [r for r in reports if r.temp_c is not None]
    return max(usable, key=lambda r: r.obs_time) if usable else None

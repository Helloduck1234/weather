"""Book resolved markets, and check each result against our reading of the rules.

The audit is the bot's early warning. A market's text can change resolver at any
time (polymarket.com moved from Wunderground to METAR in October 2026), and when
it does, the first sign is the winning bucket disagreeing with our own number.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .config import Config, Station
from .core import Bucket, Rule
from .ledger import Ledger
from .metar import covers_day, qualifying_high_c
from .timeutil import day_window_utc, market_degree

_SETTLE_DELAY = 3600  # the day's last reports and corrections are still arriving


def settle_venue(cfg: Config, ledger: Ledger, venue: Any, now: int) -> dict:
    counts: Counter = Counter()
    for row in ledger.unsettled_events(venue.name):
        station = cfg.stations.get(row["station"])
        if station is None:
            continue
        day = date.fromisoformat(row["day"])
        _, end = day_window_utc(day, station.tz, row["day_clock"])
        if now < end + _SETTLE_DELAY:
            continue

        pending = [
            m["market_id"] for m in ledger.event_markets(venue.name, row["event_id"]) if m["payout_yes"] is None
        ]
        try:
            outcomes = venue.outcomes(row["event_id"], pending)
        except Exception:  # one garbled reply can't be allowed to hold up every other event
            counts["errors"] += 1
            continue
        for market_id in pending:
            if market_id in outcomes:
                ledger.settle_market(venue.name, market_id, outcomes[market_id], now)
                counts["markets"] += 1

        if ledger.event_row(venue.name, row["event_id"])["settled_at"] is not None:
            _audit(ledger, venue.name, row, station, day, now)
            counts["events"] += 1
    return dict(counts)


def _audit(ledger: Ledger, venue: str, row, station: Station, day: date, now: int) -> None:
    rule = Rule(row["family"], row["station"], row["unit"], row["day_clock"], row["obs_policy"])
    start, end = day_window_utc(day, station.tz, rule.day_clock)
    reports = ledger.obs_between(rule.station, start, end + _SETTLE_DELAY)
    high = qualifying_high_c(reports, rule, day, station.tz)

    markets = ledger.event_markets(venue, row["event_id"])
    winners = [m["market_id"] for m in markets if m["payout_yes"] == 1]
    actual = winners[0] if len(winners) == 1 else None
    degree = expected = None
    # A hole in the reports can hide the peak, and a high that reads low would poison the bias record for weeks.
    if high is not None and covers_day(reports, rule, day, station.tz):
        ledger.add_daily_high(rule.station, day, rule.obs_policy, high, sum(start <= r.obs_time < end for r in reports))
        degree = market_degree(high, rule.unit)
        expected = next((m["market_id"] for m in markets if Bucket(m["lo"], m["hi"]).contains(degree)), None)
    agrees = None if expected is None or actual is None else expected == actual
    ledger.add_audit(venue, row["event_id"], rule.family, rule.station, day, high, degree, expected, actual, agrees, now)

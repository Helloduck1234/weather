"""Replay closed markets through the real trading cycle.

A replay runs `run_cycle` itself, so it can't drift from paper mode. Only the
data changes, and that is the weakness to keep in mind when reading a result:
Open-Meteo doesn't archive ensemble members, so a few deterministic models stand
in for the ensemble, and no venue keeps old order books, so depth is assumed.

Nothing may see the future. Quotes, reports and forecast runs are downloaded up
front, then each one stays hidden until the moment it could have been seen.
Results go to their own ledger, which keeps the downloads between replays.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .config import Config, Station
from .core import Book, Event, Market
from .engine import Services, run_cycle
from .executors import PaperExecutor
from .forecast import Forecasts
from .http import HttpError
from .ledger import Ledger
from .report import Report, build_report, write_report
from .settle import settle_venue
from .timeutil import day_window_utc

PROXY_MODELS = ("ecmwf_ifs025", "gfs_global", "icon_global", "ecmwf_aifs025_single")
DEFAULT_DEPTH = 100.0  # shares a side, since no venue keeps old books
_HALF_SPREAD = {"us": 0.0, "intl": 0.01}  # the US history is the real top of book, the CLOB's is one price
_PROXY_KERNEL_C = 1.5  # four members leave more of the spread to the kernel
_HOUR = 3600
_DAY = 24 * _HOUR
_RUN_STEP = 12 * _HOUR
_PUBLISH_DELAY = 11 * _HOUR  # as slow as the slowest run we've seen appear
_MAX_QUOTE_AGE = _HOUR  # quotes arrive every 15 minutes
_CYCLE_OFFSET = 20 * 60  # by then every report stamped earlier has been published
_FORECAST_PAUSE = 0.15  # stays under the free tier's 600 calls a minute
_REPORT_WINDOW_HOURS = 120  # half-hourly stations stay well under the 400-row cap


def backtest_config(cfg: Config) -> Config:
    """Quotes can't show whether a resting order would have filled, so a replay always crosses the spread."""
    return replace(
        cfg,
        db_path=str(Path(cfg.db_path).with_name("backtest.sqlite")),
        report_dir=str(Path(cfg.report_dir) / "backtest"),
        strategy=replace(cfg.strategy, execution="taker"),
        model=replace(cfg.model, kernel_sigma_start_c=_PROXY_KERNEL_C),
        forecast=replace(cfg.forecast, models=PROXY_MODELS, max_age_hours=24.0),
    )


def default_stations(cfg: Config, venue: str) -> set[str] | None:
    """polymarket.com lists about 50 cities a day, and only US stations report tenths of a degree."""
    return {icao for icao in cfg.stations if icao.startswith("K")} if venue == "intl" else None


def synthetic_book(bid: float, ask: float, tick: float, half_spread: float, depth: float) -> Book:
    """One level a side at a recorded quote. A locked quote is a last trade with nothing resting behind it."""
    bid = _snap(math.floor, bid - half_spread, tick)
    ask = _snap(math.ceil, ask + half_spread, tick)
    if bid >= ask:
        return Book((), ())
    return Book.from_levels([(bid, depth)] if bid > 0 else [], [(ask, depth)] if ask < 1 else [])


def _snap(direction: Callable[[float], int], price: float, tick: float) -> float:
    return round(direction(round(price / tick, 6)) * tick, 6)


class ReplayVenue:
    """Closed events shown as if open, with the quotes in force at the cycle's moment."""

    def __init__(self, name: str, events: list[Event], ledger: Ledger, half_spread: float, depth: float):
        self.name = name
        self._events = events
        self._ledger = ledger
        self._half_spread = half_spread
        self._depth = depth
        self._now = 0

    def open_events(self, now: int) -> tuple[list[Event], Counter]:
        self._now = now
        return self._events, Counter()

    def books(self, markets: list[Market]) -> dict[str, Book]:
        books = {}
        for market in markets:
            quote = self._ledger.quote_at(self.name, market.token, self._now, _MAX_QUOTE_AGE)
            if quote is not None:
                books[market.token] = synthetic_book(*quote, market.tick, self._half_spread, self._depth)
        return books

    def outcomes(self, event_id: str, market_ids: list[str]) -> dict[str, float]:
        return self._ledger.outcomes(self.name, market_ids)


class _NoAviation:
    """Reports are already in the ledger, and the engine only reads those stamped before now."""

    def recent(self, ids: list[str]) -> list:
        return []

    def backfill(self, ids: list[str]) -> list:
        return []


class _ArchivedForecasts(Forecasts):
    """Past runs are already in the ledger, stamped with when they could first be seen."""

    def refresh(self, stations: Iterable[Station], now: int) -> int:
        return 0


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def replay_span(cfg: Config, since: date, until: date, now: int) -> tuple[int, int]:
    """From the first moment the first event could be in scope to well after the last one resolved."""
    return _midnight(since) - (cfg.max_lead_days + 1) * _DAY, min(_midnight(until) + 3 * _DAY, now)


def collect(cfg: Config, ledger: Ledger, venue: Any, aviation: Any, weather: Any, since: date, until: date, now: int,
            stations: set[str] | None = None, pause: Callable[[float], None] = time.sleep,
            log: Callable[[str], None] = print) -> tuple[list[Event], Counter]:
    """Download what the replay needs and the ledger doesn't hold yet. After a failure, just run it again."""
    counts: Counter = Counter()
    listed, _ = venue.closed_events(_midnight(since) - _DAY)
    wanted = [e for e in listed if _wanted(cfg, e, since, until, stations)]
    log(f"{len(wanted)} closed events from {since} to {until}. Downloading outcomes and quotes.")

    events = []
    for event in wanted:
        if _archive_event(cfg, ledger, venue, event, now):
            events.append(replace(event, markets=tuple(replace(m, tradable=True) for m in event.markets)))
        else:
            counts["unresolved_events"] += 1

    start, end = replay_span(cfg, since, until, now)
    places = sorted({cfg.stations[e.rule.station] for e in events}, key=lambda s: s.icao)
    log(f"Downloading reports and past forecast runs for {len(places)} stations.")
    for station in places:
        for last in range(end, start - _DAY, -_REPORT_WINDOW_HOURS * _HOUR):
            ledger.add_obs(aviation.history(station.icao, last, _REPORT_WINDOW_HOURS))
    _archive_runs(cfg, ledger, weather, places, start - _DAY, end, now, pause, counts)
    return events, counts


def _wanted(cfg: Config, event: Event, since: date, until: date, stations: set[str] | None) -> bool:
    icao = event.rule.station
    return since <= event.day <= until and icao in cfg.stations and (stations is None or icao in stations)


def _archive_event(cfg: Config, ledger: Ledger, venue: Any, event: Event, now: int) -> bool:
    ids = [m.market_id for m in event.markets]
    missing = [i for i in ids if i not in ledger.outcomes(venue.name, ids)]
    if missing:
        ledger.add_outcomes(venue.name, venue.outcomes(event.event_id, missing))
    if len(ledger.outcomes(venue.name, ids)) < len(ids):
        return False

    station = cfg.stations[event.rule.station]
    day_start, day_end = day_window_utc(event.day, station.tz, event.rule.day_clock)
    first, last = day_start - (cfg.max_lead_days + 1) * _DAY, min(day_end + 2 * _DAY, now)
    for market in event.markets:
        if not ledger.has_quotes(venue.name, market.token):
            ledger.add_quotes(venue.name, market.token, venue.quote_history(market, first, last))
    return True


def _archive_runs(cfg: Config, ledger: Ledger, weather: Any, stations: list[Station], start: int, end: int, now: int,
                  pause: Callable[[float], None], counts: Counter) -> None:
    for init in range(start - start % _RUN_STEP, min(end, now - _PUBLISH_DELAY), _RUN_STEP):
        for model in cfg.forecast.models:
            for station in stations:
                if ledger.has_snapshot(station.icao, model, init):
                    continue
                pause(_FORECAST_PAUSE)
                try:
                    snapshot = weather.single_run(model, station, init)
                except HttpError as exc:
                    if exc.status == 429:
                        raise
                    counts["forecast_errors"] += 1
                    continue
                if np.isnan(snapshot.members).all():
                    counts["empty_runs"] += 1
                    continue
                ledger.save_snapshot(station.icao, snapshot, init + _PUBLISH_DELAY)


def replay(cfg: Config, ledger: Ledger, events: list[Event], venue: str, start: int, end: int, step: int,
           half_spread: float, depth: float) -> Counter:
    """Step through time. Clearing first means a rerun can't see the last replay's results."""
    ledger.clear_results()
    replayed = ReplayVenue(venue, events, ledger, half_spread, depth)
    forecasts = _ArchivedForecasts(None, ledger, cfg.forecast)
    services = Services(cfg, ledger, replayed, _NoAviation(), forecasts, PaperExecutor(ledger, venue, mode="backtest"))
    totals: Counter = Counter()
    for now in range(start - start % step + _CYCLE_OFFSET, end, step):
        totals["cycles"] += 1
        totals["orders"] += run_cycle(services, now).get("orders", 0)
        totals["settled_events"] += settle_venue(cfg, ledger, replayed, now).get("events", 0)
    return totals


def run_backtest(cfg: Config, venue: Any, aviation: Any, weather: Any, since: date, until: date, now: int,
                 stations: set[str] | None = None, step_hours: int = 3, depth: float = DEFAULT_DEPTH,
                 half_spread: float | None = None, pause: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print) -> tuple[Report, Path, Path | None]:
    bt = backtest_config(cfg)
    ledger = Ledger(bt.db_path)
    try:
        scope = stations if stations is not None else default_stations(bt, venue.name)
        events, counts = collect(bt, ledger, venue, aviation, weather, since, until, now, scope, pause, log)
        if counts:
            log(f"Download problems: {dict(counts)}")
        start, end = replay_span(bt, since, until, now)
        spread = _HALF_SPREAD[venue.name] if half_spread is None else half_spread
        log(f"Replaying {len(events)} events.")
        totals = replay(bt, ledger, events, venue.name, start, end, step_hours * _HOUR, spread, depth)
        log(f"Replay done: {dict(totals)}")
        report = build_report(ledger, venue.name, "backtest", bt.bankroll_usd, end)
        markdown, chart = write_report(report, bt.report_dir)
        return report, markdown, chart
    finally:
        ledger.close()

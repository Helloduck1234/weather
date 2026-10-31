"""One trading cycle: refresh data, price every open bucket, act on the edges."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from .aviation import Aviation
from .config import Config
from .core import Book, Event, Market
from .forecast import Forecasts, StationForecast
from .http import HttpError
from .ledger import DecisionRow, Ledger
from .metar import covers_day, latest_obs, opening_temp_c, qualifying_high_c, unverified_high_c
from .model import DayInput, NoForecast, bucket_probabilities, mean_daily_high, price_day
from .strategy import Decision, Exposure, evaluate
from .timeutil import clock_date, day_window_utc, local_hour

_HOUR = 3600
_BACKFILL_AFTER = 2 * _HOUR
_OBS_LOOKBACK = 7 * _HOUR  # the anchor can be a report from before local midnight


@dataclass(frozen=True)
class Services:
    cfg: Config
    ledger: Ledger
    venue: Any  # name, open_events, books, outcomes
    aviation: Aviation
    forecasts: Forecasts
    executor: Any  # mode, sync, submit, cancel


@dataclass(frozen=True)
class _Quote:
    """One market's model view and book, ready to decide on."""

    event: Event
    market: Market
    book: Book
    book_id: int
    p: float
    lo: float
    hi: float
    runs: str
    floor_c: float | None
    lead_hours: float
    hold: str | None  # why this quote is scored but not traded


def run_cycle(s: Services, now: int) -> dict:
    run = s.ledger.start_run(s.venue.name, s.executor.mode, now)
    counts: Counter = Counter()
    skips: Counter = Counter()
    try:
        _cycle(s, run, now, counts, skips)
    except Exception as exc:
        s.ledger.finish_run(run, now, "error", counts, skips, f"{type(exc).__name__}: {exc}")
        raise
    s.ledger.finish_run(run, now, "ok", counts, skips)
    return dict(counts)


def _cycle(s: Services, run: int, now: int, counts: Counter, skips: Counter) -> None:
    s.executor.check_stops(now)  # first, so a market data outage can't hold a stop back
    listed, parse_skips = s.venue.open_events(now)
    skips.update(parse_skips)
    events = [e for e in listed if _in_scope(s, e, now, skips)]
    for event in events:
        s.ledger.upsert_event(event, now)
    counts["events"] = len(events)
    counts["markets"] = sum(len(e.markets) for e in events)

    stations = sorted({s.cfg.stations[e.rule.station] for e in events}, key=lambda x: x.icao)
    counts["metar_errors"] = _refresh_observations(s, [x.icao for x in stations], now)
    counts["forecast_errors"] = s.forecasts.refresh(stations, now)

    tokens = s.venue.books([m for e in events for m in e.markets])
    books = {m.market_id: tokens[m.token] for e in events for m in e.markets if m.token in tokens}
    counts["fills"] = s.executor.sync(books, now)

    quotes = {q.market.market_id: q for e in events for q in _price_event(s, e, books, now, skips)}
    resting, takers = _manage_orders(s, quotes, now, counts)
    _decide(s, run, quotes, resting, takers, now, counts)


def _in_scope(s: Services, event: Event, now: int, skips: Counter) -> bool:
    station = s.cfg.stations.get(event.rule.station)
    if station is None:
        skips["station_not_configured"] += 1
        return False
    if (event.day - clock_date(now, station.tz, event.rule.day_clock)).days > s.cfg.max_lead_days:
        skips["beyond_lead_limit"] += 1
        return False
    # Once a day closes only completeness could justify a price, and a missing report doesn't announce itself.
    _, end = day_window_utc(event.day, station.tz, event.rule.day_clock)
    if now >= end:
        skips["day_over"] += 1
        return False
    return True


def _refresh_observations(s: Services, icaos: list[str], now: int) -> int:
    behind = [i for i in icaos if _is_behind(s, i, now)]
    current = [i for i in icaos if i not in behind]
    errors = 0
    for fetch, ids in ((s.aviation.backfill, behind), (s.aviation.recent, current)):
        if not ids:
            continue
        try:
            s.ledger.add_obs(fetch(ids))
        except HttpError:
            errors += 1
            continue
        s.ledger.mark_fetched(ids, now)
    return errors


def _is_behind(s: Services, icao: str, now: int) -> bool:
    last = s.ledger.last_fetch(icao)
    return last is None or now - last > _BACKFILL_AFTER


def _price_event(s: Services, event: Event, books: dict[str, Book], now: int, skips: Counter) -> list[_Quote]:
    rule = event.rule
    station = s.cfg.stations[rule.station]
    start, end = day_window_utc(event.day, station.tz, rule.day_clock)
    reports = [
        r for r in s.ledger.obs_between(rule.station, start - _OBS_LOOKBACK, end + _HOUR) if r.obs_time <= now
    ]
    latest = latest_obs(reports)
    in_progress = now >= start
    if in_progress and (latest is None or now - latest.obs_time > s.cfg.max_obs_age_hours * _HOUR):
        skips["stale_obs"] += 1
        return []

    fc = s.forecasts.for_station(rule.station, now)
    if fc is None:
        skips["no_forecast"] += 1
        return []
    runs = json.dumps(fc.runs, sort_keys=True)

    lead_days = (event.day - clock_date(now, station.tz, rule.day_clock)).days
    members, ready = _corrected(s, fc, event, lead_days, start, end, now)
    hold = None
    if not set(s.cfg.forecast.models) <= set(fc.rows):
        hold = "model_missing"
    elif not ready:
        hold = "warming_up"
    elif in_progress and not covers_day(reports, rule, event.day, station.tz, now):
        hold = "thin_observations"
    if hold:
        skips[hold] += 1

    inp = DayInput(
        fc.times,
        members,
        start,
        end,
        now,
        qualifying_high_c(reports, rule, event.day, station.tz),
        latest.temp_c if latest else None,
        latest.obs_time if latest else None,
        local_hour(now, station.tz),
        unverified_high_c(reports, rule, event.day, station.tz),
        opening_temp_c(reports, rule, event.day, station.tz),
    )
    try:
        dist = price_day(inp, s.cfg.model)
    except NoForecast as exc:
        skips[str(exc)] += 1
        return []

    probs = bucket_probabilities(dist, [m.bucket for m in event.markets], rule.unit)
    quotes = []
    for i, market in enumerate(event.markets):
        book = books.get(market.market_id)
        if book is not None:
            quotes.append(
                _Quote(event, market, book, s.ledger.save_book(book), float(probs.p[i]), float(probs.lo[i]),
                       float(probs.hi[i]), runs, inp.floor_c, (end - now) / _HOUR, hold)
            )
    return quotes


def _corrected(s: Services, fc: StationForecast, event: Event, lead_days: int, start: int, end: int,
               now: int) -> tuple[np.ndarray, bool]:
    """Put each model's raw high on its record, then correct it by that record. Models can disagree by degrees.

    Also says whether every model has a long enough record: raw highs run far enough
    from the market that uncorrected edges are mostly the forecast's own bias.
    """
    rule, cfg = event.rule, s.cfg.bias
    members = fc.members.copy()
    ready = True
    for model, rows in fc.rows.items():
        mean = mean_daily_high(fc.times, fc.members[rows], start, end, s.cfg.model)
        if mean is not None:
            s.ledger.log_forecast(rule.station, event.day, rule.obs_policy, lead_days, model, fc.runs[model], mean, now)
        bias = s.ledger.bias(rule.station, rule.obs_policy, lead_days, model, event.day, cfg.window_days,
                             cfg.shrink_days)
        members[rows] += bias.value
        ready = ready and bias.days >= cfg.min_days
    return members, ready


def _evaluate(s: Services, q: _Quote, exposure: Exposure, force_taker: bool = False) -> Decision:
    if q.hold:
        return Decision("skip", q.hold)
    return evaluate(q.market, q.book, q.lo, 1 - q.hi, exposure, s.cfg.strategy, force_taker)


def _unencumbered(s: Services) -> Exposure:
    return Exposure(s.cfg.bankroll_usd, 0.0, 0.0, 0.0, 0.0)


def _manage_orders(s: Services, quotes: dict[str, _Quote], now: int, counts: Counter) -> tuple[set[str], set[str]]:
    """Cancel orders that lost their reason to exist. Returns markets still resting, and those due to cross."""
    resting: set[str] = set()
    takers: set[str] = set()
    for order in s.ledger.open_orders(s.venue.name, s.executor.mode):
        quote = quotes.get(order.market_id)
        if quote is None:
            reason = "cannot_price"
        else:
            d = _evaluate(s, quote, _unencumbered(s))
            if d.action != "buy" or d.side != order.side:
                reason = "signal_gone"
            elif now - order.created_at >= s.cfg.order_ttl_minutes * 60:
                reason = "ttl"
                takers.add(order.market_id)
            else:
                resting.add(order.market_id)
                continue
        s.executor.cancel(order, reason, now)
        counts["cancels"] += 1
    # A cancel can still be pending, and it could lose to a fill. Crossing now would then buy twice.
    still_open = {o.market_id for o in s.ledger.open_orders(s.venue.name, s.executor.mode)}
    return resting | still_open, takers - still_open


def _decide(s: Services, run: int, quotes: dict[str, _Quote], resting: set[str], takers: set[str], now: int,
            counts: Counter) -> None:
    candidates = []
    for market_id, q in quotes.items():
        d = _evaluate(s, q, _unencumbered(s), market_id in takers)
        if market_id in resting:
            _log(s, run, q, Decision("skip", "order_resting", d.side, d.edge, d.q), now, counts)
        elif d.action == "buy":
            candidates.append((d.edge, market_id, q))
        else:
            _log(s, run, q, d, now, counts)

    # Caps are scarce, so the best edge in the cycle gets first call on them.
    for _, market_id, q in sorted(candidates, key=lambda c: c[:2], reverse=True):
        exposure = s.ledger.exposure(s.venue.name, s.executor.mode, q.event.event_id, market_id,
                                     s.cfg.bankroll_usd, now)
        d = _evaluate(s, q, exposure, market_id in takers)
        decision_id = _log(s, run, q, d, now, counts)
        if d.action == "buy":
            s.executor.submit(decision_id, q.event, q.market, d, now)
            counts["orders"] += 1


def _log(s: Services, run: int, q: _Quote, d: Decision, now: int, counts: Counter) -> int | None:
    decision_id = s.ledger.log_decision(
        DecisionRow(
            run_id=run,
            ts=now,
            venue=s.venue.name,
            event_id=q.event.event_id,
            market_id=q.market.market_id,
            p_model=q.p,
            p_lo=q.lo,
            p_hi=q.hi,
            q=d.q,
            best_bid=q.book.best_bid,
            best_ask=q.book.best_ask,
            fee_rate=q.market.fee.rate,
            edge=d.edge,
            side=d.side,
            action=d.action,
            reason=d.reason,
            style=d.style,
            limit_px=d.limit,
            shares=d.shares,
            book_id=q.book_id,
            forecast_runs=q.runs,
            obs_floor_c=q.floor_c,
            lead_hours=q.lead_hours,
        )
    )
    if decision_id is not None:
        counts["decisions"] += 1
    return decision_id

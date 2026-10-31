import json
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from test_engine import HOUR, KLGA, SHARP, config, event, strategy, utc
from weatherbot.backtest import (
    PROXY_MODELS,
    backtest_config,
    collect,
    default_stations,
    replay,
    replay_span,
    run_backtest,
    synthetic_book,
)
from weatherbot.config import Config, ForecastConfig, Station
from weatherbot.executors import PaperExecutor
from weatherbot.http import HttpError
from weatherbot.ledger import Ledger
from weatherbot.metar import Obs
from weatherbot.openmeteo import Snapshot

DAY = 24 * HOUR
MODEL = "ecmwf_ifs025"
EVENT_DAY = date(2026, 7, 2)
INIT = utc(2026, 7, 1, 0)
START = utc(2026, 7, 1, 12)
FIRST_CYCLE = utc(2026, 7, 1, 12, 20)
KPHL = Station("KPHL", "Philadelphia", "America/New_York", 39.9, -75.2, 2)


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    yield db
    db.close()


def replay_cfg(**kw):
    defaults = dict(strategy=strategy(execution="taker", max_depth_share=0.15),
                    forecast=ForecastConfig(models=(MODEL,), max_age_hours=24.0))
    return config(**{**defaults, **kw})


def archive_forecast(ledger, high=10.0, init=INIT, fetched_at=INIT + 11 * HOUR):
    times = np.array([init + i * HOUR for i in range(96)])
    ledger.save_snapshot("KLGA", Snapshot(MODEL, init, times, np.full((3, 96), float(high))), fetched_at)


def archive_outcomes(ledger, venue="intl"):
    ledger.add_outcomes(venue, {"m0": 0.0, "m1": 1.0, "m2": 0.0, "m3": 0.0})


def underpriced(ledger, ts=START, bid=0.40, ask=0.45):
    """Bucket 10C trades at `ask` at one moment. The model says it is nearly certain."""
    ledger.add_quotes("intl", "tok-m1", [(ts, bid, ask)])


def run_replay(ledger, cfg=None, events=None, start=START, end=utc(2026, 7, 3, 12), step=3 * HOUR):
    return replay(cfg or replay_cfg(), ledger, events or [event(EVENT_DAY)], "intl", start, end, step, 0.0, 100.0)


def fills(ledger):
    return ledger.query(
        """SELECT o.market_id, o.mode, o.style, f.ts, f.price, f.shares
           FROM fills f JOIN orders o ON o.id = f.order_id ORDER BY f.id"""
    )


def skips(ledger):
    total = Counter()
    for row in ledger.query("SELECT skips FROM runs"):
        total.update(json.loads(row["skips"]))
    return total


# The book a replay trades against


def test_book_holds_the_recorded_quote():
    book = synthetic_book(0.25, 0.77, 0.01, 0.0, 100.0)
    assert (book.best_bid, book.best_ask) == (0.25, 0.77)
    assert (book.bids[0].size, book.asks[0].size) == (100.0, 100.0)


def test_half_spread_widens_a_single_price_out_to_ticks():
    book = synthetic_book(0.245, 0.245, 0.01, 0.01, 100.0)
    assert (book.best_bid, book.best_ask) == (0.23, 0.26)


def test_locked_quote_is_not_a_book():
    # A real book can't rest locked: this is the display price falling back on the last trade.
    book = synthetic_book(0.97, 0.97, 0.01, 0.0, 100.0)
    assert (book.best_bid, book.best_ask) == (None, None)


def test_price_at_the_edge_of_the_range_means_no_quote_on_that_side():
    assert synthetic_book(0.0, 0.5, 0.01, 0.0, 100.0).best_bid is None
    assert synthetic_book(0.5, 1.0, 0.01, 0.0, 100.0).best_ask is None
    assert synthetic_book(0.005, 0.005, 0.001, 0.01, 100.0).best_bid is None


# Settings


def test_backtest_config_keeps_away_from_the_paper_ledger():
    cfg = backtest_config(Config(db_path="data/weatherbot.sqlite", report_dir="reports"))
    assert Path(cfg.db_path) == Path("data/backtest.sqlite")
    assert Path(cfg.report_dir) == Path("reports/backtest")


def test_backtest_config_crosses_the_spread_on_proxy_models():
    cfg = backtest_config(Config())
    assert cfg.strategy.execution == "taker"
    assert cfg.forecast.models == PROXY_MODELS
    assert cfg.forecast.max_age_hours == 24
    assert cfg.model.kernel_sigma_start_c == 1.5


def test_default_stations_for_intl_are_those_with_tenths_in_their_reports():
    cfg = Config(stations={"KLGA": KLGA, "EGLC": replace(KLGA, icao="EGLC")})
    assert default_stations(cfg, "intl") == {"KLGA"}
    assert default_stations(cfg, "us") is None


def test_span_starts_when_the_first_event_comes_into_range_and_ends_after_the_last_resolved():
    cfg = Config(max_lead_days=2)
    assert replay_span(cfg, EVENT_DAY, EVENT_DAY, utc(2026, 8, 1)) == (utc(2026, 6, 29), utc(2026, 7, 5))


def test_span_never_runs_past_now():
    cfg = Config(max_lead_days=2)
    assert replay_span(cfg, EVENT_DAY, EVENT_DAY, utc(2026, 7, 3, 6))[1] == utc(2026, 7, 3, 6)


# No lookahead


def test_replay_takes_the_recorded_ask_and_settles_it(ledger):
    archive_forecast(ledger)
    underpriced(ledger)
    archive_outcomes(ledger)
    run_replay(ledger)
    assert [tuple(f) for f in fills(ledger)] == [("m1", "backtest", "taker", FIRST_CYCLE, 0.45, 15.0)]
    assert ledger.realized_pnl("intl", "backtest") == pytest.approx(8.25)


def test_replay_trades_against_the_assumed_spread_and_depth(ledger):
    archive_forecast(ledger)
    underpriced(ledger)
    archive_outcomes(ledger)
    replay(replay_cfg(), ledger, [event(EVENT_DAY)], "intl", START, utc(2026, 7, 3, 12), 3 * HOUR, 0.01, 200.0)
    assert [tuple(f) for f in fills(ledger)] == [("m1", "backtest", "taker", FIRST_CYCLE, 0.46, 30.0)]


def test_cycles_run_after_every_earlier_report_has_been_published(ledger):
    archive_forecast(ledger)
    archive_outcomes(ledger)
    run_replay(ledger, end=FIRST_CYCLE + 1)
    assert [r["started_at"] for r in ledger.query("SELECT started_at FROM runs")] == [FIRST_CYCLE]


def test_quote_recorded_after_the_cycle_is_invisible(ledger):
    archive_forecast(ledger)
    underpriced(ledger, ts=FIRST_CYCLE + 600)
    archive_outcomes(ledger)
    run_replay(ledger)
    assert fills(ledger) == []


def test_quote_gone_stale_is_not_traded(ledger):
    archive_forecast(ledger)
    underpriced(ledger, ts=FIRST_CYCLE - HOUR - 1)
    archive_outcomes(ledger)
    run_replay(ledger)
    assert fills(ledger) == []


def test_forecast_run_is_invisible_until_published(ledger):
    archive_forecast(ledger, fetched_at=FIRST_CYCLE + 1)
    underpriced(ledger, ts=FIRST_CYCLE)
    archive_outcomes(ledger)
    run_replay(ledger, end=FIRST_CYCLE + 1)
    assert fills(ledger) == []
    assert skips(ledger)["no_forecast"] == 1


def test_later_observation_does_not_raise_the_floor(ledger):
    noon = utc(2026, 7, 2, 15)
    archive_forecast(ledger, init=utc(2026, 7, 2, 0), fetched_at=utc(2026, 7, 2, 11))
    hourly = [Obs("KLGA", t, 9.0, "routine", "m") for t in range(utc(2026, 7, 2, 4), noon, HOUR)]
    ledger.add_obs(hourly + [Obs("KLGA", noon + 3 * HOUR, 13.0, "routine", "m")])
    underpriced(ledger, ts=noon)
    archive_outcomes(ledger)
    run_replay(ledger, start=noon, end=noon + HOUR)
    assert ledger.query("SELECT obs_floor_c FROM decisions")[0][0] == 9.0
    assert len(fills(ledger)) == 1


def test_quotes_after_the_day_closes_are_never_traded(ledger):
    # New York's day ends 04:00 UTC, and the bucket still shows a price hours later.
    archive_forecast(ledger)
    ledger.add_obs([Obs("KLGA", t, 9.0, "routine", "m") for t in range(utc(2026, 7, 2, 4), utc(2026, 7, 3, 4), HOUR)])
    underpriced(ledger, ts=utc(2026, 7, 3, 6))
    archive_outcomes(ledger)
    run_replay(ledger)
    assert fills(ledger) == []
    assert skips(ledger)["day_over"] > 0


def test_outcome_stays_hidden_until_the_day_is_over(ledger):
    archive_forecast(ledger)
    archive_outcomes(ledger)
    run_replay(ledger)
    # New York's day ends 04:00 UTC, and settling waits an hour for the last reports.
    assert ledger.market_row("intl", "m1")["settled_at"] == utc(2026, 7, 3, 6, 20)


def test_rerun_gives_the_same_result(ledger):
    archive_forecast(ledger)
    underpriced(ledger)
    archive_outcomes(ledger)
    run_replay(ledger)
    first = ledger.realized_pnl("intl", "backtest")
    run_replay(ledger)
    assert ledger.count("fills") == 1
    assert ledger.realized_pnl("intl", "backtest") == first


def test_executor_books_under_the_mode_it_was_given(ledger):
    assert PaperExecutor(ledger, "intl").mode == "paper"
    assert PaperExecutor(ledger, "intl", mode="backtest").mode == "backtest"


# Collecting history


class ClosedVenue:
    name = "intl"

    def __init__(self, events, unresolved=()):
        self.events = events
        self.unresolved = set(unresolved)
        self.quote_calls = []
        self.outcome_calls = 0

    def closed_events(self, since):
        return self.events, Counter({"not_high_temperature": 2})

    def quote_history(self, market, start, end):
        self.quote_calls.append((market.token, start, end))
        return [(start, 0.40, 0.45)]

    def outcomes(self, event_id, market_ids):
        self.outcome_calls += 1
        return {m: 0.0 for m in market_ids if m not in self.unresolved}


class ReportArchive:
    def __init__(self):
        self.calls = []

    def history(self, icao, end, hours):
        self.calls.append((icao, end, hours))
        return [Obs(icao, end - HOUR, 10.0, "routine", "raw")]


class PastRuns:
    def __init__(self, failures=None, nulls=()):
        self.calls = []
        self.failures = failures or {}
        self.nulls = set(nulls)

    def single_run(self, model, station, init):
        self.calls.append((model, station.icao, init))
        if model in self.failures:
            raise HttpError(self.failures[model], "url")
        value = np.nan if model in self.nulls else 10.0
        return Snapshot(model, init, np.array([init, init + HOUR]), np.full((1, 2), value))


LATER = utc(2026, 7, 10)


def gather(ledger, venue=None, reports=None, runs=None, cfg=None, since=EVENT_DAY, until=EVENT_DAY, now=LATER,
           pause=lambda _: None, **kw):
    return collect(
        cfg or replay_cfg(), ledger, venue or ClosedVenue([event(EVENT_DAY)]), reports or ReportArchive(),
        runs or PastRuns(), since, until, now, pause=pause, log=lambda _: None, **kw,
    )


def test_collect_keeps_only_the_days_asked_for(ledger):
    venue = ClosedVenue(
        [event(date(2026, 7, 1), "e1", "a"), event(EVENT_DAY, "e2", "b"), event(date(2026, 7, 3), "e3", "c")]
    )
    events, _ = gather(ledger, venue)
    assert [e.event_id for e in events] == ["e2"]


def test_collect_keeps_only_the_stations_asked_for(ledger):
    cfg = replay_cfg(stations={"KLGA": KLGA, "KPHL": KPHL})
    philly = replace(event(EVENT_DAY, "e2", "b"), rule=replace(event().rule, station="KPHL"))
    venue = ClosedVenue([event(EVENT_DAY, "e1", "a"), philly])
    events, _ = gather(ledger, venue, cfg=cfg, stations={"KPHL"})
    assert [e.event_id for e in events] == ["e2"]


def test_collect_skips_a_station_without_coordinates(ledger):
    unknown = replace(event(EVENT_DAY), rule=replace(event().rule, station="KPHL"))
    events, _ = gather(ledger, ClosedVenue([unknown]))
    assert events == []


def test_closed_markets_replay_as_tradable(ledger):
    closed = event(EVENT_DAY)
    closed = replace(closed, markets=tuple(replace(m, tradable=False) for m in closed.markets))
    events, _ = gather(ledger, ClosedVenue([closed]))
    assert all(m.tradable for m in events[0].markets)


def test_event_still_unresolved_is_left_out(ledger):
    events, counts = gather(ledger, ClosedVenue([event(EVENT_DAY)], unresolved={"m2"}))
    assert events == []
    assert counts["unresolved_events"] == 1


def test_collect_stores_outcomes_and_quotes(ledger):
    gather(ledger)
    assert ledger.outcomes("intl", ["m0", "m1", "m2", "m3"]) == {"m0": 0.0, "m1": 0.0, "m2": 0.0, "m3": 0.0}
    for token in ("tok-m0", "tok-m1", "tok-m2", "tok-m3"):
        assert ledger.has_quotes("intl", token)


def test_quotes_span_the_whole_time_the_event_could_trade(ledger):
    venue = ClosedVenue([event(EVENT_DAY)])
    gather(ledger, venue)
    midnight, next_midnight = utc(2026, 7, 2, 4), utc(2026, 7, 3, 4)
    assert venue.quote_calls[0] == ("tok-m0", midnight - 3 * DAY, next_midnight + 2 * DAY)


def test_quotes_stop_at_the_present(ledger):
    venue = ClosedVenue([event(EVENT_DAY)])
    gather(ledger, venue, now=utc(2026, 7, 3, 12))
    assert venue.quote_calls[0][2] == utc(2026, 7, 3, 12)


def test_forecast_runs_are_stamped_with_when_they_could_be_seen(ledger):
    gather(ledger)
    row = ledger.query("SELECT init_time, fetched_at FROM forecast_snapshots LIMIT 1")[0]
    assert row["fetched_at"] == row["init_time"] + 11 * HOUR


def test_forecast_runs_cover_the_replay_at_two_a_day(ledger):
    runs = PastRuns()
    gather(ledger, runs=runs)
    inits = sorted({call[2] for call in runs.calls})
    assert inits[0] == utc(2026, 6, 28, 0)
    assert inits[-1] == utc(2026, 7, 4, 12)
    assert all(b - a == 12 * HOUR for a, b in zip(inits, inits[1:]))


def test_forecast_run_not_published_yet_is_not_requested(ledger):
    runs = PastRuns()
    gather(ledger, runs=runs, now=utc(2026, 7, 2, 13))  # the 12Z run exists, but only 1 hour ago
    assert max(call[2] for call in runs.calls) == utc(2026, 7, 2, 0)


def test_collect_pauses_between_forecast_calls_to_respect_the_rate_limit(ledger):
    runs = PastRuns()
    pauses = []
    gather(ledger, runs=runs, pause=pauses.append)
    assert len(pauses) == len(runs.calls)
    assert 60 / pauses[0] < 600


def test_second_collect_downloads_nothing_new(ledger):
    venue, runs = ClosedVenue([event(EVENT_DAY)]), PastRuns()
    gather(ledger, venue, runs=runs)
    quotes, outcomes, forecasts = len(venue.quote_calls), venue.outcome_calls, len(runs.calls)
    gather(ledger, venue, runs=runs)
    assert (len(venue.quote_calls), venue.outcome_calls, len(runs.calls)) == (quotes, outcomes, forecasts)


def test_all_null_run_is_counted_and_not_saved(ledger):
    runs = PastRuns(nulls={MODEL})
    _, counts = gather(ledger, runs=runs)
    assert ledger.count("forecast_snapshots") == 0
    assert counts["empty_runs"] == len(runs.calls)


def test_unavailable_run_is_counted_and_the_rest_still_arrive(ledger):
    cfg = replay_cfg(forecast=ForecastConfig(models=(MODEL, "icon_global"), max_age_hours=24.0))
    _, counts = gather(ledger, runs=PastRuns(failures={MODEL: 400}), cfg=cfg)
    assert counts["forecast_errors"] > 0
    assert ledger.latest_init("KLGA", "icon_global") is not None


def test_rate_limit_stops_the_download_rather_than_burning_more_calls(ledger):
    runs = PastRuns(failures={MODEL: 429})
    with pytest.raises(HttpError):
        gather(ledger, runs=runs)
    assert len(runs.calls) == 1


def test_reports_cover_the_replay_in_windows_the_row_cap_cannot_truncate(ledger):
    reports = ReportArchive()
    gather(ledger, reports=reports)
    assert all(hours <= 120 for _, _, hours in reports.calls)
    ends = sorted(end for _, end, _ in reports.calls)
    assert ends[0] - reports.calls[0][2] * HOUR <= utc(2026, 6, 28, 0)
    assert ends[-1] >= utc(2026, 7, 5, 0)
    assert all(b - a <= 120 * HOUR for a, b in zip(ends, ends[1:]))


# The whole run


def test_run_backtest_reports_on_its_own_ledger(tmp_path):
    base = replay_cfg(db_path=str(tmp_path / "data" / "weatherbot.sqlite"), report_dir=str(tmp_path / "reports"),
                      model=SHARP)
    paper = Ledger(base.db_path)
    paper.start_run("intl", "paper", 1)
    paper.close()

    report, markdown, _ = run_backtest(
        base, ClosedVenue([event(EVENT_DAY)]), ReportArchive(), PastRuns(), EVENT_DAY, EVENT_DAY, LATER,
        pause=lambda _: None, log=lambda _: None,
    )

    assert report.mode == "backtest"
    assert markdown == tmp_path / "reports" / "backtest" / "report-intl-backtest.md"
    assert (tmp_path / "data" / "backtest.sqlite").exists()
    untouched = Ledger(base.db_path)
    assert untouched.count("runs") == 1
    assert untouched.count("price_history") == 0
    untouched.close()


class SteadyVenue(ClosedVenue):
    def quote_history(self, market, start, end):
        return [(ts, 0.40, 0.45) for ts in range(start, end, HOUR)]


class LongRuns(PastRuns):
    def single_run(self, model, station, init):
        self.calls.append((model, station.icao, init))
        times = np.array([init + i * HOUR for i in range(96)])
        return Snapshot(model, init, times, np.full((1, 96), 10.0))


def backtest_rows(tmp_path, sql, **kw):
    base = replay_cfg(db_path=str(tmp_path / "data" / "paper.sqlite"), report_dir=str(tmp_path / "reports"), **kw)
    return lambda venue, **options: _run(base, venue, sql, options)


def _run(cfg, venue, sql, options):
    run_backtest(cfg, venue, ReportArchive(), LongRuns(), EVENT_DAY, EVENT_DAY, LATER, pause=lambda _: None,
                 log=lambda _: None, **options)
    db = Ledger(Path(cfg.db_path).with_name("backtest.sqlite"))
    try:
        return [tuple(r) for r in db.query(sql)]
    finally:
        db.close()


def test_intl_quotes_get_the_assumed_spread_by_default(tmp_path):
    rows = backtest_rows(tmp_path, "SELECT DISTINCT best_bid, best_ask FROM decisions")
    assert rows(SteadyVenue([event(EVENT_DAY)])) == [(0.39, 0.46)]


def test_spread_can_be_overridden(tmp_path):
    rows = backtest_rows(tmp_path, "SELECT DISTINCT best_bid, best_ask FROM decisions")
    assert rows(SteadyVenue([event(EVENT_DAY)]), half_spread=0.0) == [(0.40, 0.45)]


def test_us_quotes_are_the_real_top_of_book_so_they_stay_as_recorded(tmp_path):
    us = SteadyVenue([replace(event(EVENT_DAY), venue="us")])
    us.name = "us"
    rows = backtest_rows(tmp_path, "SELECT DISTINCT best_bid, best_ask FROM decisions")
    assert rows(us) == [(0.40, 0.45)]


def test_intl_replay_stays_on_us_stations_unless_told_otherwise(tmp_path):
    london = replace(KLGA, icao="EGLC")
    abroad = replace(event(EVENT_DAY, "e2", "b"), rule=replace(event().rule, station="EGLC"))
    venue = SteadyVenue([event(EVENT_DAY, "e1", "a"), abroad])
    rows = backtest_rows(tmp_path, "SELECT DISTINCT event_id FROM events", stations={"KLGA": KLGA, "EGLC": london})
    assert rows(venue) == [("e1",)]
    assert sorted(rows(venue, stations={"EGLC"})) == [("e2",)]

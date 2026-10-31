import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone

import numpy as np
import pytest

from weatherbot.config import BiasConfig, Config, ForecastConfig, Station
from weatherbot.core import Book, Bucket, Event, Market, Rule
from weatherbot.engine import Services, run_cycle
from weatherbot.executors import PaperExecutor
from weatherbot.fees import FeeSchedule
from weatherbot.forecast import StationForecast
from weatherbot.http import HttpError
from weatherbot.ledger import Ledger
from weatherbot.metar import Obs
from weatherbot.model import ModelParams
from weatherbot.strategy import StrategyParams

HOUR = 3600
TODAY = date(2026, 7, 1)
TOMORROW = date(2026, 7, 2)
KLGA = Station("KLGA", "NYC", "America/New_York", 40.8, -73.9, 9)
RULE = Rule("metar_routine", "KLGA", "C", "local_dst", "routine")
NWS = Rule("nws_cli", "KLGA", "C", "local_standard", "nws_cli")


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


NOW = utc(2026, 7, 1, 16)  # noon in New York
DAY_END = utc(2026, 7, 2, 4)  # local midnight, when July 1 closes
SHARP = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.05, kernel_sigma_end_c=0.05,
                    edge_shift_c=0.0, prob_epsilon=0.0)
QUIET = ((0.01, 100),), ((0.99, 100),)  # nobody wants this bucket


def strategy(**kw):
    defaults = dict(edge_threshold=0.05, kelly_fraction=0.15, max_trade_usd=25.0, max_day_usd=100.0,
                    max_event_usd=40.0, max_depth_share=1.0, execution="maker_first")
    return StrategyParams(**{**defaults, **kw})


def config(**kw):
    defaults = dict(stations={"KLGA": KLGA}, strategy=strategy(), model=SHARP, order_ttl_minutes=30.0,
                    bankroll_usd=1000.0, max_lead_days=2, max_obs_age_hours=3.0, bias=BiasConfig(min_days=0),
                    forecast=ForecastConfig(models=("gfs025",)))
    return Config(**{**defaults, **kw})


def markets(prefix="m", fee=FeeSchedule(0.0)):
    buckets = (Bucket(None, 9), Bucket(10, 10), Bucket(11, 11), Bucket(12, None))
    return tuple(
        Market(f"{prefix}{i}", f"b{i}", b, f"tok-{prefix}{i}", 0.01, 1.0, fee) for i, b in enumerate(buckets)
    )


def event(day=TOMORROW, event_id="e1", prefix="m", rule=RULE, fee=FeeSchedule(0.0)):
    return Event("intl", event_id, f"slug-{event_id}", "Highest temperature in NYC", day, rule, markets(prefix, fee))


def forecast(high=10.0, members=10, runs=None):
    times = np.array([utc(2026, 7, 1, 0) + i * HOUR for i in range(96)])
    runs = runs or {"gfs025": NOW - 2 * HOUR}
    return StationForecast(times, np.full((members, 96), float(high)), runs, {m: slice(0, members) for m in runs})


class FakeVenue:
    name = "intl"

    def __init__(self, events, books=None):
        self.events = events
        self.book_map = {}
        self.parse_skips = Counter()
        self.fail = None
        for tok, (bids, asks) in (books or {}).items():
            self.set_book(tok, bids, asks)

    def set_book(self, token, bids, asks):
        self.book_map[token] = Book.from_levels(bids, asks)

    def open_events(self, now):
        if self.fail:
            raise self.fail
        return self.events, self.parse_skips

    def books(self, wanted):
        return {m.token: self.book_map[m.token] for m in wanted if m.token in self.book_map}


class FakeAviation:
    def __init__(self, reports=()):
        self.reports = list(reports)

    def recent(self, ids):
        return self.reports

    def backfill(self, ids):
        return self.reports


class FakeForecasts:
    def __init__(self, value):
        self.value = value

    def refresh(self, stations, now):
        return 0

    def for_station(self, icao, now):
        return self.value


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    yield db
    db.close()


def services(ledger, venue, cfg=None, aviation=None, forecasts=None):
    return Services(
        cfg=cfg or config(),
        ledger=ledger,
        venue=venue,
        aviation=aviation or FakeAviation(),
        forecasts=forecasts or FakeForecasts(forecast()),
        executor=PaperExecutor(ledger, venue.name),
    )


def signal_venue(ask=0.45, bid=0.40, size=100.0):
    """Bucket 10C is underpriced; every other bucket has no signal."""
    venue = FakeVenue([event()])
    for token in ("tok-m0", "tok-m2", "tok-m3"):
        venue.set_book(token, *QUIET)
    venue.set_book("tok-m1", [(bid, size)], [(ask, size)])
    return venue


def orders(ledger):
    return ledger.query("SELECT * FROM orders ORDER BY id")


def fills(ledger):
    return ledger.query("SELECT * FROM fills ORDER BY id")


def decision(ledger, market_id):
    return ledger.query("SELECT * FROM decisions WHERE market_id=? ORDER BY id DESC", (market_id,))[0]


def skips(ledger):
    return json.loads(ledger.query("SELECT skips FROM runs ORDER BY id DESC")[0]["skips"])


def today_reports(temp=9.0, until=NOW):
    """A station reporting on the hour since local midnight."""
    return [Obs("KLGA", t, temp, "routine", "raw") for t in range(DAY_END - 24 * HOUR, until, HOUR)]


def test_cycle_logs_one_decision_per_market(ledger):
    run_cycle(services(ledger, signal_venue()), NOW)
    assert ledger.count("decisions") == 4


def test_decision_records_probability_price_book_and_run(ledger):
    run_cycle(services(ledger, signal_venue()), NOW)
    d = decision(ledger, "m1")
    assert d["p_model"] == pytest.approx(1.0)
    assert (d["best_bid"], d["best_ask"]) == (0.40, 0.45)
    assert json.loads(d["forecast_runs"]) == {"gfs025": NOW - 2 * HOUR}
    assert ledger.load_book(d["book_id"]).best_ask == 0.45
    assert d["lead_hours"] == pytest.approx(36.0)


def test_signal_posts_maker_order_inside_spread(ledger):
    run_cycle(services(ledger, signal_venue()), NOW)
    assert [(o["market_id"], o["side"], o["style"], o["limit_px"], o["status"]) for o in orders(ledger)] == [
        ("m1", "yes", "maker", 0.41, "open")
    ]
    assert fills(ledger) == []


def test_resting_order_is_not_duplicated(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    run_cycle(s, NOW + 900)
    assert len(orders(ledger)) == 1
    assert decision(ledger, "m1")["reason"] == "order_resting"


def test_resting_order_fills_when_ask_reaches_it(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    size = orders(ledger)[0]["shares"]
    venue.set_book("tok-m1", [(0.38, 100)], [(0.40, 10000)])
    run_cycle(s, NOW + 900)
    got = fills(ledger)
    assert [(f["price"], f["shares"], f["fee"]) for f in got] == [(0.41, size, 0.0)]
    assert orders(ledger)[0]["status"] == "filled"


def test_resting_order_fills_only_for_available_size(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    venue.set_book("tok-m1", [(0.38, 100)], [(0.40, 7.0), (0.60, 10000)])
    run_cycle(s, NOW + 900)
    assert [f["shares"] for f in fills(ledger)] == [7.0]
    assert orders(ledger)[0]["status"] == "open"


def test_ask_above_limit_does_not_fill(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    venue.set_book("tok-m1", [(0.40, 100)], [(0.42, 100)])
    run_cycle(s, NOW + 900)
    assert fills(ledger) == []


def test_ttl_expiry_falls_back_to_taker_when_edge_persists(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    run_cycle(s, NOW + 31 * 60)
    got = orders(ledger)
    assert [o["status"] for o in got] == ["cancelled", "filled"]
    assert got[0]["reason"] == "ttl"
    assert got[1]["style"] == "taker"
    assert fills(ledger)[0]["price"] == 0.45


def test_signal_gone_cancels_resting_order(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    s.forecasts.value = forecast(high=11.0)
    run_cycle(s, NOW + 900)
    got = orders(ledger)[0]
    assert (got["status"], got["reason"]) == ("cancelled", "signal_gone")


def test_taker_mode_fills_at_once(ledger):
    cfg = config(strategy=strategy(execution="taker"))
    run_cycle(services(ledger, signal_venue(), cfg), NOW)
    got = orders(ledger)[0]
    assert (got["style"], got["status"]) == ("taker", "filled")
    assert fills(ledger)[0]["price"] == 0.45


def test_taker_fill_charges_fee(ledger):
    venue = signal_venue()
    venue.events = [event(fee=FeeSchedule(0.05))]
    cfg = config(strategy=strategy(execution="taker", max_trade_usd=10.0))
    run_cycle(services(ledger, venue, cfg), NOW)
    f = fills(ledger)[0]
    assert f["fee"] == pytest.approx(f["shares"] * 0.05 * 0.45 * 0.55)


def test_no_side_order_uses_no_prices(ledger):
    # Model says bucket 11 can't happen but the market pays 0.70 for YES.
    venue = FakeVenue([event()])
    for token in ("tok-m0", "tok-m1", "tok-m3"):
        venue.set_book(token, *QUIET)
    venue.set_book("tok-m2", [(0.70, 100)], [(0.75, 100)])
    run_cycle(services(ledger, venue), NOW)
    got = orders(ledger)[0]
    assert (got["market_id"], got["side"], got["limit_px"]) == ("m2", "no", 0.26)


def test_resting_no_order_fills_against_mirrored_book(ledger):
    venue = FakeVenue([event()])
    for token in ("tok-m0", "tok-m1", "tok-m3"):
        venue.set_book(token, *QUIET)
    venue.set_book("tok-m2", [(0.70, 100)], [(0.75, 100)])
    s = services(ledger, venue)
    run_cycle(s, NOW)
    # YES bid at 0.75 means NO can be bought at 0.25, under our 0.26 limit.
    venue.set_book("tok-m2", [(0.75, 10000)], [(0.80, 100)])
    run_cycle(s, NOW + 900)
    assert fills(ledger)[0]["price"] == 0.26


def test_highest_edge_gets_scarce_day_budget_first(ledger):
    worse = event(event_id="e1", prefix="a")
    better = event(event_id="e2", prefix="b")
    venue = FakeVenue([worse, better])
    for prefix in "ab":
        for i in (0, 2, 3):
            venue.set_book(f"tok-{prefix}{i}", *QUIET)
    venue.set_book("tok-a1", [(0.60, 1000)], [(0.70, 1000)])
    venue.set_book("tok-b1", [(0.30, 1000)], [(0.40, 1000)])
    cfg = config(strategy=strategy(max_day_usd=10.0))
    run_cycle(services(ledger, venue, cfg), NOW)
    assert [o["market_id"] for o in orders(ledger)] == ["b1"]
    assert decision(ledger, "a1")["reason"] == "day_cap"


def test_event_cap_is_shared_across_buckets(ledger):
    # Half the members land in each of two buckets, both underpriced.
    members = np.vstack([np.full((5, 96), 10.0), np.full((5, 96), 11.0)])
    times = forecast().times
    fc = FakeForecasts(StationForecast(times, members, {"gfs025": NOW}, {"gfs025": slice(0, 10)}))
    venue = FakeVenue([event()])
    for token in ("tok-m0", "tok-m3"):
        venue.set_book(token, *QUIET)
    venue.set_book("tok-m1", [(0.05, 1000)], [(0.10, 1000)])
    venue.set_book("tok-m2", [(0.10, 1000)], [(0.15, 1000)])
    cfg = config(strategy=strategy(max_event_usd=20.0, max_trade_usd=20.0))
    run_cycle(services(ledger, venue, cfg, forecasts=fc), NOW)
    assert [o["market_id"] for o in orders(ledger)] == ["m1"]
    assert decision(ledger, "m2")["reason"] == "event_cap"


def test_observed_floor_rules_out_lower_buckets(ledger):
    venue = signal_venue()
    venue.events = [event(day=TODAY)]
    aviation = FakeAviation(today_reports() + [Obs("KLGA", NOW - 1800, 11.4, "routine", "raw")])
    run_cycle(services(ledger, venue, aviation=aviation), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(0)
    assert decision(ledger, "m2")["p_model"] == pytest.approx(1)
    assert decision(ledger, "m2")["obs_floor_c"] == 11.4


def test_speci_does_not_set_the_floor_on_routine_markets(ledger):
    venue = signal_venue()
    venue.events = [event(day=TODAY)]
    aviation = FakeAviation([Obs("KLGA", NOW - 3000, 9.0, "routine", "raw"),
                             Obs("KLGA", NOW - 1800, 14.0, "speci", "raw")])
    run_cycle(services(ledger, venue, aviation=aviation), NOW)
    assert decision(ledger, "m1")["obs_floor_c"] == 9.0


def test_sensor_gap_reaches_the_price_of_an_nws_market(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY, rule=NWS)]
    cfg = config(model=replace(SHARP, sensor_gap_f=(0.5, 0.3, 0.2)))
    run_cycle(services(ledger, venue, cfg, FakeAviation(today_reports(temp=10.0)), FakeForecasts(forecast(5.0))), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(0.5)
    assert decision(ledger, "m2")["p_model"] == pytest.approx(0.5)


def test_sensor_gap_leaves_markets_settled_on_reports_alone(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    cfg = config(model=replace(SHARP, sensor_gap_f=(0.5, 0.3, 0.2)))
    run_cycle(services(ledger, venue, cfg, FakeAviation(today_reports(temp=10.0)), FakeForecasts(forecast(5.0))), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)


def test_high_at_the_first_moment_of_the_day_reaches_the_model(ledger):
    # A front came through overnight, so the day opened at 10C and fell. No report landed on the opening.
    start = utc(2026, 7, 1, 5)
    reports = [Obs("KLGA", start - 1800, 12.0, "routine", "raw")]
    reports += [Obs("KLGA", t, 8.0, "routine", "raw") for t in range(start + 1800, NOW, HOUR)]
    venue = signal_venue()
    venue.events = [event(TODAY, rule=NWS)]
    cfg = config(model=replace(SHARP, sensor_gap_f=(1.0,)))
    run_cycle(services(ledger, venue, cfg, FakeAviation(reports), FakeForecasts(forecast(5.0))), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)
    assert decision(ledger, "m1")["obs_floor_c"] == 8.0


def test_todays_market_is_skipped_when_station_goes_quiet(ledger):
    venue = signal_venue()
    venue.events = [event(day=TODAY)]
    aviation = FakeAviation([Obs("KLGA", NOW - 4 * HOUR, 9.0, "routine", "raw")])
    run_cycle(services(ledger, venue, aviation=aviation), NOW)
    assert ledger.count("decisions") == 0
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"stale_obs": 1}


def test_tomorrows_market_needs_no_observations(ledger):
    run_cycle(services(ledger, signal_venue()), NOW)
    assert ledger.count("decisions") == 4


def test_finished_day_takes_no_orders_however_cheap_it_looks(ledger):
    # Reports may be missing, so "the high so far" could be nowhere near the high.
    venue = signal_venue()
    venue.events = [event(day=date(2026, 6, 30))]
    run_cycle(services(ledger, venue, aviation=FakeAviation(today_reports())), NOW)
    assert orders(ledger) == []
    assert ledger.count("decisions") == 0
    assert skips(ledger) == {"day_over": 1}


def test_last_second_of_the_day_can_still_trade(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    aviation = FakeAviation(today_reports(temp=10.0, until=DAY_END))
    run_cycle(services(ledger, venue, aviation=aviation), DAY_END - 1)
    assert [o["market_id"] for o in orders(ledger)] == ["m1"]


def test_trading_stops_the_moment_the_day_closes(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    aviation = FakeAviation(today_reports(temp=10.0, until=DAY_END))
    run_cycle(services(ledger, venue, aviation=aviation), DAY_END)
    assert orders(ledger) == []
    assert skips(ledger) == {"day_over": 1}


def test_resting_order_does_not_outlive_its_day(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    s = services(ledger, venue, aviation=FakeAviation(today_reports(temp=10.0, until=DAY_END)))
    run_cycle(s, DAY_END - 60)
    assert orders(ledger)[0]["status"] == "open"
    venue.set_book("tok-m1", [(0.38, 100)], [(0.40, 10000)])
    run_cycle(s, DAY_END + 60)
    assert fills(ledger) == []
    assert orders(ledger)[0]["status"] == "cancelled"


def holed_reports():
    """Five hours missing, enough to hide the high, so the floor means little."""
    return [r for r in today_reports() if not 3 <= (r.obs_time - DAY_END) / HOUR + 24 <= 7]


def test_hole_in_todays_reports_stops_trading(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    run_cycle(services(ledger, venue, aviation=FakeAviation(holed_reports())), NOW)
    assert orders(ledger) == []
    assert decision(ledger, "m1")["reason"] == "thin_observations"
    assert skips(ledger) == {"thin_observations": 1}


def test_hole_in_todays_reports_still_records_the_probability_for_scoring(ledger):
    venue = signal_venue()
    venue.events = [event(TODAY)]
    run_cycle(services(ledger, venue, aviation=FakeAviation(holed_reports())), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)


def test_tomorrow_needs_no_reports_at_all(ledger):
    run_cycle(services(ledger, signal_venue(), aviation=FakeAviation([])), NOW)
    assert [o["market_id"] for o in orders(ledger)] == ["m1"]


def test_lead_days_follow_the_settlement_clock(ledger):
    # 00:10 EDT on July 2 is 23:10 EST on July 1, so an NWS day for July 1 is still running.
    late = utc(2026, 7, 2, 4, 10)
    nws = Rule("nws_cli", "KLGA", "C", "local_standard", "nws_cli")
    reports = [Obs("KLGA", t, 9.0, "routine", "raw") for t in range(utc(2026, 7, 1, 5), late, HOUR)]
    venue = signal_venue()
    venue.events = [event(TODAY, rule=nws), event(TOMORROW, event_id="e2", prefix="n", rule=nws)]
    run_cycle(services(ledger, venue, aviation=FakeAviation(reports)), late)
    rows = ledger.query("SELECT day, lead_days FROM forecast_log ORDER BY day")
    assert [tuple(r) for r in rows] == [("2026-07-01", 0), ("2026-07-02", 1)]



def test_unconfigured_station_is_skipped_with_reason(ledger):
    venue = signal_venue()
    venue.events = [event(rule=Rule("metar_routine", "KBKF", "C", "local_dst", "routine"))]
    run_cycle(services(ledger, venue), NOW)
    assert ledger.count("decisions") == 0
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"station_not_configured": 1}


def test_event_beyond_lead_limit_is_skipped(ledger):
    venue = signal_venue()
    venue.events = [event(day=date(2026, 7, 5))]
    run_cycle(services(ledger, venue), NOW)
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"beyond_lead_limit": 1}


def test_missing_forecast_is_skipped_with_reason(ledger):
    run_cycle(services(ledger, signal_venue(), forecasts=FakeForecasts(None)), NOW)
    assert ledger.count("decisions") == 0
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"no_forecast": 1}


def test_parse_skips_reach_the_run_log(ledger):
    venue = signal_venue()
    venue.parse_skips = Counter({"unsupported_source": 2})
    run_cycle(services(ledger, venue), NOW)
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"unsupported_source": 2}


def test_market_with_no_book_is_left_alone(ledger):
    venue = signal_venue()
    del venue.book_map["tok-m1"]
    run_cycle(services(ledger, venue), NOW)
    assert orders(ledger) == []
    assert ledger.count("decisions") == 3


def test_unchanged_market_logs_once(ledger):
    venue = FakeVenue([event()])
    for token in ("tok-m0", "tok-m1", "tok-m2", "tok-m3"):
        venue.set_book(token, *QUIET)
    s = services(ledger, venue)
    run_cycle(s, NOW)
    run_cycle(s, NOW + 900)
    assert ledger.count("decisions") == 4


def test_failed_cycle_is_recorded_and_raised(ledger):
    venue = signal_venue()
    venue.fail = RuntimeError("gamma down")
    with pytest.raises(RuntimeError):
        run_cycle(services(ledger, venue), NOW)
    row = ledger.query("SELECT status, error FROM runs")[0]
    assert row["status"] == "error"
    assert "gamma down" in row["error"]


def test_successful_cycle_counts_work(ledger):
    counts = run_cycle(services(ledger, signal_venue()), NOW)
    assert counts["events"] == 1
    assert counts["markets"] == 4
    assert counts["orders"] == 1


def test_resting_order_is_cancelled_when_its_book_disappears(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    del venue.book_map["tok-m1"]
    run_cycle(s, NOW + 900)
    got = orders(ledger)[0]
    assert (got["status"], got["reason"]) == ("cancelled", "cannot_price")


def test_resting_order_is_cancelled_when_market_leaves_the_listing(ledger):
    venue = signal_venue()
    s = services(ledger, venue)
    run_cycle(s, NOW)
    venue.events = []
    run_cycle(s, NOW + 900)
    assert orders(ledger)[0]["status"] == "cancelled"


def test_resting_order_is_cancelled_when_day_cannot_be_priced(ledger):
    venue = signal_venue()
    venue.events = [event(day=TODAY)]
    aviation = FakeAviation(today_reports(temp=10.0))
    s = services(ledger, venue, aviation=aviation)
    run_cycle(s, NOW)
    assert orders(ledger)[0]["status"] == "open"
    run_cycle(s, NOW + 4 * HOUR)
    assert orders(ledger)[0]["reason"] == "cannot_price"


def test_metar_outage_only_stops_todays_markets(ledger):
    class Down(FakeAviation):
        def backfill(self, ids):
            raise HttpError(503, "metar")

    venue = signal_venue()
    venue.events = [event(day=TODAY, event_id="today", prefix="t"), event(day=TOMORROW)]
    counts = run_cycle(services(ledger, venue, aviation=Down()), NOW)
    assert ledger.count("decisions") == 4
    assert counts["metar_errors"] == 1
    assert json.loads(ledger.query("SELECT skips FROM runs")[0][0]) == {"stale_obs": 1}


def test_failed_fetch_leaves_station_due_for_backfill(ledger):
    calls = []

    class Flaky(FakeAviation):
        def backfill(self, ids):
            calls.append("backfill")
            raise HttpError(503, "metar")

    s = services(ledger, signal_venue(), aviation=Flaky())
    run_cycle(s, NOW)
    run_cycle(s, NOW + 900)
    assert calls == ["backfill", "backfill"]


def test_forecast_mean_is_logged_once_per_lead(ledger):
    s = services(ledger, signal_venue())
    run_cycle(s, NOW)
    s.forecasts.value = forecast(high=11.0)
    run_cycle(s, NOW + 900)
    rows = ledger.query("SELECT station, day, policy, lead_days, model, mean_high_c FROM forecast_log")
    assert [tuple(r) for r in rows] == [("KLGA", "2026-07-02", "routine", 1, "gfs025", 10.0)]


def two_models(gfs, ecmwf):
    """Five members from each model, flat at these highs."""
    members = np.vstack([np.full((5, 96), float(gfs)), np.full((5, 96), float(ecmwf))])
    runs = {"gfs025": NOW, "ecmwf_ifs025": NOW}
    return StationForecast(forecast().times, members, runs, {"gfs025": slice(0, 5), "ecmwf_ifs025": slice(5, 10)})


def track_record(ledger, model, forecast_c, high_c, days=10):
    for offset in range(days):
        day = date(2026, 6, 10 + offset)
        ledger.log_forecast("KLGA", day, "routine", 1, model, NOW, forecast_c, NOW)
        ledger.add_daily_high("KLGA", day, "routine", high_c, 24)


def test_station_bias_moves_the_forecast(ledger):
    track_record(ledger, "gfs025", 10.0, 11.0)
    cfg = config(bias=BiasConfig(window_days=30, shrink_days=0.0, min_days=0))
    run_cycle(services(ledger, signal_venue(), cfg), NOW)
    assert decision(ledger, "m2")["p_model"] == pytest.approx(1)


def test_each_model_is_corrected_on_its_own_record(ledger):
    # GFS runs 2C cold and ECMWF doesn't, so together they agree on 10C rather than splitting in two.
    track_record(ledger, "gfs025", 8.0, 10.0)
    track_record(ledger, "ecmwf_ifs025", 10.0, 10.0)
    cfg = config(bias=BiasConfig(window_days=30, shrink_days=0.0, min_days=0))
    run_cycle(services(ledger, signal_venue(), cfg, forecasts=FakeForecasts(two_models(8, 10))), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)


def test_correction_leaves_the_stored_forecast_alone(ledger):
    fc = two_models(8, 10)
    track_record(ledger, "gfs025", 8.0, 10.0)
    cfg = config(bias=BiasConfig(window_days=30, shrink_days=0.0, min_days=0))
    run_cycle(services(ledger, signal_venue(), cfg, forecasts=FakeForecasts(fc)), NOW)
    assert fc.members[0, 0] == 8.0


def test_each_models_raw_high_goes_on_its_record(ledger):
    run_cycle(services(ledger, signal_venue(), forecasts=FakeForecasts(two_models(8, 10))), NOW)
    rows = ledger.query("SELECT model, init_time, mean_high_c FROM forecast_log ORDER BY mean_high_c")
    assert [tuple(r) for r in rows] == [("gfs025", NOW, 8.0), ("ecmwf_ifs025", NOW, 10.0)]


@pytest.mark.parametrize("gfs_days, ecmwf_days", [(5, 4), (4, 5)])
def test_no_trade_until_every_model_has_a_track_record(ledger, gfs_days, ecmwf_days):
    track_record(ledger, "gfs025", 10.0, 10.0, days=gfs_days)
    track_record(ledger, "ecmwf_ifs025", 10.0, 10.0, days=ecmwf_days)
    cfg = config(bias=BiasConfig(min_days=5))
    run_cycle(services(ledger, signal_venue(), cfg, forecasts=FakeForecasts(two_models(10, 10))), NOW)
    assert orders(ledger) == []
    assert decision(ledger, "m1")["reason"] == "warming_up"


def test_trading_starts_once_the_track_record_is_long_enough(ledger):
    track_record(ledger, "gfs025", 10.0, 10.0, days=5)
    track_record(ledger, "ecmwf_ifs025", 10.0, 10.0, days=5)
    cfg = config(bias=BiasConfig(min_days=5))
    run_cycle(services(ledger, signal_venue(), cfg, forecasts=FakeForecasts(two_models(10, 10))), NOW)
    assert [o["market_id"] for o in orders(ledger)] == ["m1"]


def test_no_trade_while_a_configured_model_is_missing(ledger):
    # Its record is what makes the pool honest, and one model alone is a different forecast from the two together.
    cfg = config(forecast=ForecastConfig(models=("gfs025", "ecmwf_ifs025")))
    run_cycle(services(ledger, signal_venue(), cfg), NOW)
    assert orders(ledger) == []
    assert decision(ledger, "m1")["reason"] == "model_missing"
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)
    assert skips(ledger)["model_missing"] == 1


def test_warming_up_still_records_the_probability_for_scoring(ledger):
    cfg = config(bias=BiasConfig(min_days=5))
    run_cycle(services(ledger, signal_venue(), cfg), NOW)
    assert decision(ledger, "m1")["p_model"] == pytest.approx(1)


def test_warming_up_shows_in_the_run_skips(ledger):
    cfg = config(bias=BiasConfig(min_days=5))
    run_cycle(services(ledger, signal_venue(), cfg), NOW)
    assert json.loads(ledger.query("SELECT skips FROM runs")[0]["skips"])["warming_up"] == 1


def test_warming_up_cancels_a_resting_order(ledger):
    s = services(ledger, signal_venue())
    run_cycle(s, NOW)
    assert [o["status"] for o in orders(ledger)] == ["open"]
    run_cycle(replace(s, cfg=config(bias=BiasConfig(min_days=5))), NOW + 900)
    assert [o["status"] for o in orders(ledger)] == ["cancelled"]


def test_stale_station_triggers_backfill(ledger):
    calls = []

    class Spy(FakeAviation):
        def backfill(self, ids):
            calls.append(("backfill", tuple(ids)))
            return []

        def recent(self, ids):
            calls.append(("recent", tuple(ids)))
            return []

    s = services(ledger, signal_venue(), aviation=Spy())
    run_cycle(s, NOW)
    run_cycle(s, NOW + 900)
    assert calls == [("backfill", ("KLGA",)), ("recent", ("KLGA",))]

from datetime import date, datetime, timezone

import numpy as np
import pytest

from weatherbot.core import Book, Bucket, Event, Market, Rule
from weatherbot.fees import FeeSchedule
from weatherbot.ledger import DecisionRow, Ledger
from weatherbot.metar import Obs
from weatherbot.openmeteo import Snapshot

RULE = Rule("metar_routine", "KLGA", "F", "local_dst", "routine")
DAY = date(2026, 11, 1)


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


NOW = utc(2026, 11, 1, 12)


def market(market_id="m1", bucket=Bucket(50, 51)):
    return Market(market_id, "50-51F", bucket, f"tok-{market_id}", 0.01, 5.0, FeeSchedule(0.05, 1.0, 0.25))


def event(event_id="e1", markets=None):
    return Event("intl", event_id, "slug", "Highest temperature in NYC", DAY, RULE, tuple(markets or [market()]))


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    db.upsert_event(event(), NOW)
    yield db
    db.close()


def row(**kw):
    base = dict(run_id=1, ts=NOW, venue="intl", event_id="e1", market_id="m1", p_model=0.5, p_lo=0.4, p_hi=0.6,
                q=0.4, best_bid=0.3, best_ask=0.35, fee_rate=0.05, edge=0.04, side="yes", action="skip",
                reason="edge_below_threshold", style=None, limit_px=None, shares=0.0, book_id=None,
                forecast_runs='{"gfs025": 1}', obs_floor_c=None, lead_hours=20.0)
    return DecisionRow(**{**base, **kw})


def buy(ledger, side="yes", shares=10.0, limit=0.4, ts=NOW, market_id="m1", event_id="e1", style="taker"):
    return ledger.add_order(None, "intl", "paper", event_id, market_id, side, style, limit, shares, ts)


def test_opening_twice_keeps_data(tmp_path):
    path = tmp_path / "t.sqlite"
    first = Ledger(path)
    first.upsert_event(event(), NOW)
    first.close()
    second = Ledger(path)
    assert second.event_row("intl", "e1")["station"] == "KLGA"
    second.close()


def test_upserting_event_keeps_first_seen_and_settlement(ledger):
    ledger.settle_market("intl", "m1", 1.0, NOW + 5)
    ledger.upsert_event(event(), NOW + 10)
    assert ledger.event_row("intl", "e1")["first_seen"] == NOW
    assert ledger.market_row("intl", "m1")["payout_yes"] == 1.0


def test_corrected_observation_replaces_original(ledger):
    ledger.add_obs([Obs("KLGA", 100, 10.0, "routine", "first")])
    ledger.add_obs([Obs("KLGA", 100, 11.0, "routine", "COR")])
    got = ledger.obs_between("KLGA", 0, 200)
    assert [(o.temp_c, o.raw) for o in got] == [(11.0, "COR")]


def test_routine_and_speci_at_same_time_are_both_kept(ledger):
    ledger.add_obs([Obs("KLGA", 100, 10.0, "routine", "a"), Obs("KLGA", 100, 11.0, "speci", "b")])
    assert len(ledger.obs_between("KLGA", 0, 200)) == 2


def test_obs_between_filters_on_station_and_time(ledger):
    ledger.add_obs([Obs("KLGA", 100, 1.0, "routine", "a"), Obs("KLGA", 300, 2.0, "routine", "b"),
                    Obs("KJFK", 100, 3.0, "routine", "c")])
    assert [o.temp_c for o in ledger.obs_between("KLGA", 50, 200)] == [1.0]


def test_max_groups_survive_storage(ledger):
    ledger.add_obs([Obs("KLGA", 100, 1.0, "routine", "a", 2.5, 3.5)])
    got = ledger.obs_between("KLGA", 0, 200)[0]
    assert (got.max6h_c, got.max24h_c) == (2.5, 3.5)


def test_snapshot_round_trip(ledger):
    snap = Snapshot("gfs025", 500, np.array([0, 3600]), np.array([[1.5, np.nan], [2.5, 3.5]]))
    ledger.save_snapshot("KLGA", snap, 600)
    got = ledger.latest_snapshot("KLGA", "gfs025")
    assert got.init_time == 500
    assert got.times.tolist() == [0, 3600]
    assert got.members[1].tolist() == [2.5, 3.5]
    assert np.isnan(got.members[0, 1])


def test_latest_snapshot_picks_newest_run(ledger):
    for init in (500, 700, 600):
        ledger.save_snapshot("KLGA", Snapshot("gfs025", init, np.array([0]), np.array([[1.0]])), init + 10)
    assert ledger.latest_snapshot("KLGA", "gfs025").init_time == 700


def test_latest_snapshot_can_look_back_for_replay(ledger):
    for init in (500, 700):
        ledger.save_snapshot("KLGA", Snapshot("gfs025", init, np.array([0]), np.array([[1.0]])), init + 10)
    assert ledger.latest_snapshot("KLGA", "gfs025", fetched_by=600).init_time == 500


def test_missing_snapshot_is_none(ledger):
    assert ledger.latest_snapshot("KJFK", "gfs025") is None


def test_saving_same_run_twice_is_harmless(ledger):
    snap = Snapshot("gfs025", 500, np.array([0]), np.array([[1.0]]))
    ledger.save_snapshot("KLGA", snap, 600)
    ledger.save_snapshot("KLGA", snap, 601)
    assert ledger.latest_snapshot("KLGA", "gfs025").init_time == 500


def test_identical_books_share_a_row(ledger):
    book = Book.from_levels([(0.4, 10)], [(0.5, 20)])
    assert ledger.save_book(book) == ledger.save_book(book)


def test_book_round_trip_trims_depth(ledger):
    asks = [(0.5 + i / 100, 10) for i in range(20)]
    stored = ledger.load_book(ledger.save_book(Book.from_levels([(0.4, 10)], asks)))
    assert stored.best_ask == 0.5
    assert stored.best_bid == 0.4
    assert len(stored.asks) == 10


def test_repeated_decision_state_is_not_logged_twice(ledger):
    assert ledger.log_decision(row()) is not None
    assert ledger.log_decision(row(ts=NOW + 900)) is None
    assert ledger.count("decisions") == 1


def test_changed_price_is_logged(ledger):
    ledger.log_decision(row())
    assert ledger.log_decision(row(best_ask=0.36)) is not None


def test_changed_model_probability_is_logged(ledger):
    ledger.log_decision(row())
    assert ledger.log_decision(row(p_model=0.6)) is not None


def test_small_model_wobble_is_not_logged(ledger):
    ledger.log_decision(row(p_model=0.5001))
    assert ledger.log_decision(row(p_model=0.5002)) is None


def test_crossing_into_new_lead_bucket_is_logged(ledger):
    ledger.log_decision(row(lead_hours=25.0))
    assert ledger.log_decision(row(lead_hours=23.0)) is not None


def test_buy_decisions_are_always_logged(ledger):
    assert ledger.log_decision(row(action="buy")) is not None
    assert ledger.log_decision(row(action="buy")) is not None


def test_taker_fill_shows_up_in_exposure(ledger):
    order = buy(ledger)
    ledger.add_fill(order, NOW, 0.4, 10.0, 0.12)
    ledger.close_order(order, "filled", NOW)
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == pytest.approx(4.12)
    assert e.event_spent == pytest.approx(4.12)
    assert e.held_yes == pytest.approx(4.12)
    assert e.held_no == 0


def test_resting_order_counts_as_committed(ledger):
    buy(ledger, side="no", style="maker", shares=10.0, limit=0.3)
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == pytest.approx(3.0)
    assert e.held_no == pytest.approx(3.0)


def test_partly_filled_order_counts_fill_plus_remainder(ledger):
    order = buy(ledger, style="maker", shares=10.0, limit=0.3)
    ledger.add_fill(order, NOW, 0.3, 4.0, 0.0)
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == pytest.approx(3.0)


def test_order_with_an_unknown_outcome_still_counts_as_spent(ledger):
    order = buy(ledger, shares=10.0, limit=0.3, style="maker")
    ledger.close_order(order, "unknown", NOW + 1, "outcome_unknown")
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == pytest.approx(3.0)
    assert e.event_spent == pytest.approx(3.0)
    assert e.held_yes == pytest.approx(3.0)


def test_unknown_order_from_an_earlier_day_leaves_todays_cap_alone(ledger):
    order = buy(ledger, shares=10.0, limit=0.3, style="maker", ts=NOW - 2 * 86400)
    ledger.close_order(order, "unknown", NOW - 2 * 86400, "outcome_unknown")
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == 0
    assert e.event_spent == pytest.approx(3.0)


def test_unknown_order_stays_in_its_own_mode(ledger):
    order = ledger.add_order(None, "intl", "live", "e1", "m1", "yes", "maker", 0.3, 10.0, NOW)
    ledger.close_order(order, "unknown", NOW + 1, "outcome_unknown")
    assert ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW).day_spent == 0


def test_cancelled_order_releases_exposure(ledger):
    order = buy(ledger, style="maker")
    ledger.close_order(order, "cancelled", NOW + 1, "signal_gone")
    assert ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW).day_spent == 0


def test_yesterdays_spending_does_not_use_todays_cap(ledger):
    yesterday = NOW - 86400
    order = buy(ledger, ts=yesterday)
    ledger.add_fill(order, yesterday, 0.4, 10.0, 0.0)
    ledger.close_order(order, "filled", yesterday)
    e = ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW)
    assert e.day_spent == 0
    assert e.event_spent == pytest.approx(4.0)


def test_paper_and_live_books_are_separate(ledger):
    order = ledger.add_order(None, "intl", "live", "e1", "m1", "yes", "taker", 0.4, 10.0, NOW)
    ledger.add_fill(order, NOW, 0.4, 10.0, 0.0)
    assert ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW).day_spent == 0
    assert ledger.exposure("intl", "live", "e1", "m1", 1000.0, NOW).day_spent == pytest.approx(4.0)


def test_winning_trade_adds_to_bankroll(ledger):
    order = buy(ledger)
    ledger.add_fill(order, NOW, 0.4, 10.0, 0.12)
    ledger.settle_market("intl", "m1", 1.0, NOW + 1)
    # 10 shares pay $10, less $4 cost and $0.12 fee.
    assert ledger.realized_pnl("intl", "paper") == pytest.approx(5.88)
    assert ledger.exposure("intl", "paper", "e1", "m1", 1000.0, NOW).bankroll == pytest.approx(1005.88)


def test_losing_no_trade_loses_stake(ledger):
    order = buy(ledger, side="no", limit=0.3)
    ledger.add_fill(order, NOW, 0.3, 10.0, 0.0)
    ledger.settle_market("intl", "m1", 1.0, NOW + 1)
    assert ledger.realized_pnl("intl", "paper") == pytest.approx(-3.0)


def test_half_payout_is_honoured(ledger):
    order = buy(ledger, limit=0.4)
    ledger.add_fill(order, NOW, 0.4, 10.0, 0.0)
    ledger.settle_market("intl", "m1", 0.5, NOW + 1)
    assert ledger.realized_pnl("intl", "paper") == pytest.approx(1.0)


def test_unsettled_trade_has_no_realized_pnl(ledger):
    order = buy(ledger)
    ledger.add_fill(order, NOW, 0.4, 10.0, 0.0)
    assert ledger.realized_pnl("intl", "paper") == 0


def test_open_orders_lists_remaining_size(ledger):
    order = buy(ledger, style="maker", shares=10.0)
    ledger.add_fill(order, NOW, 0.4, 4.0, 0.0)
    got = ledger.open_orders("intl", "paper")
    assert [(o.id, o.remaining) for o in got] == [(order, 6.0)]


def test_closed_orders_are_not_open(ledger):
    order = buy(ledger, style="maker")
    ledger.close_order(order, "cancelled", NOW)
    assert ledger.open_orders("intl", "paper") == []


def test_unsettled_events_lists_events_with_unpaid_markets(ledger):
    assert [e["event_id"] for e in ledger.unsettled_events("intl")] == ["e1"]
    ledger.settle_market("intl", "m1", 1.0, NOW)
    assert ledger.unsettled_events("intl") == []


def test_run_bookkeeping(ledger):
    run = ledger.start_run("intl", "paper", NOW)
    ledger.finish_run(run, NOW + 5, "ok", {"events": 3}, {"unsupported_source": 1})
    got = ledger.run_row(run)
    assert (got["status"], got["finished_at"]) == ("ok", NOW + 5)
    assert '"events": 3' in got["counts"]


def daily(ledger, day, forecast_c, high_c, lead=1, station="KLGA", policy="routine", model="gfs025"):
    ledger.log_forecast(station, day, policy, lead, model, NOW, forecast_c, NOW)
    ledger.add_daily_high(station, day, policy, high_c, 24)


def test_bias_is_mean_error_shrunk_by_sample_size(ledger):
    for offset in range(5):
        daily(ledger, date(2026, 6, 1 + offset), 10.0, 12.0)
    # Five days of +2C error, kept at 5 / (5 + 5).
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 5.0).value == pytest.approx(1.0)


def test_bias_says_how_many_days_it_rests_on(ledger):
    for offset in range(3):
        daily(ledger, date(2026, 6, 1 + offset), 10.0, 12.0)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 5.0).days == 3


def test_a_forecast_with_no_observed_high_yet_is_not_history(ledger):
    ledger.log_forecast("KLGA", date(2026, 6, 9), "routine", 1, "gfs025", NOW, 10.0, NOW)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 5.0).days == 0


def test_bias_is_kept_per_model(ledger):
    daily(ledger, date(2026, 6, 9), 10.0, 12.0, model="gfs025")
    daily(ledger, date(2026, 6, 9), 11.5, 12.0, model="ecmwf_ifs025")
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 0.0).value == pytest.approx(2.0)
    assert ledger.bias("KLGA", "routine", 1, "ecmwf_ifs025", date(2026, 6, 10), 30, 0.0).value == pytest.approx(0.5)


def test_bias_without_history_is_zero(ledger):
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 5.0).value == 0


def test_bias_ignores_days_outside_window(ledger):
    daily(ledger, date(2026, 4, 1), 10.0, 20.0)
    daily(ledger, date(2026, 6, 9), 10.0, 11.0)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 0.0).value == pytest.approx(1.0)


def test_bias_never_looks_at_the_day_being_priced(ledger):
    daily(ledger, date(2026, 6, 10), 10.0, 20.0)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 0.0).value == 0


def test_bias_is_kept_per_lead_policy_and_station(ledger):
    daily(ledger, date(2026, 6, 9), 10.0, 11.0, lead=2)
    daily(ledger, date(2026, 6, 9), 10.0, 12.0, policy="nws_cli")
    daily(ledger, date(2026, 6, 9), 10.0, 13.0, station="KJFK")
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 0.0).value == 0


def test_first_forecast_for_a_lead_wins(ledger):
    ledger.log_forecast("KLGA", DAY, "routine", 1, "gfs025", NOW, 10.0, NOW)
    ledger.log_forecast("KLGA", DAY, "routine", 1, "gfs025", NOW, 15.0, NOW + 900)
    ledger.add_daily_high("KLGA", DAY, "routine", 11.0, 24)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 11, 2), 30, 0.0).value == pytest.approx(1.0)


def test_corrected_daily_high_replaces_original(ledger):
    daily(ledger, date(2026, 6, 9), 10.0, 11.0)
    ledger.add_daily_high("KLGA", date(2026, 6, 9), "routine", 12.0, 25)
    assert ledger.bias("KLGA", "routine", 1, "gfs025", date(2026, 6, 10), 30, 0.0).value == pytest.approx(2.0)


def archived(ledger):
    ledger.add_quotes("intl", "tok", [(NOW, 0.4, 0.45)])
    ledger.add_outcomes("intl", {"m1": 1.0})
    ledger.add_obs([Obs("KLGA", NOW, 10.0, "routine", "raw")])
    ledger.save_snapshot("KLGA", Snapshot("gfs_global", NOW, np.array([NOW]), np.array([[10.0]])), NOW)


def test_quote_at_returns_the_newest_quote_not_after_now(ledger):
    ledger.add_quotes("intl", "tok", [(NOW - 900, 0.40, 0.45), (NOW, 0.41, 0.46), (NOW + 1, 0.9, 0.95)])
    assert ledger.quote_at("intl", "tok", NOW, 3600) == (0.41, 0.46)


def test_quote_at_ignores_a_quote_older_than_max_age(ledger):
    ledger.add_quotes("intl", "tok", [(NOW - 3601, 0.40, 0.45)])
    assert ledger.quote_at("intl", "tok", NOW, 3600) is None


def test_quotes_are_kept_per_venue_and_token(ledger):
    ledger.add_quotes("intl", "tok", [(NOW, 0.40, 0.45)])
    assert ledger.quote_at("us", "tok", NOW, 3600) is None
    assert ledger.quote_at("intl", "other", NOW, 3600) is None
    assert ledger.has_quotes("intl", "tok")
    assert not ledger.has_quotes("us", "tok")


def test_replay_outcomes_come_back_for_the_markets_asked_about(ledger):
    ledger.add_outcomes("intl", {"m1": 1.0, "m2": 0.0, "m3": 0.0})
    assert ledger.outcomes("intl", ["m1", "m2", "missing"]) == {"m1": 1.0, "m2": 0.0}
    assert ledger.outcomes("us", ["m1"]) == {}


def test_has_snapshot_names_one_run(ledger):
    ledger.save_snapshot("KLGA", Snapshot("gfs_global", NOW, np.array([NOW]), np.array([[10.0]])), NOW)
    assert ledger.has_snapshot("KLGA", "gfs_global", NOW)
    assert not ledger.has_snapshot("KLGA", "gfs_global", NOW + 1)
    assert not ledger.has_snapshot("KLGA", "icon_global", NOW)
    assert not ledger.has_snapshot("KJFK", "gfs_global", NOW)


def test_clear_results_empties_everything_a_replay_produced(ledger):
    archived(ledger)
    run = ledger.start_run("intl", "backtest", NOW)
    ledger.upsert_event(event(), NOW)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.save_book(Book.from_levels([(0.4, 1)], [(0.5, 1)]))
    ledger.log_decision(row(run_id=run))
    order = ledger.add_order(1, "intl", "backtest", "e1", "m1", "yes", "taker", 0.5, 1.0, NOW)
    ledger.add_fill(order, NOW, 0.5, 1.0, 0.0)
    ledger.log_forecast("KLGA", DAY, "routine", 1, "gfs025", NOW, 10.0, NOW)
    ledger.add_daily_high("KLGA", DAY, "routine", 11.0, 24)
    ledger.add_audit("intl", "e1", "metar_routine", "KLGA", DAY, 11.0, 52, "m1", "m1", True, NOW)
    ledger.mark_fetched(["KLGA"], NOW)
    ledger.day_equity("intl", DAY, 100.0)

    ledger.clear_results()

    for table in ("runs", "events", "markets", "books", "decisions", "decision_state", "orders", "fills",
                  "forecast_log", "daily_highs", "rule_audit", "station_state", "day_equity"):
        assert ledger.count(table) == 0, table


def test_clear_results_keeps_what_was_downloaded(ledger):
    archived(ledger)
    ledger.clear_results()
    for table in ("price_history", "replay_outcomes", "metar_obs", "forecast_snapshots"):
        assert ledger.count(table) == 1, table


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_clear_results_refuses_a_ledger_that_holds_anything_but_replays(ledger, mode):
    ledger.start_run("intl", mode, NOW)
    with pytest.raises(ValueError):
        ledger.clear_results()
    assert ledger.count("runs") == 1

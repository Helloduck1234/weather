from datetime import date, datetime, timezone

import pytest

from weatherbot.core import Bucket, Event, Market, Rule
from weatherbot.fees import FeeSchedule
from weatherbot.ledger import DecisionRow, Ledger
from weatherbot import report as report_module
from weatherbot.report import build_report, render_markdown, write_report

RULE = Rule("metar_routine", "KLGA", "C", "local_dst", "routine")
DENVER = Rule("metar_routine", "KDEN", "C", "local_dst", "routine")


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


NOW = utc(2026, 7, 10, 12)


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    yield db
    db.close()


def add_event(ledger, event_id="e1", day=date(2026, 7, 1), rule=RULE, market_ids=("m1",)):
    markets = tuple(
        Market(m, m, Bucket(i, i), f"tok-{m}", 0.01, 1.0, FeeSchedule(0.0)) for i, m in enumerate(market_ids)
    )
    ledger.upsert_event(Event("intl", event_id, "slug", "Highest", day, rule, markets), NOW)


def row(ledger, market_id="m1", event_id="e1", p=0.6, bid=0.4, ask=0.5, lead=20.0, ts=NOW, edge=0.1, action="skip",
        reason="edge_below_threshold", mode="paper"):
    run = ledger.start_run("intl", mode, ts)
    return ledger.log_decision(
        DecisionRow(run, ts, "intl", event_id, market_id, p, p, p, p, bid, ask, 0.0, edge, "yes", action, reason, None,
                    None, 0.0, None, "{}", None, lead)
    )


def trade(ledger, market_id="m1", event_id="e1", side="yes", price=0.4, shares=10.0, fee=0.0, style="taker",
          decision=None, mode="paper"):
    order = ledger.add_order(decision, "intl", mode, event_id, market_id, side, style, price, shares, NOW)
    ledger.add_fill(order, NOW, price, shares, fee)
    ledger.close_order(order, "filled", NOW)


def report(ledger, mode="paper", bankroll=1000.0):
    return build_report(ledger, "intl", mode, bankroll, NOW)


def test_realized_pnl_nets_wins_losses_and_fees(ledger):
    add_event(ledger, market_ids=("m1", "m2"))
    trade(ledger, "m1", price=0.4, shares=10.0, fee=0.1)  # wins 6.0 - 0.1
    trade(ledger, "m2", price=0.3, shares=10.0)  # loses 3.0
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.settle_market("intl", "m2", 0.0, NOW)
    got = report(ledger).pnl
    assert got.realized == pytest.approx(2.9)
    assert got.fees == pytest.approx(0.1)
    assert (got.fills, got.settled) == (2, 2)
    assert got.win_rate == pytest.approx(0.5)


def test_paper_report_ignores_live_trades(ledger):
    add_event(ledger)
    trade(ledger, mode="live")
    ledger.settle_market("intl", "m1", 1.0, NOW)
    assert report(ledger).pnl.fills == 0


def test_open_position_is_marked_to_latest_mid(ledger):
    add_event(ledger)
    row(ledger, bid=0.40, ask=0.50, ts=NOW - 100)
    row(ledger, bid=0.60, ask=0.70, ts=NOW)
    trade(ledger, price=0.40, shares=10.0)
    got = report(ledger).pnl
    assert got.unrealized == pytest.approx(2.5)
    assert got.open_cost == pytest.approx(4.0)


def test_open_no_position_is_marked_from_the_other_side(ledger):
    add_event(ledger)
    row(ledger, bid=0.60, ask=0.70)
    trade(ledger, side="no", price=0.30, shares=10.0)
    assert report(ledger).pnl.unrealized == pytest.approx(0.5)


def test_open_position_with_no_quote_is_held_at_cost(ledger):
    add_event(ledger)
    trade(ledger, price=0.40, shares=10.0)
    assert report(ledger).pnl.unrealized == 0


def test_equity_is_bankroll_plus_both_kinds_of_pnl(ledger):
    add_event(ledger, market_ids=("m1", "m2"))
    row(ledger, "m2", bid=0.60, ask=0.70)
    trade(ledger, "m1", price=0.4, shares=10.0)
    trade(ledger, "m2", price=0.4, shares=10.0)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    assert report(ledger, bankroll=100.0).pnl.equity == pytest.approx(108.5)


def test_fee_stress_charges_the_old_taker_curve_on_every_fill(ledger):
    add_event(ledger)
    trade(ledger, price=0.4, shares=100.0)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    # 100 x 0.05 x 0.4 x 0.6 = 1.20 off a 60.00 win.
    assert report(ledger).pnl.fee_stress == pytest.approx(58.8)


def test_max_drawdown_is_worst_fall_from_a_peak_by_day(ledger):
    add_event(ledger, "e1", date(2026, 7, 1), market_ids=("a",))
    add_event(ledger, "e2", date(2026, 7, 2), market_ids=("b",))
    add_event(ledger, "e3", date(2026, 7, 3), market_ids=("c",))
    add_event(ledger, "e4", date(2026, 7, 4), market_ids=("d",))
    for market, event, payout in (("a", "e1", 1.0), ("b", "e2", 0.0), ("c", "e3", 0.0), ("d", "e4", 1.0)):
        trade(ledger, market, event, price=0.5, shares=10.0)
        ledger.settle_market("intl", market, payout, NOW)
    # Daily P&L +5, -5, -5, +5: peak 5, trough -5.
    assert report(ledger).pnl.max_drawdown == pytest.approx(10.0)


def test_pnl_interval_needs_at_least_two_days(ledger):
    add_event(ledger)
    trade(ledger)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    assert report(ledger).pnl.interval is None


def test_pnl_interval_brackets_the_total(ledger):
    for day in range(1, 11):
        add_event(ledger, f"e{day}", date(2026, 6, day), market_ids=(f"m{day}",))
        trade(ledger, f"m{day}", f"e{day}", price=0.5, shares=10.0)
        ledger.settle_market("intl", f"m{day}", 1.0 if day % 3 else 0.0, NOW)
    got = report(ledger).pnl
    assert got.interval[0] < got.realized < got.interval[1]


def test_entry_edge_sits_beside_realized_edge(ledger):
    add_event(ledger)
    decision = row(ledger, edge=0.20, action="buy", reason="signal")
    trade(ledger, price=0.40, shares=10.0, decision=decision)
    ledger.settle_market("intl", "m1", 0.0, NOW)
    got = report(ledger).pnl
    assert got.entry_edge == pytest.approx(0.20)
    assert got.realized_edge == pytest.approx(-0.40)


def test_pnl_breaks_down_by_station_lead_side_and_style(ledger):
    add_event(ledger, "e1", market_ids=("m1",))
    add_event(ledger, "e2", rule=DENVER, market_ids=("m2",))
    first = row(ledger, "m1", "e1", lead=3.0, action="buy", reason="signal")
    second = row(ledger, "m2", "e2", lead=30.0, action="buy", reason="signal")
    trade(ledger, "m1", "e1", price=0.4, shares=10.0, decision=first)
    trade(ledger, "m2", "e2", side="no", price=0.3, shares=10.0, style="maker", decision=second)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.settle_market("intl", "m2", 1.0, NOW)
    groups = report(ledger).groups
    assert groups["station"] == {"KLGA": (1, pytest.approx(6.0)), "KDEN": (1, pytest.approx(-3.0))}
    assert groups["lead"] == {"0-6h": (1, pytest.approx(6.0)), "24-48h": (1, pytest.approx(-3.0))}
    assert groups["side"] == {"yes": (1, pytest.approx(6.0)), "no": (1, pytest.approx(-3.0))}
    assert groups["style"] == {"taker": (1, pytest.approx(6.0)), "maker": (1, pytest.approx(-3.0))}


def test_brier_compares_model_with_market_mid(ledger):
    add_event(ledger, market_ids=("m1", "m2"))
    row(ledger, "m1", p=0.8, bid=0.5, ask=0.7)
    row(ledger, "m2", p=0.1, bid=0.1, ask=0.3)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.settle_market("intl", "m2", 0.0, NOW)
    got = report(ledger).brier
    assert got.n == 2
    assert got.model == pytest.approx((0.04 + 0.01) / 2)
    assert got.market == pytest.approx((0.16 + 0.04) / 2)


def test_brier_scores_first_decision_in_each_lead_bucket(ledger):
    add_event(ledger)
    row(ledger, p=0.9, lead=30.0, ts=NOW - 300)
    row(ledger, p=0.1, lead=29.0, ts=NOW - 200)  # same bucket: later price, ignored
    row(ledger, p=0.5, lead=20.0, ts=NOW - 100)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    got = report(ledger).brier
    assert got.n == 2
    assert got.model == pytest.approx((0.01 + 0.25) / 2)
    assert set(got.by_lead) == {"24-48h", "6-24h"}


def test_brier_skips_one_sided_books(ledger):
    add_event(ledger)
    row(ledger, bid=None, ask=0.5)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    assert report(ledger).brier.n == 0


def test_brier_skips_voided_markets(ledger):
    add_event(ledger)
    row(ledger)
    ledger.settle_market("intl", "m1", 0.5, NOW)
    assert report(ledger).brier.n == 0


def test_contested_brier_ignores_buckets_the_market_had_already_ruled_out(ledger):
    add_event(ledger, market_ids=("m1", "m2"))
    row(ledger, "m1", p=0.8, bid=0.5, ask=0.7)
    row(ledger, "m2", p=0.0, bid=0.0, ask=0.02)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.settle_market("intl", "m2", 0.0, NOW)
    got = report(ledger).brier
    assert got.contested_n == 1
    assert got.contested_model == pytest.approx(0.04)
    assert got.contested_market == pytest.approx(0.16)


def test_calibration_bins_hold_mean_forecast_and_hit_rate(ledger):
    add_event(ledger, market_ids=("a", "b", "c", "d"))
    for market, p, payout in (("a", 0.72, 1.0), ("b", 0.74, 1.0), ("c", 0.76, 0.0), ("d", 0.78, 1.0)):
        row(ledger, market, p=p, bid=0.1, ask=0.3)
        ledger.settle_market("intl", market, payout, NOW)
    model = report(ledger).calibration["model"]
    assert len(model) == 1
    got = model[0]
    assert (got.lo, got.hi, got.n) == (0.7, 0.8, 4)
    assert got.mean_forecast == pytest.approx(0.75)
    assert got.observed == pytest.approx(0.75)
    assert got.low < 0.75 < got.high


def test_interval_is_the_wilson_interval(ledger):
    add_event(ledger, market_ids=("a", "b", "c", "d"))
    for market, payout in (("a", 1.0), ("b", 0.0), ("c", 0.0), ("d", 0.0)):
        row(ledger, market, p=0.3, bid=0.1, ask=0.3)
        ledger.settle_market("intl", market, payout, NOW)
    got = report(ledger).calibration["model"][0]
    assert (got.low, got.high) == pytest.approx((0.0579, 0.6438), abs=0.0001)


def test_probability_of_exactly_one_lands_in_the_top_bin(ledger):
    add_event(ledger)
    row(ledger, p=1.0, bid=0.1, ask=0.3)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    got = report(ledger).calibration["model"][0]
    assert (got.lo, got.hi) == (0.9, 1.0)


def settled_bucket(ledger, n, p, payout):
    ids = tuple(f"m{i}" for i in range(n))
    add_event(ledger, market_ids=ids)
    for market in ids:
        row(ledger, market, p=p, bid=0.1, ask=0.3)
        ledger.settle_market("intl", market, payout, NOW)


@pytest.mark.parametrize("n", [7, 11, 16, 30, 33])
def test_interval_holds_a_hit_rate_of_one(ledger, n):
    settled_bucket(ledger, n, 0.95, 1.0)
    got = report(ledger).calibration["model"][0]
    assert got.low <= got.observed == 1.0 <= got.high


@pytest.mark.parametrize("n", [79, 95, 103, 109, 151])
def test_interval_holds_a_hit_rate_of_zero(ledger, n):
    settled_bucket(ledger, n, 0.05, 0.0)
    got = report(ledger).calibration["model"][0]
    assert got.low <= got.observed == 0.0 <= got.high


@pytest.mark.parametrize("n, p, payout", [(7, 0.95, 1.0), (11, 0.95, 1.0), (16, 0.95, 1.0), (79, 0.05, 0.0)])
def test_chart_draws_when_every_forecast_in_a_bin_came_true(ledger, tmp_path, n, p, payout):
    settled_bucket(ledger, n, p, payout)
    _, chart = write_report(report(ledger), tmp_path / "out")
    assert chart.read_bytes()[:4] == b"\x89PNG"


def test_chart_failure_costs_the_chart_not_the_report(ledger, tmp_path, monkeypatch):
    def broken(report, path):
        raise ValueError("'yerr' must not contain negative values")

    monkeypatch.setattr(report_module, "_draw_calibration", broken)
    add_event(ledger)
    row(ledger, p=0.8, bid=0.5, ask=0.7)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    md, chart = write_report(report(ledger), tmp_path / "out")
    assert chart is None
    text = md.read_text(encoding="utf8")
    assert "Realized P&L" in text
    assert "Calibration chart" not in text


def test_live_report_scores_only_live_decisions(ledger):
    add_event(ledger, market_ids=("m1", "m2"))
    row(ledger, "m1", p=0.9, mode="paper")
    row(ledger, "m2", p=0.2, mode="live")
    ledger.settle_market("intl", "m1", 1.0, NOW)
    ledger.settle_market("intl", "m2", 1.0, NOW)
    assert report(ledger, mode="paper").brier.model == pytest.approx(0.01)
    assert report(ledger, mode="live").brier.model == pytest.approx(0.64)


def test_audit_counts_agreement_by_rule_family(ledger):
    ledger.add_audit("intl", "e1", "metar_routine", "KLGA", date(2026, 7, 1), 10.0, 10, "a", "a", True, NOW)
    ledger.add_audit("intl", "e2", "metar_routine", "KLGA", date(2026, 7, 2), 10.0, 10, "a", "b", False, NOW)
    ledger.add_audit("intl", "e3", "metar_routine", "KLGA", date(2026, 7, 3), None, None, None, "b", None, NOW)
    got = report(ledger).audit
    assert got == {"metar_routine": (2, 1, 1)}


def test_health_sums_skip_reasons_and_run_status(ledger):
    first = ledger.start_run("intl", "paper", NOW - 900)
    ledger.finish_run(first, NOW - 900, "ok", {}, {"stale_obs": 2})
    second = ledger.start_run("intl", "paper", NOW)
    ledger.finish_run(second, NOW, "error", {}, {"stale_obs": 1, "no_forecast": 3}, "boom")
    got = report(ledger).health
    assert (got.runs, got.failed, got.last_run) == (2, 1, NOW)
    assert got.skips == {"stale_obs": 3, "no_forecast": 3}


def test_health_counts_a_run_that_died_without_finishing(ledger):
    ledger.start_run("intl", "paper", NOW - 7200)
    assert report(ledger).health.failed == 1


def test_health_leaves_a_run_still_going_alone(ledger):
    ledger.start_run("intl", "paper", NOW - 60)
    assert report(ledger).health.failed == 0


def test_health_flags_finished_events_with_no_result(ledger):
    add_event(ledger, "stuck", date(2026, 7, 1), market_ids=("a",))
    add_event(ledger, "recent", date(2026, 7, 8), market_ids=("b",))
    add_event(ledger, "done", date(2026, 7, 1), market_ids=("c",))
    ledger.settle_market("intl", "c", 1.0, NOW)
    got = report(ledger)
    assert got.health.overdue == 1
    assert "1 finished events have no result after 3 days" in render_markdown(got)


def test_event_exactly_three_days_old_is_not_overdue_yet(ledger):
    add_event(ledger, "boundary", date(2026, 7, 7), market_ids=("a",))
    assert report(ledger).health.overdue == 0


def test_no_overdue_line_when_nothing_is_stuck(ledger):
    assert "finished events have no result" not in render_markdown(report(ledger))


def test_markdown_shows_the_numbers_that_decide_trust(ledger):
    add_event(ledger)
    row(ledger, p=0.8, bid=0.5, ask=0.7)
    trade(ledger, price=0.4, shares=10.0)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    text = render_markdown(report(ledger))
    assert "Realized P&L" in text
    assert "+$6.00" in text
    assert "Brier" in text
    assert "0.0400" in text


def test_markdown_says_so_when_nothing_has_settled(ledger):
    text = render_markdown(report(ledger))
    assert "No settled forecasts yet" in text


def test_write_report_saves_markdown_and_chart(ledger, tmp_path):
    add_event(ledger)
    row(ledger, p=0.8, bid=0.5, ask=0.7)
    ledger.settle_market("intl", "m1", 1.0, NOW)
    md, chart = write_report(report(ledger), tmp_path / "out")
    assert "Calibration" in md.read_text(encoding="utf8")
    assert chart.read_bytes()[:4] == b"\x89PNG"


def test_write_report_skips_chart_when_nothing_is_scored(ledger, tmp_path):
    md, chart = write_report(report(ledger), tmp_path / "out")
    assert md.exists()
    assert chart is None


def test_backtest_report_opens_with_what_it_cannot_show(ledger):
    text = render_markdown(report(ledger, mode="backtest"))
    assert text.index("Not a measure of the edge") < text.index("## P&L")
    assert "deterministic models" in text
    assert "depth is assumed" in text


def test_paper_report_has_no_backtest_caveat(ledger):
    assert "Not a measure of the edge" not in render_markdown(report(ledger, mode="paper"))

import math

import pytest

from weatherbot.core import Book, Bucket, Market
from weatherbot.fees import FeeSchedule
from weatherbot.strategy import Exposure, StrategyParams, evaluate, walk_asks

FREE = FeeSchedule(rate=0.0)
WEATHER = FeeSchedule(rate=0.05, rebate=0.25)
OPEN = Exposure(bankroll=1000.0, day_spent=0.0, event_spent=0.0, held_yes=0.0, held_no=0.0)


def market(fee=FREE, tick=0.01, min_size=5.0, tradable=True):
    return Market("m1", "10C", Bucket(10, 10), "tok", tick, min_size, fee, tradable)


def book(bids, asks):
    return Book.from_levels(bids, asks)


def params(**kw):
    defaults = dict(edge_threshold=0.05, kelly_fraction=0.15, max_trade_usd=25.0, max_day_usd=100.0,
                    max_event_usd=40.0, max_depth_share=0.15, execution="maker_first")
    return StrategyParams(**{**defaults, **kw})


def test_buy_yes_when_model_beats_ask():
    d = evaluate(market(), book([(0.40, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert (d.action, d.side) == ("buy", "yes")
    assert d.edge == pytest.approx(0.15)


def test_fee_can_kill_the_edge():
    # Fee at 0.44 is 0.05 * 0.44 * 0.56 = 0.01232, so edge is 0.0477.
    d = evaluate(market(WEATHER), book([(0.40, 100)], [(0.44, 100)]), 0.50, 0.50, OPEN, params())
    assert (d.action, d.reason) == ("skip", "edge_below_threshold")
    assert d.edge == pytest.approx(0.04768)


def test_same_prices_trade_with_no_fee():
    d = evaluate(market(FREE), book([(0.40, 100)], [(0.44, 100)]), 0.50, 0.50, OPEN, params())
    assert d.action == "buy"


def test_no_side_uses_mirrored_book():
    # NO asks at 1 - 0.70 = 0.30 and NO model is 0.50.
    d = evaluate(market(), book([(0.70, 100)], [(0.75, 100)]), 0.50, 0.50, OPEN, params())
    assert (d.action, d.side) == ("buy", "no")
    assert d.edge == pytest.approx(0.20)


def test_maker_quote_improves_bid_inside_spread():
    d = evaluate(market(), book([(0.40, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert (d.style, d.limit) == ("maker", 0.41)


def test_maker_quote_joins_bid_when_spread_is_one_tick():
    d = evaluate(market(), book([(0.40, 100)], [(0.41, 100)]), 0.60, 0.40, OPEN, params())
    assert d.limit == 0.40


def test_maker_quote_with_empty_bid_side_sits_one_tick_under_ask():
    d = evaluate(market(), book([], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert d.limit == 0.44


def test_maker_limit_respects_fine_ticks():
    d = evaluate(market(tick=0.001), book([(0.402, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert d.limit == 0.403


def test_maker_cost_excludes_fee():
    d = evaluate(market(WEATHER), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, OPEN, params())
    assert d.est_fee == 0


def test_taker_mode_crosses_and_pays_the_fee():
    d = evaluate(market(WEATHER), book([(0.40, 100)], [(0.50, 1000)]), 0.60, 0.40, OPEN,
                 params(execution="taker", max_depth_share=1.0, max_trade_usd=10.0))
    assert d.style == "taker"
    assert d.limit == 0.50
    # Cost per share is 0.50 + 0.0125, so $10 buys 19.51 shares.
    assert d.shares == pytest.approx(19.51)
    assert d.est_fee == pytest.approx(0.2439, abs=0.001)


def test_kelly_stake_scales_with_bankroll():
    # f* = (0.60 - 0.45) / 0.55 = 0.2727; 0.15 Kelly of $100 is $4.09; maker limit 0.41 buys 9.97 shares.
    small = Exposure(bankroll=100.0, day_spent=0, event_spent=0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, small,
                 params(max_depth_share=1.0))
    assert d.shares == pytest.approx(9.97, abs=0.02)


def test_trade_cap_limits_stake():
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, OPEN,
                 params(max_depth_share=1.0, max_trade_usd=10.0))
    assert d.shares * d.limit == pytest.approx(10.0, abs=0.01)


def test_day_cap_blocks_new_risk():
    spent = Exposure(bankroll=1000.0, day_spent=100.0, event_spent=0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent, params())
    assert (d.action, d.reason) == ("skip", "day_cap")


def test_day_cap_trims_stake():
    spent = Exposure(bankroll=1000.0, day_spent=95.0, event_spent=0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent,
                 params(max_depth_share=1.0))
    assert d.shares * d.limit == pytest.approx(5.0, abs=0.01)


def test_event_cap_blocks_correlated_buckets():
    spent = Exposure(bankroll=1000.0, day_spent=40.0, event_spent=40.0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent, params())
    assert (d.action, d.reason) == ("skip", "event_cap")


def test_day_room_under_one_minimum_order_is_a_cap_hit():
    # Floored share counts leave cents behind. Blame the cap, not the order size.
    spent = Exposure(bankroll=1000.0, day_spent=99.99, event_spent=0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent, params())
    assert (d.action, d.reason) == ("skip", "day_cap")


def test_event_room_under_one_minimum_order_is_a_cap_hit():
    spent = Exposure(bankroll=1000.0, day_spent=39.99, event_spent=39.99, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent, params())
    assert (d.action, d.reason) == ("skip", "event_cap")


def test_room_for_exactly_one_minimum_order_still_trades():
    # 5 shares at the 0.45 ask costs 2.25, and the maker limit of 0.41 costs less.
    spent = Exposure(bankroll=1000.0, day_spent=97.75, event_spent=0, held_yes=0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, spent, params())
    assert d.action == "buy"


def test_depth_share_caps_size_on_thin_books():
    # Both ask levels clear the threshold: 300 visible, 15% is 45 shares.
    d = evaluate(market(), book([(0.40, 100)], [(0.45, 100), (0.47, 200)]), 0.60, 0.40, OPEN, params())
    assert d.shares == 45.0


def test_depth_counts_only_levels_with_edge():
    # 0.56 misses the threshold, so only 100 shares of depth are usable.
    d = evaluate(market(), book([(0.40, 100)], [(0.45, 100), (0.56, 5000)]), 0.60, 0.40, OPEN, params())
    assert d.shares == 15.0


def test_below_minimum_order_is_skipped():
    d = evaluate(market(min_size=50), book([(0.40, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert (d.action, d.reason) == ("skip", "below_min_size")


def test_existing_position_counts_against_kelly_target():
    # Target is $40.91 at bankroll 1000. Holding $38 leaves room for $2.91.
    held = Exposure(bankroll=1000.0, day_spent=0, event_spent=0, held_yes=38.0, held_no=0)
    d = evaluate(market(min_size=1), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, held,
                 params(max_depth_share=1.0))
    assert d.shares * d.limit == pytest.approx(2.91, abs=0.02)


def test_full_position_skips():
    held = Exposure(bankroll=1000.0, day_spent=0, event_spent=0, held_yes=50.0, held_no=0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, held,
                 params(max_depth_share=1.0))
    assert (d.action, d.reason) == ("skip", "kelly_target_met")


def test_opposite_position_blocks_trade():
    held = Exposure(bankroll=1000.0, day_spent=0, event_spent=0, held_yes=0, held_no=10.0)
    d = evaluate(market(), book([(0.40, 1000)], [(0.45, 1000)]), 0.60, 0.40, held, params())
    assert (d.action, d.reason) == ("skip", "opposite_side_held")


def test_extreme_cheap_ask_is_skipped():
    d = evaluate(market(), book([], [(0.015, 1000)]), 0.30, 0.70, OPEN, params())
    assert (d.action, d.reason) == ("skip", "extreme_price")


def test_extreme_rich_ask_is_skipped():
    d = evaluate(market(), book([(0.97, 1000)], [(0.99, 1000)]), 1.0, 0.0, OPEN, params())
    assert (d.action, d.reason) == ("skip", "extreme_price")


def test_walk_stops_before_extreme_levels():
    d = evaluate(market(), book([(0.90, 10)], [(0.97, 10), (0.99, 10000)]), 1.0, 0.0, OPEN,
                 params(execution="taker", max_depth_share=1.0, edge_threshold=0.01))
    assert d.shares == 10.0
    assert d.limit == 0.97


def test_maker_quote_under_price_floor_is_skipped():
    # Ask 0.02 is allowed but the quote one tick below it is not.
    d = evaluate(market(), book([], [(0.02, 1000)]), 0.30, 0.70, OPEN, params())
    assert (d.action, d.reason) == ("skip", "extreme_price")


def test_empty_book_is_skipped():
    d = evaluate(market(), book([], []), 0.50, 0.50, OPEN, params())
    assert (d.action, d.reason) == ("skip", "no_ask")


def test_market_not_tradable_is_skipped():
    d = evaluate(market(tradable=False), book([(0.40, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params())
    assert (d.action, d.reason) == ("skip", "not_tradable")


def test_force_taker_overrides_maker_preference():
    d = evaluate(market(), book([(0.40, 100)], [(0.45, 100)]), 0.60, 0.40, OPEN, params(), force_taker=True)
    assert d.style == "taker"


def test_better_side_wins_when_only_one_clears():
    d = evaluate(market(), book([(0.48, 100)], [(0.52, 100)]), 0.60, 0.39, OPEN, params())
    assert d.side == "yes"


def test_skip_reports_the_better_side():
    d = evaluate(market(), book([(0.48, 100)], [(0.52, 100)]), 0.54, 0.45, OPEN, params())
    assert (d.action, d.side) == ("skip", "yes")
    assert d.edge == pytest.approx(0.02)


def test_walk_asks_fills_levels_in_order():
    fills = walk_asks(((0.45, 10), (0.50, 10), (0.55, 10)), 15.0)
    assert fills == [(0.45, 10), (0.50, 5.0)]


def test_walk_asks_stops_when_book_runs_out():
    assert walk_asks(((0.45, 10),), 15.0) == [(0.45, 10)]


def test_taker_is_sized_for_the_worst_price_it_can_pay():
    # A limit order can fill entirely at its limit if the cheaper level goes first, so $25 buys 50 at .50, not 52.
    d = evaluate(market(), book([(0.30, 100)], [(0.40, 10), (0.50, 1000)]), 0.90, 0.10, OPEN,
                 params(execution="taker", max_depth_share=1.0))
    assert (d.limit, d.shares) == (0.50, 50.0)


def test_taker_stays_on_the_cheap_level_when_reaching_further_buys_less():
    d = evaluate(market(), book([], [(0.40, 100), (0.90, 1000)]), 0.99, 0.01, OPEN,
                 params(execution="taker", max_depth_share=1.0))
    assert (d.limit, d.shares) == (0.40, 62.5)


def test_taker_sizing_counts_the_fee_at_the_worst_price():
    d = evaluate(market(WEATHER), book([], [(0.40, 10), (0.50, 1000)]), 0.90, 0.10, OPEN,
                 params(execution="taker", max_depth_share=1.0))
    assert d.limit == 0.50
    assert d.shares == 48.78  # 25 / (.50 + .05 * .25), rounded down


@pytest.mark.parametrize("yes_q, no_q", [(math.nan, 0.5), (0.5, math.nan), (math.inf, 0.5), (0.5, -math.inf)])
def test_impossible_probability_never_trades(yes_q, no_q):
    d = evaluate(market(), book([(0.30, 100)], [(0.40, 1000)]), yes_q, no_q, OPEN, params(execution="taker"))
    assert (d.action, d.reason) == ("skip", "bad_probability")

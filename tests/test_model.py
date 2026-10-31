from dataclasses import replace

import numpy as np
import pytest

from weatherbot.core import Bucket
from weatherbot.model import (
    DayInput,
    ModelParams,
    NoForecast,
    bucket_probabilities,
    mean_daily_high,
    obs_weight,
    price_day,
)

BASE = 1_800_000_000
HOUR = 3600
EXACT = ModelParams(
    spread_inflation=1.0,
    kernel_sigma_start_c=1e-6,
    kernel_sigma_end_c=1e-6,
    edge_shift_c=0.0,
    prob_epsilon=0.0,
)
C_BUCKETS = (Bucket(None, 9), Bucket(10, 10), Bucket(11, 11), Bucket(12, None))


def day_input(highs, now=BASE - HOUR, floor=None, latest=None, hour=12.0, hours=25, start=0,
              unverified=None, opening=None):
    """Members flat at their high, except a dip each morning."""
    times = np.array([BASE + (start + i) * HOUR for i in range(hours)])
    members = np.tile(np.array(highs, dtype=float).reshape(-1, 1), (1, hours))
    return DayInput(
        times=times,
        members=members,
        start=BASE,
        end=BASE + 24 * HOUR,
        now=now,
        floor_c=floor,
        latest_c=latest[0] if latest else None,
        latest_time=latest[1] if latest else None,
        local_hour=hour,
        unverified_c=unverified,
        opening_c=opening,
    )


@pytest.mark.parametrize("hour, want", [(0, 0), (6, 0), (11.5, 0.5), (17, 1), (23, 1)])
def test_obs_weight_ramps_through_the_day(hour, want):
    assert obs_weight(hour, ModelParams()) == want


def test_bucket_probability_is_share_of_members():
    dist = price_day(day_input([10.2, 10.4, 11.2, 12.2]), EXACT)
    p = bucket_probabilities(dist, C_BUCKETS, "C").p
    assert p == pytest.approx([0, 0.5, 0.25, 0.25])


def test_open_ended_buckets_take_the_tails():
    dist = price_day(day_input([3.0, 30.0]), EXACT)
    p = bucket_probabilities(dist, C_BUCKETS, "C").p
    assert p == pytest.approx([0.5, 0, 0, 0.5])


def test_probabilities_sum_to_one_with_kernel_and_inflation():
    dist = price_day(day_input([9.5, 10.5, 11.5]), ModelParams())
    assert bucket_probabilities(dist, C_BUCKETS, "C").p.sum() == pytest.approx(1)


def test_only_hours_inside_local_day_count():
    inp = day_input([10.0])
    inp.members[0, 0] = 10.0
    outside = DayInput(
        times=np.concatenate(([BASE - HOUR], inp.times, [BASE + 25 * HOUR])),
        members=np.hstack(([[40.0]], inp.members, [[40.0]])),
        start=inp.start, end=inp.end, now=inp.now, floor_c=None,
        latest_c=None, latest_time=None, local_hour=12,
    )
    assert price_day(outside, EXACT).highs == pytest.approx([10.0])


def test_end_of_window_is_exclusive():
    # BASE+24h is the next local midnight, which belongs to the next day.
    inp = day_input([10.0])
    inp.members[0, 24] = 40.0
    assert price_day(inp, EXACT).highs == pytest.approx([10.0])


def test_spread_inflates_about_the_mean_before_any_obs_weight():
    params = ModelParams(spread_inflation=1.5)
    dist = price_day(day_input([10.0, 12.0]), params)
    assert dist.highs == pytest.approx([9.5, 12.5])


def test_inflation_fades_once_observations_dominate():
    params = ModelParams(spread_inflation=1.5)
    now = BASE + 18 * HOUR
    dist = price_day(day_input([10.0, 12.0], now=now, hour=18.0), params)
    assert dist.highs == pytest.approx([10.0, 12.0])


def test_kernel_narrows_through_the_day():
    params = ModelParams(kernel_sigma_start_c=0.8, kernel_sigma_end_c=0.2)
    assert price_day(day_input([10.0]), params).sigma_c == pytest.approx(0.8)
    now = BASE + 18 * HOUR
    assert price_day(day_input([10.0], now=now, hour=18.0), params).sigma_c == pytest.approx(0.2)


def test_observed_floor_removes_lower_buckets():
    now = BASE + 12 * HOUR
    dist = price_day(day_input([9.0, 10.0, 11.0, 12.0], now=now, floor=11.2), EXACT)
    p = bucket_probabilities(dist, C_BUCKETS, "C").p
    # 9 and 10 are clamped up into the bucket holding the floor.
    assert p == pytest.approx([0, 0, 0.75, 0.25])


def test_floor_on_half_degree_counts_in_upper_bucket():
    # 16.5 rounds half-up to 17, so the 16 bucket is impossible.
    now = BASE + 12 * HOUR
    dist = price_day(day_input([15.0], now=now, floor=16.5), EXACT)
    p = bucket_probabilities(dist, (Bucket(None, 16), Bucket(17, None)), "C").p
    assert p == pytest.approx([0, 1])


def test_fahrenheit_edges_use_market_rounding():
    # 18.3C is 64.94F, which counts as 65 and sits in the 64-65 bucket.
    now = BASE + 12 * HOUR
    buckets = (Bucket(None, 63), Bucket(64, 65), Bucket(66, None))
    dist = price_day(day_input([10.0], now=now, floor=18.3), EXACT)
    assert bucket_probabilities(dist, buckets, "F").p == pytest.approx([0, 1, 0])


def test_fahrenheit_kernel_is_scaled():
    # One member on the 68F edge (20C): half the mass lands each side in either unit.
    buckets = (Bucket(None, 67), Bucket(68, None))
    params = ModelParams(spread_inflation=1.0, edge_shift_c=0.0, prob_epsilon=0.0)
    inp = day_input([19.72222222])  # 67.5F exactly on the edge
    p = bucket_probabilities(price_day(inp, params), buckets, "F").p
    assert p == pytest.approx([0.5, 0.5], abs=1e-4)


LAST_HOUR = BASE + 23 * HOUR + 40 * 60  # the next forecast step is past the end of the day


def test_last_hour_ignores_what_the_forecast_said():
    dist = price_day(day_input([30.0], now=LAST_HOUR, floor=11.3, latest=(9.0, LAST_HOUR), hours=24), EXACT)
    assert bucket_probabilities(dist, C_BUCKETS, "C").p == pytest.approx([0, 0, 1, 0])


def test_last_hour_is_settled_once_temperature_is_well_below_the_high():
    inp = day_input([30.0], now=LAST_HOUR, floor=11.3, latest=(9.0, LAST_HOUR), hours=24)
    probs = bucket_probabilities(price_day(inp, ModelParams()), C_BUCKETS, "C")
    assert probs.p == pytest.approx([0.0025, 0.0025, 0.9925, 0.0025])


def test_last_hour_stays_uncertain_while_temperature_sits_at_the_high():
    # A new high is still possible, so one last report can move the winning bucket.
    inp = day_input([30.0], now=LAST_HOUR, floor=11.3, latest=(11.3, LAST_HOUR), hours=24)
    probs = bucket_probabilities(price_day(inp, ModelParams()), C_BUCKETS, "C")
    assert probs.p[2] == pytest.approx(0.783, abs=0.001)
    assert probs.lo[2] == pytest.approx(0.419, abs=0.001)


def test_last_hour_without_a_current_reading_centres_on_the_high():
    inp = day_input([30.0], now=LAST_HOUR, floor=11.3, hours=24)
    assert price_day(inp, ModelParams()).highs == pytest.approx([11.3])


def test_last_hour_without_observations_is_unpriceable():
    with pytest.raises(NoForecast, match="no_observations"):
        price_day(day_input([30.0], now=LAST_HOUR, hours=24), ModelParams())


GAP = replace(EXACT, sensor_gap_f=(0.5, 0.3, 0.2))
F_BUCKETS = (Bucket(None, 49), Bucket(50, 50), Bucket(51, 51), Bucket(52, None))  # 10C is 50F
MIDDAY = BASE + 12 * HOUR


def test_sensor_can_have_peaked_above_the_highest_report():
    inp = day_input([5.0], now=MIDDAY, floor=10.0, unverified=10.0)
    assert bucket_probabilities(price_day(inp, GAP), F_BUCKETS, "F").p == pytest.approx([0, 0.5, 0.3, 0.2])


def test_rest_of_the_day_can_still_beat_the_sensor_gap():
    # 51F is still to come, so only the gap's top step can land above it.
    inp = day_input([10.5555556], now=MIDDAY, floor=10.0, unverified=10.0)
    assert bucket_probabilities(price_day(inp, GAP), F_BUCKETS, "F").p == pytest.approx([0, 0, 0.8, 0.2])


def test_sensor_maximum_leaves_no_gap():
    inp = day_input([5.0], now=MIDDAY, floor=10.0)
    assert bucket_probabilities(price_day(inp, GAP), F_BUCKETS, "F").p == pytest.approx([0, 1, 0, 0])


def test_lower_unverified_reading_cannot_pull_the_high_down():
    # 49.1F plus the gap's steps: 49.1, 50.1 and 51.1, with the sensor's 50F under all of them.
    inp = day_input([5.0], now=MIDDAY, floor=10.0, unverified=9.5)
    assert bucket_probabilities(price_day(inp, GAP), F_BUCKETS, "F").p == pytest.approx([0, 0.8, 0.2, 0])


def test_last_hour_keeps_the_sensor_gap():
    inp = day_input([30.0], now=LAST_HOUR, floor=10.0, latest=(5.0, LAST_HOUR), hours=24, unverified=10.0)
    assert bucket_probabilities(price_day(inp, GAP), F_BUCKETS, "F").p == pytest.approx([0, 0.5, 0.3, 0.2])


def test_opening_temperature_holds_up_members_below_it():
    inp = day_input([9.0, 12.2], now=BASE + HOUR, opening=10.0)
    assert bucket_probabilities(price_day(inp, EXACT), C_BUCKETS, "C").p == pytest.approx([0, 0.5, 0, 0.5])


def test_opening_temperature_counts_in_the_last_hour():
    inp = day_input([30.0], now=LAST_HOUR, floor=9.3, latest=(9.0, LAST_HOUR), hours=24, opening=11.0)
    assert bucket_probabilities(price_day(inp, EXACT), C_BUCKETS, "C").p == pytest.approx([0, 0, 1, 0])


def test_last_hour_keeps_a_reading_above_the_opening():
    inp = day_input([30.0], now=LAST_HOUR, floor=12.2, latest=(12.2, LAST_HOUR), hours=24, opening=10.0)
    assert bucket_probabilities(price_day(inp, EXACT), C_BUCKETS, "C").p == pytest.approx([0, 0, 0, 1])


def test_opening_temperature_keeps_its_own_narrow_kernel():
    # KNYC's cold front, early in the day. How unsure the forecast is says nothing about a temperature already past.
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=1.5, kernel_sigma_end_c=0.25,
                         edge_shift_c=0.0, prob_epsilon=0.0)
    inp = day_input([15.5], now=BASE + HOUR, hour=1.0, opening=16.78)
    p = bucket_probabilities(price_day(inp, params), (Bucket(None, 61), Bucket(62, 63), Bucket(64, None)), "F").p
    assert p[1] == pytest.approx(0.864, abs=0.002)


def test_opening_temperature_is_an_estimate_so_the_band_moves_it():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.01, kernel_sigma_end_c=0.01,
                         edge_shift_c=0.25, prob_epsilon=0.0)
    probs = bucket_probabilities(price_day(day_input([5.0], now=BASE + HOUR, opening=10.45), params), C_BUCKETS, "C")
    assert probs.lo[1] == pytest.approx(0, abs=1e-6)
    assert probs.hi[1] == pytest.approx(1, abs=1e-6)


def test_anchor_carries_observed_residual_into_next_hours():
    now = BASE + 12 * HOUR + 1800
    inp = day_input([20.0], now=now, latest=(22.0, now), hour=17.0)
    dist = price_day(inp, EXACT)
    # Next hour is 0.5h ahead: 20 + 2 * exp(-0.5/6)
    assert dist.highs == pytest.approx([21.84], abs=0.005)


def test_anchor_has_no_effect_before_observations_get_weight():
    now = BASE + 2 * HOUR + 1800
    inp = day_input([20.0], now=now, latest=(22.0, now), hour=5.0)
    assert price_day(inp, EXACT).highs == pytest.approx([20.0])


def test_anchor_is_scaled_by_observation_weight():
    now = BASE + 8 * HOUR + 1800
    inp = day_input([20.0], now=now, latest=(22.0, now), hour=11.5)
    # Half weight at 11:30: 20 + 0.5 * 2 * exp(-0.5/6)
    assert price_day(inp, EXACT).highs == pytest.approx([20.92], abs=0.005)


def test_anchor_ignored_for_future_days():
    inp = day_input([20.0], latest=(25.0, BASE - HOUR), hour=23.0)
    assert price_day(inp, EXACT).highs == pytest.approx([20.0])


def test_past_hours_of_today_are_not_counted_from_forecast():
    # The forecast peaked this morning. The floor says it never happened.
    now = BASE + 12 * HOUR
    inp = day_input([15.0], now=now, floor=13.0)
    inp.members[0, :6] = 30.0
    assert price_day(inp, EXACT).highs == pytest.approx([15.0])


def test_member_with_no_data_is_dropped():
    inp = day_input([10.0, 12.0])
    inp.members[1, :] = np.nan
    assert price_day(inp, EXACT).highs == pytest.approx([10.0])


def test_no_forecast_at_all_is_refused():
    inp = day_input([10.0])
    inp.members[:, :] = np.nan
    with pytest.raises(NoForecast):
        price_day(inp, EXACT)


def test_partial_forecast_horizon_is_refused():
    # Data stops at local noon, so the afternoon peak is unknown.
    with pytest.raises(NoForecast):
        price_day(day_input([10.0], hours=12), EXACT)


def test_band_widens_for_member_near_bucket_edge():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.01,
                         kernel_sigma_end_c=0.01, edge_shift_c=0.25, prob_epsilon=0.0)
    probs = bucket_probabilities(price_day(day_input([10.45]), params), C_BUCKETS, "C")
    assert probs.lo[1] == pytest.approx(0, abs=1e-6)
    assert probs.hi[1] == pytest.approx(1, abs=1e-6)


def test_band_is_tight_away_from_edges():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.01,
                         kernel_sigma_end_c=0.01, edge_shift_c=0.25, prob_epsilon=0.0)
    probs = bucket_probabilities(price_day(day_input([10.0]), params), C_BUCKETS, "C")
    assert probs.lo[1] == pytest.approx(1)
    assert probs.hi[1] == pytest.approx(1)


def test_band_does_not_move_the_observed_floor():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.01,
                         kernel_sigma_end_c=0.01, edge_shift_c=0.25, prob_epsilon=0.0)
    now = BASE + 12 * HOUR
    dist = price_day(day_input([9.0], now=now, floor=10.4), params)
    probs = bucket_probabilities(dist, C_BUCKETS, "C")
    assert probs.lo[1] == pytest.approx(1)


def test_yes_and_no_quantities_are_conservative():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=0.01,
                         kernel_sigma_end_c=0.01, edge_shift_c=0.25, prob_epsilon=0.0)
    probs = bucket_probabilities(price_day(day_input([10.45]), params), C_BUCKETS, "C")
    # Near an edge neither side may claim certainty.
    assert probs.yes_q[1] == pytest.approx(0, abs=1e-6)
    assert probs.no_q[1] == pytest.approx(0, abs=1e-6)


def test_epsilon_keeps_impossible_buckets_above_zero():
    params = ModelParams(spread_inflation=1.0, kernel_sigma_start_c=1e-6,
                         kernel_sigma_end_c=1e-6, edge_shift_c=0.0, prob_epsilon=0.04)
    p = bucket_probabilities(price_day(day_input([10.0]), params), C_BUCKETS, "C").p
    assert p == pytest.approx([0.01, 0.97, 0.01, 0.01])


def test_mean_daily_high_averages_member_maxima():
    times = np.arange(0, 24) * 3600
    members = np.array([[5.0] * 23 + [9.0], [7.0] * 24])
    assert mean_daily_high(times, members, 0, 86400, ModelParams()) == pytest.approx(8.0)


def test_mean_daily_high_ignores_dead_members():
    times = np.arange(0, 24) * 3600
    members = np.array([[np.nan] * 24, [7.0] * 24])
    assert mean_daily_high(times, members, 0, 86400, ModelParams()) == pytest.approx(7.0)


def test_mean_daily_high_needs_most_of_the_day():
    times = np.arange(0, 12) * 3600
    assert mean_daily_high(times, np.full((2, 12), 7.0), 0, 86400, ModelParams()) is None


def test_mean_daily_high_ignores_hours_outside_the_day():
    times = np.arange(0, 48) * 3600
    members = np.array([[5.0] * 24 + [30.0] * 24])
    assert mean_daily_high(times, members, 0, 86400, ModelParams()) == pytest.approx(5.0)

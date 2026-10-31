"""Turn ensemble members and observations into bucket probabilities.

All weather maths is in Celsius. Only the final bucket comparison happens in
the market's unit, so rounding matches the settlement rule exactly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .core import Bucket
from .timeutil import c_to_unit

_Z = np.linspace(-8, 8, 3201)
_PHI = np.array([0.5 * (1 + math.erf(z / math.sqrt(2))) for z in _Z])


class NoForecast(Exception):
    """Not enough data to price this day. Message is a short reason code."""


@dataclass(frozen=True)
class ModelParams:
    spread_inflation: float = 1.3
    kernel_sigma_start_c: float = 0.7
    kernel_sigma_end_c: float = 0.25
    obs_start_hour: float = 6.0
    obs_full_hour: float = 17.0
    anchor_tau_hours: float = 6.0
    edge_shift_c: float = 0.25
    prob_epsilon: float = 0.01
    min_horizon_coverage: float = 0.75
    sensor_gap_f: tuple[float, ...] = (0.46, 0.41, 0.08, 0.03, 0.02)


@dataclass(frozen=True)
class DayInput:
    times: np.ndarray  # unix seconds, shape (T,)
    members: np.ndarray  # Celsius, shape (members, T), may hold NaN
    start: int  # local day window [start, end)
    end: int
    now: int
    floor_c: float | None  # highest qualifying reading so far
    latest_c: float | None  # newest reading of any kind, for anchoring
    latest_time: int | None
    local_hour: float
    unverified_c: float | None = None
    opening_c: float | None = None


@dataclass(frozen=True)
class HighDist:
    """Distribution of the final daily high."""

    highs: np.ndarray
    sigma_c: float
    floors: tuple[tuple[float, float], ...]  # (high so far, its chance), empty before any reading
    shift_c: float
    epsilon: float
    opening_c: float | None = None
    opening_sigma_c: float = 1.0


@dataclass(frozen=True)
class Probabilities:
    p: np.ndarray
    lo: np.ndarray
    hi: np.ndarray

    @property
    def yes_q(self) -> np.ndarray:
        return self.lo

    @property
    def no_q(self) -> np.ndarray:
        return 1 - self.hi


def obs_weight(hour: float, params: ModelParams) -> float:
    span = params.obs_full_hour - params.obs_start_hour
    return max(0.0, min(1.0, (hour - params.obs_start_hour) / span))


def price_day(inp: DayInput, params: ModelParams) -> HighDist:
    in_progress = inp.now >= inp.start
    from_time = max(inp.now, inp.start)
    mask = (inp.times >= from_time) & (inp.times < inp.end)
    if in_progress and not mask.any():
        return _last_hour(inp, params)

    hours_left = (inp.end - from_time) / 3600
    future = inp.members[:, mask]
    has_data = ~np.isnan(future).all(axis=0)
    if has_data.sum() < int(hours_left * params.min_horizon_coverage) or not has_data.any():
        raise NoForecast("forecast_horizon")

    keep = ~np.isnan(future).all(axis=1)
    path = future[keep]
    w = obs_weight(inp.local_hour, params) if in_progress else 0.0

    if w > 0 and inp.latest_c is not None:
        path = path + _anchor(inp, keep, inp.times[mask], w, params)

    highs = np.nanmax(path, axis=1)
    alpha = 1 + (params.spread_inflation - 1) * (1 - w)
    mean = highs.mean()
    highs = mean + alpha * (highs - mean)
    sigma = params.kernel_sigma_start_c + (params.kernel_sigma_end_c - params.kernel_sigma_start_c) * w
    return _dist(inp, params, highs, sigma)


def _last_hour(inp: DayInput, params: ModelParams) -> HighDist:
    """No forecast step left, so only the temperature right now can still beat the high."""
    if inp.floor_c is None:
        raise NoForecast("no_observations")
    now_c = inp.floor_c if inp.latest_c is None else inp.latest_c
    return _dist(inp, params, np.array([now_c]), params.kernel_sigma_end_c)


def _dist(inp: DayInput, params: ModelParams, highs: np.ndarray, sigma_c: float) -> HighDist:
    # The opening has already happened, so the forecast's uncertainty doesn't apply to it.
    return HighDist(highs, sigma_c, _floors(inp, params), params.edge_shift_c, params.prob_epsilon,
                    inp.opening_c, params.kernel_sigma_end_c)


def _floors(inp: DayInput, params: ModelParams) -> tuple[tuple[float, float], ...]:
    if inp.floor_c is None:
        return ()
    if inp.unverified_c is None:
        return ((inp.floor_c, 1.0),)
    # The sensor's maximum can sit above the highest report, by whole degrees F.
    return tuple((max(inp.floor_c, inp.unverified_c + step / 1.8), odds) for step, odds in enumerate(params.sensor_gap_f))


def _anchor(inp: DayInput, keep: np.ndarray, times: np.ndarray, w: float, params: ModelParams) -> np.ndarray:
    """Carry each member's current error forward, decaying with lead time."""
    i = int(np.argmin(np.abs(inp.times - inp.latest_time)))
    residual = inp.latest_c - inp.members[keep, i]
    residual = np.nan_to_num(residual)
    lead = (times - inp.latest_time) / 3600
    return w * residual[:, None] * np.exp(-lead / params.anchor_tau_hours)


def mean_daily_high(
    times: np.ndarray, members: np.ndarray, start: int, end: int, params: ModelParams
) -> float | None:
    """Ensemble mean of the raw daily high: the baseline station bias is measured against."""
    day = members[:, (times >= start) & (times < end)]
    has_data = ~np.isnan(day).all(axis=0)
    if has_data.sum() < int((end - start) / 3600 * params.min_horizon_coverage) or not has_data.any():
        return None
    return float(np.nanmax(day[~np.isnan(day).all(axis=1)], axis=1).mean())


def bucket_probabilities(dist: HighDist, buckets: Sequence[Bucket], unit: str) -> Probabilities:
    base = _masses(dist, buckets, unit, 0.0)
    up = _masses(dist, buckets, unit, dist.shift_c)
    down = _masses(dist, buckets, unit, -dist.shift_c)
    lo = np.minimum.reduce([base, up, down])
    hi = np.maximum.reduce([base, up, down])
    uniform = 1 / len(buckets)
    shrink = lambda x: (1 - dist.epsilon) * x + dist.epsilon * uniform
    return Probabilities(shrink(base), shrink(lo), shrink(hi))


def _masses(dist: HighDist, buckets: Sequence[Bucket], unit: str, shift_c: float) -> np.ndarray:
    scale = 1.8 if unit == "F" else 1.0
    floors = [(round(c_to_unit(floor, unit), 6), odds) for floor, odds in dist.floors]
    highs = c_to_unit(dist.highs + shift_c, unit)
    sigma = dist.sigma_c * scale
    opening = None if dist.opening_c is None else c_to_unit(dist.opening_c + shift_c, unit)

    def cdf(x: float) -> float:
        # P(final high < x). Half-up rounding puts a reading on the edge in the upper bucket.
        if math.isinf(x):
            return 1.0 if x > 0 else 0.0
        cleared = sum(odds for floor, odds in floors if x > floor) if floors else 1.0
        if opening is not None:
            cleared *= float(np.interp((x - opening) / (dist.opening_sigma_c * scale), _Z, _PHI))
        return cleared * float(np.interp((x - highs) / sigma, _Z, _PHI).mean())

    return np.array([cdf(b.hi_edge) - cdf(b.lo_edge) for b in buckets])

"""Decide whether a market is worth trading, on which side, how, and how much."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .core import Book, Level, Market


@dataclass(frozen=True)
class StrategyParams:
    edge_threshold: float = 0.05
    kelly_fraction: float = 0.15
    min_price: float = 0.02
    max_price: float = 0.98
    max_trade_usd: float = 25.0
    max_day_usd: float = 100.0
    max_event_usd: float = 40.0
    max_depth_share: float = 0.15
    execution: str = "maker_first"  # maker_first | taker


@dataclass(frozen=True)
class Exposure:
    """Risk already committed. Open orders count as if filled."""

    bankroll: float
    day_spent: float
    event_spent: float
    held_yes: float
    held_no: float


@dataclass(frozen=True)
class Decision:
    action: str  # buy | skip
    reason: str
    side: str | None = None
    edge: float | None = None
    q: float | None = None
    style: str | None = None  # maker | taker
    limit: float | None = None
    shares: float = 0.0
    est_cost: float = 0.0
    est_fee: float = 0.0
    fills: tuple[tuple[float, float], ...] = field(default=())


@dataclass(frozen=True)
class _Signal:
    side: str
    q: float
    book: Book
    edge: float | None
    reason: str  # empty when the signal is good


def evaluate(
    market: Market,
    book: Book,
    yes_q: float,
    no_q: float,
    exposure: Exposure,
    params: StrategyParams,
    force_taker: bool = False,
) -> Decision:
    if not market.tradable:
        return Decision("skip", "not_tradable")
    if not (math.isfinite(yes_q) and math.isfinite(no_q)):
        return Decision("skip", "bad_probability")

    signals = (
        _signal(market, book, yes_q, "yes", params),
        _signal(market, book.mirrored(), no_q, "no", params),
    )
    best = max(signals, key=lambda s: (not s.reason, s.edge if s.edge is not None else -math.inf))
    skip = lambda reason: Decision("skip", reason, best.side, best.edge, best.q)
    if best.reason:
        return skip(best.reason)

    held = exposure.held_yes if best.side == "yes" else exposure.held_no
    opposite = exposure.held_no if best.side == "yes" else exposure.held_yes
    if opposite > 0:
        return skip("opposite_side_held")

    ask = best.book.asks[0].price
    smallest_order = market.min_size * ask
    day_room = params.max_day_usd - exposure.day_spent
    if day_room < smallest_order:
        return skip("day_cap")
    event_room = params.max_event_usd - exposure.event_spent
    if event_room < smallest_order:
        return skip("event_cap")

    cost = ask + market.fee.taker_per_share(ask)
    target = params.kelly_fraction * (best.q - cost) / (1 - cost) * exposure.bankroll
    kelly_room = target - held
    if kelly_room <= 0:
        return skip("kelly_target_met")

    budget = min(kelly_room, params.max_trade_usd, day_room, event_room)
    levels = _usable_levels(market, best, params)
    max_shares = params.max_depth_share * sum(x.size for x in levels)

    style = "taker" if force_taker or params.execution == "taker" else "maker"
    if style == "maker":
        limit = _maker_limit(market, best.book)
        if not params.min_price <= limit <= params.max_price:
            return skip("extreme_price")
        shares = _floor_shares(min(budget / limit, max_shares))
        if shares < market.min_size:
            return skip("below_min_size")
        return Decision("buy", "signal", best.side, best.edge, best.q, style, limit, shares, shares * limit)

    shares = _taker_shares(market, levels, budget, max_shares)
    if shares < market.min_size:
        return skip("below_min_size")
    fills = walk_asks(levels, shares)
    return Decision(
        "buy",
        "signal",
        best.side,
        best.edge,
        best.q,
        style,
        fills[-1][0],
        shares,
        sum(p * s for p, s in fills),
        sum(s * market.fee.taker_per_share(p) for p, s in fills),
        tuple(fills),
    )


def _signal(market: Market, book: Book, q: float, side: str, params: StrategyParams) -> _Signal:
    ask = book.best_ask
    if ask is None:
        return _Signal(side, q, book, None, "no_ask")
    edge = q - ask - market.fee.taker_per_share(ask)
    if not params.min_price <= ask <= params.max_price:
        return _Signal(side, q, book, edge, "extreme_price")
    if edge < params.edge_threshold:
        return _Signal(side, q, book, edge, "edge_below_threshold")
    return _Signal(side, q, book, edge, "")


def _usable_levels(market: Market, signal: _Signal, params: StrategyParams) -> list[Level]:
    usable = []
    for level in signal.book.asks:
        edge = signal.q - level.price - market.fee.taker_per_share(level.price)
        if level.price > params.max_price or edge < params.edge_threshold:
            break
        usable.append(level)
    return usable


def _maker_limit(market: Market, book: Book) -> float:
    below_ask = book.best_ask - market.tick
    limit = below_ask if book.best_bid is None else min(below_ask, book.best_bid + market.tick)
    return _floor_tick(limit, market.tick)


def _floor_tick(price: float, tick: float) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 6)


def _floor_shares(shares: float) -> float:
    return math.floor(round(shares * 100, 6)) / 100


def _taker_shares(market: Market, levels: list[Level], budget: float, max_shares: float) -> float:
    """As many shares as the budget covers at the worst price paid. The limit is the last level the order
    reaches, and the cheaper levels before it can be gone by the time the order lands."""
    best = depth = 0.0
    for price, size in levels:
        depth += size
        unit_cost = price + market.fee.taker_per_share(price)
        best = max(best, min(depth, max_shares, budget / unit_cost))
    return _floor_shares(best)


def walk_asks(levels, shares: float) -> list[tuple[float, float]]:
    """Fill a buy against best-first (price, size) levels."""
    fills = []
    left = shares
    for price, size in levels:
        if left <= 0:
            break
        take = min(size, left)
        fills.append((price, take))
        left -= take
    return fills

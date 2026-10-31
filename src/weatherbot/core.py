"""Domain types shared across the bot."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from .fees import FeeSchedule


@dataclass(frozen=True)
class Rule:
    """How a market turns observations into a settled whole-degree value."""

    family: str  # metar_routine | noaa_timeseries | wunderground | nws_cli
    station: str  # ICAO
    unit: str  # "F" | "C"
    day_clock: str  # local_dst | local_standard
    obs_policy: str  # routine | all | nws_cli


@dataclass(frozen=True)
class Bucket:
    """Inclusive whole-degree range. None means open-ended on that side."""

    lo: int | None
    hi: int | None

    @property
    def lo_edge(self) -> float:
        return -math.inf if self.lo is None else self.lo - 0.5

    @property
    def hi_edge(self) -> float:
        return math.inf if self.hi is None else self.hi + 0.5

    def contains(self, degree: int) -> bool:
        return (self.lo is None or degree >= self.lo) and (self.hi is None or degree <= self.hi)


@dataclass(frozen=True)
class Market:
    market_id: str  # conditionId on intl, market slug on us
    label: str
    bucket: Bucket
    token: str  # YES token id on intl, market slug on us
    tick: float
    min_size: float
    fee: FeeSchedule
    tradable: bool = True


@dataclass(frozen=True)
class Event:
    venue: str
    event_id: str
    slug: str
    title: str
    day: date
    rule: Rule
    markets: tuple[Market, ...]


class Level(NamedTuple):
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    """Order book for the YES outcome. Levels are best-first on both sides."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @classmethod
    def from_levels(cls, bids, asks) -> "Book":
        """Build a book from (price, size) pairs in any order, dropping empty levels."""
        bid = sorted((Level(p, s) for p, s in bids if s > 0), key=lambda x: -x.price)
        ask = sorted((Level(p, s) for p, s in asks if s > 0), key=lambda x: x.price)
        return cls(tuple(bid), tuple(ask))

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def mirrored(self) -> "Book":
        """The same book seen from the NO outcome."""
        return Book(
            tuple(Level(round(1 - x.price, 6), x.size) for x in self.asks),
            tuple(Level(round(1 - x.price, 6), x.size) for x in self.bids),
        )

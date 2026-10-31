"""Taker fee schedules. Rates vary by market, so they are read per market."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    rate: float
    exponent: float = 1.0
    rebate: float = 0.0
    source: str = "market"

    def taker_per_share(self, price: float) -> float:
        return self.rate * (price * (1 - price)) ** self.exponent


# Published weather rate, used when a market carries no schedule of its own.
FALLBACK = FeeSchedule(rate=0.05, exponent=1.0, rebate=0.25, source="fallback")

"""Turn buy decisions into orders. Paper mode simulates fills against the visible book."""
from __future__ import annotations

from .core import Book, Event, Market
from .ledger import Ledger, OrderRow
from .strategy import Decision


class PaperExecutor:
    def __init__(self, ledger: Ledger, venue: str, mode: str = "paper"):
        self._ledger = ledger
        self._venue = venue
        self.mode = mode

    def check_stops(self, now: int) -> None:
        """Paper has no stops."""

    def sync(self, books: dict[str, Book], now: int) -> int:
        """Fill resting orders the market has reached. Returns how many got a fill.

        A book snapshot can't show trades that came and went between cycles, so
        this misses some fills. It only fills once the ask has come down to our
        limit, which is also how adverse selection shows up for a real maker.
        """
        filled = 0
        for order in self._ledger.open_orders(self._venue, self.mode):
            book = books.get(order.market_id)
            if book is None:
                continue
            view = book if order.side == "yes" else book.mirrored()
            available = sum(level.size for level in view.asks if level.price <= order.limit_px)
            shares = min(order.remaining, available)
            if shares <= 0:
                continue
            self._ledger.add_fill(order.id, now, order.limit_px, shares, 0.0)
            filled += 1
            if shares >= order.remaining:
                self._ledger.close_order(order.id, "filled", now)
        return filled

    def submit(self, decision_id: int, event: Event, market: Market, decision: Decision, now: int) -> int:
        order = self._ledger.add_order(
            decision_id, self._venue, self.mode, event.event_id, market.market_id, decision.side,
            decision.style, decision.limit, decision.shares, now,
        )
        if decision.style == "taker":
            for price, shares in decision.fills:
                self._ledger.add_fill(order, now, price, shares, shares * market.fee.taker_per_share(price))
            self._ledger.close_order(order, "filled", now)
        return order

    def cancel(self, order: OrderRow, reason: str, now: int) -> None:
        self._ledger.close_order(order.id, "cancelled", now, reason)

"""Polymarket US public market data, served by the retail gateway."""
from __future__ import annotations

import math

from ..core import Book, Market
from ..http import Http, HttpError
from ..rules import parse_us_event
from . import parse_all

GATEWAY = "https://gateway.polymarket.us"
_PAGE = 100
_MAX_PAGES = 100  # far past any real listing, so a gateway repeating itself can't hold the run forever
_HISTORY_MINUTES = 15
_MAX_BOOK_FAILURES = 3


class UsVenue:
    name = "us"

    def __init__(self, http: Http):
        self._http = http

    def _pages(self, **filters: str) -> list[dict]:
        url = f"{GATEWAY}/v1/events"
        raw: list[dict] = []
        for offset in range(0, _PAGE * _MAX_PAGES, _PAGE):
            page = self._http.get(
                url,
                {"limit": _PAGE, "offset": offset, "categories": "climate", **filters},
            ).get("events") or []
            raw.extend(page)
            if len(page) < _PAGE:
                return raw
        raise HttpError(200, url, "listing never ended")

    def _raw_open_events(self) -> list[dict]:
        return self._pages(active="true", closed="false")

    def open_events(self, now: int):
        return parse_all(self._raw_open_events(), parse_us_event)

    def closed_events(self, since: int):
        """Every closed event. The gateway ignores sort order, so `since` can't end the walk early."""
        return parse_all(self._pages(closed="true"), parse_us_event)

    def quote_history(self, market: Market, start: int, end: int) -> list[tuple[int, float, float]]:
        """(time, best bid, best ask). longPrice is the ask and shortPrice is one minus the bid, not trades."""
        raw = self._http.get(
            f"{GATEWAY}/v1/price-history",
            {
                "symbol": market.token,
                "fidelity": _HISTORY_MINUTES,
                "timestamp.startTimestamp": start,
                "timestamp.endTimestamp": end,
            },
        )
        return [
            (int(x["timestamp"]), round(1 - float(x["shortPrice"]), 6), float(x["longPrice"]))
            for x in raw.get("history") or []
        ]

    def books(self, markets: list[Market]) -> dict[str, Book]:
        books = {}
        failures = 0
        for market in markets:
            try:
                raw = self._http.get(f"{GATEWAY}/v1/markets/{market.token}/book")
            except HttpError as exc:
                if exc.status == 404:
                    continue
                failures += 1
                if failures == _MAX_BOOK_FAILURES:
                    raise  # an outage, and every market left would burn its own retries finding that out
                continue
            failures = 0
            data = raw.get("marketData") or {}
            books[market.token] = Book.from_levels(_levels(data.get("bids")), _levels(data.get("offers")))
        return books

    def outcomes(self, event_id: str, market_ids: list[str]) -> dict[str, float]:
        results = {}
        for slug in market_ids:
            url = f"{GATEWAY}/v1/markets/{slug}/settlement"
            try:
                raw = self._http.get(url)
            except HttpError as exc:
                if exc.status == 404:
                    continue
                raise
            if raw.get("settlement") is not None:
                results[slug] = _payout(raw["settlement"], url)
        return results


def _payout(value: object, url: str) -> float:
    try:
        payout = float(value)
    except (TypeError, ValueError):
        payout = math.nan
    if not 0 <= payout <= 1:
        raise HttpError(200, url, f"bad settlement {value!r}")
    return payout


def _levels(raw: list[dict] | None):
    return ((float(x["px"]["value"]), float(x["qty"])) for x in raw or [])

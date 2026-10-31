"""polymarket.com market data: Gamma for events, CLOB for order books."""
from __future__ import annotations

import json
from datetime import datetime

from ..core import Book, Event, Market
from ..http import Http, HttpError
from ..rules import parse_gamma_event
from . import parse_all

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_TAG_SLUGS = ("daily-temperature", "weather")
_PAGE = 100
_STALE_SECONDS = 2 * 86400
_HISTORY_MINUTES = 15


class IntlVenue:
    name = "intl"

    def __init__(self, http: Http, book_chunk: int = 200):
        self._http = http
        self._book_chunk = book_chunk

    def tag_id(self) -> str:
        for slug in _TAG_SLUGS:
            try:
                return str(self._http.get(f"{GAMMA}/tags/slug/{slug}")["id"])
            except HttpError as exc:
                if exc.status != 404:
                    raise
        raise HttpError(404, f"{GAMMA}/tags/slug", "no weather tag found")

    def _raw_open_events(self, now: int) -> list[dict]:
        tag = self.tag_id()
        raw: list[dict] = []
        offset = 0
        while True:
            page = self._http.get(
                f"{GAMMA}/events",
                {
                    "tag_id": tag,
                    "active": "true",
                    "closed": "false",
                    "order": "endDate",
                    "ascending": "false",
                    "limit": _PAGE,
                    "offset": offset,
                },
            )
            raw.extend(page)
            if len(page) < _PAGE or all(_ends_before(e, now - _STALE_SECONDS) for e in page):
                return raw
            offset += _PAGE

    def open_events(self, now: int):
        return parse_all(self._raw_open_events(now), parse_gamma_event)

    def closed_events(self, since: int):
        """Closed events back to `since`, newest first. Offset paging gives out at about 2,000."""
        params = {"tag_id": self.tag_id(), "closed": "true", "limit": _PAGE, "order": "endDate", "ascending": "false"}
        raw: list[dict] = []
        while True:
            page = self._http.get(f"{GAMMA}/events/keyset", params)
            events = page.get("events") or []
            raw.extend(events)
            cursor = page.get("next_cursor")
            if not cursor or not events or all(_ends_before(e, since) for e in events):
                return parse_all(raw, parse_gamma_event)
            params = {**params, "after_cursor": cursor}

    def quote_history(self, market: Market, start: int, end: int) -> list[tuple[int, float, float]]:
        """(time, bid, ask). The CLOB keeps one price per moment, so both sides come back the same."""
        raw = self._http.get(
            f"{CLOB}/prices-history",
            {"market": market.token, "interval": "max", "fidelity": _HISTORY_MINUTES},
        )
        return [(x["t"], x["p"], x["p"]) for x in raw.get("history") or [] if start <= x["t"] < end]

    def books(self, markets: list[Market]) -> dict[str, Book]:
        tokens = [m.token for m in markets]
        books: dict[str, Book] = {}
        for i in range(0, len(tokens), self._book_chunk):
            chunk = [{"token_id": t} for t in tokens[i : i + self._book_chunk]]
            for raw in self._http.post(f"{CLOB}/books", chunk):
                books[raw["asset_id"]] = _book(raw)
        return books

    def outcomes(self, event_id: str, market_ids: list[str]) -> dict[str, float]:
        raw = self._http.get(f"{GAMMA}/events/{event_id}")
        results = {}
        for m in raw.get("markets") or []:
            if not m.get("closed") or m.get("umaResolutionStatus") != "resolved":
                continue
            prices = json.loads(m["outcomePrices"])
            names = json.loads(m["outcomes"])
            results[m["conditionId"]] = float(prices[names.index("Yes")])
        return results


def _ends_before(raw_event: dict, cutoff: int) -> bool:
    end = raw_event.get("endDate")
    if not end:
        return False
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() < cutoff
    except ValueError:
        return False


def _book(raw: dict) -> Book:
    return Book.from_levels(
        ((float(x["price"]), float(x["size"])) for x in raw.get("bids") or []),
        ((float(x["price"]), float(x["size"])) for x in raw.get("asks") or []),
    )

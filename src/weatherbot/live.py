"""Live trading on Polymarket US: the gates, the signed client, and an executor that fails closed.

Nothing here runs unless every gate passes. When the bot can't tell what happened
to real money, it engages the kill switch and stops; a human decides when it restarts.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, NoReturn

from .config import Config, LiveConfig
from .core import Book, Event, Market
from .http import Http, HttpError
from .ledger import Ledger, OrderRow
from .strategy import Decision

API = "https://api.polymarket.us"
KEY_ENV = "POLYMARKET_KEY_ID"
SECRET_ENV = "POLYMARKET_SECRET_KEY"
METEO_ENV = "OPEN_METEO_API_KEY"

_CLOSED = {
    "ORDER_STATE_FILLED": "filled",
    "ORDER_STATE_CANCELED": "cancelled",
    "ORDER_STATE_EXPIRED": "cancelled",
    "ORDER_STATE_REPLACED": "cancelled",
    "ORDER_STATE_REJECTED": "rejected",
}
_REJECTED = 400  # the only status the docs give for a bad order, so anything else leaves the order's fate open
_PRICE_SLACK = 1e-6
_MONEY_SLACK = 1e-6
_SHARE_SLACK = 1e-9


class LiveRefused(Exception):
    """A gate failed, so the bot will not start in live mode."""


class Halted(Exception):
    """The kill switch is on. Trading has stopped until a human clears it."""


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    detail: str


def gates(cfg: Config, venue: str, env: Mapping[str, str]) -> list[Gate]:
    kill = Path(cfg.live.kill_file)
    return [
        Gate("venue", venue == "us",
             "live trades only on Polymarket US: polymarket.com is geoblocked here and its Python client is dead"),
        Gate("jurisdiction", cfg.live.jurisdiction_confirmed is True,
             "set live.jurisdiction_confirmed after confirming Polymarket US is available in Illinois"),
        Gate("polymarket_keys", bool(env.get(KEY_ENV) and env.get(SECRET_ENV)), f"set {KEY_ENV} and {SECRET_ENV}"),
        Gate("open_meteo_key", bool(env.get(METEO_ENV)), f"set {METEO_ENV}: the free tier is non-commercial"),
        Gate("kill_switch", not kill.exists(), f"{kill} exists: review why, then run weatherbot unkill"),
    ]


def require_gates(cfg: Config, venue: str, env: Mapping[str, str]) -> None:
    failed = [g for g in gates(cfg, venue, env) if not g.ok]
    if failed:
        raise LiveRefused("; ".join(f"{g.name}: {g.detail}" for g in failed))


def live_config(cfg: Config) -> Config:
    """Live runs on its own small bankroll, under whichever caps are tighter."""
    s, live = cfg.strategy, cfg.live
    strategy = replace(
        s,
        max_trade_usd=min(s.max_trade_usd, live.max_order_usd),
        max_day_usd=min(s.max_day_usd, live.max_day_usd),
        max_event_usd=min(s.max_event_usd, live.max_day_usd),
    )
    return replace(cfg, bankroll_usd=live.bankroll_usd, strategy=strategy)


def engage_kill(path: str | Path, reason: str, now: int) -> None:
    """Turn the kill switch on. The first reason stands, since it's the one worth reading."""
    path = Path(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_iso(now)} {reason}\n", encoding="utf8")


def release_kill(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def _iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_from_env(env: Mapping[str, str], clock: Callable[[], float] = time.time) -> "UsTrading":
    return UsTrading(Http(retries=0), env[KEY_ENV], env[SECRET_ENV], clock)


class UsTrading:
    """Signed REST client. Retries stay off, because a resent order is a second order."""

    def __init__(self, http: Http, key_id: str, secret: str, clock: Callable[[], float] = time.time):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._http = http
        self._key_id = key_id
        self._key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
        self._clock = clock

    def _headers(self, method: str, path: str) -> dict[str, str]:
        stamp = str(int(self._clock() * 1000))
        signature = self._key.sign(f"{stamp}{method}{path}".encode())
        return {
            "X-PM-Access-Key": self._key_id,
            "X-PM-Timestamp": stamp,
            "X-PM-Signature": base64.b64encode(signature).decode(),
        }

    def _get(self, path: str):
        return self._http.get(f"{API}{path}", headers=self._headers("GET", path))

    def _post(self, path: str, body: dict):
        return self._http.post(f"{API}{path}", body, headers=self._headers("POST", path))

    def place(self, body: dict) -> dict:
        return self._post("/v1/orders", body)

    def order(self, order_id: str) -> dict:
        return self._get(f"/v1/order/{order_id}")["order"]

    def cancel(self, order_id: str, slug: str) -> None:
        self._post(f"/v1/order/{order_id}/cancel", {"marketSlug": slug})

    def cancel_slugs(self, slugs: list[str]) -> list[str]:
        if not slugs:
            raise ValueError("an empty slug list would cancel every order on the account")
        reply = self._post("/v1/orders/open/cancel", {"slugs": list(slugs)})
        return (reply or {}).get("canceledOrderIds", [])

    def equity(self) -> float:
        """Cash plus securities, so a loss shows once a position is marked down or settles."""
        path = "/v1/account/balances"
        rows = self._get(path).get("balances") or []
        if not rows:
            raise HttpError(200, f"{API}{path}", "no balances in the reply")
        return sum(float(r.get("currentBalance") or 0) + float(r.get("assetNotional") or 0) for r in rows)


def order_body(market: Market, decision: Decision, expires_at: int) -> dict:
    """Prices are always in YES terms on this venue, so buying NO at X means a limit of 1 - X."""
    long = decision.side == "yes"
    body = {
        "marketSlug": market.market_id,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": _price_text(decision.limit if long else 1 - decision.limit), "currency": "USD"},
        "quantity": decision.shares,
        "intent": "ORDER_INTENT_BUY_LONG" if long else "ORDER_INTENT_BUY_SHORT",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    if decision.style == "maker":
        return body | {
            "tif": "TIME_IN_FORCE_GOOD_TILL_DATE",
            "goodTillTime": _iso(expires_at),
            "participateDontInitiate": True,
        }
    return body | {"tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}


def _price_text(price: float) -> str:
    return f"{price:.6f}".rstrip("0").rstrip(".")


class LiveExecutor:
    mode = "live"

    def __init__(self, ledger: Ledger, venue: str, client, cfg: LiveConfig, ttl_seconds: float):
        self._ledger = ledger
        self._venue = venue
        self._client = client
        self._cfg = cfg
        self._kill = Path(cfg.kill_file)
        self._ttl = ttl_seconds

    def check_stops(self, now: int) -> None:
        self._check_kill(now)
        self._check_daily_loss(now)

    def sync(self, books: dict[str, Book], now: int) -> int:
        """Stop if we should, then bring the ledger up to date. Returns how many orders got a fill."""
        self.check_stops(now)
        filled = 0
        for order in self._open():
            if order.external_id is None:
                self._halt(f"order {order.id} has no exchange id, so its outcome is unknown", now)
            report = self._client.order(order.external_id)
            filled += self._apply(order, report, now)
            self._check_fill_price(order, report, now)
        return filled

    def submit(self, decision_id: int, event: Event, market: Market, decision: Decision, now: int) -> int:
        self._check_kill(now)
        self._check_caps(event, market, decision, now)
        # The row goes first, so a crash during the post still leaves a trace of the order.
        order = self._ledger.add_order(
            decision_id, self._venue, self.mode, event.event_id, market.market_id, decision.side,
            decision.style, decision.limit, decision.shares, now,
        )
        # Twice the TTL, so the engine gets to act first and this only catches a dead bot.
        body = order_body(market, decision, now + int(2 * self._ttl))
        try:
            reply = self._client.place(body)
        except Exception as exc:
            if isinstance(exc, HttpError) and exc.status == _REJECTED:
                self._ledger.close_order(order, "rejected", now, f"http_{exc.status}")
                return order
            self._halt(f"order {order} may or may not have reached the exchange: {exc}", now)
        external_id = reply.get("id") if isinstance(reply, dict) else None
        if not external_id or not isinstance(external_id, str):
            self._halt(f"order {order} came back without an id", now)
        self._ledger.set_external_id(order, external_id)
        if order not in {row.id for row in self._open()}:
            # A kill closed the row while the post was in flight, so nothing else will ever cancel this.
            self._client.cancel(external_id, market.market_id)
            self._halt(f"order {order} reached the exchange after a kill closed it", now)
        return order

    def cancel(self, order: OrderRow, reason: str, now: int) -> None:
        try:
            self._client.cancel(order.external_id, order.market_id)
        except HttpError as exc:
            if not 400 <= exc.status < 500:
                raise  # otherwise it most likely finished first, which the report below will show
        report = self._client.order(order.external_id)
        self._apply(order, report, now, reason)
        self._check_fill_price(order, report, now)

    def stop(self, now: int) -> int:
        """Cancel everything we have open and catch the ledger up. Returns how many aren't confirmed closed."""
        orders = self._open()
        if not orders:
            return 0
        unknown = 0
        # Only our markets: an empty list would also cancel the user's own orders.
        self._client.cancel_slugs(sorted({o.market_id for o in orders}))
        for order in orders:
            if order.external_id is None:
                self._ledger.close_order(order.id, "unknown", now, "outcome_unknown")
                unknown += 1
            else:
                self._apply(order, self._client.order(order.external_id), now, "kill_switch")
        return len(self._open()) + unknown

    def _open(self) -> list[OrderRow]:
        return self._ledger.open_orders(self._venue, self.mode)

    def _apply(self, order: OrderRow, report: dict, now: int, reason: str | None = None) -> int:
        """Book any new shares and any final state. Returns 1 when shares were booked."""
        filled = self._book_new_shares(order, report, now)
        state = report.get("state")
        status = _CLOSED.get(state)
        if status:
            self._ledger.close_order(order.id, status, now, None if status == "filled" else reason or state)
        return filled

    def _book_new_shares(self, order: OrderRow, report: dict, now: int) -> int:
        cum = float(report.get("cumQuantity") or 0)
        shares, notional, fee = self._ledger.order_fills(order.id)
        fresh = cum - shares
        if fresh <= _SHARE_SLACK:
            return 0
        total_fee = float((report.get("commissionNotionalTotalCollected") or {}).get("value") or 0)
        price = (cum * _side_price(order, report) - notional) / fresh
        self._ledger.add_fill(order.id, now, price, fresh, max(total_fee - fee, 0.0))
        return 1

    def _check_fill_price(self, order: OrderRow, report: dict, now: int) -> None:
        """A price we didn't ask for means our reading of the venue's terms is wrong. A maker fills at its limit."""
        if float(report.get("cumQuantity") or 0) <= 0:
            return
        price = _side_price(order, report)
        over = price > order.limit_px + _PRICE_SLACK
        off = order.style == "maker" and abs(price - order.limit_px) > _PRICE_SLACK
        if over or off:
            self._halt(f"order {order.id} filled at {price:.4f} against a limit of {order.limit_px:.4f}", now)

    def _check_kill(self, now: int) -> None:
        if self._kill.exists():
            self._halt(f"kill switch is on: {self._kill.read_text(encoding='utf8').strip()}", now)

    def _check_daily_loss(self, now: int) -> None:
        equity = self._client.equity()
        day: date = datetime.fromtimestamp(now, timezone.utc).date()
        loss = self._ledger.day_equity(self._venue, day, equity) - equity
        limit = self._cfg.daily_loss_limit_usd
        if loss >= limit:
            self._halt(f"daily loss ${loss:.2f} reached the ${limit:.2f} limit", now)

    def _check_caps(self, event: Event, market: Market, decision: Decision, now: int) -> None:
        """The strategy already sizes under these, so tripping one means a bug. Stop rather than trade through it."""
        # A limit order can fill entirely at its limit, however cheap the book looked.
        fee = market.fee.taker_per_share(decision.limit) if decision.style == "taker" else 0.0
        cost = decision.shares * (decision.limit + fee)
        if cost > self._cfg.max_order_usd + _MONEY_SLACK:
            self._halt(f"${cost:.2f} order is over the ${self._cfg.max_order_usd:.2f} cap", now)
        spent = self._ledger.exposure(self._venue, self.mode, event.event_id, market.market_id, 0.0, now).day_spent
        if spent + cost > self._cfg.max_day_usd + _MONEY_SLACK:
            self._halt(f"${spent + cost:.2f} for the day is over the ${self._cfg.max_day_usd:.2f} cap", now)

    def _halt(self, reason: str, now: int) -> NoReturn:
        engage_kill(self._kill, reason, now)
        try:
            left = self.stop(now)
        except Exception as exc:
            raise Halted(f"{reason}. Open orders may still be live: {exc}") from exc
        if left:
            raise Halted(f"{reason}. {left} orders aren't confirmed closed, so check the app")
        raise Halted(reason)


def _side_price(order: OrderRow, report: dict) -> float:
    average = float(report["avgPx"]["value"])
    return average if order.side == "yes" else 1 - average

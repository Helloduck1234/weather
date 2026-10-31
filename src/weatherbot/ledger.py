"""SQLite ledger: every decision, order, fill, book, forecast and observation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import astuple, dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, NamedTuple

import numpy as np

from .core import Book, Event, Level
from .metar import Obs
from .openmeteo import Snapshot, decode_array, encode_array
from .strategy import Exposure

_BOOK_DEPTH = 10
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, venue TEXT, mode TEXT, started_at INTEGER, finished_at INTEGER,
    status TEXT, counts TEXT, skips TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    venue TEXT, event_id TEXT, slug TEXT, title TEXT, day TEXT, station TEXT, unit TEXT,
    family TEXT, day_clock TEXT, obs_policy TEXT, first_seen INTEGER, settled_at INTEGER,
    PRIMARY KEY (venue, event_id)
);
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT, market_id TEXT, event_id TEXT, label TEXT, lo INTEGER, hi INTEGER, token TEXT,
    tick REAL, min_size REAL, fee_rate REAL, fee_exponent REAL, fee_rebate REAL, fee_source TEXT,
    payout_yes REAL, settled_at INTEGER,
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, data BLOB);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, run_id INTEGER, ts INTEGER, venue TEXT, event_id TEXT, market_id TEXT,
    p_model REAL, p_lo REAL, p_hi REAL, q REAL, best_bid REAL, best_ask REAL, fee_rate REAL,
    edge REAL, side TEXT, action TEXT, reason TEXT, style TEXT, limit_px REAL, shares REAL,
    book_id INTEGER, forecast_runs TEXT, obs_floor_c REAL, lead_hours REAL
);
CREATE INDEX IF NOT EXISTS decisions_market ON decisions (venue, market_id, ts);
CREATE TABLE IF NOT EXISTS decision_state (
    venue TEXT, market_id TEXT, state TEXT, PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, decision_id INTEGER, venue TEXT, mode TEXT, event_id TEXT,
    market_id TEXT, side TEXT, style TEXT, limit_px REAL, shares REAL, status TEXT,
    created_at INTEGER, closed_at INTEGER, reason TEXT, external_id TEXT
);
CREATE INDEX IF NOT EXISTS orders_open ON orders (venue, mode, status);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, order_id INTEGER, ts INTEGER, price REAL, shares REAL, fee REAL
);
CREATE INDEX IF NOT EXISTS fills_order ON fills (order_id);
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    station TEXT, model TEXT, init_time INTEGER, fetched_at INTEGER, n_members INTEGER,
    n_times INTEGER, times BLOB, members BLOB,
    PRIMARY KEY (station, model, init_time)
);
CREATE TABLE IF NOT EXISTS metar_obs (
    icao TEXT, obs_time INTEGER, kind TEXT, temp_c REAL, max6h_c REAL, max24h_c REAL, raw TEXT,
    PRIMARY KEY (icao, obs_time, kind)
);
CREATE TABLE IF NOT EXISTS station_state (
    icao TEXT PRIMARY KEY, last_fetch INTEGER
);
CREATE TABLE IF NOT EXISTS forecast_log (
    station TEXT, day TEXT, policy TEXT, lead_days INTEGER, model TEXT, init_time INTEGER, mean_high_c REAL,
    logged_at INTEGER,
    PRIMARY KEY (station, day, policy, lead_days, model)
);
CREATE TABLE IF NOT EXISTS daily_highs (
    icao TEXT, day TEXT, policy TEXT, high_c REAL, n_obs INTEGER,
    PRIMARY KEY (icao, day, policy)
);
CREATE TABLE IF NOT EXISTS rule_audit (
    venue TEXT, event_id TEXT, family TEXT, station TEXT, day TEXT, high_c REAL, degree INTEGER,
    expected_market TEXT, actual_market TEXT, agrees INTEGER, checked_at INTEGER,
    PRIMARY KEY (venue, event_id)
);
CREATE TABLE IF NOT EXISTS day_equity (
    venue TEXT, day TEXT, equity REAL, PRIMARY KEY (venue, day)
);
CREATE TABLE IF NOT EXISTS price_history (
    venue TEXT, token TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (venue, token, ts)
);
CREATE TABLE IF NOT EXISTS replay_outcomes (
    venue TEXT, market_id TEXT, payout_yes REAL, PRIMARY KEY (venue, market_id)
);
"""
_RESULT_TABLES = (
    "runs", "events", "markets", "books", "decisions", "decision_state", "orders", "fills", "forecast_log",
    "daily_highs", "rule_audit", "station_state", "day_equity",
)


class Bias(NamedTuple):
    value: float
    days: int  # how many settled days it rests on


@dataclass(frozen=True)
class DecisionRow:
    run_id: int
    ts: int
    venue: str
    event_id: str
    market_id: str
    p_model: float
    p_lo: float
    p_hi: float
    q: float | None
    best_bid: float | None
    best_ask: float | None
    fee_rate: float
    edge: float | None
    side: str | None
    action: str
    reason: str
    style: str | None
    limit_px: float | None
    shares: float
    book_id: int | None
    forecast_runs: str
    obs_floor_c: float | None
    lead_hours: float


@dataclass(frozen=True)
class OrderRow:
    id: int
    venue: str
    mode: str
    event_id: str
    market_id: str
    side: str
    style: str
    limit_px: float
    shares: float
    remaining: float
    created_at: int
    external_id: str | None


def lead_bucket(hours: float) -> str:
    if hours <= 6:
        return "0-6h"
    if hours <= 24:
        return "6-24h"
    if hours <= 48:
        return "24-48h"
    return "48h+"


class Ledger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def count(self, table: str) -> int:
        return self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self._db.execute(sql, tuple(params)).fetchall()

    # Runs

    def start_run(self, venue: str, mode: str, now: int) -> int:
        cursor = self._db.execute(
            "INSERT INTO runs (venue, mode, started_at, status) VALUES (?, ?, ?, 'running')",
            (venue, mode, now),
        )
        return cursor.lastrowid

    def finish_run(self, run_id: int, now: int, status: str, counts: dict, skips: dict, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE runs SET finished_at=?, status=?, counts=?, skips=?, error=? WHERE id=?",
            (now, status, json.dumps(counts, sort_keys=True), json.dumps(skips, sort_keys=True), error, run_id),
        )

    def run_row(self, run_id: int) -> sqlite3.Row:
        return self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # Events and markets

    def upsert_event(self, event: Event, now: int) -> None:
        r = event.rule
        self._db.execute(
            """INSERT INTO events (venue, event_id, slug, title, day, station, unit, family, day_clock,
                                   obs_policy, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (venue, event_id) DO UPDATE SET
                   station=excluded.station, unit=excluded.unit, family=excluded.family,
                   day_clock=excluded.day_clock, obs_policy=excluded.obs_policy""",
            (event.venue, event.event_id, event.slug, event.title, event.day.isoformat(), r.station,
             r.unit, r.family, r.day_clock, r.obs_policy, now),
        )
        for m in event.markets:
            self._db.execute(
                """INSERT INTO markets (venue, market_id, event_id, label, lo, hi, token, tick, min_size,
                                        fee_rate, fee_exponent, fee_rebate, fee_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (venue, market_id) DO UPDATE SET
                       fee_rate=excluded.fee_rate, fee_exponent=excluded.fee_exponent,
                       fee_rebate=excluded.fee_rebate, fee_source=excluded.fee_source""",
                (event.venue, m.market_id, event.event_id, m.label, m.bucket.lo, m.bucket.hi, m.token,
                 m.tick, m.min_size, m.fee.rate, m.fee.exponent, m.fee.rebate, m.fee.source),
            )

    def event_row(self, venue: str, event_id: str) -> sqlite3.Row:
        return self._db.execute(
            "SELECT * FROM events WHERE venue=? AND event_id=?", (venue, event_id)
        ).fetchone()

    def market_row(self, venue: str, market_id: str) -> sqlite3.Row:
        return self._db.execute(
            "SELECT * FROM markets WHERE venue=? AND market_id=?", (venue, market_id)
        ).fetchone()

    def event_markets(self, venue: str, event_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM markets WHERE venue=? AND event_id=?", (venue, event_id))

    def unsettled_events(self, venue: str) -> list[sqlite3.Row]:
        return self.query(
            """SELECT * FROM events e WHERE venue=? AND EXISTS (
                   SELECT 1 FROM markets m
                   WHERE m.venue=e.venue AND m.event_id=e.event_id AND m.payout_yes IS NULL)""",
            (venue,),
        )

    def settle_market(self, venue: str, market_id: str, payout_yes: float, now: int) -> None:
        self._db.execute(
            "UPDATE markets SET payout_yes=?, settled_at=? WHERE venue=? AND market_id=?",
            (payout_yes, now, venue, market_id),
        )
        self._db.execute(
            """UPDATE events SET settled_at=? WHERE venue=? AND settled_at IS NULL AND event_id IN (
                   SELECT event_id FROM markets m WHERE venue=? AND market_id=?
               ) AND NOT EXISTS (
                   SELECT 1 FROM markets m
                   WHERE m.venue=events.venue AND m.event_id=events.event_id AND m.payout_yes IS NULL)""",
            (now, venue, venue, market_id),
        )

    def add_audit(self, venue: str, event_id: str, family: str, station: str, day: date, high_c: float | None,
                  degree: int | None, expected: str | None, actual: str | None, agrees: bool | None,
                  now: int) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO rule_audit
               (venue, event_id, family, station, day, high_c, degree, expected_market, actual_market,
                agrees, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (venue, event_id, family, station, day.isoformat(), high_c, degree, expected, actual, agrees, now),
        )

    # Observations

    def add_obs(self, reports: Iterable[Obs]) -> None:
        self._db.executemany(
            """INSERT INTO metar_obs (icao, obs_time, kind, temp_c, max6h_c, max24h_c, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (icao, obs_time, kind) DO UPDATE SET
                   temp_c=excluded.temp_c, max6h_c=excluded.max6h_c,
                   max24h_c=excluded.max24h_c, raw=excluded.raw""",
            [(r.icao, r.obs_time, r.kind, r.temp_c, r.max6h_c, r.max24h_c, r.raw) for r in reports],
        )

    def obs_between(self, icao: str, start: int, end: int) -> list[Obs]:
        rows = self.query(
            """SELECT * FROM metar_obs WHERE icao=? AND obs_time>=? AND obs_time<?
               ORDER BY obs_time""",
            (icao, start, end),
        )
        return [
            Obs(r["icao"], r["obs_time"], r["temp_c"], r["kind"], r["raw"], r["max6h_c"], r["max24h_c"])
            for r in rows
        ]

    def last_fetch(self, icao: str) -> int | None:
        row = self._db.execute("SELECT last_fetch FROM station_state WHERE icao=?", (icao,)).fetchone()
        return row[0] if row else None

    def mark_fetched(self, icaos: Iterable[str], now: int) -> None:
        self._db.executemany(
            """INSERT INTO station_state (icao, last_fetch) VALUES (?, ?)
               ON CONFLICT (icao) DO UPDATE SET last_fetch=excluded.last_fetch""",
            [(i, now) for i in icaos],
        )

    # Forecast archive

    def save_snapshot(self, station: str, snapshot: Snapshot, fetched_at: int) -> None:
        n_members, n_times = snapshot.members.shape
        self._db.execute(
            """INSERT OR IGNORE INTO forecast_snapshots
               (station, model, init_time, fetched_at, n_members, n_times, times, members)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (station, snapshot.model, snapshot.init_time, fetched_at, n_members, n_times,
             zlib.compress(snapshot.times.astype(np.int64).tobytes()), encode_array(snapshot.members)),
        )

    def latest_init(self, station: str, model: str) -> int | None:
        row = self._db.execute(
            "SELECT MAX(init_time) FROM forecast_snapshots WHERE station=? AND model=?", (station, model)
        ).fetchone()
        return row[0]

    def latest_snapshot(self, station: str, model: str, fetched_by: int | None = None) -> Snapshot | None:
        row = self._db.execute(
            """SELECT * FROM forecast_snapshots
               WHERE station=? AND model=? AND fetched_at<=?
               ORDER BY init_time DESC LIMIT 1""",
            (station, model, fetched_by if fetched_by is not None else 2**62),
        ).fetchone()
        if row is None:
            return None
        times = np.frombuffer(zlib.decompress(row["times"]), dtype=np.int64)
        members = decode_array(row["members"], (row["n_members"], row["n_times"]))
        return Snapshot(row["model"], row["init_time"], times, members)

    def has_snapshot(self, station: str, model: str, init_time: int) -> bool:
        return self._db.execute(
            "SELECT 1 FROM forecast_snapshots WHERE station=? AND model=? AND init_time=?",
            (station, model, init_time),
        ).fetchone() is not None

    # Replay archive

    def add_quotes(self, venue: str, token: str, quotes: Iterable[tuple[int, float, float]]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO price_history (venue, token, ts, bid, ask) VALUES (?, ?, ?, ?, ?)",
            [(venue, token, ts, bid, ask) for ts, bid, ask in quotes],
        )

    def has_quotes(self, venue: str, token: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM price_history WHERE venue=? AND token=? LIMIT 1", (venue, token)
        ).fetchone() is not None

    def quote_at(self, venue: str, token: str, now: int, max_age: int) -> tuple[float, float] | None:
        """The (bid, ask) in force at `now`, if the market was still being quoted then."""
        row = self._db.execute(
            """SELECT bid, ask FROM price_history
               WHERE venue=? AND token=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1""",
            (venue, token, now, now - max_age),
        ).fetchone()
        return None if row is None else (row["bid"], row["ask"])

    def add_outcomes(self, venue: str, outcomes: dict[str, float]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO replay_outcomes (venue, market_id, payout_yes) VALUES (?, ?, ?)",
            [(venue, market_id, payout) for market_id, payout in outcomes.items()],
        )

    def outcomes(self, venue: str, market_ids: Iterable[str]) -> dict[str, float]:
        wanted = set(market_ids)
        rows = self.query("SELECT market_id, payout_yes FROM replay_outcomes WHERE venue=?", (venue,))
        return {r["market_id"]: r["payout_yes"] for r in rows if r["market_id"] in wanted}

    def clear_results(self) -> None:
        """Forget a replay's results but keep what it downloaded, so the next replay starts clean."""
        if self.query("SELECT 1 FROM runs WHERE mode<>'backtest' LIMIT 1"):
            raise ValueError("this ledger holds paper or live history, so it is not a backtest ledger")
        for table in _RESULT_TABLES:
            self._db.execute(f"DELETE FROM {table}")

    # Station bias

    def log_forecast(self, station: str, day: date, policy: str, lead_days: int, model: str, init_time: int,
                     mean_high_c: float, now: int) -> None:
        """Keep the first forecast seen at each lead, so later runs can't flatter the record."""
        self._db.execute(
            """INSERT OR IGNORE INTO forecast_log
               (station, day, policy, lead_days, model, init_time, mean_high_c, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (station, day.isoformat(), policy, lead_days, model, init_time, mean_high_c, now),
        )

    def add_daily_high(self, icao: str, day: date, policy: str, high_c: float, n_obs: int) -> None:
        self._db.execute(
            """INSERT INTO daily_highs (icao, day, policy, high_c, n_obs) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (icao, day, policy) DO UPDATE SET high_c=excluded.high_c, n_obs=excluded.n_obs""",
            (icao, day.isoformat(), policy, high_c, n_obs),
        )

    def bias(self, station: str, policy: str, lead_days: int, model: str, day: date, window_days: int,
             shrink_days: float) -> Bias:
        """Mean recent (observed - forecast) error, shrunk toward zero while history is short."""
        mean, days = self._db.execute(
            """SELECT AVG(h.high_c - f.mean_high_c), COUNT(*)
               FROM forecast_log f
               JOIN daily_highs h ON h.icao = f.station AND h.day = f.day AND h.policy = f.policy
               WHERE f.station=? AND f.policy=? AND f.lead_days=? AND f.model=? AND f.day>=? AND f.day<?""",
            (station, policy, lead_days, model, (day - timedelta(days=window_days)).isoformat(), day.isoformat()),
        ).fetchone()
        return Bias(mean * days / (days + shrink_days) if days else 0.0, days)

    # Books

    def save_book(self, book: Book) -> int:
        payload = {
            "b": [list(x) for x in book.bids[:_BOOK_DEPTH]],
            "a": [list(x) for x in book.asks[:_BOOK_DEPTH]],
        }
        text = json.dumps(payload, separators=(",", ":"))
        digest = hashlib.sha1(text.encode()).hexdigest()
        row = self._db.execute("SELECT id FROM books WHERE hash=?", (digest,)).fetchone()
        if row:
            return row[0]
        return self._db.execute(
            "INSERT INTO books (hash, data) VALUES (?, ?)", (digest, zlib.compress(text.encode()))
        ).lastrowid

    def load_book(self, book_id: int) -> Book:
        row = self._db.execute("SELECT data FROM books WHERE id=?", (book_id,)).fetchone()
        data = json.loads(zlib.decompress(row[0]))
        return Book(tuple(Level(*x) for x in data["b"]), tuple(Level(*x) for x in data["a"]))

    # Decisions

    def log_decision(self, row: DecisionRow) -> int | None:
        """Store a decision unless nothing about the market has changed. Buys always store."""
        state = "|".join(
            (row.action, row.reason, row.side or "", f"{row.p_model:.3f}", str(row.best_bid),
             str(row.best_ask), lead_bucket(row.lead_hours))
        )
        if row.action != "buy":
            prior = self._db.execute(
                "SELECT state FROM decision_state WHERE venue=? AND market_id=?",
                (row.venue, row.market_id),
            ).fetchone()
            if prior and prior[0] == state:
                return None
        self._db.execute(
            """INSERT INTO decision_state (venue, market_id, state) VALUES (?, ?, ?)
               ON CONFLICT (venue, market_id) DO UPDATE SET state=excluded.state""",
            (row.venue, row.market_id, state),
        )
        names = [f.name for f in fields(DecisionRow)]
        marks = ", ".join("?" * len(names))
        return self._db.execute(
            f"INSERT INTO decisions ({', '.join(names)}) VALUES ({marks})", astuple(row)
        ).lastrowid

    # Orders and fills

    def add_order(self, decision_id, venue, mode, event_id, market_id, side, style, limit_px, shares, now,
                  external_id=None) -> int:
        return self._db.execute(
            """INSERT INTO orders (decision_id, venue, mode, event_id, market_id, side, style, limit_px,
                                   shares, status, created_at, external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (decision_id, venue, mode, event_id, market_id, side, style, limit_px, shares, now, external_id),
        ).lastrowid

    def add_fill(self, order_id: int, ts: int, price: float, shares: float, fee: float) -> None:
        self._db.execute(
            "INSERT INTO fills (order_id, ts, price, shares, fee) VALUES (?, ?, ?, ?, ?)",
            (order_id, ts, price, shares, fee),
        )

    def close_order(self, order_id: int, status: str, now: int, reason: str | None = None) -> None:
        self._db.execute(
            "UPDATE orders SET status=?, closed_at=?, reason=? WHERE id=?", (status, now, reason, order_id)
        )

    def set_external_id(self, order_id: int, external_id: str) -> None:
        self._db.execute("UPDATE orders SET external_id=? WHERE id=?", (external_id, order_id))

    def order_fills(self, order_id: int) -> tuple[float, float, float]:
        """Shares, notional and fees booked against an order so far."""
        row = self._db.execute(
            """SELECT COALESCE(SUM(shares), 0), COALESCE(SUM(price * shares), 0), COALESCE(SUM(fee), 0)
               FROM fills WHERE order_id=?""",
            (order_id,),
        ).fetchone()
        return tuple(row)

    def open_orders(self, venue: str, mode: str) -> list[OrderRow]:
        rows = self.query(
            """SELECT o.*, COALESCE(SUM(f.shares), 0) AS filled
               FROM orders o LEFT JOIN fills f ON f.order_id = o.id
               WHERE o.venue=? AND o.mode=? AND o.status='open'
               GROUP BY o.id ORDER BY o.id""",
            (venue, mode),
        )
        return [
            OrderRow(r["id"], r["venue"], r["mode"], r["event_id"], r["market_id"], r["side"], r["style"],
                     r["limit_px"], r["shares"], r["shares"] - r["filled"], r["created_at"], r["external_id"])
            for r in rows
        ]

    # Risk

    def exposure(self, venue: str, mode: str, event_id: str, market_id: str, start_bankroll: float, now: int) -> Exposure:
        day_start = now - now % 86400
        day = event = yes = no = 0.0
        rows = self.query(
            """SELECT o.event_id, o.market_id, o.side, f.ts, f.price * f.shares + f.fee AS cost
               FROM fills f JOIN orders o ON o.id = f.order_id
               WHERE o.venue=? AND o.mode=?""",
            (venue, mode),
        )
        resting = [
            (o.event_id, o.market_id, o.side, now, o.remaining * o.limit_px)
            for o in self.open_orders(venue, mode)
        ]
        # Nobody knows what became of these, so the money stays spoken for.
        unknown = self.query(
            """SELECT event_id, market_id, side, created_at, shares * limit_px FROM orders
               WHERE venue=? AND mode=? AND status='unknown'""",
            (venue, mode),
        )
        for ev, mkt, side, ts, cost in [tuple(r) for r in (*rows, *unknown)] + resting:
            if ts >= day_start:
                day += cost
            if ev == event_id:
                event += cost
            if mkt == market_id:
                if side == "yes":
                    yes += cost
                else:
                    no += cost
        return Exposure(start_bankroll + self.realized_pnl(venue, mode), day, event, yes, no)

    def day_equity(self, venue: str, day: date, equity: float) -> float:
        """The account's equity at the start of a day. The first reading of the day is the one that stands."""
        self._db.execute(
            "INSERT OR IGNORE INTO day_equity (venue, day, equity) VALUES (?, ?, ?)",
            (venue, day.isoformat(), equity),
        )
        return self._db.execute(
            "SELECT equity FROM day_equity WHERE venue=? AND day=?", (venue, day.isoformat())
        ).fetchone()[0]

    def realized_pnl(self, venue: str, mode: str) -> float:
        row = self._db.execute(
            """SELECT SUM(f.shares * CASE o.side WHEN 'yes' THEN m.payout_yes ELSE 1 - m.payout_yes END
                          - f.price * f.shares - f.fee)
               FROM fills f
               JOIN orders o ON o.id = f.order_id
               JOIN markets m ON m.venue = o.venue AND m.market_id = o.market_id
               WHERE o.venue=? AND o.mode=? AND m.payout_yes IS NOT NULL""",
            (venue, mode),
        ).fetchone()
        return row[0] or 0.0

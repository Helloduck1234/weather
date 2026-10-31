"""Measure the edge: P&L, Brier score against the market, and a calibration chart."""
from __future__ import annotations

import json
import math
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import PercentFormatter

from .ledger import Ledger, lead_bucket

_STRESS_FEE_RATE = 0.05
_BINS = 10
_BOOTSTRAP_DRAWS = 2000
_Z_90 = 1.6449
_CONTESTED = (0.05, 0.95)
_DEAD_RUN_SECONDS = 3600  # far longer than any cycle, so a run still open by then died without saying so
_OVERDUE_DAYS = 3
_BACKTEST_CAVEAT = (
    "**Not a measure of the edge.** Open-Meteo doesn't archive ensemble members, so a few deterministic "
    "models stand in for the ensemble the bot trades on. Book depth is assumed, polymarket.com quotes are "
    "one price with an assumed spread, and settlement timing is approximate. Use this to check the pipeline. "
    "Only paper trading measures the edge."
)

_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_SECONDARY = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"
_SERIES = (("model", "Model", "#2a78d6"), ("market", "Market mid", "#eb6834"))


@dataclass(frozen=True)
class Pnl:
    bankroll: float
    realized: float
    unrealized: float
    equity: float
    open_cost: float
    fees: float
    fee_stress: float  # realized P&L if every fill paid the old 5c taker curve
    fills: int
    settled: int
    win_rate: float | None  # by position
    max_drawdown: float
    interval: tuple[float, float] | None  # 90% on realized P&L, resampling days
    entry_edge: float | None
    realized_edge: float | None  # per share, after fees


@dataclass(frozen=True)
class Brier:
    n: int
    model: float | None
    market: float | None
    interval: tuple[float, float] | None  # 90% on model minus market. Below zero favours the model.
    by_lead: dict[str, tuple[int, float, float]]
    contested_n: int
    contested_model: float | None
    contested_market: float | None


@dataclass(frozen=True)
class Bin:
    lo: float
    hi: float
    n: int
    mean_forecast: float
    observed: float
    low: float
    high: float


@dataclass(frozen=True)
class Health:
    runs: int
    failed: int
    last_run: int | None
    skips: dict[str, int]
    overdue: int  # finished events still waiting for a result


@dataclass(frozen=True)
class Report:
    venue: str
    mode: str
    generated_at: int
    pnl: Pnl
    groups: dict[str, dict[str, tuple[int, float]]]
    brier: Brier
    calibration: dict[str, list[Bin]]
    audit: dict[str, tuple[int, int, int]]  # family -> with verdict, agreed, no verdict
    health: Health


@dataclass(frozen=True)
class _Fill:
    market_id: str
    side: str
    style: str
    station: str
    day: str
    price: float
    shares: float
    fee: float
    payout_yes: float | None
    edge: float | None
    lead_hours: float | None

    @property
    def payout(self) -> float | None:
        if self.payout_yes is None:
            return None
        return self.payout_yes if self.side == "yes" else 1 - self.payout_yes

    @property
    def pnl(self) -> float:
        return self.shares * (self.payout - self.price) - self.fee


@dataclass(frozen=True)
class _Scored:
    day: str
    p_model: float
    p_market: float
    outcome: float
    lead: str


def build_report(ledger: Ledger, venue: str, mode: str, bankroll: float, now: int) -> Report:
    fills = _fills(ledger, venue, mode)
    scored = _scored(ledger, venue, mode)
    return Report(
        venue,
        mode,
        now,
        _pnl(fills, _open_mids(ledger, venue), bankroll),
        _groups([f for f in fills if f.payout is not None]),
        _brier(scored),
        {
            "model": _bins([(s.p_model, s.outcome) for s in scored]),
            "market": _bins([(s.p_market, s.outcome) for s in scored]),
        },
        _audit(ledger, venue),
        _health(ledger, venue, mode, now),
    )


def _fills(ledger: Ledger, venue: str, mode: str) -> list[_Fill]:
    rows = ledger.query(
        """SELECT o.market_id, o.side, o.style, e.station, e.day, f.price, f.shares, f.fee, m.payout_yes,
                  d.edge, d.lead_hours
           FROM fills f
           JOIN orders o ON o.id = f.order_id
           JOIN markets m ON m.venue = o.venue AND m.market_id = o.market_id
           JOIN events e ON e.venue = o.venue AND e.event_id = o.event_id
           LEFT JOIN decisions d ON d.id = o.decision_id
           WHERE o.venue=? AND o.mode=?
           ORDER BY f.id""",
        (venue, mode),
    )
    return [_Fill(*row) for row in rows]


def _open_mids(ledger: Ledger, venue: str) -> dict[str, float]:
    rows = ledger.query(
        """SELECT d.market_id, d.best_bid, d.best_ask
           FROM decisions d JOIN markets m ON m.venue = d.venue AND m.market_id = d.market_id
           WHERE d.venue=? AND m.payout_yes IS NULL AND d.best_bid IS NOT NULL AND d.best_ask IS NOT NULL
           ORDER BY d.ts, d.id""",
        (venue,),
    )
    return {r["market_id"]: (r["best_bid"] + r["best_ask"]) / 2 for r in rows}


def _pnl(fills: list[_Fill], mids: dict[str, float], bankroll: float) -> Pnl:
    settled = [f for f in fills if f.payout is not None]
    still_open = [f for f in fills if f.payout is None]
    realized = sum(f.pnl for f in settled)
    unrealized = sum(f.shares * (_mark(f, mids) - f.price) - f.fee for f in still_open)

    positions: dict[tuple[str, str], float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    for f in settled:
        positions[(f.market_id, f.side)] += f.pnl
        daily[f.day] += f.pnl
    by_day = np.array([daily[d] for d in sorted(daily)])

    weighted = [f for f in settled if f.edge is not None]
    shares = sum(f.shares for f in settled)
    return Pnl(
        bankroll,
        realized,
        unrealized,
        bankroll + realized + unrealized,
        sum(f.shares * f.price for f in still_open),
        sum(f.fee for f in fills),
        sum(f.shares * (f.payout - f.price - _STRESS_FEE_RATE * f.price * (1 - f.price)) for f in settled),
        len(fills),
        len(settled),
        sum(v > 0 for v in positions.values()) / len(positions) if positions else None,
        _max_drawdown(by_day),
        _resample(by_day, np.ones(len(by_day)), scale_to_total=True),
        sum(f.edge * f.shares for f in weighted) / sum(f.shares for f in weighted) if weighted else None,
        realized / shares if shares else None,
    )


def _mark(fill: _Fill, mids: dict[str, float]) -> float:
    mid = mids.get(fill.market_id)
    if mid is None:
        return fill.price
    return mid if fill.side == "yes" else 1 - mid


def _max_drawdown(by_day: np.ndarray) -> float:
    if not by_day.size:
        return 0.0
    equity = np.concatenate(([0.0], np.cumsum(by_day)))
    return float((np.maximum.accumulate(equity) - equity).max())


def _resample(totals: np.ndarray, counts: np.ndarray, scale_to_total: bool = False) -> tuple[float, float] | None:
    """90% interval by resampling days, since a day's buckets and cities move together."""
    n = len(totals)
    if n < 2:
        return None
    picks = np.random.default_rng(0).integers(0, n, (_BOOTSTRAP_DRAWS, n))
    stat = totals[picks].sum(axis=1)
    if not scale_to_total:
        stat = stat / counts[picks].sum(axis=1)
    low, high = np.percentile(stat, [5, 95])
    return float(low), float(high)


def _groups(settled: list[_Fill]) -> dict[str, dict[str, tuple[int, float]]]:
    keys = {
        "station": lambda f: f.station,
        "lead": lambda f: "unknown" if f.lead_hours is None else lead_bucket(f.lead_hours),
        "side": lambda f: f.side,
        "style": lambda f: f.style,
    }
    groups = {}
    for name, key in keys.items():
        totals: dict[str, tuple[int, float]] = {}
        for f in settled:
            n, pnl = totals.get(key(f), (0, 0.0))
            totals[key(f)] = (n + 1, pnl + f.pnl)
        groups[name] = totals
    return groups


def _scored(ledger: Ledger, venue: str, mode: str) -> list[_Scored]:
    """First two-sided quote per market and lead bucket, so a chatty market can't dominate."""
    rows = ledger.query(
        """SELECT d.market_id, e.day, d.p_model, d.best_bid, d.best_ask, d.lead_hours, m.payout_yes
           FROM decisions d
           JOIN markets m ON m.venue = d.venue AND m.market_id = d.market_id
           JOIN events e ON e.venue = d.venue AND e.event_id = d.event_id
           JOIN runs r ON r.id = d.run_id
           WHERE d.venue=? AND r.mode=? AND m.payout_yes IN (0, 1)
             AND d.best_bid IS NOT NULL AND d.best_ask IS NOT NULL
           ORDER BY d.ts, d.id""",
        (venue, mode),
    )
    seen = set()
    scored = []
    for r in rows:
        lead = lead_bucket(r["lead_hours"])
        if (r["market_id"], lead) in seen:
            continue
        seen.add((r["market_id"], lead))
        scored.append(_Scored(r["day"], r["p_model"], (r["best_bid"] + r["best_ask"]) / 2, r["payout_yes"], lead))
    return scored


def _scores(items: list[_Scored]) -> tuple[float | None, float | None]:
    if not items:
        return None, None
    return (
        sum((s.p_model - s.outcome) ** 2 for s in items) / len(items),
        sum((s.p_market - s.outcome) ** 2 for s in items) / len(items),
    )


def _brier(scored: list[_Scored]) -> Brier:
    model, market = _scores(scored)
    leads = defaultdict(list)
    for s in scored:
        leads[s.lead].append(s)
    low, high = _CONTESTED
    contested = [s for s in scored if low <= s.p_market <= high]
    contested_model, contested_market = _scores(contested)

    daily: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for s in scored:
        daily[s.day][0] += (s.p_model - s.outcome) ** 2 - (s.p_market - s.outcome) ** 2
        daily[s.day][1] += 1
    totals = np.array([v[0] for v in daily.values()])
    counts = np.array([v[1] for v in daily.values()])
    return Brier(
        len(scored),
        model,
        market,
        _resample(totals, counts),
        {lead: (len(items), *_scores(items)) for lead, items in leads.items()},
        len(contested),
        contested_model,
        contested_market,
    )


def _bins(pairs: list[tuple[float, float]]) -> list[Bin]:
    grouped = defaultdict(list)
    for p, outcome in pairs:
        grouped[min(int(p * _BINS), _BINS - 1)].append((p, outcome))
    bins = []
    for i in sorted(grouped):
        items = grouped[i]
        n = len(items)
        hits = sum(outcome for _, outcome in items)
        low, high = _wilson(hits, n)
        bins.append(Bin(i / _BINS, (i + 1) / _BINS, n, sum(p for p, _ in items) / n, hits / n, low, high))
    return bins


def _wilson(hits: float, n: int) -> tuple[float, float]:
    rate = hits / n
    z2 = _Z_90**2
    denominator = 1 + z2 / n
    center = (rate + z2 / (2 * n)) / denominator
    half = _Z_90 * math.sqrt(rate * (1 - rate) / n + z2 / (4 * n * n)) / denominator
    # Rounding can leave the bounds a hair past the rate itself at 0% or 100%.
    return min(max(0.0, center - half), rate), max(min(1.0, center + half), rate)


def _audit(ledger: Ledger, venue: str) -> dict[str, tuple[int, int, int]]:
    rows = ledger.query(
        """SELECT family, COUNT(agrees), COALESCE(SUM(agrees), 0), COUNT(*) - COUNT(agrees)
           FROM rule_audit WHERE venue=? GROUP BY family""",
        (venue,),
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _health(ledger: Ledger, venue: str, mode: str, now: int) -> Health:
    rows = ledger.query("SELECT started_at, status, skips FROM runs WHERE venue=? AND mode=?", (venue, mode))
    skips: Counter = Counter()
    for r in rows:
        skips.update(json.loads(r["skips"] or "{}"))
    overdue_before = (datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=_OVERDUE_DAYS)).isoformat()
    return Health(
        len(rows),
        sum(r["status"] == "error" or (r["status"] == "running" and now - r["started_at"] > _DEAD_RUN_SECONDS)
            for r in rows),
        max((r["started_at"] for r in rows), default=None),
        dict(skips),
        sum(r["day"] < overdue_before for r in ledger.unsettled_events(venue)),
    )


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _score(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.4f}"


def _percent(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _cents(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}c"


def _table(header: tuple[str, ...], rows: list[tuple]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines + [""]


def render_markdown(report: Report, chart_name: str | None = None) -> str:
    p, b = report.pnl, report.brier
    stamp = datetime.fromtimestamp(report.generated_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# {report.venue} {report.mode} report", "", f"Generated {stamp}.", ""]
    if report.mode == "backtest":
        lines += [_BACKTEST_CAVEAT, ""]
    lines += ["## P&L", ""]
    interval = "n/a (needs two settled days)" if p.interval is None else (
        f"{_money(p.interval[0])} to {_money(p.interval[1])}"
    )
    lines += _table(
        ("Measure", "Value"),
        [
            ("Starting bankroll", f"${p.bankroll:,.2f}"),
            ("Realized P&L", _money(p.realized)),
            ("Unrealized P&L (at mid)", _money(p.unrealized)),
            ("Equity", f"${p.equity:,.2f}"),
            ("Cost of open positions", f"${p.open_cost:,.2f}"),
            ("Fees paid", f"${p.fees:,.2f}"),
            ("Realized P&L if every fill paid 0.05 x p x (1-p)", _money(p.fee_stress)),
            ("Fills, of which settled", f"{p.fills}, {p.settled}"),
            ("Win rate by position", _percent(p.win_rate)),
            ("Max drawdown by event day", f"${p.max_drawdown:,.2f}"),
            ("90% interval on realized P&L, resampling days", interval),
            ("Average edge at entry, per share", _cents(p.entry_edge)),
            ("Realized edge after fees, per share", _cents(p.realized_edge)),
        ],
    )
    for name, title in (("station", "station"), ("lead", "lead time"), ("side", "side"), ("style", "order style")):
        if report.groups[name]:
            lines += [f"### Settled P&L by {title}", ""]
            items = sorted(report.groups[name].items(), key=lambda kv: kv[1][1], reverse=True)
            lines += _table((title.capitalize(), "Fills", "P&L"), [(k, n, _money(v)) for k, (n, v) in items])

    lines += ["## Brier score", ""]
    if not b.n:
        lines += ["No settled forecasts yet.", ""]
    else:
        lines += ["Lower is better. Each row pairs the model with the market mid at the same moment.", ""]
        rows = [("All buckets", b.n, _score(b.model), _score(b.market))]
        rows.append(("Market mid 5-95%", b.contested_n, _score(b.contested_model), _score(b.contested_market)))
        rows += [(lead, n, _score(m), _score(k)) for lead, (n, m, k) in sorted(b.by_lead.items())]
        lines += _table(("Set", "Forecasts", "Model", "Market"), rows)
        if b.interval:
            lines += [f"Model minus market, 90% interval resampling days: {b.interval[0]:+.4f} to {b.interval[1]:+.4f}.",
                      "An interval wholly below zero favours the model.", ""]

    lines += ["## Calibration", ""]
    if chart_name:
        lines += [f"![Calibration chart]({chart_name})", ""]
    for key, label, _ in _SERIES:
        if report.calibration[key]:
            lines += [f"### {label}", ""]
            lines += _table(
                ("Bin", "Forecasts", "Mean forecast", "Happened", "90% interval"),
                [(f"{x.lo:.0%}-{x.hi:.0%}", x.n, f"{x.mean_forecast:.1%}", f"{x.observed:.1%}",
                  f"{x.low:.1%}-{x.high:.1%}") for x in report.calibration[key]],
            )
    if not any(report.calibration.values()):
        lines += ["No settled forecasts yet.", ""]

    lines += ["## Rule audit", "", "Our own reading of each result, against the bucket that actually won.", ""]
    if report.audit:
        lines += _table(
            ("Rule family", "Checked", "Agreed", "Share", "No verdict"),
            [(family, n, agreed, _percent(agreed / n if n else None), missing)
             for family, (n, agreed, missing) in sorted(report.audit.items())],
        )
    else:
        lines += ["Nothing audited yet.", ""]

    h = report.health
    lines += ["## Health", "", f"{h.runs} cycles, {h.failed} failed.", ""]
    if h.overdue:
        lines += [f"{h.overdue} finished events have no result after {_OVERDUE_DAYS} days. Check them by hand.", ""]
    if h.skips:
        top = sorted(h.skips.items(), key=lambda kv: kv[1], reverse=True)
        lines += _table(("Skip reason", "Events"), top)
    return "\n".join(lines)


def _draw_calibration(report: Report, path: Path) -> None:
    fig = Figure(figsize=(6.4, 6.8), dpi=200, facecolor=_SURFACE)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0.115, 0.08, 0.845, 0.76), facecolor=_SURFACE)

    ax.plot([0, 1], [0, 1], color=_AXIS, linewidth=1.0, solid_capstyle="round", zorder=1, label="Perfect calibration")
    for key, label, color in _SERIES:
        bins = report.calibration[key]
        y = np.array([b.observed for b in bins])
        ax.errorbar(
            [b.mean_forecast for b in bins],
            y,
            yerr=[y - [b.low for b in bins], [b.high for b in bins] - y],
            color=color,
            linewidth=1.5,
            elinewidth=0.9,
            capsize=0,
            marker="o",
            markersize=7,
            markerfacecolor=color,
            markeredgecolor=_SURFACE,
            markeredgewidth=1.5,
            label=label,
            zorder=3,
        )

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks(np.arange(0, 1.01, 0.2))
    ax.set_yticks(np.arange(0, 1.01, 0.2))
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(color=_GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, labelsize=8, labelcolor=_MUTED, pad=5)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_AXIS)
        ax.spines[side].set_linewidth(0.7)
    ax.set_xlabel("Forecast probability", fontsize=9, color=_SECONDARY, labelpad=7)
    ax.set_ylabel("How often it happened", fontsize=9, color=_SECONDARY, labelpad=7)

    fig.text(0.115, 0.965, f"Calibration: {report.venue} {report.mode}", fontsize=12.5, fontweight="bold",
             color=_INK, va="top")
    fig.text(0.115, 0.925, "Whiskers are 90% intervals. Buckets in one event move together, so true "
             "uncertainty is wider.", fontsize=8, color=_SECONDARY, va="top")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.012), ncols=3, frameon=False, fontsize=9,
              labelcolor=_SECONDARY, borderaxespad=0, borderpad=0, handletextpad=0.5, columnspacing=1.6)
    fig.savefig(path, facecolor=_SURFACE)


def _chart(report: Report, path: Path) -> Path | None:
    """The numbers are the point, so a chart that can't be drawn mustn't take them down with it."""
    try:
        _draw_calibration(report, path)
    except Exception:
        traceback.print_exc()
        return None
    return path


def write_report(report: Report, out_dir: str | Path) -> tuple[Path, Path | None]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{report.venue}-{report.mode}"
    chart = None
    if any(report.calibration.values()):
        chart = _chart(report, out / f"calibration-{stem}.png")
    markdown = out / f"report-{stem}.md"
    markdown.write_text(render_markdown(report, chart.name if chart else None), encoding="utf8")
    return markdown, chart

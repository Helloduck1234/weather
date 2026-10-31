"""Command line. Paper is the default. Live needs --live, API keys and every gate open."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import traceback
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

import yaml

from .aviation import Aviation
from .backtest import DEFAULT_DEPTH, run_backtest
from .config import VENUES, Config, ConfigError, kill_file_path, load_config
from .engine import Services, run_cycle
from .executors import PaperExecutor
from .forecast import Forecasts
from .http import Http, HttpError
from .ledger import Ledger
from .live import (
    KEY_ENV,
    METEO_ENV,
    SECRET_ENV,
    Halted,
    LiveExecutor,
    LiveRefused,
    client_from_env,
    engage_kill,
    gates,
    live_config,
    release_kill,
    require_gates,
)
from .openmeteo import OpenMeteo
from .report import build_report, write_report
from .settle import settle_venue
from .venues.intl import IntlVenue
from .venues.us import UsVenue

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
_REPORT_ARCHIVE_DAYS = 25  # the METAR archive thins out from here and ends at about 30
_KILL_WAIT_SECONDS = 30  # a run sees the switch at its next step, so most runs are out of the way by then


class Overlap(Exception):
    pass


@contextmanager
def _single_run(cfg: Config, wait: float = 0) -> Iterator[None]:
    """Hold SQLite's file lock for the whole run. The OS drops it with the process, so a crash can't wedge cron."""
    path = Path(cfg.db_path).with_name("run.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = sqlite3.connect(path, timeout=wait, isolation_level=None)
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise Overlap(f"Another run is already running ({path}).") from exc
        yield
    finally:
        lock.close()


def _venue(name: str, http: Http):
    return UsVenue(http) if name == "us" else IntlVenue(http)


def _weather(cfg: Config, http: Http, env: Mapping[str, str]) -> OpenMeteo:
    return OpenMeteo(http, env.get(METEO_ENV), cfg.forecast.past_days, cfg.forecast.forecast_days)


def _names(args: argparse.Namespace, cfg: Config) -> tuple[str, ...]:
    return (args.venue,) if args.venue else cfg.venues


def _summary(counts: dict) -> str:
    return " ".join(f"{key}={value}" for key, value in counts.items())


def _publish(cfg: Config, ledger: Ledger, venue: str, mode: str, now: int) -> Path:
    return write_report(build_report(ledger, venue, mode, cfg.bankroll_usd, now), cfg.report_dir)[0]


def _each_venue(names: tuple[str, ...], action: Callable[[str], object]) -> int:
    """One venue failing leaves the other running, but the exit code still says so."""
    status = 0
    for name in names:
        try:
            print(f"{name}: {action(name)}")
        except Halted:
            raise
        except Exception:
            print(f"{name}: failed", file=sys.stderr)
            traceback.print_exc()
            status = 1
    return status


def _run(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    if args.live:
        require_gates(cfg, args.venue or "", env)
        cfg = live_config(cfg)

    mode = "live" if args.live else "paper"
    with _single_run(cfg), closing(Ledger(cfg.db_path)) as ledger:
        http = Http()

        def trade(name: str) -> str:
            if args.live:
                executor = LiveExecutor(ledger, name, client_from_env(env), cfg.live, cfg.order_ttl_minutes * 60)
            else:
                executor = PaperExecutor(ledger, name)
            forecasts = Forecasts(_weather(cfg, http, env), ledger, cfg.forecast)
            counts = run_cycle(Services(cfg, ledger, _venue(name, http), Aviation(http), forecasts, executor), now)
            return _summary({"mode": mode, **counts})

        def settle(name: str) -> str:
            return _summary(settle_venue(cfg, ledger, _venue(name, http), now))

        def publish(name: str) -> Path:
            return _publish(cfg, ledger, name, mode, now)

        # A bad cycle shouldn't stop us booking and reporting what has already happened.
        return max(_each_venue(_names(args, cfg), step) for step in (trade, settle, publish))


def _settle(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    with _single_run(cfg), closing(Ledger(cfg.db_path)) as ledger:
        http = Http()
        return _each_venue(
            _names(args, cfg),
            lambda name: _summary(settle_venue(cfg, ledger, _venue(name, http), now)),
        )


def _report(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    scoped = live_config(cfg) if args.mode == "live" else cfg
    with closing(Ledger(cfg.db_path)) as ledger:
        return _each_venue(_names(args, cfg), lambda name: _publish(scoped, ledger, name, args.mode, now))


def _backtest(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    until = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    since = until - timedelta(days=args.days - 1)
    if args.days > _REPORT_ARCHIVE_DAYS:
        print("METAR history reaches back about 30 days, so the oldest days may have no observations.")
    stations = set(args.stations.split(",")) if args.stations else None
    http = Http()

    def replay(name: str) -> Path:
        return run_backtest(
            cfg, _venue(name, http), Aviation(http), _weather(cfg, http, env), since, until, now,
            stations=stations, step_hours=args.step_hours, depth=args.depth, half_spread=args.half_spread,
        )[1]

    return _each_venue(_names(args, cfg), replay)


def _check_live(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    """Read-only: shows what stands between here and --live. It can't place anything."""
    http = Http()
    try:
        geo = http.get(GEOBLOCK_URL)
        # The reply also holds the caller's IP, which has no place in a log.
        print(f"polymarket.com: blocked={geo.get('blocked')} country={geo.get('country')} region={geo.get('region')}")
    except HttpError as exc:
        print(f"polymarket.com: could not check (HTTP {exc.status})")

    listed = 0
    try:
        listed = len(_venue("us", http).open_events(now)[0])
        print(f"Polymarket US: {listed} open temperature events")
    except HttpError as exc:
        print(f"Polymarket US: could not check (HTTP {exc.status})")

    checks = gates(cfg, "us", env)
    for gate in checks:
        print(f"ok   {gate.name}" if gate.ok else f"FAIL {gate.name}: {gate.detail}")
    return 0 if listed and all(g.ok for g in checks) else 1


def _kill(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    # The switch goes on first, so nothing below can stand in its way.
    engage_kill(cfg.live.kill_file, args.reason, now)
    print(f"Kill switch on: {cfg.live.kill_file}")
    try:
        with _single_run(cfg, _KILL_WAIT_SECONDS):
            return _cancel_live(cfg, env, now)
    except Overlap:
        # A wedged run can't be allowed to keep real orders alive.
        print("A run is still going, so cancelling alongside it.", file=sys.stderr)
        return _cancel_live(cfg, env, now)


def _kill_without_config(args: argparse.Namespace, now: int) -> None:
    # A config typo mustn't take the brake away. Live runs refuse a bad config too, so new orders are already stopped.
    path = kill_file_path(args.config)
    engage_kill(path, args.reason, now)
    print(f"Kill switch on: {path}. Nothing was cancelled, so cancel any open orders in the app.", file=sys.stderr)


def _cancel_live(cfg: Config, env: Mapping[str, str], now: int) -> int:
    with closing(Ledger(cfg.db_path)) as ledger:
        count = len(ledger.open_orders("us", "live"))
        if not count:
            return 0
        if not (env.get(KEY_ENV) and env.get(SECRET_ENV)):
            print(f"{count} live orders are open and there are no API keys here. Cancel them in the app.",
                  file=sys.stderr)
            return 1
        try:
            executor = LiveExecutor(ledger, "us", client_from_env(env), cfg.live, cfg.order_ttl_minutes * 60)
            left = executor.stop(now)
        except Exception:
            print("Cancelling failed. Open orders may still be live, so check the app.", file=sys.stderr)
            traceback.print_exc()
            return 1
    print(f"{count - left} live orders closed.")
    if left:
        print(f"{left} orders aren't confirmed closed. Run kill again, or check the app.", file=sys.stderr)
        return 1
    return 0


def _unkill(args: argparse.Namespace, cfg: Config, env: Mapping[str, str], now: int) -> int:
    release_kill(cfg.live.kill_file)
    print("Kill switch off.")
    return 0


def _parser() -> argparse.ArgumentParser:
    config = argparse.ArgumentParser(add_help=False)
    config.add_argument("--config", default="config.yaml")
    venue = argparse.ArgumentParser(add_help=False, parents=[config])
    venue.add_argument("--venue", choices=VENUES, help="default: every venue in the config")

    parser = argparse.ArgumentParser(prog="weatherbot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", parents=[venue], allow_abbrev=False, help="one cycle, settlement and report")
    run.add_argument("--live", action="store_true", help="trade real money on Polymarket US (also needs --venue us)")
    run.set_defaults(action=_run)

    commands.add_parser("settle", parents=[venue], help="settle finished markets").set_defaults(action=_settle)

    report = commands.add_parser("report", parents=[venue], help="rebuild the report from the ledger")
    report.add_argument("--mode", choices=("paper", "live"), default="paper")
    report.set_defaults(action=_report)

    backtest = commands.add_parser("backtest", parents=[venue], help="replay closed markets")
    backtest.add_argument("--days", type=int, default=21)
    backtest.add_argument("--stations", help="comma-separated ICAO codes")
    backtest.add_argument("--step-hours", type=int, default=3)
    backtest.add_argument("--depth", type=float, default=DEFAULT_DEPTH, help="assumed shares a side")
    backtest.add_argument("--half-spread", type=float, help="assumed half spread around each recorded price")
    backtest.set_defaults(action=_backtest)

    commands.add_parser("check-live", parents=[config], help="read-only check of every live gate").set_defaults(
        action=_check_live
    )

    kill = commands.add_parser("kill", parents=[config], help="stop live trading and cancel our orders")
    kill.add_argument("--reason", default="manual")
    kill.set_defaults(action=_kill)

    commands.add_parser("unkill", parents=[config], help="release the kill switch").set_defaults(action=_unkill)
    return parser


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] = os.environ,
    clock: Callable[[], float] = time.time,
) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        print(f"Bad config: {exc}", file=sys.stderr)
        if args.action is _kill and Path(args.config).is_file():
            _kill_without_config(args, int(clock()))
        return 2
    try:
        return args.action(args, cfg, env, int(clock()))
    except Overlap as exc:
        print(exc, file=sys.stderr)
        return 1
    except LiveRefused as exc:
        print(f"Live trading refused. {exc}", file=sys.stderr)
        return 2
    except Halted as exc:
        print(f"Kill switch engaged: {exc}", file=sys.stderr)
        return 3

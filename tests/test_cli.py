"""The command line. Live paths run only against fakes."""
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from test_engine import KLGA, NOW, QUIET, FakeAviation, FakeForecasts, FakeVenue, event, forecast
from test_live import ENV, FakeTrading, remote
from weatherbot import cli
from weatherbot.config import load_config
from weatherbot.http import Http, HttpError
from weatherbot.ledger import Ledger
from weatherbot.openmeteo import CUSTOMER_URL
from weatherbot.venues.intl import IntlVenue
from weatherbot.venues.us import UsVenue


class Bot:
    """A config on disk, plus a market that always has one underpriced bucket."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.config = tmp_path / "config.yaml"
        self.venues = {"us": self._venue("us"), "intl": self._venue("intl")}
        self.client = FakeTrading()
        self.write()
        monkeypatch.setattr(cli, "_venue", lambda name, http: self.venues[name])
        monkeypatch.setattr(cli, "Aviation", lambda http: FakeAviation())
        monkeypatch.setattr(cli, "Forecasts", self._forecasts)
        monkeypatch.setattr(cli, "client_from_env", lambda env: self.client)

    def _forecasts(self, client, ledger, cfg):
        self.weather = client
        return FakeForecasts(forecast())

    @staticmethod
    def _venue(name):
        venue = FakeVenue([replace(event(), venue=name)])
        venue.name = name
        for token in ("tok-m0", "tok-m2", "tok-m3"):
            venue.set_book(token, *QUIET)
        venue.set_book("tok-m1", [(0.40, 100.0)], [(0.45, 100.0)])
        return venue

    def write(self, confirmed=False):
        root = self.root.as_posix()
        self.config.write_text(
            f"""
db_path: {root}/data/bot.sqlite
report_dir: {root}/reports
strategy: {{max_depth_share: 1.0}}
bias: {{min_days: 0}}
forecast: {{models: [gfs025]}}
model: {{spread_inflation: 1.0, kernel_sigma_start_c: 0.05, kernel_sigma_end_c: 0.05, edge_shift_c: 0.0, prob_epsilon: 0.0}}
live:
  jurisdiction_confirmed: {str(confirmed).lower()}
  kill_file: {root}/data/KILL
stations:
  KLGA: {{name: NYC, tz: America/New_York, lat: 40.8, lon: -73.9, elev_m: 9}}
""",
            encoding="utf8",
        )

    @property
    def kill_file(self):
        return self.root / "data" / "KILL"

    def run(self, *args, env=None):
        return cli.main([*args, "--config", str(self.config)], env={} if env is None else env, clock=lambda: NOW)

    def ledger(self):
        return Ledger(self.root / "data" / "bot.sqlite")

    def rows(self, sql):
        db = self.ledger()
        try:
            return [tuple(r) for r in db.query(sql)]
        finally:
            db.close()


@pytest.fixture
def bot(tmp_path, monkeypatch):
    return Bot(tmp_path, monkeypatch)


# Paper


def test_run_trades_on_paper_on_every_configured_venue(bot):
    assert bot.run("run") == 0
    assert sorted(bot.rows("SELECT venue, mode FROM orders")) == [("intl", "paper"), ("us", "paper")]


def test_run_can_name_one_venue(bot):
    assert bot.run("run", "--venue", "intl") == 0
    assert bot.rows("SELECT venue FROM orders") == [("intl",)]


def test_run_writes_a_report_for_each_venue(bot):
    bot.run("run")
    assert (bot.root / "reports" / "report-us-paper.md").exists()
    assert (bot.root / "reports" / "report-intl-paper.md").exists()


def test_one_venue_failing_leaves_the_other_running_and_fails_the_exit_code(bot, capsys):
    bot.venues["us"].fail = HttpError(503, "https://gateway.polymarket.us")
    assert bot.run("run") == 1
    assert bot.rows("SELECT venue FROM orders") == [("intl",)]
    assert "us: failed" in capsys.readouterr().err


def test_failed_cycle_still_settles_finished_days_and_writes_the_report(bot):
    finished = replace(event(date(2026, 6, 28)), venue="intl")
    db = bot.ledger()
    db.upsert_event(finished, NOW)
    db.close()
    bot.venues["intl"].fail = HttpError(503, "https://gamma-api.polymarket.com")
    bot.venues["intl"].outcomes = lambda event_id, ids: {"m1": 1.0, "m0": 0.0, "m2": 0.0, "m3": 0.0}
    assert bot.run("run", "--venue", "intl") == 1
    assert bot.rows("SELECT market_id FROM markets WHERE payout_yes=1") == [("m1",)]
    assert (bot.root / "reports" / "report-intl-paper.md").exists()


def test_failed_settlement_still_writes_the_report_and_fails_the_exit_code(bot, monkeypatch):
    def broken(*_):
        raise RuntimeError("settlement store is corrupt")

    monkeypatch.setattr(cli, "settle_venue", broken)
    assert bot.run("run", "--venue", "intl") == 1
    assert (bot.root / "reports" / "report-intl-paper.md").exists()


def test_paper_run_never_builds_a_trading_client_even_with_keys_present(bot, monkeypatch):
    monkeypatch.setattr(cli, "client_from_env", None)
    assert bot.run("run", env=ENV) == 0
    assert sorted(bot.rows("SELECT DISTINCT mode FROM orders")) == [("paper",)]


def test_overlapping_run_is_refused(bot, capsys):
    with cli._single_run(load_config(bot.config)):
        assert bot.run("run") == 1
    assert bot.rows("SELECT * FROM orders") == []
    assert "already running" in capsys.readouterr().err


def test_lock_is_released_when_a_run_ends(bot):
    assert bot.run("run") == 0
    assert bot.run("run") == 0


def test_lock_is_released_when_a_run_fails(bot):
    bot.venues["us"].fail = HttpError(503, "https://gateway.polymarket.us")
    bot.run("run")
    with cli._single_run(load_config(bot.config)):
        pass


def test_settle_books_a_finished_day(bot):
    day = date(2026, 6, 28)
    finished = replace(event(day), venue="intl")
    bot.venues["intl"] = FakeVenue([finished])
    bot.venues["intl"].outcomes = lambda event_id, ids: {"m1": 1.0}
    db = bot.ledger()
    db.upsert_event(finished, NOW)
    db.close()
    assert bot.run("settle", "--venue", "intl") == 0
    assert bot.rows("SELECT market_id FROM markets WHERE payout_yes=1") == [("m1",)]


def test_run_settles_what_has_finished(bot):
    finished = replace(event(date(2026, 6, 28), "done", "d"), venue="intl")
    bot.venues["intl"].outcomes = lambda event_id, ids: {"d1": 1.0}
    db = bot.ledger()
    db.upsert_event(finished, NOW)
    db.close()
    assert bot.run("run", "--venue", "intl") == 0
    assert bot.rows("SELECT market_id FROM markets WHERE payout_yes=1") == [("d1",)]


def test_settle_waits_for_a_run_in_progress(bot, capsys):
    with cli._single_run(load_config(bot.config)):
        assert bot.run("settle") == 1
    assert "already running" in capsys.readouterr().err


def test_report_rebuilds_from_the_ledger(bot):
    bot.run("run", "--venue", "intl")
    target = bot.root / "reports" / "report-intl-paper.md"
    target.unlink()
    assert bot.run("report", "--venue", "intl") == 0
    assert target.exists()


def test_report_measures_live_against_the_live_bankroll(bot):
    assert bot.run("report", "--venue", "us", "--mode", "live") == 0
    text = (bot.root / "reports" / "report-us-live.md").read_text(encoding="utf8")
    assert "$100.00" in text


# Live gates


def test_live_is_refused_until_the_jurisdiction_is_confirmed(bot, capsys):
    bot.venues["us"].fail = AssertionError("must not reach the market")
    assert bot.run("run", "--live", "--venue", "us", env=ENV) == 2
    assert "jurisdiction" in capsys.readouterr().err
    assert bot.client.placed == []


def test_live_needs_api_keys(bot, capsys):
    bot.write(confirmed=True)
    assert bot.run("run", "--live", "--venue", "us", env={}) == 2
    assert "polymarket_keys" in capsys.readouterr().err


@pytest.mark.parametrize("venue", [(), ("--venue", "intl")])
def test_live_only_trades_on_polymarket_us(bot, capsys, venue):
    bot.write(confirmed=True)
    assert bot.run("run", "--live", *venue, env=ENV) == 2
    assert "venue" in capsys.readouterr().err
    assert bot.client.placed == []


def test_live_flag_cannot_be_abbreviated(bot):
    bot.write(confirmed=True)
    with pytest.raises(SystemExit):
        bot.run("run", "--li", "--venue", "us", env=ENV)
    assert bot.client.placed == []


def test_kill_switch_blocks_a_live_start(bot, capsys):
    bot.write(confirmed=True)
    bot.kill_file.parent.mkdir(parents=True)
    bot.kill_file.write_text("stop", encoding="utf8")
    assert bot.run("run", "--live", "--venue", "us", env=ENV) == 2
    assert "kill_switch" in capsys.readouterr().err
    assert bot.client.placed == []


def test_live_run_orders_through_the_live_executor_under_live_caps(bot):
    bot.write(confirmed=True)
    assert bot.run("run", "--live", "--venue", "us", env=ENV) == 0
    assert len(bot.client.placed) == 1
    (mode, limit, shares), = bot.rows("SELECT mode, limit_px, shares FROM orders")
    assert mode == "live"
    assert limit * shares <= 10.0  # live.max_order_usd, not strategy.max_trade_usd
    assert (bot.root / "reports" / "report-us-live.md").exists()


def test_live_orders_lapse_after_twice_the_order_ttl(bot):
    bot.write(confirmed=True)
    bot.run("run", "--live", "--venue", "us", env=ENV)
    assert bot.client.placed[0]["goodTillTime"] == "2026-07-01T17:00:00Z"  # 30 minutes in the config


@respx.mock
def test_forecasts_use_the_paid_key_when_there_is_one(bot):
    bot.run("run", "--venue", "us", env=ENV)
    route = respx.get(CUSTOMER_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": [NOW], "temperature_2m": [1.0]}})
    )
    bot.weather.fetch("gfs025", KLGA, NOW)
    assert route.called


def test_halt_during_a_live_run_stops_with_its_own_exit_code(bot, capsys):
    bot.write(confirmed=True)
    bot.client.place_error = HttpError(503, "https://api.polymarket.us/v1/orders")
    assert bot.run("run", "--live", "--venue", "us", env=ENV) == 3
    assert bot.kill_file.exists()
    assert "Kill switch" in capsys.readouterr().err


def test_exchange_read_failure_aborts_a_live_run(bot, capsys):
    bot.write(confirmed=True)
    bot.client.equity_error = HttpError(503, "https://api.polymarket.us/v1/account/balances")
    assert bot.run("run", "--live", "--venue", "us", env=ENV) == 1
    assert bot.client.placed == []
    assert "503" in capsys.readouterr().err


# Kill switch


def open_live_order(bot):
    db = bot.ledger()
    db.add_order(None, "us", "live", "e1", "m1", "yes", "maker", 0.4, 5.0, NOW, external_id="x1")
    db.close()
    bot.client.orders["x1"] = remote()


def test_kill_engages_the_switch_and_cancels_our_orders(bot):
    open_live_order(bot)
    assert bot.run("kill", "--reason", "looks wrong", env=ENV) == 0
    assert "looks wrong" in bot.kill_file.read_text(encoding="utf8")
    assert bot.client.cancelled_slugs == [["m1"]]
    assert bot.rows("SELECT status FROM orders") == [("cancelled",)]


def test_kill_with_nothing_open_needs_no_keys(bot):
    assert bot.run("kill") == 0
    assert bot.kill_file.exists()


def test_kill_without_keys_still_engages_and_says_orders_remain(bot, capsys):
    open_live_order(bot)
    assert bot.run("kill", env={}) == 1
    assert bot.kill_file.exists()
    assert "Cancel them in the app" in capsys.readouterr().err


def test_kill_stays_on_when_cancelling_fails(bot, capsys):
    open_live_order(bot)
    bot.client.cancel_error = HttpError(503, "https://api.polymarket.us/v1/orders/open/cancel")
    assert bot.run("kill", env=ENV) == 1
    assert bot.kill_file.exists()
    assert "may still be live" in capsys.readouterr().err


def test_kill_again_retries_the_cancel_after_an_outage(bot):
    """Live runs refuse while the switch is on, so this is the only retry there is."""
    open_live_order(bot)
    bot.client.cancel_error = HttpError(0, "https://api.polymarket.us/v1/orders/open/cancel", "ConnectError")
    assert bot.run("kill", env=ENV) == 1
    bot.client.cancel_error = None
    assert bot.run("kill", env=ENV) == 0
    assert bot.rows("SELECT status FROM orders") == [("cancelled",)]


def test_kill_says_so_when_an_order_is_not_confirmed_closed(bot, capsys):
    open_live_order(bot)
    bot.client.cancel_slugs = lambda slugs: bot.client.orders.update(x1=remote("ORDER_STATE_PENDING_CANCEL"))
    assert bot.run("kill", env=ENV) == 1
    assert bot.rows("SELECT status FROM orders") == [("open",)]
    printed = capsys.readouterr()
    assert "0 live orders closed." in printed.out
    assert "1 orders aren't confirmed closed" in printed.err


def test_kill_says_so_when_an_order_has_no_exchange_id(bot, capsys):
    db = bot.ledger()
    db.add_order(None, "us", "live", "e1", "m1", "yes", "taker", 0.45, 20.0, NOW)
    db.close()
    assert bot.run("kill", env=ENV) == 1
    printed = capsys.readouterr()
    assert "0 live orders closed." in printed.out
    assert "1 orders aren't confirmed closed" in printed.err


@pytest.mark.parametrize("broken", [
    "strategy: {edge_treshold: 0.05}\n",
    "model: {sensor_gap_f: [0.5]}\n",
    "strategy: [\n",
    "- just a list\n",
    "live: 5\n",
    "live: {kill_file: 5}\n",
])
def test_kill_engages_the_switch_when_the_config_is_broken(bot, capsys, broken):
    bot.config.write_text(broken, encoding="utf8")
    assert bot.run("kill") == 2
    switch = bot.root / "data" / "KILL"
    assert switch.exists()
    assert str(switch) in capsys.readouterr().err


def test_kill_engages_the_switch_when_the_config_is_not_text(bot):
    bot.config.write_bytes(b"\xff\xfe\x00strategy")
    assert bot.run("kill") == 2
    assert (bot.root / "data" / "KILL").exists()


def test_kill_with_a_broken_config_uses_the_switch_that_config_names(bot):
    """A live run on the repaired config looks there, so a switch anywhere else stops nothing."""
    bot.config.write_text("strategy: {edge_treshold: 0.05}\nlive: {kill_file: elsewhere/KILL}\n", encoding="utf8")
    assert bot.run("kill") == 2
    assert (bot.root / "elsewhere" / "KILL").exists()
    assert not (bot.root / "data" / "KILL").exists()


def test_kill_with_no_config_does_not_claim_a_switch(bot, capsys):
    """Nothing says where the bot lives, so a file written here could stop nothing."""
    bot.config.unlink(missing_ok=True)
    assert bot.run("kill") == 2
    assert not (bot.root / "data" / "KILL").exists()
    assert "Kill switch on" not in capsys.readouterr().err


def test_kill_with_a_broken_config_says_nothing_was_cancelled(bot, capsys):
    bot.config.write_text("strategy: {edge_treshold: 0.05}\n", encoding="utf8")
    bot.run("kill")
    assert "cancel any open orders in the app" in capsys.readouterr().err


def test_other_commands_leave_the_switch_alone_when_the_config_is_broken(bot):
    bot.config.write_text("strategy: {edge_treshold: 0.05}\n", encoding="utf8")
    assert bot.run("run") == 2
    assert not (bot.root / "data" / "KILL").exists()


def test_kill_reaches_the_real_switch_from_another_folder(bot, tmp_path_factory, monkeypatch):
    bot.config.write_text(bot.config.read_text(encoding="utf8").replace(f"{bot.root.as_posix()}/", ""), encoding="utf8")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)
    assert bot.run("kill") == 0
    assert bot.kill_file.exists()
    assert not (elsewhere / "data").exists()


def test_kill_waits_for_a_run_that_is_finishing(bot):
    """Two writers working the same order could book its fill twice."""
    open_live_order(bot)
    entered, finished = threading.Event(), threading.Event()
    seen = []
    cancel_slugs = bot.client.cancel_slugs
    bot.client.cancel_slugs = lambda slugs: seen.append(finished.is_set()) or cancel_slugs(slugs)

    def run_in_progress():
        with cli._single_run(load_config(bot.config)):
            entered.set()
            time.sleep(0.3)
            finished.set()

    thread = threading.Thread(target=run_in_progress)
    thread.start()
    entered.wait()
    assert bot.run("kill", env=ENV) == 0
    thread.join()
    assert seen == [True]


def test_kill_cancels_anyway_when_a_run_never_lets_go(bot, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_KILL_WAIT_SECONDS", 0)
    open_live_order(bot)
    with cli._single_run(load_config(bot.config)):
        assert bot.run("kill", env=ENV) == 0
    assert bot.rows("SELECT status FROM orders") == [("cancelled",)]
    assert "still going" in capsys.readouterr().err


def test_unkill_releases_the_switch(bot):
    bot.run("kill")
    assert bot.run("unkill") == 0
    assert not bot.kill_file.exists()


# check-live


@respx.mock
def test_check_live_shows_the_geoblock_but_never_the_ip(bot, capsys):
    respx.get(cli.GEOBLOCK_URL).mock(
        return_value=httpx.Response(200, json={"blocked": True, "ip": "203.0.113.7", "country": "US", "region": "IL"})
    )
    bot.run("check-live")
    out = capsys.readouterr().out
    assert "blocked=True" in out
    assert "region=IL" in out
    assert "203.0.113.7" not in out


@respx.mock
def test_check_live_counts_what_polymarket_us_lists(bot, capsys):
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(200, json={"blocked": False}))
    bot.run("check-live")
    assert "1 open temperature events" in capsys.readouterr().out


@respx.mock
def test_check_live_fails_when_polymarket_us_lists_nothing(bot):
    bot.write(confirmed=True)
    bot.venues["us"].events = []
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(200, json={"blocked": False}))
    assert bot.run("check-live", env=ENV) == 1


@respx.mock
def test_check_live_fails_when_polymarket_us_cannot_be_reached(bot, capsys):
    bot.write(confirmed=True)
    bot.venues["us"].fail = HttpError(503, "https://gateway.polymarket.us")
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(200, json={"blocked": False}))
    assert bot.run("check-live", env=ENV) == 1
    assert "could not check" in capsys.readouterr().out


@respx.mock
def test_check_live_fails_while_a_gate_is_shut(bot, capsys):
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(200, json={"blocked": False}))
    assert bot.run("check-live", env=ENV) == 1
    assert "FAIL jurisdiction" in capsys.readouterr().out


@respx.mock
def test_check_live_passes_with_every_gate_open_and_places_nothing(bot, capsys):
    bot.write(confirmed=True)
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(200, json={"blocked": False}))
    assert bot.run("check-live", env=ENV) == 0
    assert bot.client.placed == []


@respx.mock
def test_check_live_carries_on_when_the_geoblock_check_fails(bot, capsys):
    respx.get(cli.GEOBLOCK_URL).mock(return_value=httpx.Response(403))
    bot.run("check-live")
    out = capsys.readouterr().out
    assert "could not check" in out
    assert "FAIL" in out


# Backtest


def test_backtest_replays_the_days_before_today(bot, monkeypatch, capsys):
    calls = []

    def fake(cfg, venue, aviation, weather, since, until, now, stations, step_hours, depth, half_spread):
        calls.append((venue.name, since, until, now, stations, step_hours, depth, half_spread))
        return None, Path("report.md"), None

    monkeypatch.setattr(cli, "run_backtest", fake)
    code = bot.run("backtest", "--venue", "us", "--days", "7", "--stations", "KNYC,KPHL", "--step-hours", "6",
                   "--depth", "50", "--half-spread", "0.02")
    assert code == 0
    assert calls == [("us", date(2026, 6, 24), date(2026, 6, 30), NOW, {"KNYC", "KPHL"}, 6, 50.0, 0.02)]
    assert "report.md" in capsys.readouterr().out


def test_backtest_defaults(bot, monkeypatch):
    calls = []

    def fake(cfg, venue, aviation, weather, since, until, now, stations, step_hours, depth, half_spread):
        calls.append((venue.name, since, stations, step_hours, depth, half_spread))
        return None, Path("report.md"), None

    monkeypatch.setattr(cli, "run_backtest", fake)
    assert bot.run("backtest") == 0
    assert calls == [
        ("us", date(2026, 6, 10), None, 3, 100.0, None),
        ("intl", date(2026, 6, 10), None, 3, 100.0, None),
    ]


def test_backtest_warns_when_reports_may_have_aged_out(bot, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_backtest", lambda *a, **k: (None, Path("report.md"), None))
    bot.run("backtest", "--venue", "us", "--days", "40")
    assert "30 days" in capsys.readouterr().out


# Errors


def test_each_venue_name_gets_its_own_market_data():
    http = Http()
    assert isinstance(cli._venue("us", http), UsVenue)
    assert isinstance(cli._venue("intl", http), IntlVenue)


def test_bad_config_is_a_message_not_a_traceback(bot, capsys):
    bot.config.write_text("strategy: {kelly_fraction: 0.9}", encoding="utf8")
    assert bot.run("run") == 2
    assert "kelly_fraction" in capsys.readouterr().err


def test_missing_config_is_a_message_not_a_traceback(tmp_path, capsys):
    assert cli.main(["run", "--config", str(tmp_path / "nope.yaml")], env={}) == 2
    assert "nope.yaml" in capsys.readouterr().err

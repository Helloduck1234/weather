"""Live trading code, tested only against fakes. Nothing here reaches a real exchange."""
import base64
import json
from dataclasses import replace
from datetime import date, datetime, timezone

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ed25519

from test_engine import FakeForecasts, FakeAviation, config, forecast, signal_venue
from weatherbot.config import Config, LiveConfig, Station
from weatherbot.core import Bucket, Event, Market, Rule
from weatherbot.engine import Services, run_cycle
from weatherbot.fees import FeeSchedule
from weatherbot.http import HttpError
from weatherbot.ledger import Ledger
from weatherbot.live import (
    API,
    Halted,
    LiveExecutor,
    LiveRefused,
    client_from_env,
    engage_kill,
    gates,
    live_config,
    order_body,
    release_kill,
    require_gates,
)
from weatherbot.strategy import Decision, StrategyParams


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


NOW = utc(2026, 11, 1, 16)
SEED = bytes(range(32))
SECRET = base64.b64encode(SEED + b"\x07" * 32).decode()  # the portal issues seed + public key
PUBLIC = ed25519.Ed25519PrivateKey.from_private_bytes(SEED).public_key()
ENV = {"POLYMARKET_KEY_ID": "key-1", "POLYMARKET_SECRET_KEY": SECRET, "OPEN_METEO_API_KEY": "om-1"}
SLUG = "tc-temp-phlhigh-2026-11-01-gte62lt63f"
MARKET = Market(SLUG, "62 to 63", Bucket(62, 63), SLUG, 0.01, 0.01, FeeSchedule(0.0695))
RULE = Rule("nws_cli", "KPHL", "F", "local_standard", "nws_cli")
EVENT = Event("us", "temp-phlhigh-2026-11-01", "temp-phlhigh-2026-11-01", "Highest temperature in Philadelphia",
              date(2026, 11, 1), RULE, (MARKET,))


def live_cfg(tmp_path, **kw):
    defaults = dict(jurisdiction_confirmed=True, daily_loss_limit_usd=50.0, max_order_usd=10.0, max_day_usd=25.0,
                    kill_file=str(tmp_path / "KILL"))
    return LiveConfig(**{**defaults, **kw})


def maker(side="yes", limit=0.40, shares=5.0):
    return Decision("buy", "signal", side, 0.1, 0.6, "maker", limit, shares, limit * shares)


def taker(side="yes", limit=0.45, shares=5.0):
    return Decision("buy", "signal", side, 0.1, 0.6, "taker", limit, shares, limit * shares, 0.1,
                    ((limit, shares),))


def remote(state="ORDER_STATE_NEW", cum=0, avg=None, commission="0"):
    return {
        "state": state,
        "cumQuantity": cum,
        "avgPx": {"value": avg, "currency": "USD"} if avg is not None else None,
        "commissionNotionalTotalCollected": {"value": commission, "currency": "USD"},
    }


class FakeTrading:
    """Stands in for UsTrading, holding orders the way the exchange reports them."""

    def __init__(self, equity=1000.0):
        self.balance = equity
        self.placed = []
        self.orders = {}
        self.cancelled = []
        self.cancelled_slugs = []
        self.place_error = None
        self.cancel_error = None
        self.equity_error = None

    def place(self, body):
        if self.place_error:
            raise self.place_error
        self.placed.append(body)
        order_id = f"x{len(self.placed)}"
        self.orders[order_id] = remote()
        return {"id": order_id, "executions": []}

    def order(self, order_id):
        return self.orders[order_id]

    def cancel(self, order_id, slug):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(order_id)
        self.orders[order_id] = {**self.orders[order_id], "state": "ORDER_STATE_CANCELED"}

    def cancel_slugs(self, slugs):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled_slugs.append(sorted(slugs))
        for order_id, order in self.orders.items():
            if order["state"] == "ORDER_STATE_NEW":
                self.orders[order_id] = {**order, "state": "ORDER_STATE_CANCELED"}
        return list(self.orders)

    def equity(self):
        if self.equity_error:
            raise self.equity_error
        return self.balance


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    db.upsert_event(EVENT, NOW)
    yield db
    db.close()


def executor(ledger, tmp_path, client=None, **kw):
    return LiveExecutor(ledger, "us", client or FakeTrading(), live_cfg(tmp_path, **kw), ttl_seconds=1800)


def orders(ledger):
    return ledger.query("SELECT * FROM orders ORDER BY id")


def fills(ledger):
    return ledger.query("SELECT * FROM fills ORDER BY id")


# Gates


def passing_config(tmp_path):
    return Config(live=live_cfg(tmp_path))


def failed(checks):
    return [g.name for g in checks if not g.ok]


def test_every_gate_passes_when_everything_is_in_place(tmp_path):
    assert failed(gates(passing_config(tmp_path), "us", ENV)) == []


def test_international_venue_never_goes_live(tmp_path):
    with pytest.raises(LiveRefused, match="geoblock"):
        require_gates(passing_config(tmp_path), "intl", ENV)


def test_jurisdiction_is_off_by_default():
    assert LiveConfig().jurisdiction_confirmed is False


def test_unconfirmed_jurisdiction_is_refused(tmp_path):
    cfg = Config(live=live_cfg(tmp_path, jurisdiction_confirmed=False))
    assert failed(gates(cfg, "us", ENV)) == ["jurisdiction"]


@pytest.mark.parametrize("value", ["false", "yes", 1])
def test_only_a_real_true_opens_the_jurisdiction_gate(tmp_path, value):
    cfg = Config(live=live_cfg(tmp_path, jurisdiction_confirmed=value))
    assert failed(gates(cfg, "us", ENV)) == ["jurisdiction"]


@pytest.mark.parametrize("name, gate", [
    ("POLYMARKET_KEY_ID", "polymarket_keys"),
    ("POLYMARKET_SECRET_KEY", "polymarket_keys"),
    ("OPEN_METEO_API_KEY", "open_meteo_key"),
])
def test_missing_key_is_refused(tmp_path, name, gate):
    env = {k: v for k, v in ENV.items() if k != name}
    assert failed(gates(passing_config(tmp_path), "us", env)) == [gate]


def test_blank_key_counts_as_missing(tmp_path):
    env = {**ENV, "POLYMARKET_SECRET_KEY": ""}
    assert failed(gates(passing_config(tmp_path), "us", env)) == ["polymarket_keys"]


def test_kill_file_is_refused(tmp_path):
    cfg = passing_config(tmp_path)
    engage_kill(cfg.live.kill_file, "testing", NOW)
    assert failed(gates(cfg, "us", ENV)) == ["kill_switch"]


def test_refusal_names_every_failed_gate(tmp_path):
    cfg = Config(live=live_cfg(tmp_path, jurisdiction_confirmed=False))
    with pytest.raises(LiveRefused) as err:
        require_gates(cfg, "us", {})
    assert all(name in str(err.value) for name in ("jurisdiction", "polymarket_keys", "open_meteo_key"))


def test_first_kill_reason_stands(tmp_path):
    engage_kill(tmp_path / "KILL", "the real cause", NOW)
    engage_kill(tmp_path / "KILL", "a later echo", NOW + 60)
    assert (tmp_path / "KILL").read_text(encoding="utf8").strip() == "2026-11-01T16:00:00Z the real cause"


def test_release_kill_clears_the_gate(tmp_path):
    cfg = passing_config(tmp_path)
    engage_kill(cfg.live.kill_file, "testing", NOW)
    release_kill(cfg.live.kill_file)
    assert failed(gates(cfg, "us", ENV)) == []


def test_release_kill_with_no_kill_file_is_fine(tmp_path):
    release_kill(tmp_path / "KILL")


def test_live_config_takes_the_tighter_caps(tmp_path):
    cfg = Config(
        bankroll_usd=1000.0,
        strategy=StrategyParams(max_trade_usd=25.0, max_day_usd=100.0, max_event_usd=40.0),
        live=live_cfg(tmp_path, max_order_usd=10.0, max_day_usd=25.0, bankroll_usd=120.0),
    )
    tightened = live_config(cfg)
    assert tightened.bankroll_usd == 120.0
    assert tightened.strategy.max_trade_usd == 10.0
    assert tightened.strategy.max_day_usd == 25.0
    assert tightened.strategy.max_event_usd == 25.0


def test_live_config_never_loosens_a_cap(tmp_path):
    cfg = Config(
        strategy=StrategyParams(max_trade_usd=2.0, max_day_usd=4.0, max_event_usd=3.0),
        live=live_cfg(tmp_path, max_order_usd=10.0, max_day_usd=25.0),
    )
    tightened = live_config(cfg)
    assert tightened.strategy.max_trade_usd == 2.0
    assert tightened.strategy.max_day_usd == 4.0
    assert tightened.strategy.max_event_usd == 3.0


# The signed client, on the wire


def trading():
    return client_from_env(ENV, clock=lambda: 1_700_000_000.5)


def assert_signed(request, method, path):
    assert request.headers["X-PM-Access-Key"] == "key-1"
    assert request.headers["X-PM-Timestamp"] == "1700000000500"
    message = f"1700000000500{method}{path}".encode()
    PUBLIC.verify(base64.b64decode(request.headers["X-PM-Signature"]), message)  # raises if wrong


@respx.mock
def test_place_signs_the_documented_message():
    route = respx.post(f"{API}/v1/orders").mock(return_value=httpx.Response(200, json={"id": "o1", "executions": []}))
    assert trading().place({"marketSlug": SLUG}) == {"id": "o1", "executions": []}
    assert_signed(route.calls[0].request, "POST", "/v1/orders")
    assert json.loads(route.calls[0].request.content) == {"marketSlug": SLUG}


@respx.mock
def test_order_reads_the_inner_order():
    route = respx.get(f"{API}/v1/order/o1").mock(
        return_value=httpx.Response(200, json={"order": {"state": "ORDER_STATE_FILLED"}})
    )
    assert trading().order("o1") == {"state": "ORDER_STATE_FILLED"}
    assert_signed(route.calls[0].request, "GET", "/v1/order/o1")


@respx.mock
def test_cancel_sends_a_json_body():
    route = respx.post(f"{API}/v1/order/o1/cancel").mock(return_value=httpx.Response(200))
    trading().cancel("o1", SLUG)
    assert_signed(route.calls[0].request, "POST", "/v1/order/o1/cancel")
    assert json.loads(route.calls[0].request.content) == {"marketSlug": SLUG}


@respx.mock
def test_cancel_slugs_names_only_our_markets():
    route = respx.post(f"{API}/v1/orders/open/cancel").mock(
        return_value=httpx.Response(200, json={"canceledOrderIds": ["o1"]})
    )
    assert trading().cancel_slugs([SLUG]) == ["o1"]
    assert json.loads(route.calls[0].request.content) == {"slugs": [SLUG]}


def test_cancel_slugs_never_sends_an_empty_list():
    """An empty list means every order on the account, including the user's own."""
    with pytest.raises(ValueError):
        trading().cancel_slugs([])


@respx.mock
def test_equity_adds_cash_and_positions():
    route = respx.get(f"{API}/v1/account/balances").mock(return_value=httpx.Response(200, json={
        "balances": [{"currency": "USD", "currentBalance": 80.5, "assetNotional": 19.5}],
    }))
    assert trading().equity() == 100.0
    assert_signed(route.calls[0].request, "GET", "/v1/account/balances")


@respx.mock
def test_equity_with_no_balances_fails_rather_than_reading_zero():
    respx.get(f"{API}/v1/account/balances").mock(return_value=httpx.Response(200, json={"balances": []}))
    with pytest.raises(HttpError):
        trading().equity()


@respx.mock
def test_an_order_post_is_never_retried():
    """A retry after a timeout or a 500 could leave two live orders."""
    route = respx.post(f"{API}/v1/orders").mock(return_value=httpx.Response(500))
    with pytest.raises(HttpError):
        trading().place({"marketSlug": SLUG})
    assert route.call_count == 1


# What goes on the wire for an order


def test_yes_maker_order_is_post_only_with_an_expiry():
    assert order_body(MARKET, maker("yes", 0.40, 5.0), NOW + 1800) == {
        "marketSlug": SLUG,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.4", "currency": "USD"},
        "quantity": 5.0,
        "intent": "ORDER_INTENT_BUY_LONG",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "tif": "TIME_IN_FORCE_GOOD_TILL_DATE",
        "goodTillTime": "2026-11-01T16:30:00Z",
        "participateDontInitiate": True,
    }


def test_no_order_is_priced_in_yes_terms():
    body = order_body(MARKET, maker("no", 0.38, 5.0), NOW + 1800)
    assert body["price"] == {"value": "0.62", "currency": "USD"}
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT"


def test_taker_order_is_a_limit_that_cannot_rest():
    body = order_body(MARKET, taker("yes", 0.45, 5.0), NOW + 1800)
    assert body["type"] == "ORDER_TYPE_LIMIT"
    assert body["price"] == {"value": "0.45", "currency": "USD"}
    assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert "participateDontInitiate" not in body
    assert "goodTillTime" not in body


# Submitting


def test_submit_records_the_exchange_id(ledger, tmp_path):
    client = FakeTrading()
    order = executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    row = orders(ledger)[0]
    assert (row["id"], row["mode"], row["status"], row["external_id"]) == (order, "live", "open", "x1")
    assert client.placed[0]["marketSlug"] == SLUG


def test_resting_order_outlives_the_engine_ttl(ledger, tmp_path):
    """The engine cancels at the TTL. The exchange expiry only catches a bot that died."""
    client = FakeTrading()
    executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    assert client.placed[0]["goodTillTime"] == "2026-11-01T17:00:00Z"


def test_order_row_exists_before_the_post_goes_out(ledger, tmp_path):
    """A crash mid-post must leave a trace of the order."""
    seen = []
    client = FakeTrading()
    client.place = lambda body: seen.append(len(orders(ledger))) or {"id": "x1"}
    executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    assert seen == [1]


def test_kill_file_stops_a_submit(ledger, tmp_path):
    client = FakeTrading()
    ex = executor(ledger, tmp_path, client)
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted):
        ex.submit(None, EVENT, MARKET, maker(), NOW)
    assert client.placed == []
    assert orders(ledger) == []


def test_exchange_rejection_closes_the_order_and_carries_on(ledger, tmp_path):
    client = FakeTrading()
    client.place_error = HttpError(400, f"{API}/v1/orders")
    executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    row = orders(ledger)[0]
    assert (row["status"], row["reason"]) == ("rejected", "http_400")
    assert not (tmp_path / "KILL").exists()


@pytest.mark.parametrize("status", [0, 401, 403, 404, 408, 409, 429, 500, 503])
def test_unknown_outcome_halts_everything(ledger, tmp_path, status):
    client = FakeTrading()
    ex = executor(ledger, tmp_path, client)
    ex.submit(None, EVENT, MARKET, maker(), NOW)
    client.place_error = HttpError(status, f"{API}/v1/orders")
    with pytest.raises(Halted):
        ex.submit(None, EVENT, MARKET, maker(), NOW)
    assert (tmp_path / "KILL").exists()
    assert client.cancelled_slugs == [[SLUG]]


@pytest.mark.parametrize("reply", [{"executions": []}, None, [], ["x1"], "x1", {"id": ""}, {"id": 7}, {"id": None}])
def test_reply_without_a_usable_id_halts(ledger, tmp_path, reply):
    client = FakeTrading()
    client.place = lambda body: reply
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    assert (tmp_path / "KILL").exists()


def test_surprise_during_the_post_halts(ledger, tmp_path):
    client = FakeTrading()
    client.place_error = RuntimeError("the socket did something new")
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    assert (tmp_path / "KILL").exists()
    assert orders(ledger)[0]["status"] == "unknown"


def test_unknown_order_is_closed_as_unknown_once_cancels_land(ledger, tmp_path):
    client = FakeTrading()
    client.place_error = HttpError(500, f"{API}/v1/orders")
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(), NOW)
    row = orders(ledger)[0]
    assert (row["status"], row["reason"]) == ("unknown", "outcome_unknown")


def test_order_above_the_hard_cap_never_leaves(ledger, tmp_path):
    client = FakeTrading()
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client, max_order_usd=1.0).submit(None, EVENT, MARKET, maker(limit=0.4), NOW)
    assert client.placed == []
    assert orders(ledger) == []
    assert (tmp_path / "KILL").exists()


def test_order_taking_the_day_over_the_hard_cap_never_leaves(ledger, tmp_path):
    client = FakeTrading()
    ex = executor(ledger, tmp_path, client, max_day_usd=3.0)
    ex.submit(None, EVENT, MARKET, maker(limit=0.4, shares=5.0), NOW)  # $2.00
    with pytest.raises(Halted):
        ex.submit(None, EVENT, MARKET, maker(limit=0.4, shares=5.0), NOW)  # would be $4.00
    assert len(client.placed) == 1


def test_taker_fee_counts_toward_the_hard_cap(ledger, tmp_path):
    client = FakeTrading()
    decision = Decision("buy", "signal", "yes", 0.1, 0.6, "taker", 0.5, 2.0, 1.0, 0.03475, ((0.5, 2.0),))
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client, max_order_usd=1.02).submit(None, EVENT, MARKET, decision, NOW)
    assert client.placed == []


def test_hard_cap_judges_a_taker_at_its_limit(ledger, tmp_path):
    """The walked average is $9.10, but all 20 shares can land at .60, which is $12.33 with the fee."""
    client = FakeTrading()
    decision = Decision("buy", "signal", "yes", 0.1, 0.8, "taker", 0.6, 20.0, 9.0, 0.1, ((0.3, 10.0), (0.6, 10.0)))
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, decision, NOW)
    assert client.placed == []


def test_hard_cap_charges_no_fee_on_a_maker(ledger, tmp_path):
    client = FakeTrading()
    executor(ledger, tmp_path, client).submit(None, EVENT, MARKET, maker(limit=0.6, shares=16.6), NOW)  # $9.96
    assert len(client.placed) == 1


def test_day_cap_judges_a_taker_at_its_limit(ledger, tmp_path):
    client = FakeTrading()
    decision = Decision("buy", "signal", "yes", 0.1, 0.8, "taker", 0.6, 20.0, 9.0, 0.1, ((0.3, 10.0), (0.6, 10.0)))
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client, max_order_usd=20.0, max_day_usd=10.0).submit(None, EVENT, MARKET, decision, NOW)
    assert client.placed == []


# Syncing fills


def open_order(ledger, tmp_path, client, decision=None):
    ex = executor(ledger, tmp_path, client)
    ex.submit(None, EVENT, MARKET, decision or maker(), NOW)
    return ex


def test_partial_fill_books_the_new_shares(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_PARTIALLY_FILLED", cum=2, avg="0.40")
    assert ex.sync({}, NOW + 60) == 1
    fill = fills(ledger)[0]
    assert (fill["ts"], fill["shares"], fill["price"], fill["fee"]) == (NOW + 60, 2.0, 0.40, 0.0)
    assert ledger.open_orders("us", "live")[0].remaining == 3.0


def test_repeat_sync_does_not_book_a_fill_twice(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_PARTIALLY_FILLED", cum=2, avg="0.40")
    ex.sync({}, NOW + 60)
    assert ex.sync({}, NOW + 120) == 0
    assert len(fills(ledger)) == 1


def test_second_fill_is_priced_from_the_running_average(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client, taker(limit=0.45, shares=4.0))
    client.orders["x1"] = remote("ORDER_STATE_PARTIALLY_FILLED", cum=2, avg="0.40", commission="0.10")
    ex.sync({}, NOW + 60)
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=4, avg="0.42", commission="0.25")
    ex.sync({}, NOW + 120)
    second = fills(ledger)[1]
    assert second["shares"] == 2.0
    assert second["price"] == pytest.approx(0.44)
    assert second["fee"] == pytest.approx(0.15)
    assert orders(ledger)[0]["status"] == "filled"


def test_no_fill_is_booked_at_the_no_price(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client, maker("no", 0.38, 5.0))
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=5, avg="0.62")
    ex.sync({}, NOW + 60)
    assert fills(ledger)[0]["price"] == pytest.approx(0.38)


@pytest.mark.parametrize("state, status", [
    ("ORDER_STATE_CANCELED", "cancelled"),
    ("ORDER_STATE_EXPIRED", "cancelled"),
    ("ORDER_STATE_REJECTED", "rejected"),
])
def test_finished_order_closes(ledger, tmp_path, state, status):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote(state)
    ex.sync({}, NOW + 60)
    row = orders(ledger)[0]
    assert (row["status"], row["reason"], row["closed_at"]) == (status, state, NOW + 60)


def test_partial_fill_on_a_cancelled_order_is_still_booked(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_CANCELED", cum=2, avg="0.40")
    ex.sync({}, NOW + 60)
    assert fills(ledger)[0]["shares"] == 2.0
    assert orders(ledger)[0]["status"] == "cancelled"


@pytest.mark.parametrize("state", [
    "ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_CANCEL", "ORDER_STATE_PENDING_RISK",
    "ORDER_STATE_SOMETHING_NEW",
])
def test_order_stays_open_until_the_exchange_says_it_is_done(ledger, tmp_path, state):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote(state)
    ex.sync({}, NOW + 60)
    assert orders(ledger)[0]["status"] == "open"


def test_fill_above_our_limit_halts(ledger, tmp_path):
    """The price convention being wrong would look exactly like this."""
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client, taker(limit=0.45))
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=5, avg="0.50")
    with pytest.raises(Halted):
        ex.sync({}, NOW + 60)
    assert (tmp_path / "KILL").exists()
    assert fills(ledger)[0]["price"] == 0.50  # the ledger still tells the truth


def test_maker_fill_away_from_its_limit_halts(ledger, tmp_path):
    """A NO price read back in the wrong terms can land below the limit, which a one-sided check misses."""
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client, maker("no", 0.70, 5.0))
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=5, avg="0.70")  # would be 0.30 once converted
    with pytest.raises(Halted):
        ex.sync({}, NOW + 60)


def test_exchange_failure_during_sync_stops_the_cycle(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.order = lambda order_id: (_ for _ in ()).throw(HttpError(503, "x"))
    with pytest.raises(HttpError):
        ex.sync({}, NOW + 60)


def test_order_left_without_an_exchange_id_halts(ledger, tmp_path):
    """A crash between the post and saving the id leaves this behind."""
    client = FakeTrading()
    ledger.add_order(None, "us", "live", EVENT.event_id, SLUG, "yes", "maker", 0.4, 5.0, NOW)
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).sync({}, NOW + 60)
    assert (tmp_path / "KILL").exists()
    assert orders(ledger)[0]["status"] == "unknown"


# Cancelling


def test_cancel_closes_the_order_with_the_engine_reason(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    ex.cancel(ledger.open_orders("us", "live")[0], "signal_gone", NOW + 60)
    row = orders(ledger)[0]
    assert client.cancelled == ["x1"]
    assert (row["status"], row["reason"]) == ("cancelled", "signal_gone")


def test_cancel_books_a_fill_that_beat_it(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=5, avg="0.40")
    client.cancel_error = HttpError(400, "x")
    ex.cancel(ledger.open_orders("us", "live")[0], "ttl", NOW + 60)
    row = orders(ledger)[0]
    assert fills(ledger)[0]["shares"] == 5.0
    assert (row["status"], row["reason"]) == ("filled", None)


def test_order_stays_open_while_the_cancel_is_pending(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.cancel = lambda order_id, slug: client.orders.update(x1=remote("ORDER_STATE_PENDING_CANCEL"))
    ex.cancel(ledger.open_orders("us", "live")[0], "ttl", NOW + 60)
    assert orders(ledger)[0]["status"] == "open"


def test_failed_cancel_is_not_swallowed(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.cancel_error = HttpError(503, "x")
    with pytest.raises(HttpError):
        ex.cancel(ledger.open_orders("us", "live")[0], "ttl", NOW + 60)
    assert orders(ledger)[0]["status"] == "open"


# Daily loss limit


def test_small_loss_leaves_trading_on(ledger, tmp_path):
    client = FakeTrading(equity=1000.0)
    ex = open_order(ledger, tmp_path, client)
    ex.sync({}, NOW)
    client.balance = 950.01
    ex.sync({}, NOW + 60)
    assert not (tmp_path / "KILL").exists()


def test_reaching_the_daily_loss_limit_trips_the_kill_switch(ledger, tmp_path):
    client = FakeTrading(equity=1000.0)
    ex = open_order(ledger, tmp_path, client)
    ex.sync({}, NOW)
    client.balance = 950.0
    with pytest.raises(Halted, match="daily loss"):
        ex.sync({}, NOW + 60)
    assert (tmp_path / "KILL").exists()
    assert client.cancelled_slugs == [[SLUG]]
    assert orders(ledger)[0]["status"] == "cancelled"


def test_kill_file_survives_the_loss_recovering(ledger, tmp_path):
    client = FakeTrading(equity=1000.0)
    ex = open_order(ledger, tmp_path, client)
    ex.sync({}, NOW)
    client.balance = 900.0
    with pytest.raises(Halted):
        ex.sync({}, NOW + 60)
    client.balance = 1100.0
    with pytest.raises(Halted):
        ex.sync({}, NOW + 120)


def test_loss_limit_measures_from_the_start_of_each_utc_day(ledger, tmp_path):
    client = FakeTrading(equity=1000.0)
    ex = executor(ledger, tmp_path, client)
    ex.sync({}, utc(2026, 11, 1, 23, 50))
    client.balance = 960.0
    ex.sync({}, utc(2026, 11, 2, 0, 10))  # a fresh day starts from 960
    client.balance = 915.0
    ex.sync({}, utc(2026, 11, 2, 0, 20))
    assert not (tmp_path / "KILL").exists()
    client.balance = 910.0
    with pytest.raises(Halted):
        ex.sync({}, utc(2026, 11, 2, 0, 30))


def test_unreadable_balance_means_no_trading(ledger, tmp_path):
    client = FakeTrading()
    client.equity_error = HttpError(503, "x")
    with pytest.raises(HttpError):
        executor(ledger, tmp_path, client).sync({}, NOW)


# Kill switch


def test_kill_file_makes_sync_cancel_and_stop(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted, match="operator"):
        ex.sync({}, NOW + 60)
    assert client.cancelled_slugs == [[SLUG]]
    assert orders(ledger)[0]["status"] == "cancelled"


def test_halt_books_fills_from_orders_it_cancels(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_NEW", cum=2, avg="0.40")
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted):
        ex.sync({}, NOW + 60)
    assert fills(ledger)[0]["shares"] == 2.0


def test_halt_with_nothing_open_sends_no_cancel(ledger, tmp_path):
    client = FakeTrading()
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted):
        executor(ledger, tmp_path, client).sync({}, NOW)
    assert client.cancelled_slugs == []


def test_halt_during_an_outage_warns_that_orders_may_be_live(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.cancel_error = HttpError(0, "x", "ConnectError")
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted, match="may still be live"):
        ex.sync({}, NOW + 60)
    assert orders(ledger)[0]["status"] == "open"


def test_garbled_report_during_a_halt_still_halts(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    client.orders["x1"] = remote("ORDER_STATE_FILLED", cum=5)  # filled, with no average price
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted, match="may still be live"):
        ex.sync({}, NOW + 60)


def pending_cancel(client):
    client.cancel_slugs = lambda slugs: client.orders.update(x1=remote("ORDER_STATE_PENDING_CANCEL"))


def test_halt_says_so_when_an_order_is_not_confirmed_closed(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    pending_cancel(client)
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted, match="confirmed closed"):
        ex.sync({}, NOW + 60)
    assert orders(ledger)[0]["status"] == "open"


def test_stop_cancels_without_raising(ledger, tmp_path):
    """The kill command wants open orders gone, not an exception."""
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    assert ex.stop(NOW + 60) == 0
    assert orders(ledger)[0]["status"] == "cancelled"


def test_stop_counts_what_it_could_not_close(ledger, tmp_path):
    client = FakeTrading()
    ex = open_order(ledger, tmp_path, client)
    pending_cancel(client)
    assert ex.stop(NOW + 60) == 1
    assert orders(ledger)[0]["status"] == "open"


def test_stop_counts_an_order_it_cannot_identify_as_not_closed(ledger, tmp_path):
    """A crash mid-post leaves a row with no exchange id, and that order may have filled."""
    ledger.add_order(None, "us", "live", EVENT.event_id, MARKET.market_id, "yes", "taker", 0.45, 20.0, NOW)
    assert executor(ledger, tmp_path).stop(NOW + 60) == 1
    assert orders(ledger)[0]["status"] == "unknown"


def test_halt_says_when_an_order_cannot_be_identified(ledger, tmp_path):
    ledger.add_order(None, "us", "live", EVENT.event_id, MARKET.market_id, "yes", "taker", 0.45, 20.0, NOW)
    engage_kill(tmp_path / "KILL", "operator", NOW)
    with pytest.raises(Halted, match="1 orders aren't confirmed closed"):
        executor(ledger, tmp_path).sync({}, NOW + 60)


def test_order_landing_after_a_kill_closed_its_row_is_cancelled(ledger, tmp_path):
    """A kill that stopped waiting closes a row whose post is still in flight. Nothing else would cancel that order."""
    client = FakeTrading()
    ex = executor(ledger, tmp_path, client)
    place = client.place

    def slow_post(body):
        engage_kill(tmp_path / "KILL", "operator", NOW)
        ex.stop(NOW)
        return place(body)

    client.place = slow_post
    with pytest.raises(Halted):
        ex.submit(None, EVENT, MARKET, maker(), NOW)
    assert client.cancelled == ["x1"]
    assert client.orders["x1"]["state"] == "ORDER_STATE_CANCELED"


def test_order_landing_normally_is_left_alone(ledger, tmp_path):
    client = FakeTrading()
    open_order(ledger, tmp_path, client)
    assert client.cancelled == []
    assert orders(ledger)[0]["external_id"] == "x1"


# Through the real engine


def engine_services(ledger, tmp_path, client):
    cfg = live_config(replace(config(), live=live_cfg(tmp_path)))
    venue = signal_venue()
    venue.name = "us"
    return Services(
        cfg=cfg, ledger=ledger, venue=venue, aviation=FakeAviation(),
        forecasts=FakeForecasts(forecast()),
        executor=LiveExecutor(ledger, "us", client, live_cfg(tmp_path), ttl_seconds=1800),
    )


def engine_now():
    return utc(2026, 7, 1, 16)


def test_cycle_posts_a_real_order_shape_through_the_engine(ledger, tmp_path):
    client = FakeTrading()
    run_cycle(engine_services(ledger, tmp_path, client), engine_now())
    assert len(client.placed) == 1
    assert client.placed[0]["participateDontInitiate"] is True
    assert client.placed[0]["price"]["value"] == "0.41"
    row = ledger.query("SELECT * FROM orders WHERE mode='live'")[0]
    assert row["external_id"] == "x1"


def test_order_waiting_on_its_cancel_is_never_crossed(ledger, tmp_path):
    """The cancel could still lose to a fill, and crossing now would then buy twice."""
    client = FakeTrading()
    s = engine_services(ledger, tmp_path, client)
    run_cycle(s, engine_now())
    client.cancel = lambda order_id, slug: client.orders.update(x1=remote("ORDER_STATE_PENDING_CANCEL"))
    run_cycle(s, engine_now() + 31 * 60)
    assert len(client.placed) == 1


def test_kill_file_records_an_error_run_and_places_nothing(ledger, tmp_path):
    client = FakeTrading()
    engage_kill(tmp_path / "KILL", "operator", engine_now())
    with pytest.raises(Halted):
        run_cycle(engine_services(ledger, tmp_path, client), engine_now())
    assert client.placed == []
    assert ledger.query("SELECT status FROM runs")[0]["status"] == "error"


def market_data_down(*_):
    raise HttpError(503, "https://gateway.polymarket.us/v1/events")


@pytest.mark.parametrize("feed", ["open_events", "books"])
def test_loss_limit_fires_with_the_market_data_down(ledger, tmp_path, feed):
    client = FakeTrading(equity=1000.0)
    s = engine_services(ledger, tmp_path, client)
    open_order(ledger, tmp_path, client)
    ledger.day_equity("us", date(2026, 7, 1), 1100.0)
    setattr(s.venue, feed, market_data_down)
    with pytest.raises(Halted):
        run_cycle(s, engine_now())
    assert (tmp_path / "KILL").exists()
    assert orders(ledger)[0]["status"] == "cancelled"


@pytest.mark.parametrize("feed", ["open_events", "books"])
def test_kill_file_fires_with_the_market_data_down(ledger, tmp_path, feed):
    client = FakeTrading()
    s = engine_services(ledger, tmp_path, client)
    open_order(ledger, tmp_path, client)
    engage_kill(tmp_path / "KILL", "operator", engine_now())
    setattr(s.venue, feed, market_data_down)
    with pytest.raises(Halted):
        run_cycle(s, engine_now())
    assert orders(ledger)[0]["status"] == "cancelled"


def test_market_data_outage_with_no_stop_due_stays_an_outage(ledger, tmp_path):
    client = FakeTrading()
    s = engine_services(ledger, tmp_path, client)
    open_order(ledger, tmp_path, client)
    s.venue.books = market_data_down
    with pytest.raises(HttpError):
        run_cycle(s, engine_now())
    assert not (tmp_path / "KILL").exists()
    assert orders(ledger)[0]["status"] == "open"


def test_loss_limit_stops_the_cycle_before_any_order(ledger, tmp_path):
    client = FakeTrading(equity=1000.0)
    s = engine_services(ledger, tmp_path, client)
    ledger.day_equity("us", date(2026, 7, 1), 1100.0)
    with pytest.raises(Halted):
        run_cycle(s, engine_now())
    assert client.placed == []

from datetime import date, datetime, timezone

import pytest

from weatherbot.config import Config, Station
from weatherbot.core import Bucket, Event, Market, Rule
from weatherbot.fees import FeeSchedule
from weatherbot.http import HttpError
from weatherbot.ledger import Ledger
from weatherbot.metar import Obs
from weatherbot.settle import settle_venue

DAY = date(2026, 7, 1)
KLGA = Station("KLGA", "NYC", "America/New_York", 40.8, -73.9, 9)
CFG = Config(stations={"KLGA": KLGA})
RULE = Rule("metar_routine", "KLGA", "C", "local_dst", "routine")


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


DAY_END = utc(2026, 7, 2, 4)  # midnight in New York
LATER = DAY_END + 2 * 3600
BUCKETS = (Bucket(None, 9), Bucket(10, 10), Bucket(11, None))


def event(rule=RULE, station_rule=None):
    markets = tuple(
        Market(f"m{i}", f"b{i}", b, f"tok{i}", 0.01, 1.0, FeeSchedule(0.0)) for i, b in enumerate(BUCKETS)
    )
    return Event("intl", "e1", "slug", "Highest temperature in NYC", DAY, rule, markets)


class FakeVenue:
    name = "intl"

    def __init__(self, outcomes=None):
        self.results = outcomes if outcomes is not None else {"m0": 0.0, "m1": 1.0, "m2": 0.0}
        self.calls = []
        self.fail = False

    def outcomes(self, event_id, market_ids):
        self.calls.append((event_id, tuple(market_ids)))
        if self.fail:
            raise HttpError(503, "gamma")
        return self.results


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    db.upsert_event(event(), DAY_END - 86400)
    yield db
    db.close()


def audit(ledger):
    return ledger.query("SELECT * FROM rule_audit")


def full_day(high=10.3):
    """A report every hour, peaking at `high` at six in the evening."""
    hourly = [Obs("KLGA", t, 9.0, "routine", "raw") for t in range(DAY_END - 86400, DAY_END, 3600)]
    return hourly + [Obs("KLGA", DAY_END - 6 * 3600 + 60, high, "routine", "raw")]


def test_paper_trade_settles_into_realized_pnl(ledger):
    order = ledger.add_order(None, "intl", "paper", "e1", "m1", "yes", "taker", 0.4, 10.0, DAY_END - 3600)
    ledger.add_fill(order, DAY_END - 3600, 0.4, 10.0, 0.0)
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    assert ledger.realized_pnl("intl", "paper") == pytest.approx(6.0)


def test_event_is_marked_settled_once_every_market_pays(ledger):
    counts = settle_venue(CFG, ledger, FakeVenue(), LATER)
    assert ledger.event_row("intl", "e1")["settled_at"] == LATER
    assert (counts["events"], counts["markets"]) == (1, 3)


def test_settled_event_is_not_asked_about_again(ledger):
    venue = FakeVenue()
    settle_venue(CFG, ledger, venue, LATER)
    settle_venue(CFG, ledger, venue, LATER + 900)
    assert len(venue.calls) == 1


def test_day_still_running_is_left_alone(ledger):
    venue = FakeVenue()
    settle_venue(CFG, ledger, venue, DAY_END - 60)
    assert venue.calls == []


def test_first_hour_after_midnight_is_left_alone(ledger):
    # The day's last reports and any corrections are still arriving.
    venue = FakeVenue()
    settle_venue(CFG, ledger, venue, DAY_END + 1800)
    assert venue.calls == []


def test_partly_resolved_event_stays_open_and_retries(ledger):
    settle_venue(CFG, ledger, FakeVenue({"m0": 0.0}), LATER)
    venue = FakeVenue({"m1": 1.0, "m2": 0.0})
    settle_venue(CFG, ledger, venue, LATER + 900)
    assert venue.calls == [("e1", ("m1", "m2"))]
    assert ledger.event_row("intl", "e1")["settled_at"] == LATER + 900


def test_unknown_market_in_response_is_ignored(ledger):
    counts = settle_venue(CFG, ledger, FakeVenue({"other": 1.0}), LATER)
    assert counts.get("markets", 0) == 0


def test_outage_is_counted_not_raised(ledger):
    venue = FakeVenue()
    venue.fail = True
    assert settle_venue(CFG, ledger, venue, LATER)["errors"] == 1


def test_garbled_reply_costs_one_event_not_the_rest(ledger):
    second = Market("x", "b", Bucket(None, None), "tok-x", 0.01, 1.0, FeeSchedule(0.0))
    ledger.upsert_event(Event("intl", "e2", "s2", "t", DAY, RULE, (second,)), DAY_END - 86400)

    class Garbled(FakeVenue):
        def outcomes(self, event_id, market_ids):
            if event_id == "e1":
                raise ValueError("Yes is not in list")
            return {"x": 1.0}

    counts = settle_venue(CFG, ledger, Garbled(), LATER)
    assert (counts["errors"], counts["events"]) == (1, 1)
    assert ledger.event_row("intl", "e2")["settled_at"] == LATER


def test_audit_agrees_when_rule_picks_the_winner(ledger):
    ledger.add_obs(full_day())
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    row = audit(ledger)[0]
    assert (row["high_c"], row["degree"], row["expected_market"], row["actual_market"], row["agrees"]) == (
        10.3, 10, "m1", "m1", 1
    )


def test_audit_flags_a_wrong_rule(ledger):
    # The resolver paid the 11-and-up bucket, so a qualifying report was missed.
    ledger.add_obs(full_day())
    settle_venue(CFG, ledger, FakeVenue({"m0": 0.0, "m1": 0.0, "m2": 1.0}), LATER)
    row = audit(ledger)[0]
    assert (row["expected_market"], row["actual_market"], row["agrees"]) == ("m1", "m2", 0)


def test_audit_ignores_speci_on_routine_markets(ledger):
    ledger.add_obs(full_day() + [Obs("KLGA", DAY_END - 5 * 3600 + 60, 14.0, "speci", "raw")])
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    assert audit(ledger)[0]["agrees"] == 1


def test_audit_without_observations_stores_no_verdict(ledger):
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    row = audit(ledger)[0]
    assert (row["high_c"], row["expected_market"], row["agrees"]) == (None, None, None)


def test_thin_day_gets_no_audit_verdict(ledger):
    # Half a day of reports reads low, so a mismatch here would only be a false alarm.
    ledger.add_obs([r for r in full_day() if r.obs_time < DAY_END - 12 * 3600])
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    row = audit(ledger)[0]
    assert (row["high_c"], row["expected_market"], row["agrees"]) == (9.0, None, None)


def test_thin_day_stays_out_of_the_bias_record(ledger):
    ledger.add_obs([r for r in full_day() if r.obs_time < DAY_END - 12 * 3600])
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    assert ledger.query("SELECT * FROM daily_highs") == []


def test_sensor_maximum_for_the_whole_day_is_enough_on_an_nws_day(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    nws = Rule("nws_cli", "KLGA", "C", "local_standard", "nws_cli")
    db.upsert_event(event(rule=nws), DAY_END - 86400)
    midnight_lst = DAY_END + 3600
    db.add_obs([Obs("KLGA", midnight_lst - 420, 9.0, "routine", "raw", max24h_c=10.3)])
    settle_venue(CFG, db, FakeVenue(), midnight_lst + 2 * 3600)
    assert [tuple(r) for r in db.query("SELECT high_c, n_obs FROM daily_highs")] == [(10.3, 1)]
    assert audit(db)[0]["agrees"] == 1
    db.close()


def test_audit_of_voided_event_stores_no_verdict(ledger):
    ledger.add_obs(full_day())
    settle_venue(CFG, ledger, FakeVenue({"m0": 0.5, "m1": 0.5, "m2": 0.5}), LATER)
    row = audit(ledger)[0]
    assert (row["actual_market"], row["agrees"]) == (None, None)


def test_settlement_records_the_daily_high_for_bias(ledger):
    ledger.add_obs(full_day())
    settle_venue(CFG, ledger, FakeVenue(), LATER)
    rows = ledger.query("SELECT icao, day, policy, high_c, n_obs FROM daily_highs")
    assert [tuple(r) for r in rows] == [("KLGA", "2026-07-01", "routine", 10.3, 25)]


def test_station_removed_from_config_is_skipped(ledger):
    counts = settle_venue(Config(stations={}), ledger, FakeVenue(), LATER)
    assert counts.get("events", 0) == 0


def test_unconfigured_station_does_not_stop_later_events(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    other = Rule("metar_routine", "KXXX", "C", "local_dst", "routine")
    stray = Market("x0", "b", Bucket(None, None), "tok-x", 0.01, 1.0, FeeSchedule(0.0))
    db.upsert_event(Event("intl", "e0", "s", "t", DAY, other, (stray,)), DAY_END)
    db.upsert_event(event(), DAY_END)
    settle_venue(CFG, db, FakeVenue(), LATER)
    assert db.event_row("intl", "e1")["settled_at"] == LATER
    db.close()

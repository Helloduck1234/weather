import numpy as np
import pytest

from weatherbot.config import ForecastConfig, Station
from weatherbot.forecast import Forecasts
from weatherbot.http import HttpError
from weatherbot.ledger import Ledger
from weatherbot.openmeteo import Snapshot

HOUR = 3600
NOW = 1_800_000_000
KLGA = Station("KLGA", "NYC", "America/New_York", 40.8, -73.9, 9)
KDEN = Station("KDEN", "Denver", "America/Denver", 39.8, -104.7, 1656)
CFG = ForecastConfig(models=("gfs025", "ecmwf_ifs025"), max_age_hours=18)


class FakeClient:
    """Stands in for OpenMeteo. Records which fetches actually happened."""

    def __init__(self, runs):
        self.runs = runs
        self.fetched = []
        self.attempts = 0
        self.fail_fetch = None  # HTTP status to fail with
        self.fail_meta = False

    def latest_run(self, model):
        if self.fail_meta:
            raise HttpError(503, "meta")
        return self.runs[model]

    def fetch(self, model, station, init_time):
        self.attempts += 1
        if self.fail_fetch:
            raise HttpError(self.fail_fetch, "ensemble")
        self.fetched.append((model, station.icao))
        n = 31 if model == "gfs025" else 51
        return Snapshot(model, init_time, np.array([NOW, NOW + HOUR]), np.full((n, 2), 10.0))


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "t.sqlite")
    yield db
    db.close()


def runs(age_hours=1):
    return {"gfs025": NOW - age_hours * HOUR, "ecmwf_ifs025": NOW - age_hours * HOUR}


def test_new_run_is_fetched_for_every_station_and_model(ledger):
    client = FakeClient(runs())
    Forecasts(client, ledger, CFG).refresh([KLGA, KDEN], NOW)
    assert len(client.fetched) == 4


def test_unchanged_run_is_not_fetched_again(ledger):
    client = FakeClient(runs())
    forecasts = Forecasts(client, ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    client.fetched.clear()
    forecasts.refresh([KLGA], NOW + 900)
    assert client.fetched == []


def test_only_the_model_with_a_new_run_is_fetched(ledger):
    client = FakeClient(runs())
    forecasts = Forecasts(client, ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    client.fetched.clear()
    client.runs["gfs025"] += 6 * HOUR
    forecasts.refresh([KLGA], NOW + 900)
    assert client.fetched == [("gfs025", "KLGA")]


def test_for_station_pools_both_models(ledger):
    forecasts = Forecasts(FakeClient(runs()), ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    got = forecasts.for_station("KLGA", NOW)
    assert got.members.shape == (82, 2)
    assert got.runs == {"gfs025": NOW - HOUR, "ecmwf_ifs025": NOW - HOUR}


def test_for_station_says_which_members_came_from_which_model(ledger):
    forecasts = Forecasts(FakeClient(runs()), ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    got = forecasts.for_station("KLGA", NOW)
    assert got.rows == {"gfs025": slice(0, 31), "ecmwf_ifs025": slice(31, 82)}


def test_for_station_without_data_is_none(ledger):
    assert Forecasts(FakeClient(runs()), ledger, CFG).for_station("KLGA", NOW) is None


def test_stale_run_is_dropped_but_fresh_model_survives(ledger):
    client = FakeClient({"gfs025": NOW - HOUR, "ecmwf_ifs025": NOW - 19 * HOUR})
    forecasts = Forecasts(client, ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    got = forecasts.for_station("KLGA", NOW)
    assert list(got.runs) == ["gfs025"]
    assert got.members.shape[0] == 31


def test_everything_stale_gives_nothing(ledger):
    forecasts = Forecasts(FakeClient(runs(age_hours=19)), ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    assert forecasts.for_station("KLGA", NOW) is None


def test_failed_refresh_reuses_last_run_within_age_limit(ledger):
    client = FakeClient(runs())
    forecasts = Forecasts(client, ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    client.runs["gfs025"] += 6 * HOUR
    client.fail_fetch = 503
    errors = forecasts.refresh([KLGA], NOW + 900)
    assert errors == 1
    assert forecasts.for_station("KLGA", NOW + 900).runs["gfs025"] == NOW - HOUR


def test_meta_outage_is_counted_not_raised(ledger):
    client = FakeClient(runs())
    client.fail_meta = True
    assert Forecasts(client, ledger, CFG).refresh([KLGA], NOW) == 2


def test_rate_limit_stops_further_fetches_for_that_cycle(ledger):
    # Hammering a 429 only digs the hole deeper.
    client = FakeClient(runs())
    client.fail_fetch = 429
    errors = Forecasts(client, ledger, CFG).refresh([KLGA, KDEN], NOW)
    assert errors == 1
    assert client.attempts == 1


def test_server_error_moves_on_to_the_next_station(ledger):
    client = FakeClient(runs())
    client.fail_fetch = 503
    errors = Forecasts(client, ledger, CFG).refresh([KLGA, KDEN], NOW)
    assert errors == 4
    assert client.attempts == 4


def test_run_saved_later_is_invisible_to_an_earlier_moment(ledger):
    # A replay asks about the past, so the archive must not hand it the future.
    client = FakeClient(runs())
    forecasts = Forecasts(client, ledger, CFG)
    forecasts.refresh([KLGA], NOW)
    client.runs["gfs025"] += 6 * HOUR
    forecasts.refresh([KLGA], NOW + 6 * HOUR)

    assert forecasts.for_station("KLGA", NOW + 5 * HOUR).runs["gfs025"] == NOW - HOUR
    assert forecasts.for_station("KLGA", NOW + 6 * HOUR).runs["gfs025"] == NOW + 5 * HOUR
    assert forecasts.for_station("KLGA", NOW - 1) is None

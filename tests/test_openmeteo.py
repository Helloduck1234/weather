import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from weatherbot.config import Station
from weatherbot.http import Http
from weatherbot.openmeteo import (
    CUSTOMER_URL,
    FREE_URL,
    META_URL,
    SINGLE_RUNS_URL,
    OpenMeteo,
    Snapshot,
    decode_array,
    encode_array,
    parse_forecast,
    pool,
)

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = Station("KLGA", "NYC", "America/New_York", 40.77945, -73.88027, 9)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


def http():
    return Http(sleep=lambda _: None)


def test_parse_real_gefs_response_has_31_members():
    times, members = parse_forecast(load("om_gfs025.json"))
    assert members.shape == (31, 96)
    assert times[1] - times[0] == 3600


def test_parse_real_ecmwf_response_has_51_members():
    _, members = parse_forecast(load("om_ecmwf_ifs025.json"))
    assert members.shape == (51, 96)


def test_parse_accepts_model_suffixed_keys():
    raw = {"hourly": {
        "time": [0, 3600],
        "temperature_2m_ncep_gefs025": [1.0, 2.0],
        "temperature_2m_member01_ncep_gefs025": [3.0, 4.0],
    }}
    assert parse_forecast(raw)[1].tolist() == [[1.0, 2.0], [3.0, 4.0]]


def test_parse_turns_nulls_into_nan():
    raw = {"hourly": {"time": [0, 3600], "temperature_2m": [1.0, None]}}
    assert np.isnan(parse_forecast(raw)[1][0, 1])


def test_parse_ignores_other_variables():
    raw = {"hourly": {"time": [0], "temperature_2m": [1.0], "precipitation": [9.0]}}
    assert parse_forecast(raw)[1].shape == (1, 1)


@respx.mock
def test_fetch_asks_for_unix_time_and_station_elevation():
    route = respx.get(FREE_URL).mock(return_value=httpx.Response(200, json=load("om_gfs025.json")))
    OpenMeteo(http()).fetch("gfs025", KLGA, 1793383200)
    params = route.calls.last.request.url.params
    assert params["models"] == "gfs025"
    assert params["hourly"] == "temperature_2m"
    assert params["timeformat"] == "unixtime"
    assert params["timezone"] == "auto"
    assert params["elevation"] == "9"
    assert params["latitude"] == "40.77945"
    assert "apikey" not in params


@respx.mock
def test_fetch_keeps_run_time_on_snapshot():
    respx.get(FREE_URL).mock(return_value=httpx.Response(200, json=load("om_gfs025.json")))
    snapshot = OpenMeteo(http()).fetch("gfs025", KLGA, 1793383200)
    assert (snapshot.model, snapshot.init_time) == ("gfs025", 1793383200)


@respx.mock
def test_api_key_switches_to_commercial_host():
    route = respx.get(CUSTOMER_URL).mock(return_value=httpx.Response(200, json=load("om_gfs025.json")))
    OpenMeteo(http(), api_key="k").fetch("gfs025", KLGA, 1)
    assert route.calls.last.request.url.params["apikey"] == "k"


@pytest.mark.parametrize(
    "model, path, fixture, init",
    [
        ("gfs025", "ncep_gefs025", "om_meta_gefs.json", 1793383200),
    ],
)
@respx.mock
def test_latest_run_reads_model_meta(model, path, fixture, init):
    respx.get(f"{META_URL}/{path}/static/meta.json").mock(return_value=httpx.Response(200, json=load(fixture)))
    assert OpenMeteo(http()).latest_run(model) == init


@respx.mock
def test_latest_run_maps_ecmwf_to_its_data_path():
    route = respx.get(f"{META_URL}/ecmwf_ifs025_ensemble/static/meta.json").mock(
        return_value=httpx.Response(200, json=load("om_meta_ecmwf.json"))
    )
    OpenMeteo(http()).latest_run("ecmwf_ifs025")
    assert route.called


def test_pool_stacks_members_on_a_shared_clock():
    a = Snapshot("gfs025", 1, np.array([0, 3600]), np.array([[1.0, 2.0]]))
    b = Snapshot("ecmwf_ifs025", 2, np.array([3600, 7200]), np.array([[3.0, 4.0], [5.0, 6.0]]))
    times, members = pool([a, b])
    assert times.tolist() == [0, 3600, 7200]
    assert members.shape == (3, 3)
    assert members[0, 1] == 2.0
    assert np.isnan(members[0, 2])
    assert np.isnan(members[1, 0])
    assert members[2, 2] == 6.0


def test_array_codec_keeps_tenths_and_nan():
    raw = np.array([[12.34, np.nan], [-40.0, 0.05]])
    back = decode_array(encode_array(raw), raw.shape)
    assert back[0, 0] == pytest.approx(12.3)
    assert np.isnan(back[0, 1])
    assert back[1, 0] == -40.0


@respx.mock
def test_single_run_reads_one_past_run_as_one_member():
    raw = json.loads((FIXTURES / "om_single_run.json").read_text(encoding="utf8"))
    route = respx.get(SINGLE_RUNS_URL).mock(return_value=httpx.Response(200, json=raw))
    snapshot = OpenMeteo(http()).single_run("ecmwf_ifs025", KLGA, 1792454400)  # 2026-10-20 00:00 UTC
    params = route.calls.last.request.url.params
    assert params["run"] == "2026-10-20T00:00"
    assert params["models"] == "ecmwf_ifs025"
    assert params["timeformat"] == "unixtime"
    assert params["elevation"] == "9"
    assert snapshot.init_time == 1792454400
    assert snapshot.members.shape == (1, 120)
    assert snapshot.times[0] == 1792454400
    assert snapshot.members[0, 0] == 11.7

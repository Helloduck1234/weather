import copy
import json
from datetime import date
from pathlib import Path

import pytest

from weatherbot.core import Bucket, Rule
from weatherbot.fees import FeeSchedule
from weatherbot.rules import (
    SkipEvent,
    parse_bucket_label,
    parse_gamma_event,
    parse_us_event,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


@pytest.fixture
def gamma():
    return {e["title"].split(" on ")[0].split(" in ")[-1]: e for e in load("gamma_open_events.json")}


@pytest.fixture
def us():
    return {e["slug"]: e for e in load("us_events.json")}


@pytest.mark.parametrize(
    "label, bucket, unit",
    [
        ("52-53°F", Bucket(52, 53), "F"),
        ("51°F or below", Bucket(None, 51), "F"),
        ("70°F or higher", Bucket(70, None), "F"),
        ("9°C", Bucket(9, 9), "C"),
        ("-3°C", Bucket(-3, -3), "C"),
        ("-3°C or below", Bucket(None, -3), "C"),
        ("18°C or higher", Bucket(18, None), "C"),
        ("-4--3°F", Bucket(-4, -3), "F"),
        ("-1-0°F", Bucket(-1, 0), "F"),
    ],
)
def test_parse_bucket_label(label, bucket, unit):
    assert parse_bucket_label(label) == (bucket, unit)


@pytest.mark.parametrize("label", ["", "Other", "52 to 53", "warm"])
def test_parse_bucket_label_rejects_unknown(label):
    with pytest.raises(SkipEvent):
        parse_bucket_label(label)


def test_real_fahrenheit_event(gamma):
    e = parse_gamma_event(gamma["Atlanta"])
    assert e.venue == "intl"
    assert e.day == date(2026, 11, 1)
    assert e.rule == Rule("metar_routine", "KATL", "F", "local_dst", "routine")
    assert [m.bucket for m in e.markets][:3] == [Bucket(None, 57), Bucket(58, 59), Bucket(60, 61)]
    assert e.markets[-1].bucket == Bucket(76, None)
    assert len(e.markets) == 11


def test_real_celsius_event(gamma):
    e = parse_gamma_event(gamma["Toronto"])
    assert e.rule == Rule("metar_routine", "CYYZ", "C", "local_dst", "routine")
    assert e.markets[0].bucket.lo is None
    assert e.markets[-1].bucket.hi is None
    assert all(m.bucket.lo == m.bucket.hi for m in e.markets[1:-1])


def test_market_fields_come_from_each_market(gamma):
    m = parse_gamma_event(gamma["Atlanta"]).markets[0]
    assert m.market_id == "0x2f2ab2ba4dc717f53226d0cb5bbd159cfbe7abea04006b57f719759471598c62"
    assert m.token.startswith("7251988657")
    assert (m.tick, m.min_size) == (0.001, 5.0)
    assert m.fee == FeeSchedule(rate=0, exponent=1, rebate=0)
    assert m.tradable


def test_yes_token_follows_outcome_order(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    raw["markets"][0]["outcomes"] = '["No", "Yes"]'
    assert parse_gamma_event(raw).markets[0].token.startswith("2137304828")


def test_market_without_fee_schedule_uses_fallback(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    del raw["markets"][0]["feeSchedule"]
    fee = parse_gamma_event(raw).markets[0].fee
    assert (fee.rate, fee.source) == (0.05, "fallback")


def test_not_accepting_orders_is_not_tradable(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    raw["markets"][0]["acceptingOrders"] = False
    assert not parse_gamma_event(raw).markets[0].tradable


def test_lowest_temperature_event_is_skipped(gamma):
    with pytest.raises(SkipEvent, match="not_high_temperature"):
        parse_gamma_event(gamma["Mexico City"])


def test_hong_kong_observatory_is_unsupported(gamma):
    with pytest.raises(SkipEvent, match="unsupported_source"):
        parse_gamma_event(gamma["Hong Kong"])


def test_market_shell_without_tokens_is_skipped(gamma):
    with pytest.raises(SkipEvent, match="no_markets"):
        parse_gamma_event(gamma["Zhengzhou"])


def test_gap_in_buckets_is_skipped(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    del raw["markets"][4]
    with pytest.raises(SkipEvent, match="buckets_not_contiguous"):
        parse_gamma_event(raw)


def test_missing_open_ended_bucket_is_skipped(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    del raw["markets"][0]
    with pytest.raises(SkipEvent, match="buckets_not_contiguous"):
        parse_gamma_event(raw)


def test_station_disagreement_is_skipped(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    for m in raw["markets"]:
        m["resolutionSource"] = m["resolutionSource"].replace("KATL", "KPDK")
    with pytest.raises(SkipEvent, match="station_mismatch"):
        parse_gamma_event(raw)


def test_unit_disagreement_is_skipped(gamma):
    raw = copy.deepcopy(gamma["Atlanta"])
    for m in raw["markets"]:
        m["description"] = m["description"].replace("whole degrees Fahrenheit", "whole degrees Celsius")
    with pytest.raises(SkipEvent, match="unit_mismatch"):
        parse_gamma_event(raw)


def legacy(gamma, source, description):
    raw = copy.deepcopy(gamma["Atlanta"])
    for m in raw["markets"]:
        m["resolutionSource"] = source
        m["description"] = description
    return raw


def test_wunderground_family(gamma):
    raw = legacy(
        gamma,
        "https://www.wunderground.com/history/daily/us/co/aurora/KBKF",
        "highest temperature recorded at the Buckley Space Force Base Station in degrees Fahrenheit",
    )
    assert parse_gamma_event(raw).rule == Rule("wunderground", "KBKF", "F", "local_dst", "all")


def test_noaa_timeseries_family(gamma):
    raw = legacy(gamma, "https://www.weather.gov/wrh/timeseries?site=mpmg", "degrees Fahrenheit")
    assert parse_gamma_event(raw).rule == Rule("noaa_timeseries", "MPMG", "F", "local_dst", "all")


def test_event_with_no_station_is_skipped(gamma):
    raw = legacy(gamma, "", "highest temperature recorded somewhere")
    with pytest.raises(SkipEvent, match="unsupported_source"):
        parse_gamma_event(raw)


def test_real_us_event(us):
    e = parse_us_event(us["temp-phlhigh-2026-11-01"])
    assert e.venue == "us"
    assert e.day == date(2026, 11, 1)
    assert e.rule == Rule("nws_cli", "KPHL", "F", "local_standard", "nws_cli")
    assert [m.bucket for m in e.markets] == [
        Bucket(None, 61), Bucket(62, 63), Bucket(64, 65),
        Bucket(66, 67), Bucket(68, 69), Bucket(70, None),
    ]


def test_us_market_fields(us):
    m = parse_us_event(us["temp-phlhigh-2026-11-01"]).markets[0]
    assert m.market_id == m.token == "tc-temp-phlhigh-2026-11-01-lt62f"
    assert (m.tick, m.min_size) == (0.01, 0.01)
    assert m.fee.rate == 0.0695
    assert m.fee.rebate == 0
    assert m.tradable


def test_us_low_event_is_skipped(us):
    with pytest.raises(SkipEvent, match="not_high_temperature"):
        parse_us_event(us["temp-mdwlow-2026-11-01"])


def test_us_event_without_station_is_skipped(us):
    raw = copy.deepcopy(us["temp-phlhigh-2026-11-01"])
    for m in raw["markets"]:
        m["description"] = m["description"].replace(" (KPHL)", "")
    with pytest.raises(SkipEvent, match="no_station"):
        parse_us_event(raw)


def test_us_title_must_agree_with_description(us):
    raw = copy.deepcopy(us["temp-phlhigh-2026-11-01"])
    raw["markets"][1]["description"] = raw["markets"][1]["description"].replace("62F and 63F", "62F and 64F")
    with pytest.raises(SkipEvent, match="bucket_mismatch"):
        parse_us_event(raw)


def test_us_market_not_open_is_not_tradable(us):
    raw = copy.deepcopy(us["temp-phlhigh-2026-11-01"])
    raw["markets"][0]["status"] = "MARKET_STATUS_HALTED"
    assert not parse_us_event(raw).markets[0].tradable

"""Turn raw venue events into typed events, or refuse them with a reason.

A market is only worth pricing if we know exactly what settles it, so anything
ambiguous raises SkipEvent rather than guessing.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from .core import Bucket, Event, Market, Rule
from .fees import FALLBACK, FeeSchedule

_RANGE = re.compile(r"^(-?\d+)-(-?\d+)°([CF])$")
_SINGLE = re.compile(r"^(-?\d+)°([CF])$")
_OPEN = re.compile(r"^(-?\d+)°([CF]) or (below|higher)$")
_ICAO_IN_TEXT = re.compile(r"\(([A-Z][A-Z0-9]{3})\)")
_INTL_DAY = re.compile(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$")
_UNIT_IN_TEXT = (
    re.compile(r"states its range in whole degrees (Fahrenheit|Celsius)"),
    re.compile(r"\bin degrees (Fahrenheit|Celsius)"),
)
_WUNDERGROUND_ICAO = re.compile(r"/history/daily/.*/([A-Za-z0-9]{4})/?$")

_US_DAY = re.compile(r"-(\d{4})-(\d{2})-(\d{2})$")
_US_RANGE = re.compile(r"^(-?\d+) to (-?\d+)$")
_US_LOW = re.compile(r"^(-?\d+) or below$")
_US_HIGH = re.compile(r"^(-?\d+) or above$")
_US_BOUND = re.compile(r"(-?\d+)F\b")
_US_DEFAULT_FEE = FeeSchedule(rate=0.0695, exponent=1.0, rebate=0.0, source="fallback")


class SkipEvent(Exception):
    """This event cannot be priced. The message is a short reason code."""


def parse_bucket_label(label: str) -> tuple[Bucket, str]:
    text = (label or "").strip()
    if m := _RANGE.match(text):
        return Bucket(int(m[1]), int(m[2])), m[3]
    if m := _SINGLE.match(text):
        return Bucket(int(m[1]), int(m[1])), m[2]
    if m := _OPEN.match(text):
        degree = int(m[1])
        bucket = Bucket(None, degree) if m[3] == "below" else Bucket(degree, None)
        return bucket, m[2]
    raise SkipEvent(f"bad_bucket_label:{text}")


def parse_gamma_event(raw: dict) -> Event:
    title = raw.get("title") or ""
    if not title.startswith("Highest temperature in "):
        raise SkipEvent("not_high_temperature")
    slug = raw.get("slug") or ""
    day = _intl_day(slug)

    usable = [m for m in raw.get("markets") or [] if m.get("clobTokenIds") and m.get("conditionId")]
    if not usable:
        raise SkipEvent("no_markets")

    parsed = []
    units = set()
    for item in usable:
        bucket, unit = parse_bucket_label(item.get("groupItemTitle"))
        units.add(unit)
        parsed.append(_gamma_market(item, bucket))
    if len(units) != 1:
        raise SkipEvent("unit_mismatch")
    unit = units.pop()

    rule = _gamma_rule(usable, unit)
    return Event("intl", str(raw.get("id")), slug, title, day, rule, _ordered(parsed))


def _intl_day(slug: str) -> date:
    m = _INTL_DAY.search(slug)
    if not m:
        raise SkipEvent("bad_date")
    try:
        return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%B %d %Y").date()
    except ValueError:
        raise SkipEvent("bad_date") from None


def _gamma_market(raw: dict, bucket: Bucket) -> Market:
    tokens = json.loads(raw["clobTokenIds"])
    outcomes = json.loads(raw.get("outcomes") or '["Yes", "No"]')
    tradable = bool(
        raw.get("acceptingOrders")
        and raw.get("enableOrderBook")
        and raw.get("active")
        and not raw.get("closed")
    )
    return Market(
        market_id=raw["conditionId"],
        label=raw["groupItemTitle"],
        bucket=bucket,
        token=tokens[outcomes.index("Yes")],
        tick=float(raw.get("orderPriceMinTickSize") or 0.01),
        min_size=float(raw.get("orderMinSize") or 5),
        fee=_gamma_fee(raw.get("feeSchedule")),
        tradable=tradable,
    )


def _gamma_fee(schedule: dict | None) -> FeeSchedule:
    if not schedule:
        return FALLBACK
    return FeeSchedule(
        rate=float(schedule.get("rate", 0)),
        exponent=float(schedule.get("exponent", 1)),
        rebate=float(schedule.get("rebateRate", 0)),
    )


def _gamma_rule(markets: list[dict], unit: str) -> Rule:
    first = markets[0]
    source = first.get("resolutionSource") or ""
    text = first.get("description") or ""
    family, station = _source_station(source)

    named = set(_ICAO_IN_TEXT.findall(text))
    if named and station not in named:
        raise SkipEvent("station_mismatch")

    if _unit_in_text(text) not in (None, unit):
        raise SkipEvent("unit_mismatch")

    policy = "routine" if family == "metar_routine" else "all"
    return Rule(family, station, unit, "local_dst", policy)


def _source_station(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    query = parse_qs(parsed.query)
    if host == "aviationweather.gov" and query.get("ids"):
        return "metar_routine", query["ids"][0].upper()
    if host == "weather.gov" and query.get("site"):
        return "noaa_timeseries", query["site"][0].upper()
    if host == "wunderground.com":
        m = _WUNDERGROUND_ICAO.search(parsed.path)
        if m:
            return "wunderground", m[1].upper()
    raise SkipEvent("unsupported_source")


def _unit_in_text(text: str) -> str | None:
    for pattern in _UNIT_IN_TEXT:
        if m := pattern.search(text):
            return m[1][0]
    return None


def parse_us_event(raw: dict) -> Event:
    title = raw.get("title") or ""
    if not title.startswith("Highest temperature in "):
        raise SkipEvent("not_high_temperature")
    slug = raw.get("slug") or ""
    m = _US_DAY.search(slug)
    if not m:
        raise SkipEvent("bad_date")
    day = date(int(m[1]), int(m[2]), int(m[3]))

    items = raw.get("markets") or []
    if not items:
        raise SkipEvent("no_markets")

    stations = set()
    parsed = []
    for item in items:
        text = item.get("description") or ""
        if "Climatological Report" not in text:
            raise SkipEvent("unsupported_source")
        stations.update(_ICAO_IN_TEXT.findall(text))
        parsed.append(_us_market(item, text))
    if not stations:
        raise SkipEvent("no_station")
    if len(stations) != 1:
        raise SkipEvent("station_mismatch")

    rule = Rule("nws_cli", stations.pop(), "F", "local_standard", "nws_cli")
    return Event("us", slug, slug, title, day, rule, _ordered(parsed))


def _us_market(raw: dict, text: str) -> Market:
    bucket = _us_bucket(raw.get("title") or "")
    bounds = [int(x) for x in _US_BOUND.findall(text)]
    if bounds != [x for x in (bucket.lo, bucket.hi) if x is not None]:
        raise SkipEvent("bucket_mismatch")
    fee = _US_DEFAULT_FEE
    if raw.get("feeCoefficient") is not None:
        fee = FeeSchedule(rate=float(raw["feeCoefficient"]), exponent=1.0, rebate=0.0)
    tradable = bool(
        raw.get("status") == "MARKET_STATUS_OPEN"
        and raw.get("active")
        and not raw.get("closed")
        and not raw.get("archived")
    )
    return Market(
        market_id=raw["slug"],
        label=raw.get("title") or "",
        bucket=bucket,
        token=raw["slug"],
        tick=float(raw.get("orderPriceMinTickSize") or 0.01),
        min_size=float(raw.get("minimumTradeQty") or 1),
        fee=fee,
        tradable=tradable,
    )


def _us_bucket(title: str) -> Bucket:
    text = title.strip()
    if m := _US_RANGE.match(text):
        return Bucket(int(m[1]), int(m[2]))
    if m := _US_LOW.match(text):
        return Bucket(None, int(m[1]))
    if m := _US_HIGH.match(text):
        return Bucket(int(m[1]), None)
    raise SkipEvent(f"bad_bucket_label:{text}")


def _ordered(markets: list[Market]) -> tuple[Market, ...]:
    """Sort low to high and require full, gap-free coverage of the number line."""
    ordered = sorted(markets, key=lambda m: (m.bucket.lo is not None, m.bucket.lo or 0))
    buckets = [m.bucket for m in ordered]
    complete = buckets[0].lo is None and buckets[-1].hi is None
    for a, b in zip(buckets, buckets[1:]):
        if a.hi is None or b.lo is None or a.hi + 1 != b.lo:
            complete = False
    if not complete:
        raise SkipEvent("buckets_not_contiguous")
    return tuple(ordered)

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from weatherbot.core import Bucket, Market
from weatherbot.fees import FeeSchedule
from weatherbot.http import Http, HttpError
from weatherbot.rules import parse_gamma_event, parse_us_event
from weatherbot.venues import parse_all
from weatherbot.venues.intl import CLOB, GAMMA, IntlVenue
from weatherbot.venues.us import GATEWAY, UsVenue

FIXTURES = Path(__file__).parent / "fixtures"
NOW = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


def http():
    return Http(sleep=lambda _: None)


def token_market(token):
    return Market("m", "x", Bucket(1, 1), token, 0.01, 5, FeeSchedule(0))


def mock_tags(tag_id=103040):
    respx.get(f"{GAMMA}/tags/slug/daily-temperature").mock(
        return_value=httpx.Response(200, json={"id": str(tag_id), "slug": "daily-temperature"})
    )


@respx.mock
def test_intl_discovers_tag_at_runtime_and_sorts_newest_first():
    mock_tags(555)
    route = respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(200, json=[]))
    IntlVenue(http()).open_events(NOW)
    params = route.calls.last.request.url.params
    assert params["tag_id"] == "555"
    assert params["order"] == "endDate"
    assert params["ascending"] == "false"
    assert params["active"] == "true"
    assert params["closed"] == "false"


@respx.mock
def test_intl_falls_back_to_weather_tag():
    respx.get(f"{GAMMA}/tags/slug/daily-temperature").mock(return_value=httpx.Response(404))
    respx.get(f"{GAMMA}/tags/slug/weather").mock(return_value=httpx.Response(200, json={"id": "84"}))
    route = respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(200, json=[]))
    IntlVenue(http()).open_events(NOW)
    assert route.calls.last.request.url.params["tag_id"] == "84"


@respx.mock
def test_intl_parses_real_events_and_counts_skips():
    mock_tags()
    raw = load("gamma_open_events.json")
    respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(200, json=raw))
    events, skips = IntlVenue(http()).open_events(NOW)
    assert [e.rule.station for e in events] == ["KATL", "CYYZ"]
    assert skips == {"not_high_temperature": 1, "unsupported_source": 1, "no_markets": 1}


def real_event(fixture, parse):
    return next(e for e in load(fixture) if parse_all([e], parse)[0])


@pytest.mark.parametrize(
    "field, value",
    [
        ("clobTokenIds", "not json"),
        ("clobTokenIds", "[]"),
        ("outcomes", '["Up", "Down"]'),
        ("orderPriceMinTickSize", "n/a"),
        ("feeSchedule", {"rate": "free"}),
    ],
)
def test_malformed_intl_market_costs_only_its_own_event(field, value):
    good = real_event("gamma_open_events.json", parse_gamma_event)
    broken = copy.deepcopy(good)
    broken["markets"][0][field] = value
    events, skips = parse_all([broken, good], parse_gamma_event)
    assert len(events) == 1
    assert skips == {"parse_error": 1}


@pytest.mark.parametrize(
    "field, value", [("feeCoefficient", "n/a"), ("orderPriceMinTickSize", "n/a"), ("minimumTradeQty", "n/a")]
)
def test_malformed_us_market_costs_only_its_own_event(field, value):
    good = real_event("us_events.json", parse_us_event)
    broken = copy.deepcopy(good)
    broken["markets"][0][field] = value
    events, skips = parse_all([broken, good], parse_us_event)
    assert len(events) == 1
    assert skips == {"parse_error": 1}


def test_impossible_us_date_costs_only_its_own_event():
    good = real_event("us_events.json", parse_us_event)
    broken = dict(good, slug="temp-phlhigh-2026-13-45")
    events, skips = parse_all([broken, good], parse_us_event)
    assert len(events) == 1
    assert skips == {"parse_error": 1}


@respx.mock
def test_garbled_end_date_does_not_stop_the_listing():
    mock_tags()
    page = [{"title": "x", "endDate": "soon"}] * 100
    respx.get(f"{GAMMA}/events").mock(side_effect=[httpx.Response(200, json=page), httpx.Response(200, json=[])])
    assert IntlVenue(http()).open_events(NOW)[0] == []


@respx.mock
def test_intl_paginates_until_events_are_stale():
    mock_tags()
    full = [{"title": "x", "endDate": "2026-11-01T12:00:00Z"}] * 100
    stale = [{"title": "x", "endDate": "2026-05-21T12:00:00Z"}] * 100
    route = respx.get(f"{GAMMA}/events").mock(
        side_effect=[httpx.Response(200, json=full), httpx.Response(200, json=stale)]
    )
    IntlVenue(http()).open_events(NOW)
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "100"]


@respx.mock
def test_intl_books_match_by_asset_id_and_normalise_order():
    raw = load("clob_book.json")
    other = dict(raw, asset_id="222", bids=[{"price": "0.2", "size": "1"}], asks=[])
    route = respx.post(f"{CLOB}/books").mock(return_value=httpx.Response(200, json=[other, raw]))
    books = IntlVenue(http()).books([token_market(raw["asset_id"]), token_market("222")])
    assert books[raw["asset_id"]].best_bid == 0.13
    assert books[raw["asset_id"]].best_ask == 0.15
    assert books["222"].best_ask is None
    assert json.loads(route.calls.last.request.content)[0] == {"token_id": raw["asset_id"]}


@respx.mock
def test_intl_books_are_requested_in_chunks():
    route = respx.post(f"{CLOB}/books").mock(return_value=httpx.Response(200, json=[]))
    IntlVenue(http(), book_chunk=2).books([token_market(str(i)) for i in range(5)])
    assert [len(json.loads(c.request.content)) for c in route.calls] == [2, 2, 1]


@respx.mock
def test_intl_outcomes_from_real_closed_event():
    raw = load("gamma_closed_events.json")[0]
    respx.get(f"{GAMMA}/events/{raw['id']}").mock(return_value=httpx.Response(200, json=raw))
    outcomes = IntlVenue(http()).outcomes(raw["id"], [])
    assert sorted(outcomes.values()) == [0.0] * (len(raw["markets"]) - 1) + [1.0]
    assert set(outcomes) == {m["conditionId"] for m in raw["markets"]}


@respx.mock
def test_intl_unresolved_market_has_no_outcome():
    raw = load("gamma_closed_events.json")[0]
    raw["markets"][0]["umaResolutionStatus"] = "proposed"
    respx.get(f"{GAMMA}/events/{raw['id']}").mock(return_value=httpx.Response(200, json=raw))
    outcomes = IntlVenue(http()).outcomes(raw["id"], [])
    assert raw["markets"][0]["conditionId"] not in outcomes


@respx.mock
def test_us_filters_to_climate_and_parses_real_events():
    raw = load("us_events.json")
    route = respx.get(f"{GATEWAY}/v1/events").mock(
        side_effect=[httpx.Response(200, json={"events": raw}), httpx.Response(200, json={"events": []})]
    )
    events, skips = UsVenue(http()).open_events(NOW)
    assert route.calls[0].request.url.params["categories"] == "climate"
    assert {e.slug for e in events} == {"temp-phlhigh-2026-11-01", "temp-phlhigh-2026-10-30"}
    assert skips == {"not_high_temperature": 1}


@respx.mock
def test_us_book_from_real_response():
    raw = load("us_book.json")
    slug = raw["marketData"]["marketSlug"]
    respx.get(f"{GATEWAY}/v1/markets/{slug}/book").mock(return_value=httpx.Response(200, json=raw))
    book = UsVenue(http()).books([token_market(slug)])[slug]
    assert book.best_bid == 0.10
    assert [x.size for x in book.bids] == [30.0, 5.0]
    assert book.best_ask is None


@respx.mock
def test_us_book_offers_are_parsed():
    raw = {"marketData": {"bids": [], "offers": [
        {"px": {"value": "0.60", "currency": "USD"}, "qty": "2.0000"},
        {"px": {"value": "0.55", "currency": "USD"}, "qty": "3.0000"},
    ]}}
    respx.get(f"{GATEWAY}/v1/markets/a/book").mock(return_value=httpx.Response(200, json=raw))
    assert UsVenue(http()).books([token_market("a")])["a"].best_ask == 0.55


@respx.mock
def test_us_failed_book_is_left_out():
    respx.get(f"{GATEWAY}/v1/markets/a/book").mock(return_value=httpx.Response(404))
    assert UsVenue(http()).books([token_market("a")]) == {}


@respx.mock
def test_us_missing_books_are_no_outage():
    """A 404 means the exchange answered, and that market has no book."""
    respx.get(url__regex=rf"{GATEWAY}/v1/markets/.*/book").mock(return_value=httpx.Response(404))
    assert UsVenue(http()).books([token_market(slug) for slug in "abcd"]) == {}


@respx.mock
def test_us_book_outage_stops_the_walk():
    """Each book would burn its own retries, and a hundred of those would still be going at the next run."""
    route = respx.get(url__regex=rf"{GATEWAY}/v1/markets/.*/book").mock(return_value=httpx.Response(503))
    with pytest.raises(HttpError):
        UsVenue(http()).books([token_market(slug) for slug in "abcd"])
    assert route.call_count == 12


@respx.mock
def test_us_failing_book_costs_only_that_market():
    raw = load("us_book.json")
    for slug in "ac":
        respx.get(f"{GATEWAY}/v1/markets/{slug}/book").mock(return_value=httpx.Response(200, json=raw))
    respx.get(f"{GATEWAY}/v1/markets/b/book").mock(return_value=httpx.Response(500))
    assert set(UsVenue(http()).books([token_market(slug) for slug in "abc"])) == {"a", "c"}


@respx.mock
def test_us_book_failures_apart_are_no_outage():
    raw = load("us_book.json")
    for slug in "abde":
        respx.get(f"{GATEWAY}/v1/markets/{slug}/book").mock(return_value=httpx.Response(500))
    for slug in "cf":
        respx.get(f"{GATEWAY}/v1/markets/{slug}/book").mock(return_value=httpx.Response(200, json=raw))
    assert set(UsVenue(http()).books([token_market(slug) for slug in "abcdef"])) == {"c", "f"}


@respx.mock
def test_us_listing_that_never_ends_is_an_error():
    route = respx.get(f"{GATEWAY}/v1/events").mock(return_value=httpx.Response(200, json={"events": [{}] * 100}))
    with pytest.raises(HttpError, match="never ended"):
        UsVenue(http()).open_events(NOW)
    assert route.call_count == 100


@respx.mock
def test_us_outcomes_read_settlement_endpoint():
    respx.get(f"{GATEWAY}/v1/markets/a/settlement").mock(
        return_value=httpx.Response(200, json={"slug": "a", "settlement": 1})
    )
    respx.get(f"{GATEWAY}/v1/markets/b/settlement").mock(
        return_value=httpx.Response(200, json={"slug": "b", "settlement": 0})
    )
    respx.get(f"{GATEWAY}/v1/markets/c/settlement").mock(return_value=httpx.Response(404))
    assert UsVenue(http()).outcomes("event", ["a", "b", "c"]) == {"a": 1.0, "b": 0.0}


@respx.mock
def test_us_outcome_from_real_settlement_reply():
    raw = load("us_settlement.json")
    respx.get(f"{GATEWAY}/v1/markets/{raw['slug']}/settlement").mock(return_value=httpx.Response(200, json=raw))
    assert UsVenue(http()).outcomes("event", [raw["slug"]]) == {raw["slug"]: 1.0}


@respx.mock
def test_us_reply_with_no_settlement_yet_has_no_outcome():
    respx.get(f"{GATEWAY}/v1/markets/a/settlement").mock(return_value=httpx.Response(200, json={"slug": "a"}))
    assert UsVenue(http()).outcomes("event", ["a"]) == {}


@respx.mock
@pytest.mark.parametrize("value", [100, -1, "paid", {"px": 1}])
def test_us_settlement_off_the_zero_to_one_scale_is_refused(value):
    # Paying out on a misread scale would book a wild profit, so it's an error instead of a number.
    reply = {"slug": "a", "settlement": value}
    respx.get(f"{GATEWAY}/v1/markets/a/settlement").mock(return_value=httpx.Response(200, json=reply))
    with pytest.raises(HttpError):
        UsVenue(http()).outcomes("event", ["a"])


@respx.mock
def test_us_closed_events_parse_a_resolved_event():
    route = respx.get(f"{GATEWAY}/v1/events").mock(
        return_value=httpx.Response(200, json=load("us_closed_events.json"))
    )
    events, _ = UsVenue(http()).closed_events(0)
    assert [e.slug for e in events] == ["temp-phlhigh-2026-10-28"]
    assert events[0].rule.station == "KPHL"
    assert len(events[0].markets) == 6
    params = route.calls.last.request.url.params
    assert params["closed"] == "true"
    assert params["categories"] == "climate"


@respx.mock
def test_us_closed_events_read_every_page_because_the_gateway_ignores_sort_order():
    raw = load("us_closed_events.json")["events"][0]
    route = respx.get(f"{GATEWAY}/v1/events").mock(
        side_effect=[
            httpx.Response(200, json={"events": [raw] * 100}),
            httpx.Response(200, json={"events": [raw]}),
        ]
    )
    events, _ = UsVenue(http()).closed_events(0)
    assert len(events) == 101
    assert route.calls[1].request.url.params["offset"] == "100"


@respx.mock
def test_us_quote_history_reads_the_recorded_top_of_book():
    route = respx.get(f"{GATEWAY}/v1/price-history").mock(
        return_value=httpx.Response(200, json=load("us_quote_history.json"))
    )
    quotes = UsVenue(http()).quote_history(token_market("tc-temp"), 1793099000, 1793300000)
    assert quotes[0] == (1793099700, 0.25, 0.77)
    assert quotes[-1] == (1793299500, 0.97, 0.97)
    params = route.calls.last.request.url.params
    assert params["symbol"] == "tc-temp"
    assert params["fidelity"] == "15"
    assert params["timestamp.startTimestamp"] == "1793099000"
    assert params["timestamp.endTimestamp"] == "1793300000"


@respx.mock
def test_intl_closed_events_follow_the_cursor_until_they_pass_since():
    london_day, san_francisco_day = load("gamma_closed_events.json")[1], load("gamma_closed_events.json")[0]
    mock_tags()
    route = respx.get(f"{GAMMA}/events/keyset").mock(
        side_effect=[
            httpx.Response(200, json={"events": [london_day], "next_cursor": "c1"}),
            httpx.Response(200, json={"events": [san_francisco_day], "next_cursor": "c2"}),
        ]
    )
    since = int(datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp())
    events, _ = IntlVenue(http()).closed_events(since)
    assert len(events) == 2
    first, second = (call.request.url.params for call in route.calls)
    assert "after_cursor" not in first
    assert first["closed"] == "true"
    assert first["tag_id"] == "103040"
    assert second["after_cursor"] == "c1"


@respx.mock
def test_intl_closed_events_stop_when_no_cursor_comes_back():
    mock_tags()
    route = respx.get(f"{GAMMA}/events/keyset").mock(
        return_value=httpx.Response(200, json={"events": load("gamma_closed_events.json")})
    )
    IntlVenue(http()).closed_events(0)
    assert route.call_count == 1


@respx.mock
def test_intl_quote_history_has_one_price_per_moment():
    route = respx.get(f"{CLOB}/prices-history").mock(
        return_value=httpx.Response(200, json=load("clob_prices_history.json"))
    )
    quotes = IntlVenue(http()).quote_history(token_market("tok"), 1793100000, 1793102000)
    assert quotes == [(1793100627, 0.245, 0.245), (1793101523, 0.24, 0.24)]
    params = route.calls.last.request.url.params
    assert params["market"] == "tok"
    assert params["interval"] == "max"
    assert params["fidelity"] == "15"

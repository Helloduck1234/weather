import json
from pathlib import Path

import httpx
import respx

from weatherbot.aviation import METAR_URL, Aviation
from weatherbot.http import Http

FIXTURES = Path(__file__).parent / "fixtures"


def http():
    return Http(sleep=lambda _: None)


@respx.mock
def test_recent_parses_real_rows():
    rows = json.loads((FIXTURES / "metar_klga.json").read_text(encoding="utf8"))
    route = respx.get(METAR_URL).mock(return_value=httpx.Response(200, json=rows))
    reports = Aviation(http()).recent(["KLGA"])
    assert route.calls.last.request.url.params["format"] == "json"
    assert route.calls.last.request.url.params["hours"] == "3"
    assert reports[0].icao == "KLGA"
    assert len(reports) == len(rows)


@respx.mock
def test_recent_splits_ids_into_chunks_under_row_cap():
    route = respx.get(METAR_URL).mock(return_value=httpx.Response(200, json=[]))
    Aviation(http()).recent([f"K{i:03d}" for i in range(45)])
    sizes = [len(c.request.url.params["ids"].split(",")) for c in route.calls]
    assert sizes == [20, 20, 5]


@respx.mock
def test_backfill_uses_small_chunks_and_long_window():
    route = respx.get(METAR_URL).mock(return_value=httpx.Response(200, json=[]))
    Aviation(http()).backfill([f"K{i:03d}" for i in range(9)])
    sizes = [len(c.request.url.params["ids"].split(",")) for c in route.calls]
    assert sizes == [4, 4, 1]
    assert route.calls.last.request.url.params["hours"] == "36"


@respx.mock
def test_empty_response_means_no_reports():
    respx.get(METAR_URL).mock(return_value=httpx.Response(204))
    assert Aviation(http()).recent(["KLGA"]) == []


@respx.mock
def test_row_without_a_report_is_dropped():
    rows = [{"icaoId": "KLGA", "obsTime": 1, "rawOb": ""}, {"icaoId": "KLGA", "rawOb": "KLGA 01/01"}]
    respx.get(METAR_URL).mock(return_value=httpx.Response(200, json=rows))
    assert Aviation(http()).recent(["KLGA"]) == []


@respx.mock
def test_history_names_the_end_of_its_window():
    rows = json.loads((FIXTURES / "metar_klga.json").read_text(encoding="utf8"))
    route = respx.get(METAR_URL).mock(return_value=httpx.Response(200, json=rows))
    reports = Aviation(http()).history("KLGA", 1792454400, 120)  # 2026-10-20 00:00 UTC
    params = route.calls.last.request.url.params
    assert params["ids"] == "KLGA"
    assert params["date"] == "20261020_0000"
    assert params["hours"] == "120"
    assert len(reports) == len(rows)


@respx.mock
def test_history_older_than_the_archive_comes_back_empty():
    respx.get(METAR_URL).mock(return_value=httpx.Response(204))
    assert Aviation(http()).history("KLGA", 1792454400, 120) == []

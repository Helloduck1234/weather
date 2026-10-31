"""Aviation Weather Center METAR client.

The endpoint silently caps a response at 400 rows, so station lists are split
into chunks small enough that the cap can't truncate a station's history.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .http import Http
from .metar import Obs, obs_from_json

METAR_URL = "https://aviationweather.gov/api/data/metar"
_RECENT_HOURS = 3
_RECENT_CHUNK = 20
_BACKFILL_HOURS = 36
_BACKFILL_CHUNK = 4


class Aviation:
    def __init__(self, http: Http):
        self._http = http

    def recent(self, ids: list[str]) -> list[Obs]:
        return self._fetch(ids, _RECENT_HOURS, _RECENT_CHUNK)

    def backfill(self, ids: list[str]) -> list[Obs]:
        return self._fetch(ids, _BACKFILL_HOURS, _BACKFILL_CHUNK)

    def history(self, icao: str, end: int, hours: int) -> list[Obs]:
        """Reports from the `hours` before `end`. The archive goes back about 30 days."""
        stamp = datetime.fromtimestamp(end, timezone.utc).strftime("%Y%m%d_%H%M")
        return _parse(self._http.get(METAR_URL, {"ids": icao, "format": "json", "hours": hours, "date": stamp}))

    def _fetch(self, ids: list[str], hours: int, chunk: int) -> list[Obs]:
        reports: list[Obs] = []
        for i in range(0, len(ids), chunk):
            params = {"ids": ",".join(ids[i : i + chunk]), "format": "json", "hours": hours}
            reports.extend(_parse(self._http.get(METAR_URL, params)))
        return reports


def _parse(rows: list[dict] | None) -> list[Obs]:
    return [obs_from_json(r) for r in rows or [] if r.get("rawOb") and r.get("obsTime")]

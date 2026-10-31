"""Keep the forecast archive current and hand out pooled ensembles.

Open-Meteo's free tier allows about 10,000 weighted calls a day. Refreshing on
every cycle would use over 30,000, so a station is only refetched when a model
publishes a new run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .config import ForecastConfig, Station
from .http import HttpError
from .ledger import Ledger
from .openmeteo import pool


@dataclass(frozen=True)
class StationForecast:
    times: np.ndarray
    members: np.ndarray
    runs: dict[str, int]  # model -> init time
    rows: dict[str, slice]  # model -> its members


class Forecasts:
    def __init__(self, client, ledger: Ledger, cfg: ForecastConfig):
        self._client = client
        self._ledger = ledger
        self._cfg = cfg

    def refresh(self, stations: Iterable[Station], now: int) -> int:
        """Fetch any run we don't have yet. Returns how many calls failed."""
        stations = list(stations)
        errors = 0
        for model in self._cfg.models:
            try:
                latest = self._client.latest_run(model)
            except HttpError:
                errors += 1
                continue
            for station in stations:
                known = self._ledger.latest_init(station.icao, model)
                if known is not None and known >= latest:
                    continue
                try:
                    snapshot = self._client.fetch(model, station, latest)
                except HttpError as exc:
                    errors += 1
                    if exc.status == 429:
                        return errors
                    continue
                self._ledger.save_snapshot(station.icao, snapshot, now)
        return errors

    def for_station(self, icao: str, now: int) -> StationForecast | None:
        cutoff = now - self._cfg.max_age_hours * 3600
        snapshots = []
        for model in self._cfg.models:
            snapshot = self._ledger.latest_snapshot(icao, model, fetched_by=now)
            if snapshot is not None and snapshot.init_time >= cutoff:
                snapshots.append(snapshot)
        if not snapshots:
            return None
        times, members = pool(snapshots)
        rows = {}
        first = 0
        for s in snapshots:
            rows[s.model] = slice(first, first + s.members.shape[0])
            first = rows[s.model].stop
        return StationForecast(times, members, {s.model: s.init_time for s in snapshots}, rows)

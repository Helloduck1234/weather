"""Open-Meteo ensemble client and the compact form we archive runs in."""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .config import Station
from .http import Http

FREE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
CUSTOMER_URL = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
META_URL = "https://ensemble-api.open-meteo.com/data"
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
_META_PATHS = {"gfs025": "ncep_gefs025", "ecmwf_ifs025": "ecmwf_ifs025_ensemble"}
_MISSING = -32768


@dataclass(frozen=True)
class Snapshot:
    model: str
    init_time: int
    times: np.ndarray  # unix seconds
    members: np.ndarray  # Celsius, shape (members, times)


class OpenMeteo:
    def __init__(self, http: Http, api_key: str | None = None, past_days: int = 1, forecast_days: int = 5):
        self._http = http
        self._api_key = api_key
        self._past_days = past_days
        self._forecast_days = forecast_days

    def latest_run(self, model: str) -> int:
        meta = self._http.get(f"{META_URL}/{_META_PATHS[model]}/static/meta.json")
        return int(meta["last_run_initialisation_time"])

    def _params(self, model: str, station: Station) -> dict:
        return {
            "latitude": station.lat,
            "longitude": station.lon,
            "elevation": station.elev_m,
            "hourly": "temperature_2m",
            "models": model,
            "timezone": "auto",
            "timeformat": "unixtime",
            "forecast_days": self._forecast_days,
        }

    def fetch(self, model: str, station: Station, init_time: int) -> Snapshot:
        params = {**self._params(model, station), "past_days": self._past_days}
        url = FREE_URL
        if self._api_key:
            url = CUSTOMER_URL
            params["apikey"] = self._api_key
        times, members = parse_forecast(self._http.get(url, params))
        return Snapshot(model, init_time, times, members)

    def single_run(self, model: str, station: Station, init_time: int) -> Snapshot:
        """One past run of a deterministic model, which comes back as a single member."""
        run = datetime.fromtimestamp(init_time, timezone.utc).strftime("%Y-%m-%dT%H:%M")
        params = {**self._params(model, station), "run": run}
        times, members = parse_forecast(self._http.get(SINGLE_RUNS_URL, params))
        return Snapshot(model, init_time, times, members)


def parse_forecast(raw: dict) -> tuple[np.ndarray, np.ndarray]:
    hourly = raw["hourly"]
    times = np.array(hourly["time"], dtype=np.int64)
    keys = [k for k in hourly if k.startswith("temperature_2m")]
    return times, np.array([hourly[k] for k in keys], dtype=float)


def pool(snapshots: list[Snapshot]) -> tuple[np.ndarray, np.ndarray]:
    """Stack every member of every model on one time axis."""
    times = np.unique(np.concatenate([s.times for s in snapshots]))
    rows = []
    for s in snapshots:
        grid = np.full((s.members.shape[0], times.size), np.nan)
        grid[:, np.searchsorted(times, s.times)] = s.members
        rows.append(grid)
    return times, np.vstack(rows)


def encode_array(values: np.ndarray) -> bytes:
    tenths = np.where(np.isnan(values), _MISSING, np.round(values * 10)).astype(np.int16)
    return zlib.compress(tenths.tobytes(), 6)


def decode_array(blob: bytes, shape: tuple[int, int]) -> np.ndarray:
    tenths = np.frombuffer(zlib.decompress(blob), dtype=np.int16).reshape(shape).astype(float)
    tenths[tenths == _MISSING] = np.nan
    return tenths / 10

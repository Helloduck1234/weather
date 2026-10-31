"""Load and validate config.yaml. Typos in risk settings must fail loudly."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .model import ModelParams
from .strategy import StrategyParams

VENUES = ("us", "intl")
MODELS = ("gfs025", "ecmwf_ifs025")

_TINY = 1e-9
_ABOVE_ZERO = (_TINY, math.inf)
_NOT_NEGATIVE = (0.0, math.inf)
_SHARE = (_TINY, 1.0)
_STRICT_SHARE = (_TINY, 1.0 - _TINY)
_HOUR_OF_DAY = (0.0, 24.0)

# What each number may be, ends included. A flipped sign or a stray zero here turns into a wrong trade.
_RANGES = {
    "bankroll_usd": _ABOVE_ZERO,
    "max_obs_age_hours": _ABOVE_ZERO,
    "order_ttl_minutes": _ABOVE_ZERO,
    "strategy.edge_threshold": _STRICT_SHARE,
    "strategy.kelly_fraction": (0.10, 0.25),
    "strategy.min_price": _STRICT_SHARE,
    "strategy.max_price": _STRICT_SHARE,
    "strategy.max_trade_usd": _ABOVE_ZERO,
    "strategy.max_day_usd": _ABOVE_ZERO,
    "strategy.max_event_usd": _ABOVE_ZERO,
    "strategy.max_depth_share": _SHARE,
    "model.spread_inflation": _ABOVE_ZERO,
    "model.kernel_sigma_start_c": _ABOVE_ZERO,
    "model.kernel_sigma_end_c": _ABOVE_ZERO,
    "model.obs_start_hour": _HOUR_OF_DAY,
    "model.obs_full_hour": _HOUR_OF_DAY,
    "model.anchor_tau_hours": _ABOVE_ZERO,
    "model.edge_shift_c": _NOT_NEGATIVE,
    "model.prob_epsilon": (0.0, 1.0 - _TINY),
    "model.min_horizon_coverage": _SHARE,
    "forecast.max_age_hours": _ABOVE_ZERO,
    "bias.shrink_days": _NOT_NEGATIVE,
    "live.bankroll_usd": _ABOVE_ZERO,
    "live.daily_loss_limit_usd": _ABOVE_ZERO,
    "live.max_order_usd": _ABOVE_ZERO,
    "live.max_day_usd": _ABOVE_ZERO,
}
_WHOLE_NUMBERS = {
    "max_lead_days": 0,
    "forecast.past_days": 0,
    "forecast.forecast_days": 1,
    "bias.window_days": 1,
    "bias.min_days": 0,
}
_PATHS = ("db_path", "report_dir", "live.kill_file")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    tz: str
    lat: float
    lon: float
    elev_m: float


@dataclass(frozen=True)
class ForecastConfig:
    models: tuple[str, ...] = MODELS
    max_age_hours: float = 18.0
    past_days: int = 1
    forecast_days: int = 5


@dataclass(frozen=True)
class BiasConfig:
    window_days: int = 30
    shrink_days: float = 3.0
    min_days: int = 7


@dataclass(frozen=True)
class LiveConfig:
    jurisdiction_confirmed: bool = False
    bankroll_usd: float = 100.0
    daily_loss_limit_usd: float = 20.0
    max_order_usd: float = 10.0
    max_day_usd: float = 25.0
    kill_file: str = "data/KILL"


@dataclass(frozen=True)
class Config:
    venues: tuple[str, ...] = VENUES
    db_path: str = "data/weatherbot.sqlite"
    report_dir: str = "reports"
    bankroll_usd: float = 1000.0
    max_lead_days: int = 2
    max_obs_age_hours: float = 3.0
    order_ttl_minutes: float = 30.0
    strategy: StrategyParams = field(default_factory=StrategyParams)
    model: ModelParams = field(default_factory=ModelParams)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    bias: BiasConfig = field(default_factory=BiasConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    stations: dict[str, Station] = field(default_factory=dict)


def _section(cls, data: dict | None, name: str):
    data = _mapping(data, name)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown setting {name}.{sorted(unknown)[0]}")
    return cls(**data)


def _mapping(data: object, name: str) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{name} must be a mapping of settings")
    return data


def _names(data: object, name: str) -> tuple:
    if not isinstance(data, list):
        raise ConfigError(f"{name} must be a list")
    return tuple(data)


def _stations(data: dict | None) -> dict[str, Station]:
    stations = {}
    for icao, item in _mapping(data, "stations").items():
        try:
            ZoneInfo(item["tz"])
            lat, lon, elev_m = item["lat"], item["lon"], item["elev_m"]
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and -500 <= elev_m <= 9000):
                raise ValueError("coordinates out of range")
            stations[icao] = Station(icao, item["name"], item["tz"], lat, lon, elev_m)
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError(f"bad station {icao}: {exc}") from exc
    return stations


def _real(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _setting(cfg: Config, name: str):
    value = cfg
    for part in name.split("."):
        value = getattr(value, part)
    return value


def _validate(cfg: Config) -> None:
    for name, (low, high) in _RANGES.items():
        value = _setting(cfg, name)
        if not _real(value) or not low <= value <= high:
            raise ConfigError(f"{name} must be a number from {low} to {high}, not {value!r}")
    for name, low in _WHOLE_NUMBERS.items():
        value = _setting(cfg, name)
        if type(value) is not int or value < low:
            raise ConfigError(f"{name} must be a whole number of at least {low}, not {value!r}")
    for name in _PATHS:
        if not isinstance(_setting(cfg, name), str):
            raise ConfigError(f"{name} must be a path")

    if not isinstance(cfg.live.jurisdiction_confirmed, bool):
        raise ConfigError("live.jurisdiction_confirmed must be true or false")
    if not cfg.forecast.models or not all(model in MODELS for model in cfg.forecast.models):
        raise ConfigError(f"forecast.models must be chosen from {list(MODELS)}")
    for venue in cfg.venues:
        if venue not in VENUES:
            raise ConfigError(f"unknown venue {venue}")
    if cfg.strategy.execution not in ("maker_first", "taker"):
        raise ConfigError(f"unknown strategy.execution {cfg.strategy.execution}")

    if cfg.strategy.min_price >= cfg.strategy.max_price:
        raise ConfigError("strategy.min_price must be below strategy.max_price")
    if cfg.model.obs_start_hour >= cfg.model.obs_full_hour:
        raise ConfigError("model.obs_start_hour must be before model.obs_full_hour")
    gap = cfg.model.sensor_gap_f
    if not all(_real(odds) and odds >= 0 for odds in gap) or not math.isclose(sum(gap), 1.0):
        raise ConfigError("model.sensor_gap_f must be chances that add up to 1")
    live = cfg.live
    if live.max_order_usd > live.max_day_usd:
        raise ConfigError("live.max_order_usd can't be over live.max_day_usd")
    if live.max_day_usd > live.bankroll_usd:
        raise ConfigError("live.max_day_usd can't be over live.bankroll_usd")
    if live.daily_loss_limit_usd > live.bankroll_usd:
        raise ConfigError("live.daily_loss_limit_usd can't be over live.bankroll_usd")


def kill_file_path(path: str | Path) -> Path:
    """The kill switch a config that won't load names, as far as it can still be read."""
    base = Path(path).resolve().parent
    try:
        return base / yaml.safe_load(Path(path).read_text(encoding="utf8"))["live"]["kill_file"]
    except (OSError, UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError):
        return base / LiveConfig.kill_file


def load_config(path: str | Path) -> Config:
    raw = dict(_mapping(yaml.safe_load(Path(path).read_text(encoding="utf8")), "config"))
    model = dict(_mapping(raw.pop("model", None), "model"))
    if "sensor_gap_f" in model:
        model["sensor_gap_f"] = _names(model["sensor_gap_f"], "model.sensor_gap_f")
    sections = {
        "strategy": _section(StrategyParams, raw.pop("strategy", None), "strategy"),
        "model": _section(ModelParams, model, "model"),
        "bias": _section(BiasConfig, raw.pop("bias", None), "bias"),
        "live": _section(LiveConfig, raw.pop("live", None), "live"),
    }
    forecast = dict(_mapping(raw.pop("forecast", None), "forecast"))
    if "models" in forecast:
        forecast["models"] = _names(forecast["models"], "forecast.models")
    sections["forecast"] = _section(ForecastConfig, forecast, "forecast")
    sections["stations"] = _stations(raw.pop("stations", None))
    if "venues" in raw:
        raw["venues"] = _names(raw["venues"], "venues")

    cfg = _section(Config, {**raw, **sections}, "config")
    _validate(cfg)
    # Relative paths follow the config file. A kill run from another folder has to reach the real switch.
    base = Path(path).resolve().parent
    return replace(
        cfg,
        db_path=str(base / cfg.db_path),
        report_dir=str(base / cfg.report_dir),
        live=replace(cfg.live, kill_file=str(base / cfg.live.kill_file)),
    )

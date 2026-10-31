from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from weatherbot.config import MODELS, ConfigError, LiveConfig, load_config
from weatherbot.openmeteo import _META_PATHS

ROOT = Path(__file__).parent.parent


def write(tmp_path, change):
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf8"))
    change(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf8")
    return path


def test_shipped_config_loads():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.stations["KLGA"].tz == "America/New_York"
    assert cfg.stations["KDEN"].elev_m == 1656
    assert len(cfg.stations) == 51


def test_shipped_config_time_zones_exist():
    for station in load_config(ROOT / "config.yaml").stations.values():
        ZoneInfo(station.tz)


def test_shipped_config_keeps_live_gate_closed():
    assert load_config(ROOT / "config.yaml").live.jurisdiction_confirmed is False


def test_shipped_config_carries_the_measured_sensor_gap():
    assert load_config(ROOT / "config.yaml").model.sensor_gap_f == (0.46, 0.41, 0.08, 0.03, 0.02)


def test_shipped_observation_ramp_ends_once_highs_are_in():
    assert load_config(ROOT / "config.yaml").model.obs_full_hour == 17


def test_mistyped_setting_is_rejected(tmp_path):
    path = write(tmp_path, lambda d: d["strategy"].update(max_trade_uds=5))
    with pytest.raises(ConfigError, match="max_trade_uds"):
        load_config(path)


def test_bad_time_zone_is_rejected(tmp_path):
    path = write(tmp_path, lambda d: d["stations"]["KLGA"].update(tz="America/Gotham"))
    with pytest.raises(ConfigError, match="KLGA"):
        load_config(path)


@pytest.mark.parametrize("kelly", [0.05, 0.3, 1.0])
def test_kelly_outside_agreed_range_is_rejected(tmp_path, kelly):
    path = write(tmp_path, lambda d: d["strategy"].update(kelly_fraction=kelly))
    with pytest.raises(ConfigError, match="kelly_fraction"):
        load_config(path)


def test_unknown_venue_is_rejected(tmp_path):
    path = write(tmp_path, lambda d: d.update(venues=["kalshi"]))
    with pytest.raises(ConfigError, match="kalshi"):
        load_config(path)


def test_shipped_loss_limit_can_trip_before_the_day_cap_runs_out():
    live = load_config(ROOT / "config.yaml").live
    assert live.daily_loss_limit_usd < live.max_day_usd
    assert LiveConfig().daily_loss_limit_usd == live.daily_loss_limit_usd


def test_every_model_the_config_allows_can_be_fetched():
    assert set(MODELS) == set(_META_PATHS)


def test_relative_paths_follow_the_config_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = write(home, lambda d: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(path)
    assert Path(cfg.db_path) == home.resolve() / "data" / "weatherbot.sqlite"
    assert Path(cfg.report_dir) == home.resolve() / "reports"
    assert Path(cfg.live.kill_file) == home.resolve() / "data" / "KILL"


def test_relative_config_path_is_pinned_when_it_loads(tmp_path, monkeypatch):
    write(tmp_path, lambda d: None)
    monkeypatch.chdir(tmp_path)
    assert Path(load_config("config.yaml").db_path) == tmp_path.resolve() / "data" / "weatherbot.sqlite"


def test_absolute_paths_stay_put(tmp_path):
    target = tmp_path / "somewhere" / "else.sqlite"
    assert Path(load_config(write(tmp_path, lambda d: d.update(db_path=str(target)))).db_path) == target


NAN = float("nan")


@pytest.mark.parametrize("name, value", [
    ("bankroll_usd", 0), ("bankroll_usd", -1000), ("bankroll_usd", NAN), ("bankroll_usd", "1000"), ("bankroll_usd", None),
    ("bankroll_usd", True), ("bankroll_usd", float("inf")), ("max_obs_age_hours", 0), ("order_ttl_minutes", 0),
])
def test_top_level_number_out_of_range_is_rejected(tmp_path, name, value):
    with pytest.raises(ConfigError, match=name):
        load_config(write(tmp_path, lambda d: d.update({name: value})))


@pytest.mark.parametrize("section, name, value", [
    ("strategy", "kelly_fraction", "0.15"),
    ("strategy", "edge_threshold", 0),
    ("strategy", "edge_threshold", 1),
    ("strategy", "min_price", 0),
    ("strategy", "max_price", 1.5),
    ("strategy", "max_trade_usd", -25),
    ("strategy", "max_day_usd", 0),
    ("strategy", "max_event_usd", NAN),
    ("strategy", "max_depth_share", 15),
    ("strategy", "max_depth_share", 0),
    ("model", "spread_inflation", 0),
    ("model", "kernel_sigma_start_c", 0),
    ("model", "kernel_sigma_end_c", -1),
    ("model", "obs_start_hour", -1),
    ("model", "obs_full_hour", 25),
    ("model", "anchor_tau_hours", 0),
    ("model", "edge_shift_c", -0.1),
    ("model", "prob_epsilon", 1),
    ("model", "min_horizon_coverage", 0),
    ("model", "min_horizon_coverage", 1.5),
    ("forecast", "max_age_hours", 0),
    ("bias", "shrink_days", -1),
    ("live", "bankroll_usd", 0),
    ("live", "daily_loss_limit_usd", 0),
    ("live", "max_order_usd", -10),
    ("live", "max_day_usd", NAN),
])
def test_number_out_of_range_is_rejected(tmp_path, section, name, value):
    with pytest.raises(ConfigError, match=name):
        load_config(write(tmp_path, lambda d: d[section].update({name: value})))


@pytest.mark.parametrize("section, name, value", [
    ("bias", "window_days", 0),
    ("bias", "window_days", 30.5),
    ("bias", "min_days", -1),
    ("forecast", "past_days", -1),
    ("forecast", "forecast_days", 0),
    ("forecast", "forecast_days", "5"),
])
def test_whole_number_setting_is_checked(tmp_path, section, name, value):
    with pytest.raises(ConfigError, match=name):
        load_config(write(tmp_path, lambda d: d[section].update({name: value})))


def test_lead_limit_must_be_a_whole_number(tmp_path):
    with pytest.raises(ConfigError, match="max_lead_days"):
        load_config(write(tmp_path, lambda d: d.update(max_lead_days=1.5)))


@pytest.mark.parametrize("section, change, name", [
    ("strategy", {"min_price": 0.5, "max_price": 0.5}, "min_price"),
    ("model", {"obs_start_hour": 15, "obs_full_hour": 15}, "obs_start_hour"),
    ("live", {"max_order_usd": 30, "max_day_usd": 25}, "max_order_usd"),
    ("live", {"max_day_usd": 200, "bankroll_usd": 100}, "max_day_usd"),
    ("live", {"max_order_usd": 1e9}, "max_order_usd"),
    ("live", {"daily_loss_limit_usd": 150, "bankroll_usd": 100}, "daily_loss_limit_usd"),
])
def test_settings_that_contradict_each_other_are_rejected(tmp_path, section, change, name):
    with pytest.raises(ConfigError, match=name):
        load_config(write(tmp_path, lambda d: d[section].update(change)))


@pytest.mark.parametrize("value", ["false", "true", 1, 0, None])
def test_jurisdiction_must_be_written_as_true_or_false(tmp_path, value):
    with pytest.raises(ConfigError, match="jurisdiction_confirmed"):
        load_config(write(tmp_path, lambda d: d["live"].update(jurisdiction_confirmed=value)))


@pytest.mark.parametrize("models", [[], ["gfs_global"], ["gfs025", "gfs_global"], [["gfs025"]], "gfs025", 5])
def test_forecast_models_must_be_ones_the_bot_can_fetch(tmp_path, models):
    with pytest.raises(ConfigError, match="models"):
        load_config(write(tmp_path, lambda d: d["forecast"].update(models=models)))


@pytest.mark.parametrize("value", [[], [0.5], [0.5, 0.6], [-0.1, 1.1], ["a", 1.0], [float("nan"), 1.0], [True],
                                   "0.5", 5, None])
def test_sensor_gap_must_be_chances_that_add_up_to_one(tmp_path, value):
    path = write(tmp_path, lambda d: d["model"].update(sensor_gap_f=value))
    with pytest.raises(ConfigError, match="sensor_gap_f"):
        load_config(path)


def test_one_step_sensor_gap_is_allowed(tmp_path):
    path = write(tmp_path, lambda d: d["model"].update(sensor_gap_f=[1.0]))
    assert load_config(path).model.sensor_gap_f == (1.0,)


def test_one_known_model_is_enough(tmp_path):
    assert load_config(write(tmp_path, lambda d: d["forecast"].update(models=["gfs025"]))).forecast.models == ("gfs025",)


def test_venues_must_be_a_list(tmp_path):
    with pytest.raises(ConfigError, match="venues"):
        load_config(write(tmp_path, lambda d: d.update(venues=5)))


@pytest.mark.parametrize("name", ["db_path", "report_dir"])
def test_path_setting_must_be_text(tmp_path, name):
    with pytest.raises(ConfigError, match=name):
        load_config(write(tmp_path, lambda d: d.update({name: 5})))


def test_kill_file_must_be_text(tmp_path):
    with pytest.raises(ConfigError, match="kill_file"):
        load_config(write(tmp_path, lambda d: d["live"].update(kill_file=5)))


@pytest.mark.parametrize("value", [[1, 2], 5, "aggressive"])
def test_section_that_is_not_a_mapping_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="strategy must be a mapping"):
        load_config(write(tmp_path, lambda d: d.update(strategy=value)))


def test_config_that_is_not_a_mapping_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n", encoding="utf8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("change", [{"lat": 91}, {"lat": -91}, {"lon": 181}, {"lon": "east"}, {"elev_m": 12000}, {"lat": NAN}])
def test_bad_station_coordinates_are_rejected(tmp_path, change):
    with pytest.raises(ConfigError, match="KLGA"):
        load_config(write(tmp_path, lambda d: d["stations"]["KLGA"].update(change)))


def test_station_that_is_not_a_mapping_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="KLGA"):
        load_config(write(tmp_path, lambda d: d["stations"].update(KLGA="La Guardia")))

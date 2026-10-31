import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from weatherbot.core import Rule
from weatherbot.metar import (
    Obs,
    covers_day,
    latest_obs,
    obs_from_json,
    opening_temp_c,
    parse_temp_c,
    qualifying_high_c,
    unverified_high_c,
)

FIXTURES = Path(__file__).parent / "fixtures"
NY = "America/New_York"


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize(
    "raw, want",
    [
        ("METAR KLGA 310151Z 32011KT 10SM FEW250 13/02 A3025 RMK AO2 SLP244 T01280017", 12.8),
        ("KJFK 121651Z 00000KT 10SM CLR 02/M01 A3000 RMK AO2 T00171011", 1.7),
        ("KJFK 121651Z 00000KT 10SM CLR M02/M03 A3000 RMK AO2 T10171022", -1.7),
        # No remark: body group in whole degrees.
        ("EGLC 310120Z AUTO 25010KT 9999 NCD 18/12 Q1012", 18.0),
        ("UUWW 310130Z 00000MPS CAVOK M11/M17 Q1030 NOSIG", -11.0),
        ("KABC 310153Z AUTO 00000KT 10SM CLR 07/ A3000 RMK AO2", 7.0),
        ("KABC 310153Z AUTO 00000KT 10SM CLR 07/XX A3000 RMK AO2", 7.0),
        # Seven digits is not a valid remark, so the body governs.
        ("KABC 310153Z 00000KT 10SM CLR 13/02 A3000 RMK AO2 T0128001", 13.0),
        # Nine digits likewise.
        ("KABC 310153Z 00000KT 10SM CLR 13/02 A3000 RMK AO2 T012800171", 13.0),
        # Fractional visibility is not a temperature group.
        ("KABC 310153Z 00000KT 1/2SM FG 04/04 A3000 RMK AO2", 4.0),
        # Six-hour max group in the remarks is not a T group.
        ("KABC 311153Z 00000KT 10SM CLR 10/05 A3000 RMK AO2 10128 20056 T01000050", 10.0),
        # No temperature anywhere.
        ("KABC 310153Z AUTO 00000KT 10SM CLR ///// A3000 RMK AO2", None),
        ("KABC 310153Z AUTO NIL", None),
    ],
)
def test_parse_temp_c(raw, want):
    assert parse_temp_c(raw) == want


def obs(t, temp, kind="routine", max6h=None, max24h=None):
    return Obs("KLGA", t, temp, kind, "raw", max6h, max24h)


ROUTINE = Rule("metar_routine", "KLGA", "F", "local_dst", "routine")
ALL_REPORTS = Rule("wunderground", "KLGA", "F", "local_dst", "all")
NWS = Rule("nws_cli", "KLGA", "F", "local_standard", "nws_cli")


def test_routine_policy_ignores_speci_spike():
    reports = [
        obs(utc(2026, 7, 1, 15), 30.0),
        obs(utc(2026, 7, 1, 15, 20), 33.0, kind="speci"),
        obs(utc(2026, 7, 1, 16), 31.0),
    ]
    assert qualifying_high_c(reports, ROUTINE, date(2026, 7, 1), NY) == 31.0


def test_all_reports_policy_counts_speci():
    reports = [obs(utc(2026, 7, 1, 15), 30.0), obs(utc(2026, 7, 1, 15, 20), 33.0, kind="speci")]
    assert qualifying_high_c(reports, ALL_REPORTS, date(2026, 7, 1), NY) == 33.0


def test_observation_on_local_midnight_boundary_belongs_to_new_day():
    # 04:00Z is local midnight in July. Half-open window: it counts for July 2.
    reports = [obs(utc(2026, 7, 2, 4), 28.0), obs(utc(2026, 7, 1, 20), 25.0)]
    assert qualifying_high_c(reports, ROUTINE, date(2026, 7, 1), NY) == 25.0
    assert qualifying_high_c(reports, ROUTINE, date(2026, 7, 2), NY) == 28.0


def test_dst_and_standard_days_differ_by_an_hour_in_summer():
    # 04:30Z July 2 is 00:30 EDT July 2 but 23:30 EST July 1.
    reports = [obs(utc(2026, 7, 2, 4, 30), 29.0)]
    assert qualifying_high_c(reports, ROUTINE, date(2026, 7, 1), NY) is None
    assert qualifying_high_c(reports, NWS, date(2026, 7, 1), NY) == 29.0


def test_no_qualifying_report_returns_none():
    assert qualifying_high_c([obs(utc(2026, 7, 1, 15), None)], ROUTINE, date(2026, 7, 1), NY) is None


def test_nws_policy_uses_six_hour_max_fully_inside_day():
    # Reported 23:53Z, covers 17:53Z-23:53Z, inside the LST day.
    reports = [obs(utc(2026, 7, 1, 20), 30.0), obs(utc(2026, 7, 1, 23, 53), 29.0, max6h=32.5)]
    assert qualifying_high_c(reports, NWS, date(2026, 7, 1), NY) == 32.5


def test_nws_policy_skips_six_hour_max_straddling_day_start():
    # Reported 05:53Z, covers 23:53Z-05:53Z, which starts before 05:00Z on July 1.
    reports = [obs(utc(2026, 7, 1, 5, 53), 20.0, max6h=35.0)]
    assert qualifying_high_c(reports, NWS, date(2026, 7, 1), NY) == 20.0


def test_nws_policy_uses_24_hour_max_ending_at_lst_midnight():
    # The midnight LST report's 24h group covers exactly the day before it.
    reports = [obs(utc(2026, 7, 2, 4, 53), 21.0, max24h=33.9)]
    assert qualifying_high_c(reports, NWS, date(2026, 7, 1), NY) == 33.9


def test_routine_policy_ignores_six_hour_max():
    reports = [obs(utc(2026, 7, 1, 23, 53), 29.0, max6h=32.5)]
    assert qualifying_high_c(reports, ROUTINE, date(2026, 7, 1), NY) == 29.0


DAY = date(2026, 7, 1)
MIDNIGHT = utc(2026, 7, 1, 4)  # local midnight in July


def every_hour(first=0, last=23, **kw):
    return [obs(MIDNIGHT + h * 3600, 20.0, **kw) for h in range(first, last + 1)]


def test_hourly_reports_cover_the_day():
    assert covers_day(every_hour(), ROUTINE, DAY, NY)


def test_hole_of_three_hours_is_allowed():
    reports = [r for r in every_hour() if r.obs_time not in (MIDNIGHT + 13 * 3600, MIDNIGHT + 14 * 3600)]
    assert covers_day(reports, ROUTINE, DAY, NY)


def test_hole_over_three_hours_can_hide_the_high():
    reports = [r for r in every_hour() if not 12 < (r.obs_time - MIDNIGHT) / 3600 < 16]
    assert not covers_day(reports, ROUTINE, DAY, NY)


def test_reports_must_start_near_midnight():
    assert not covers_day(every_hour(first=4), ROUTINE, DAY, NY)


def test_reports_must_run_near_the_end_of_the_day():
    assert not covers_day(every_hour(last=19), ROUTINE, DAY, NY)


def test_no_reports_cover_nothing():
    assert not covers_day([], ROUTINE, DAY, NY)


def test_report_without_temperature_fills_no_hole():
    reports = every_hour(last=11) + every_hour(first=16)
    assert not covers_day(reports + [obs(MIDNIGHT + 14 * 3600, None)], ROUTINE, DAY, NY)


def test_speci_fills_no_hole_on_routine_markets():
    reports = every_hour(last=11) + every_hour(first=16)
    gap_filler = obs(MIDNIGHT + 14 * 3600, 20.0, kind="speci")
    assert not covers_day(reports + [gap_filler], ROUTINE, DAY, NY)
    assert covers_day(reports + [gap_filler], ALL_REPORTS, DAY, NY)


def test_reports_from_other_days_fill_no_hole():
    late = obs(MIDNIGHT - 3600, 20.0)
    assert not covers_day([late] + every_hour(first=4), ROUTINE, DAY, NY)


def test_a_day_in_progress_is_judged_only_as_far_as_now():
    so_far = every_hour(last=11)
    assert covers_day(so_far, ROUTINE, DAY, NY, until=MIDNIGHT + 12 * 3600)
    assert not covers_day(so_far, ROUTINE, DAY, NY, until=MIDNIGHT + 16 * 3600)


def test_full_day_sensor_maximum_proves_an_nws_day():
    lst_midnight = utc(2026, 7, 1, 5)
    report = obs(lst_midnight + 24 * 3600 - 420, 21.0, max24h=33.9)
    assert covers_day([report], NWS, DAY, NY)


def test_sensor_maximum_for_a_different_day_proves_nothing():
    lst_midnight = utc(2026, 7, 1, 5)
    report = obs(lst_midnight + 3 * 3600, 21.0, max24h=33.9)
    assert not covers_day([report], NWS, DAY, NY)


def test_full_day_sensor_maximum_means_nothing_to_a_routine_market():
    report = obs(MIDNIGHT + 24 * 3600 - 420, 21.0, max24h=33.9)
    assert not covers_day([report], ROUTINE, DAY, NY)


def test_latest_obs_skips_reports_without_temperature():
    reports = [obs(10, 5.0), obs(30, None), obs(20, 6.0)]
    assert latest_obs(reports).temp_c == 6.0


def test_latest_obs_counts_speci():
    reports = [obs(10, 5.0), obs(20, 6.0, kind="speci")]
    assert latest_obs(reports).kind == "speci"


def test_latest_obs_empty():
    assert latest_obs([]) is None


def test_obs_from_real_awc_row():
    row = json.loads((FIXTURES / "metar_klga.json").read_text(encoding="utf8"))[0]
    got = obs_from_json(row)
    assert (got.icao, got.obs_time, got.temp_c, got.kind) == ("KLGA", 1793411460, 12.8, "routine")


def test_obs_from_json_classifies_speci_by_report_text_not_type_field():
    row = {"icaoId": "KLGA", "obsTime": 100, "metarType": "METAR",
           "rawOb": "SPECI KLGA 310151Z 32011KT 10SM 13/02 A3025 RMK AO2 T01280017"}
    assert obs_from_json(row).kind == "speci"


def test_obs_from_json_reads_max_groups():
    row = {"icaoId": "KLGA", "obsTime": 100, "maxT": 14.4, "maxT24": 15.0,
           "rawOb": "KLGA 310151Z 32011KT 10SM 13/02 A3025 RMK AO2 T01280017"}
    got = obs_from_json(row)
    assert (got.max6h_c, got.max24h_c) == (14.4, 15.0)


def test_obs_from_json_uses_report_text_for_temperature():
    # The rule is defined on the report, so the text wins over the JSON field.
    row = {"icaoId": "EGLC", "obsTime": 100, "temp": 99.0,
           "rawOb": "EGLC 310120Z AUTO 25010KT 9999 NCD 18/12 Q1012"}
    assert obs_from_json(row).temp_c == 18.0


REAL_SPECI = "SPECI KMIA 301245Z 34005KT 10SM SCT007 SCT025 26/24 A2997 RMK AO2 T02560239"


def test_real_speci_counts_only_where_the_rule_lets_it():
    report = obs_from_json({"icaoId": "KMIA", "obsTime": 1793364300, "rawOb": REAL_SPECI})
    assert (report.kind, report.temp_c) == ("speci", 25.6)
    day, tz = date(2026, 10, 30), "America/New_York"
    assert qualifying_high_c([report], Rule("metar_routine", "KMIA", "F", "local_dst", "routine"), day, tz) is None
    assert qualifying_high_c([report], Rule("wunderground", "KMIA", "F", "local_dst", "all"), day, tz) == 25.6


FRONT = date(2026, 10, 17)


def on_front_day(hour, minute=51):
    return utc(2026, 10, 17, hour, minute)


def test_opening_temperature_reads_between_the_reports_either_side():
    # KNYC that day: a cold front, so the high fell at the first moment of the day and no report landed on it.
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(5), 14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


def test_opening_temperature_takes_the_nearest_report_each_side():
    reports = [obs(on_front_day(6), 10.0), obs(on_front_day(4), 17.2), obs(on_front_day(3), 25.0), obs(on_front_day(5), 14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


def test_opening_temperature_counts_a_speci():
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(5, 21), 14.4, kind="speci")]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.36)


def test_opening_temperature_skips_a_report_with_no_reading():
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(4, 55), None), obs(on_front_day(5), 14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


@pytest.mark.parametrize("rule", [ROUTINE, ALL_REPORTS])
def test_opening_temperature_means_nothing_where_only_reports_count(rule):
    # These rules settle on the reports themselves, so what happened between two of them never counts.
    assert opening_temp_c([obs(on_front_day(3), 17.2), obs(on_front_day(4), 14.4)], rule, FRONT, NY) is None


@pytest.mark.parametrize("hours", [[4], [5]])
def test_opening_temperature_needs_a_report_each_side(hours):
    assert opening_temp_c([obs(on_front_day(h), 15.0) for h in hours], NWS, FRONT, NY) is None


def test_opening_temperature_takes_a_two_hour_gap():
    reports = [obs(on_front_day(3, 30), 10.0), obs(on_front_day(5, 30), 14.0)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(13.0)


def test_opening_temperature_refuses_anything_wider():
    reports = [obs(on_front_day(3, 29), 10.0), obs(on_front_day(5, 30), 14.0)]
    assert opening_temp_c(reports, NWS, FRONT, NY) is None


def test_opening_temperature_goes_once_the_full_day_is_known():
    # KDEN 2026-10-24: the sensor's maximum was in, and sat under the opening estimate.
    reports = [obs(on_front_day(4), 15.0), obs(on_front_day(5), 14.0), obs(utc(2026, 10, 18, 4, 51), 12.0, max24h=14.2)]
    assert opening_temp_c(reports, NWS, FRONT, NY) is None


def test_opening_temperature_goes_once_a_group_over_the_start_is_under_the_high():
    reports = [obs(on_front_day(4), 13.0), obs(on_front_day(5), 12.0, max6h=14.0), obs(on_front_day(15), 14.0)]
    assert opening_temp_c(reports, NWS, FRONT, NY) is None


def test_opening_temperature_stays_under_a_group_that_tops_the_high():
    # KNYC's cold front. The group over the start holds the evening before, so it says nothing of the opening.
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(5), 14.4, max6h=21.0)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


def test_opening_temperature_ignores_a_group_ending_before_the_start():
    reports = [obs(on_front_day(4), 13.0, max6h=13.0), obs(on_front_day(5), 14.0)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(13.15)


def test_opening_temperature_ignores_a_group_beginning_after_the_start():
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(5), 14.4), obs(on_front_day(11), 13.0, max6h=14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


def test_opening_temperature_goes_under_a_group_ending_on_the_start():
    reports = [obs(on_front_day(4), 14.0), obs(on_front_day(5, 0), 14.4, max6h=14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) is None


def test_opening_temperature_stays_under_a_group_beginning_on_the_start():
    reports = [obs(on_front_day(4), 17.2), obs(on_front_day(5), 14.4), obs(on_front_day(11, 0), 13.0, max6h=14.4)]
    assert opening_temp_c(reports, NWS, FRONT, NY) == pytest.approx(16.78)


@pytest.mark.parametrize("read", [opening_temp_c, unverified_high_c])
def test_day_with_no_reports_yet_has_no_high_to_hold_a_group_against(read):
    assert read([obs(on_front_day(4), 13.0, max6h=13.0)], NWS, FRONT, NY) is None


def test_reading_no_sensor_maximum_covers_is_unverified():
    reports = [obs(on_front_day(7), 12.0), obs(on_front_day(8), 13.0), obs(on_front_day(9), 12.5)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 13.0


def test_six_hour_maximum_vouches_for_the_readings_it_spans():
    reports = [obs(on_front_day(8), 13.0), obs(on_front_day(11), 12.0, max6h=13.3), obs(on_front_day(12), 12.5)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 12.5


def test_six_hour_maximum_vouches_for_the_report_carrying_it():
    assert unverified_high_c([obs(on_front_day(11), 13.0, max6h=13.3)], NWS, FRONT, NY) is None


def test_reading_at_the_very_start_of_a_span_stays_outside_it():
    # KSFO 2026-10-31: 17.8C on the first instant of a span whose group read 17.2C.
    reports = [obs(on_front_day(6), 17.8), obs(on_front_day(12), 14.0, max6h=17.2)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 17.8


def test_missing_six_hour_group_leaves_its_stretch_unverified():
    reports = [obs(on_front_day(11), 12.0, max6h=13.0), obs(on_front_day(15), 19.0),
               obs(on_front_day(17), 18.0), obs(on_front_day(23), 14.0, max6h=18.5)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 19.0


def test_higher_six_hour_group_reaching_into_yesterday_vouches_for_nothing():
    reports = [obs(on_front_day(7), 13.0), obs(on_front_day(8), 12.0, max6h=20.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 13.0


def test_higher_six_hour_group_filed_after_the_day_vouches_for_nothing():
    reports = [obs(on_front_day(23), 13.0), obs(utc(2026, 10, 18, 5), 12.0, max6h=20.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 13.0


def test_six_hour_group_reaching_into_yesterday_proves_a_reading_it_matches():
    # KAUS 2026-10-16: 23.9C, and a 23.9C group that began 7 minutes before the day.
    reports = [obs(on_front_day(7), 23.9), obs(on_front_day(8), 20.0, max6h=23.9)]
    assert unverified_high_c(reports, NWS, FRONT, NY) is None


def test_six_hour_group_filed_after_the_day_proves_a_reading_it_matches():
    reports = [obs(on_front_day(23), 13.0), obs(utc(2026, 10, 18, 5), 12.0, max6h=13.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) is None


def test_six_hour_group_under_the_high_closes_its_whole_span():
    reports = [obs(on_front_day(7), 13.0), obs(on_front_day(8), 12.0, max6h=13.3),
               obs(on_front_day(14), 14.0, max6h=14.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) is None


def test_six_hour_group_filed_after_the_day_does_not_raise_the_high():
    assert qualifying_high_c([obs(utc(2026, 10, 18, 5), 12.0, max6h=20.0)], NWS, FRONT, NY) is None


def test_twenty_four_hour_maximum_leaves_nothing_unverified():
    reports = [obs(on_front_day(15), 19.0), obs(utc(2026, 10, 18, 4, 51), 12.0, max24h=19.4)]
    assert unverified_high_c(reports, NWS, FRONT, NY) is None


def test_twenty_four_hour_group_of_another_day_settles_nothing():
    reports = [obs(on_front_day(15), 19.0), obs(on_front_day(4), 12.0, max24h=25.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 19.0


def test_only_the_days_own_readings_can_be_unverified():
    reports = [obs(on_front_day(4), 30.0), obs(on_front_day(15), 19.0), obs(utc(2026, 10, 18, 5), 30.0)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 19.0


def test_report_with_no_reading_is_not_unverified():
    reports = [obs(on_front_day(15), 19.0), obs(on_front_day(16), None)]
    assert unverified_high_c(reports, NWS, FRONT, NY) == 19.0


@pytest.mark.parametrize("rule", [ROUTINE, ALL_REPORTS])
def test_nothing_is_unverified_where_the_reports_are_the_answer(rule):
    assert unverified_high_c([obs(on_front_day(15), 19.0)], rule, FRONT, NY) is None


def test_coverage_stops_at_the_end_of_the_day_however_late_it_is_asked():
    assert covers_day(every_hour(), ROUTINE, DAY, NY, until=MIDNIGHT + 30 * 3600)

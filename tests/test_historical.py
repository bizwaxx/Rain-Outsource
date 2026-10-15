import importlib.util
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from rainout_agent.historical import (
    HISTORICAL_DIR,
    calibration_for,
    compute_climatology,
    load_historical_profile,
    rain_severity,
    rain_to_cancel_threshold_in,
    threshold_key,
)
from rainout_agent.play_probability import calculate_play_probability
from rainout_agent.status_api import _load_all_fields, build_status_result

ROOT = Path(__file__).resolve().parents[1]


def _load_fetch_script():
    spec = importlib.util.spec_from_file_location("fetch_noaa_history", ROOT / "scripts" / "fetch_noaa_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load_fetch_script()


# ----------------------------------------------------------- climatology math
def test_compute_climatology_counts_wet_and_rainout_days_per_month():
    daily = [
        (date(2024, 5, 1), 0.0),
        (date(2024, 5, 2), 0.05),
        (date(2024, 5, 3), 0.6),
        (date(2024, 5, 4), None),  # missing day is ignored
        (date(2024, 6, 1), 1.2),
    ]
    clim = compute_climatology(daily, cancel_threshold_in=0.5, period_days=5)

    may = clim["monthly"]["05"]
    assert may["days_observed"] == 3
    assert may["wet_day_frac"] == pytest.approx(2 / 3, abs=1e-3)
    assert may["rainout_day_frac"] == pytest.approx(1 / 3, abs=1e-3)
    assert may["cancel_given_wet"] == 0.5
    assert may[threshold_key(0.1)] == pytest.approx(1 / 3, abs=1e-3)
    assert clim["monthly"]["06"]["cancel_given_wet"] == 1.0
    assert clim["monthly"]["01"] == {"days_observed": 0}
    assert clim["days_observed"] == 4
    assert clim["coverage_percent"] == 80.0
    assert [d["date"] for d in clim["rainout_level_days"]] == ["2024-05-03", "2024-06-01"]


def test_threshold_key_format():
    assert threshold_key(0.5) == "p_ge_0_50"
    assert threshold_key(1.0) == "p_ge_1_00"


def test_rain_to_cancel_threshold_uses_sport_override_and_drainage():
    assert rain_to_cancel_threshold_in({"sport": "softball"}) == 0.5
    assert rain_to_cancel_threshold_in({"sport": "multipurpose fields"}) == 0.75
    assert rain_to_cancel_threshold_in({"sport": "motocross"}) == 1.0
    assert rain_to_cancel_threshold_in({"sport": "curling"}) == 0.5
    assert rain_to_cancel_threshold_in({"sport": "softball", "rain_to_cancel_threshold_in": 0.3}) == 0.3
    assert rain_to_cancel_threshold_in({"sport": "softball", "drainage": "good"}) == 0.75
    assert rain_to_cancel_threshold_in({"sport": "softball", "drainage": "poor"}) == 0.38


def test_rain_severity_blends_with_floor_and_storms_count_fully():
    assert rain_severity(0.0, False) == 0.5
    assert rain_severity(1.0, False) == 1.0
    assert rain_severity(0.3, False) == 0.65
    assert rain_severity(0.3, True) == 1.0
    assert rain_severity(5, False) == 1.0


# ------------------------------------------------------- calibrated probability
CAL = {"month": "05", "cancel_given_wet": 0.3, "rain_to_cancel_threshold_in": 0.5}


def test_calibration_raises_play_probability_for_plain_rain():
    base = calculate_play_probability("on", 60, False, 1.0)
    calibrated = calculate_play_probability("on", 60, False, 1.0, historical_calibration=CAL)
    assert base["play_probability_percent"] == 40
    assert calibrated["play_probability_percent"] == 61  # 100 - round(60 * 0.65)
    assert calibrated["risk_level"] == base["risk_level"] == "high"
    assert "NOAA" in calibrated["reason"] and "May" in calibrated["reason"]
    assert "rainfall" not in calibrated["reason"].lower()
    assert calibrated["rain_severity"] == 0.65


def test_calibration_does_not_soften_thunderstorms():
    base = calculate_play_probability("on", 67, True, 1.0)
    calibrated = calculate_play_probability("on", 67, True, 1.0, historical_calibration=CAL)
    assert calibrated["play_probability_percent"] == base["play_probability_percent"] == 25


def test_calibration_never_overrides_official_status():
    assert calculate_play_probability("cancelled", 5, False, 1.0, historical_calibration=CAL)["play_probability_percent"] == 0
    assert calculate_play_probability("unknown", 5, False, 1.0, historical_calibration=CAL)["play_probability_percent"] == 50


def test_no_calibration_keeps_original_output_shape():
    result = calculate_play_probability("on", 15, False, 0.5)
    assert set(result) == {"play_probability_percent", "risk_level", "recommendation", "reason"}


# ------------------------------------------------------------- generated data
def test_every_field_has_one_historical_doc_with_real_noaa_history():
    field_ids = {f["id"] for f in _load_all_fields()}
    docs = [json.loads(p.read_text()) for p in HISTORICAL_DIR.glob("*.json")]
    counts = Counter(d["field_id"] for d in docs)
    for field_id in field_ids:
        assert counts[field_id] == 1, field_id
        noaa = load_historical_profile(field_id).get("noaa_history")
        assert noaa, field_id
        assert noaa["station"]["id"]
        assert noaa["station"]["distance_km"] <= 40
        assert noaa["coverage_percent"] >= 75
        assert noaa["period"] == {"start": "2016-01-01", "end": "2025-12-31"}
        assert set(noaa["monthly"]) == {f"{m:02d}" for m in range(1, 13)}
        assert 15 < noaa["annual"]["mean_precip_in"] < 90  # plausible for TX, OK, LA and FL


def test_historical_docs_keep_legacy_schema_keys():
    for path in HISTORICAL_DIR.glob("*.json"):
        doc = json.loads(path.read_text())
        assert {"field_id", "historical_cancellations", "typical_cancellation_threshold_in", "notes"} <= set(doc)


def test_calibration_for_krieg_uses_austin_bergstrom():
    cal = calibration_for("austin-tx-krieg-field-softball-complex", datetime(2026, 5, 9, 19, 0))
    assert cal["station_id"] == "USW00013904"
    assert cal["month"] == "05"
    assert 0 < cal["cancel_given_wet"] < 1
    assert calibration_for("no-such-field", datetime(2026, 5, 9)) is None
    assert calibration_for("austin-tx-krieg-field-softball-complex", None) is None


def test_status_result_includes_additive_historical_calibration():
    result = build_status_result(
        field_query="Krieg",
        game_time="2026-05-09T19:00:00-05:00",
        weather={
            "rain_chance_percent": 60,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-05-09T16:00:00-05:00",
        },
        official_status="on",
    )
    cal = result["historical_calibration"]
    assert cal["station_id"] == "USW00013904"
    assert cal["month"] == "05"
    assert cal["rain_to_cancel_threshold_in"] == 0.5
    assert 40 < result["game_time_play_probability_percent"] < 100
    assert "rainfall" not in result["spoken_answer"].lower()


# ---------------------------------------------------------- fetch script units
def test_parse_stations_and_inventory():
    stations_txt = (
        "USW00013904  30.1831  -97.6800  146.6 TX AUSTIN BERGSTROM INTL AP               74745\n"
        "US1TXTV0087  30.3480  -97.7160  200.0 TX AUSTIN 3.9 NNE                              \n"
    )
    inventory_txt = (
        "USW00013904  30.1831  -97.6800 PRCP 1948 2026\n"
        "USW00013904  30.1831  -97.6800 TMAX 1948 2026\n"
        "US1TXTV0087  30.3480  -97.7160 PRCP 2010 2026\n"
    )
    stations = fetch.parse_stations(stations_txt)
    inv = fetch.parse_prcp_inventory(inventory_txt)
    assert stations["USW00013904"]["name"] == "AUSTIN BERGSTROM INTL AP"
    assert stations["USW00013904"]["lat"] == 30.1831
    assert inv == {"USW00013904": (1948, 2026), "US1TXTV0087": (2010, 2026)}

    cands = fetch.candidate_stations(30.2464, -97.7142, stations, inv, 2016, 2025, 40, 5)
    assert [c["id"] for c in cands] == ["USW00013904", "US1TXTV0087"]
    assert cands[0]["distance_km"] == pytest.approx(7.8, abs=0.2)
    assert fetch.candidate_stations(30.2464, -97.7142, stations, inv, 2000, 2025, 40, 5)[0]["id"] == "USW00013904"
    assert [c["id"] for c in fetch.candidate_stations(30.2464, -97.7142, stations, inv, 2016, 2025, 5, 5)] == []


def test_parse_daily_csv_drops_failed_qc_values_and_computes_coverage():
    csv_text = (
        '"STATION","DATE","PRCP","PRCP_ATTRIBUTES"\n'
        '"X","2024-01-01","0.00",",,W,2400"\n'
        '"X","2024-01-02","1.51",",,W,2400"\n'
        '"X","2024-01-03","9.99",",X,W,2400"\n'
        '"X","2024-01-04","0.00","T,,W,2400"\n'
    )
    values = fetch.parse_daily_csv(csv_text)
    assert values[date(2024, 1, 2)] == 1.51
    assert values[date(2024, 1, 3)] is None
    assert values[date(2024, 1, 4)] == 0.0
    assert fetch.coverage(values, date(2024, 1, 1), date(2024, 1, 10)) == 0.3
    days = list(fetch.iter_period(values, date(2024, 1, 1), date(2024, 1, 5)))
    assert len(days) == 5 and days[-1] == (date(2024, 1, 5), None)


def test_canonical_hist_name_matches_existing_scheme():
    assert fetch.canonical_hist_name("austin-tx-krieg-field-softball-complex") == "krieg-field-softball-complex-historical.json"
    assert fetch.canonical_hist_name("houston-tx-houston-sportsplex") == "houston-houston-sportsplex-historical.json"


def test_merge_cancellations_dedupes_and_sorts():
    a = {"historical_cancellations": [{"date": "2025-05-15", "game_time": "x"}]}
    b = {"historical_cancellations": [{"date": "2025-04-22", "game_time": "y"}, {"date": "2025-05-15", "game_time": "x"}]}
    merged = fetch.merge_cancellations([a, b])
    assert [r["date"] for r in merged] == ["2025-04-22", "2025-05-15"]

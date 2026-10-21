from datetime import datetime, timedelta, timezone

from rainout_agent import event_risk as er

START = datetime(2026, 5, 2, 23, tzinfo=timezone.utc)


def _rows(mm_by_offset=None, **window):
    rows = {}
    for h in range(-72, 6):
        t = START + timedelta(hours=h)
        rows[t] = {"mm": (mm_by_offset or {}).get(h, 0.0), "pop": 0, "thunder": False}
        if 0 <= h < 3:
            rows[t].update(window)
    return rows


def test_dry_day_stays_high_and_soak_hurts_dirt_more_than_turf():
    dry = er.evaluate(_rows(), _rows(), START, 3, "slow")
    assert dry["play_probability_percent"] >= 90
    wet = _rows({-20: 25.4})          # an inch the day before
    dirt = er.evaluate(wet, wet, START, 3, "slow")["play_probability_percent"]
    turf = er.evaluate(wet, wet, START, 3, "fast")["play_probability_percent"]
    assert dirt <= turf <= 79         # humility cap: rain already fell, so never "confident"
    soaked = _rows({-20: 50.8})       # two inches the day before: dirt now loses points, turf doesn't
    dirt2 = er.evaluate(soaked, soaked, START, 3, "slow")["play_probability_percent"]
    turf2 = er.evaluate(soaked, soaked, START, 3, "fast")["play_probability_percent"]
    assert dirt2 < turf2 <= 79
    # Same day: an inch this morning hurts dirt far more than turf.
    am = _rows({-4: 25.4})
    assert er.evaluate(am, am, START, 3, "slow")["play_probability_percent"] < er.evaluate(am, am, START, 3, "fast")["play_probability_percent"]


def test_storm_in_window_caps_and_wind_caps():
    storm = _rows(pop=40, thunder=True)
    assert er.evaluate(storm, storm, START, 3, "medium")["play_probability_percent"] <= 45
    windy = _rows(gust_mph=50)
    assert er.evaluate(windy, windy, START, 3, "fast")["play_probability_percent"] <= 45


def test_drainage_class_mapping():
    assert er.drainage_class(field_type="turf") == "fast"
    assert er.drainage_class(field_type="dirt_infield") == "slow"
    assert er.drainage_class(field_type="dirt_infield", maintained=True) == "medium"
    assert er.effective_drainage("slow", True) == "slow"  # maintained venues now use maint_soak_factor
    assert er.drainage_class({"sport": "softball", "surface": "unknown"}) == "slow"
    assert er.drainage_class({"sport": "soccer", "surface": "unknown"}) == "medium"


def test_wetness_bucket_dries_with_et0_and_drainage():
    from rainout_agent import event_risk as er
    series = {"rain": [25.4] + [0.0] * 23, "et0": [0.0] * 24, "has_et0": True}
    p = {**er.PARAMS, "wet_drain_mm_h": {"fast": 1.0, "medium": 0.1, "slow": 0.0, "mx": 0.0}, "wet_et_mult": 1.0}
    assert er.wetness_in(series, "slow", p) == 1.0
    assert er.wetness_in(series, "fast", p) < er.wetness_in(series, "medium", p) < 1.0
    dry = {**series, "et0": [0.5] * 24}
    assert er.wetness_in(dry, "slow", p) < 1.0
    hyb = {**p, "soak_model": "hybrid", "wet_scale": 1.0}
    feat = {"earlier_today_in": 0.0, "prior_24h_in": 0.0, "prior_48h_in": 0.0, "wetness_in": 1.0}
    assert er.soak_index(feat, "slow", hyb)[0] == 1.0


def _rows_prev(pop_prev: int):
    rows = _rows()
    for h in range(-30, -6):
        rows[START + timedelta(hours=h)]["pop"] = pop_prev
    return rows


def test_threat_caps_day_before_forecast_and_motocross_note():
    calm = _rows()
    threat = _rows_prev(70)            # storms forecast the day before, the event day itself looks dry
    params = {**er.PARAMS, "threat_cap": 79, "mx_forecast_cap": 79}
    assert not er.threat_signal(er.features(calm, calm, START, 3), params)
    assert er.threat_signal(er.features(threat, threat, START, 3), params)
    off = {**er.PARAMS, "threat_cap": None, "mx_forecast_cap": None}
    assert er.evaluate(threat, threat, START, 3, "slow", off)["play_probability_percent"] >= 90
    capped = er.evaluate(threat, threat, START, 3, "slow", params)
    assert capped["play_probability_percent"] <= 79
    mx = er.evaluate(threat, threat, START, 3, "slow", {**off, "mx_forecast_cap": 79}, ball_sport=False)
    assert mx["play_probability_percent"] <= 79
    assert mx["forecast_cancel_note"] == er.FORECAST_CANCEL_NOTE
    assert any("cancel on the forecast alone" in r for r in mx["reasons"])
    ball = er.evaluate(threat, threat, START, 3, "slow", {**off, "mx_forecast_cap": 79})
    assert ball["play_probability_percent"] >= 90 and ball["forecast_cancel_note"] is None

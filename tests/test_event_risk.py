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
    assert dirt < turf <= 79          # humility cap: rain already fell
    assert dirt < 50


def test_storm_in_window_caps_and_wind_caps():
    storm = _rows(pop=40, thunder=True)
    assert er.evaluate(storm, storm, START, 3, "medium")["play_probability_percent"] <= 45
    windy = _rows(gust_mph=50)
    assert er.evaluate(windy, windy, START, 3, "fast")["play_probability_percent"] <= 45


def test_drainage_class_mapping():
    assert er.drainage_class(field_type="turf") == "fast"
    assert er.drainage_class(field_type="dirt_infield") == "slow"
    assert er.drainage_class(field_type="dirt_infield", maintained=True) == "medium"
    assert er.drainage_class({"sport": "softball", "surface": "unknown"}) == "slow"
    assert er.drainage_class({"sport": "soccer", "surface": "unknown"}) == "medium"

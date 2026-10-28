"""A game_time with no UTC offset is the field's local time (prod 502 regression)."""
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import game_time as gt
from rainout_agent import reports, status_api
from rainout_agent.status_api import build_status_result, load_field

FIELD = "austin-tx-downs-field-ambl"  # no "timezone" key; TX + coordinates -> America/Chicago


def _periods(start: datetime, hours: int = 72) -> list[dict]:
    out = []
    for i in range(hours):
        t = (start + timedelta(hours=i)).astimezone(timezone.utc)
        out.append({"startTime": t.isoformat(), "probabilityOfPrecipitation": {"value": i % 100},
                    "shortForecast": f"Hour {i}"})
    return out


@pytest.fixture
def fake_forecast(monkeypatch):
    def install(start: datetime):
        monkeypatch.setattr(status_api, "fetch_hourly_forecast",
                            lambda field, timeout=10: {"periods": _periods(start), "source": "National Weather Service API",
                                                            "source_id": "nws", "cache": "miss", "fetched_at": "2026-10-27T00:00:00+00:00"})
    return install


def _same(a: dict, b: dict) -> None:
    for key in ("game_time", "rain_chance_percent", "forecast_period_start", "game_time_play_probability_percent",
                "official_status", "recommendation"):
        assert a.get(key) == b.get(key), key


def test_field_timezone_from_state_and_coordinates():
    field = load_field(FIELD)
    assert not field.get("timezone")
    assert str(gt.field_timezone(field)) == "America/Chicago"
    assert str(gt.field_timezone({"state": "TX", "coordinates": {"lat": 31.76, "lon": -106.49}})) == "America/Denver"
    assert str(gt.field_timezone({"timezone": "America/New_York", "state": "TX"})) == "America/New_York"


def test_no_timezone_falls_back_to_utc():
    assert gt.field_timezone({}) is timezone.utc
    assert gt.field_timezone(None) is timezone.utc
    assert gt.parse_game_time("2026-10-28T19:00", {}) == datetime(2026, 10, 28, 19, tzinfo=timezone.utc)
    assert gt.normalize_game_time("2026-10-28T19:00", {"timezone": "Not/AZone"}) == "2026-10-28T19:00:00+00:00"


def test_parse_and_normalize():
    field = load_field(FIELD)
    assert gt.parse_game_time("2026-10-28T19:00", field).utcoffset() == timedelta(hours=-5)
    assert gt.normalize_game_time("2026-10-28T19:00", field) == "2026-10-28T19:00:00-05:00"
    assert gt.normalize_game_time("2026-10-28T19:00:00-05:00", field) == "2026-10-28T19:00:00-05:00"
    assert gt.normalize_game_time("2026-10-29T00:00:00Z", field) == "2026-10-29T00:00:00Z"
    assert gt.normalize_game_time("not a time", field) == "not a time"
    assert gt.parse_game_time("", field) is None


@pytest.mark.parametrize("naive,aware", [
    ("2026-10-28T19:00", "2026-10-28T19:00:00-05:00"),   # CDT
    ("2026-10-31T19:00", "2026-10-31T19:00:00-05:00"),   # last night of CDT
    ("2026-11-01T19:00", "2026-11-01T19:00:00-06:00"),   # DST ended at 2 AM Nov 1 -> CST
    ("2026-11-01T01:30", "2026-11-01T01:30:00-05:00"),   # ambiguous hour: first (CDT) occurrence, fold=0
])
def test_naive_status_matches_explicit_offset(fake_forecast, naive, aware):
    fake_forecast(datetime(2026, 10, 27, 0, tzinfo=timezone.utc))
    explicit = build_status_result(field_query=FIELD, game_time=aware, official_status="open")
    plain = build_status_result(field_query=FIELD, game_time=naive, official_status="open")
    assert "error" not in plain
    _same(plain, explicit)


def test_naive_status_no_longer_crashes_hours_until_game():
    assert status_api._hours_until_game(gt.normalize_game_time("2099-01-01T12:00", load_field(FIELD))) > 0


def test_naive_report_time_uses_field_zone():
    now = datetime(2026, 10, 28, 20, tzinfo=timezone.utc)
    field = load_field(FIELD)
    assert reports._parse_time("2026-10-28T19:00", now, field) == datetime.fromisoformat("2026-10-28T19:00:00-05:00")
    with pytest.raises(reports.ReportError):
        reports._parse_time("tomorrow-ish", now, field)

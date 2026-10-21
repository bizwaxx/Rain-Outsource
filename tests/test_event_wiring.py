"""Production wiring of the v2 event model (/v1/status and /v1/tracks), with Open-Meteo mocked."""
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import event_weather, weather
from rainout_agent.cache import MemoryCache
from rainout_agent.status_api import build_status_result

GAME = "2026-06-07T19:00:00-05:00"
START = datetime(2026, 6, 8, 0, tzinfo=timezone.utc)
WX = {"rain_chance_percent": 10, "thunderstorm_likely": False, "source": "NWS test", "last_checked": "2026-06-07T12:00:00-05:00"}


def payload(rain_mm_per_hour=0.0, wet_hours=(), pop=10, cape=0, gust=10, temp=80, et0=0.1):
    t0 = START - timedelta(hours=7 * 24)
    n = 7 * 24 + 48
    times = [int((t0 + timedelta(hours=i)).timestamp()) for i in range(n)]
    precip = [rain_mm_per_hour if (t0 + timedelta(hours=i - 1)) in wet_hours else 0.0 for i in range(n)]
    return {"hourly": {"time": times, "precipitation": precip, "precipitation_probability": [pop] * n,
                       "et0_fao_evapotranspiration": [et0] * n,
                       "weather_code": [3] * n, "cape": [cape] * n, "wind_gusts_10m": [gust] * n, "temperature_2m": [temp] * n}}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("RAINOUT_EVENT_RISK", "on")
    monkeypatch.setattr(event_weather, "get_cache", lambda: MemoryCache())
    calls = []

    def install(body=None, exc=None):
        def fake(url, headers, timeout):
            calls.append(url)
            if exc:
                raise exc
            return body
        monkeypatch.setattr(weather, "_http_get_json", fake)
        return calls
    return install


def status(field="Downs"):
    return build_status_result(field, GAME, weather=dict(WX), official_status="unknown")


def test_dry_ground_uses_v2_and_requests_past_two_days(live):
    calls = live(payload())
    r = status()
    assert r["model"]["version"] == "v2"
    assert "past_days=7" in calls[0] and "et0_fao_evapotranspiration" in calls[0] and "cape" in calls[0] and "wind_gusts_10m" in calls[0]
    assert r["game_time_play_probability_percent"] >= 70


def test_heavy_rain_overnight_on_slow_dirt_lowers_odds(live):
    wet = {START - timedelta(hours=h) for h in range(10, 20)}       # ~2 in. last night
    live(payload(rain_mm_per_hour=5.0, wet_hours=wet))
    r = status()
    assert r["model"]["version"] == "v2"
    f = r["model"]["features"]
    assert (f["earlier_today_in"] or 0) + f["prior_24h_in"] > 1.5
    assert r["game_time_play_probability_percent"] < r["model"]["v1_play_probability_percent"]
    assert r["model"]["reasons"]


def test_high_gusts_cap_odds(live):
    live(payload(gust=55))
    r = status()
    assert r["game_time_play_probability_percent"] <= 45


def test_fetch_failure_degrades_to_v1(live):
    live(exc=OSError("boom"))
    r = status()
    assert r["model"]["version"] == "v1" and "unavailable" in r["model"]["note"]
    assert r["game_time_play_probability_percent"] is not None


def test_official_status_wins_without_fetch(live):
    calls = live(payload())
    r = build_status_result("Downs", GAME, weather=dict(WX), official_status="cancelled")
    assert r["model"]["version"] == "v1" and not calls


def test_disabled_by_default_in_suite():
    assert not event_weather.enabled()


def test_fetch_is_cached(monkeypatch):
    monkeypatch.setenv("RAINOUT_EVENT_RISK", "on")
    calls = []
    monkeypatch.setattr(weather, "_http_get_json", lambda u, h, t: calls.append(u) or payload())
    c = MemoryCache()
    event_weather.fetch_rows(30.0, -97.0, cache=c)
    event_weather.fetch_rows(30.0, -97.0, cache=c)
    assert len(calls) == 1


def test_motocross_race_day_uses_v2_and_falls_back(monkeypatch):
    from rainout_agent import motocross as mx

    monkeypatch.setenv("RAINOUT_EVENT_RISK", "on")
    monkeypatch.setattr(event_weather, "get_cache", lambda: MemoryCache())
    track = {"field_id": "t", "name": "T", "soil": "clay", "timezone": "America/Chicago",
             "coordinates": {"lat": 30.0, "lon": -97.0}}
    entry = {"play_probability": 80, "model": {}}
    ws, we = START, START + timedelta(hours=6)
    wet = {START - timedelta(hours=h) for h in range(6, 16)}
    monkeypatch.setattr(weather, "_http_get_json", lambda u, h, t: payload(rain_mm_per_hour=6.0, wet_hours=wet))
    monkeypatch.setattr(mx, "_field", lambda tr: {"coordinates": tr["coordinates"]})
    mx._apply_event_risk(track, entry, ws, we, {"thunderstorm_likely": False}, "clay")
    assert entry["model"]["version"] == "v2" and entry["play_probability"] < 80

    entry = {"play_probability": 80, "model": {}}
    def boom(*a):
        raise OSError("down")
    monkeypatch.setattr(weather, "_http_get_json", boom)
    mx._apply_event_risk(track, entry, ws, we, {"thunderstorm_likely": False}, "clay")
    assert entry["model"]["version"] == "v1" and entry["play_probability"] == 80


def test_wetness_totals_are_reported(live):
    wet = {START - timedelta(hours=h) for h in range(10, 20)}          # 50 mm last night
    live(payload(rain_mm_per_hour=5.0, wet_hours=wet))
    f = status()["model"]["features"]
    assert f["rain_72h_in"] == f["rain_7d_in"] == round(50 / 25.4, 2)
    assert f["et0_7d_in"] is not None and f["wetness_in"] is not None and f["wetness_in"] > 0

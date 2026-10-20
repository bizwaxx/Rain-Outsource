"""Unified go / wait / no-go across sports and construction (synthetic weather, stubbed sports core)."""
import json

import pytest

from rainout_agent import go_no_go as gng
from rainout_agent import mcp_server, rain, weather
from rainout_agent.cache import MemoryCache, set_cache
from rainout_agent.construction_api import handle_construction_request
from tests.construction_fakes import NOW, SiteFake, local

SHAPE = {"activity", "decision", "probability_percent", "reason", "recheck_at", "sources", "routed_to", "event_confirmed", "weather_decision"}
T0, T1 = local("2026-10-22 08:00"), local("2026-10-22 16:00")


def run(monkeypatch, activity, hour=None, **kw):
    set_cache(MemoryCache())
    monkeypatch.setattr(weather, "_http_get_json", SiteFake(hour))
    kw.setdefault("time", "2026-10-22 08:00")
    if "venue" not in kw and "track_id" not in kw and "location" not in kw:
        kw.setdefault("lat", 30.27)
        kw.setdefault("lon", -97.74)
    return gng.go_no_go(activity, now=NOW, **kw)


def chk(r, name):
    return next(c for c in r["details"]["checks"] if c["check"] == name)


def test_heat_index_matches_nws_table():
    assert gng.heat_index_f(90, 70) == pytest.approx(106, abs=1)
    assert gng.heat_index_f(100, 50) == pytest.approx(118, abs=1)
    assert gng.heat_index_f(70, 50) < 70


@pytest.mark.parametrize("activity", ["roofing", "painting", "general_outdoor_work", "concrete_pour", "soccer", "motocross"])
def test_every_activity_returns_the_same_shape(monkeypatch, activity):
    r = run(monkeypatch, activity)
    assert SHAPE <= set(r) and r["decision"] == "go" and r["recheck_at"] == "2026-10-21T20:00:00-05:00"


def test_roofing_wind_and_rain_after(monkeypatch):
    r = run(monkeypatch, "roofing", lambda t: {"gust": 38.0} if T0 <= t < T1 else {})
    assert r["decision"] == "no_go" and chk(r, "wind")["status"] == "no_go" and r["routed_to"] == "workability"
    r = run(monkeypatch, "roofing", lambda t: {"pop": 50} if T1 <= t < local("2026-10-22 18:00") else {})
    assert r["decision"] == "wait" and chk(r, "rain_after")["status"] == "wait" and "dry in the deck" in r["reason"]
    assert "not engineering advice" in r["disclaimer"]


def test_painting_cold_humid_dew(monkeypatch):
    r = run(monkeypatch, "painting", lambda t: {"temp": 45.0, "rh": 92, "dew": 43.0})
    assert r["decision"] == "wait"
    assert {c["check"] for c in r["details"]["checks"] if c["status"] == "wait"} >= {"cold", "humidity", "dew_point"}
    assert r["probability_percent"] < 70 and any("SSPC" in ref for ref in r["details"]["references"])


def test_general_work_heat_index_and_thunder(monkeypatch):
    r = run(monkeypatch, "work", lambda t: {"temp": 100.0, "rh": 50})
    assert r["activity"] == "general_outdoor_work" and r["decision"] == "no_go" and chk(r, "heat_index")["status"] == "no_go"
    r = run(monkeypatch, "general_outdoor_work", lambda t: {"code": 95, "pop": 30} if T0 <= t < T1 else {})
    assert r["decision"] == "no_go" and chk(r, "thunderstorms")["status"] == "no_go"


def test_concrete_routes_to_concrete_checker(monkeypatch):
    r = run(monkeypatch, "concrete", lambda t: {"pop": 80, "mm": 3.0} if T0 <= t < local("2026-10-22 12:00") else {}, options={"mix": "high_early"})
    assert r["routed_to"] == "check_concrete_pour" and r["decision"] == "no_go" and r["best_alternative"]["decision"] == "go"
    assert r["details"]["mix"]["key"] == "high_early"


def test_sports_without_venue_use_weather_only_odds(monkeypatch):
    r = run(monkeypatch, "softball", lambda t: {"pop": 70} if T0 <= t < T1 else {}, location="Austin, TX")
    assert r["routed_to"] == "weather_only_play_odds" and r["probability_percent"] == 42 and r["decision"] == "wait"
    assert r["details"]["official_status"] == "unknown" and r["location"]["precision"] == "place"


def test_sports_with_venue_use_status_model(monkeypatch):
    seen = {}

    def fake_status(field_query, game_time):
        seen["args"] = (field_query, game_time)
        return {"field": "Krieg Fields", "official_status": "unknown", "game_time_play_probability_percent": 60,
                "reason": "Rain chance is 40%.", "spoken_answer": "Official status is unknown.", "weather_source": "NWS",
                "official_status_source_url": "https://example.org/rainout", "rain_chance_percent": 40}

    monkeypatch.setattr("rainout_agent.status_api.build_status_result", fake_status)
    r = gng.go_no_go("softball", venue="Craig Fields", time="2026-10-23 20:20", now=NOW)
    assert seen["args"][1] == "2026-10-23T20:20:00-05:00" and r["routed_to"] == "get_venue_status"
    assert r["decision"] == "wait" and r["probability_percent"] == 60 and r["recheck_at"] == "2026-10-22T20:20:00-05:00"
    assert any(s.get("url") == "https://example.org/rainout" for s in r["sources"])
    monkeypatch.setattr("rainout_agent.status_api.build_status_result",
                        lambda **kw: {**fake_status(**kw), "official_status": "cancelled", "game_time_play_probability_percent": 0})
    assert gng.go_no_go("softball", venue="Krieg", time="2026-10-23 20:20", now=NOW)["decision"] == "no_go"
    with pytest.raises(rain.RainError, match="Unknown venue"):
        gng.go_no_go("soccer", venue="Nowhere Park Zzz", time="2026-10-23 20:20", now=NOW)


def test_motocross_with_track(monkeypatch):
    from rainout_agent import motocross

    def fake_check(track_id, date_param=None, now=None, **kw):
        assert date_param == "2026-10-24"
        return {"track": {"name": "Swan MX", "track_id": track_id}, "found": True, "answer": "Race is on, 85%.",
                "event": {"play_probability": 85, "official_status": "unknown", "start": "2026-10-24T08:00:00-05:00",
                          "end": "2026-10-24T17:00:00-05:00", "sources": [{"name": "Swan calendar", "url": "https://example.org"}]}}

    monkeypatch.setattr(motocross, "check_race", fake_check)
    track = motocross.tracks()[0]["field_id"]
    r = gng.go_no_go("motocross", track_id=track, time="2026-10-24 09:00", now=NOW)
    assert r["routed_to"] == "check_race" and r["decision"] == "go" and r["probability_percent"] == 85 and r["reason"] == "Race is on, 85%."
    assert r["event_confirmed"] is True and r["weather_decision"] == "go"
    with pytest.raises(rain.RainError, match="Unknown track_id"):
        gng.go_no_go("motocross", track_id="nope-track", now=NOW)


def test_errors(monkeypatch):
    with pytest.raises(rain.RainError, match="activity must be one of"):
        run(monkeypatch, "skydiving")
    with pytest.raises(rain.RainError, match="Pass venue"):
        gng.go_no_go("softball", now=NOW)
    with pytest.raises(rain.RainError, match="lat and lon for the site"):
        gng.go_no_go("roofing", now=NOW)
    with pytest.raises(rain.RainError, match="in the past"):
        run(monkeypatch, "roofing", time="2026-10-19 08:00")
    with pytest.raises(rain.RainError, match="No forecast"):
        run(monkeypatch, "roofing", time="2026-11-20 08:00")


def test_rest_and_mcp(monkeypatch):
    monkeypatch.setattr(weather, "_http_get_json", SiteFake())
    monkeypatch.setattr(gng, "_now", lambda: NOW)
    s, _, body, _ = handle_construction_request("GET", "/v1/go-no-go", "activity=roofing&lat=30.27&lon=-97.74&time=2026-10-22%2008:00")
    assert s == 200 and SHAPE <= set(json.loads(body))
    s, _, body, _ = handle_construction_request("POST", "/v1/go-no-go", "", json.dumps(
        {"activity": "concrete_pour", "lat": 30.27, "lon": -97.74, "time": "2026-10-22 07:00", "mix": "retarded"}).encode(), "application/json")
    assert s == 200 and json.loads(body)["details"]["mix"]["key"] == "retarded"
    assert handle_construction_request("GET", "/v1/go-no-go", "activity=nope")[0] == 400
    res = mcp_server.call_tool("go_no_go", {"activity": "painting", "lat": 30.27, "lon": -97.74, "time": "2026-10-22 08:00"})
    assert res["isError"] is False and res["structuredContent"]["routed_to"] == "workability"
    assert mcp_server.call_tool("go_no_go", {"activity": "roofing"})["isError"] is True


def _not_found(odds):
    def fake(track_id, date_param=None, now=None, **kw):
        return {"track": {"name": "Swan MX", "track_id": track_id}, "found": False, "event": None,
                "answer": "I don't see a race at Swan MX on Sat Oct 24 in its public calendars.",
                "conditions_if_racing": {"play_probability": odds, "reason": "dry"}, "next_step": "ask_user_for_events_url"}
    return fake


def test_motocross_without_a_listed_race_is_never_a_bare_go(monkeypatch):
    from rainout_agent import motocross

    track = motocross.tracks()[0]["field_id"]
    monkeypatch.setattr(motocross, "check_race", _not_found(96))
    r = gng.go_no_go("motocross", track_id=track, time="2026-10-24 09:00", now=NOW)
    assert r["decision"] == "no_event_found" and r["event_confirmed"] is False and r["weather_decision"] == "go"
    assert r["probability_percent"] == 96 and r["details"]["next_step"] == "ask_user_for_events_url"
    assert r["reason"].startswith("No race is listed then; weather alone looks fine (96% weather-only odds).")
    assert "public calendars" in r["reason"]
    monkeypatch.setattr(motocross, "check_race", _not_found(30))
    r = gng.go_no_go("motocross", track_id=track, time="2026-10-24 09:00", now=NOW)
    assert r["decision"] == "no_event_found" and r["weather_decision"] == "no_go"
    assert r["reason"].startswith("No race is listed then; weather alone looks bad")
    monkeypatch.setattr(motocross, "check_race", _not_found(None))
    r = gng.go_no_go("motocross", track_id=track, time="2026-10-24 09:00", now=NOW)
    assert r["decision"] == "no_event_found" and r["probability_percent"] is None


def test_event_confirmed_is_null_when_no_schedule_lookup_runs(monkeypatch):
    for act in ("roofing", "concrete_pour", "soccer"):
        r = run(monkeypatch, act)
        assert r["event_confirmed"] is None and r["weather_decision"] == r["decision"]

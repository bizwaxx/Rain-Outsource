"""Concrete pour go / wait / no-go (synthetic weather)."""
import json

import pytest

from rainout_agent import concrete, mcp_server, rain, weather
from rainout_agent.cache import MemoryCache, set_cache
from rainout_agent.construction_api import handle_construction_request
from tests.construction_fakes import NOW, SiteFake, local

POUR = "2026-10-22 07:00"
P0, P1 = local("2026-10-22 07:00"), local("2026-10-22 11:00")  # pour window
C1 = local("2026-10-22 15:00")  # early-set window end (standard mix, 4 h)


def run(monkeypatch, hour=None, nws=False, **kw):
    set_cache(MemoryCache())  # fresh weather per scenario
    monkeypatch.setattr(weather, "_http_get_json", SiteFake(hour, nws))
    kw.setdefault("pour_start", POUR)
    return concrete.check_concrete_pour(kw.pop("location", None), lat=kw.pop("lat", 30.27), lon=kw.pop("lon", -97.74), now=NOW, **kw)


def check(r, name):
    return next(c for c in r["checks"] if c["check"] == name)


def test_evaporation_rate_matches_aci_nomograph_equation():
    assert concrete.evaporation_rate(70, 50, 5) == pytest.approx(0.29, abs=0.01)
    assert concrete.evaporation_rate(95, 15, 20) > 1.0
    assert concrete.evaporation_rate(60, 100, 0) == 0.0


def test_dry_mild_day_is_go(monkeypatch):
    r = run(monkeypatch)
    assert r["decision"] == "go" and r["probability_percent"] == 100 and r["reason"].startswith("Go: 0% rain chance")
    assert r["pour"]["start"] == "2026-10-22T07:00:00-05:00" and r["pour"]["end"] == "2026-10-22T11:00:00-05:00"
    assert r["early_set_window"]["hours"] == 4 and r["cure_window"]["hours"] == 24
    assert r["best_alternative"] is None and r["recheck_at"] == "2026-10-21T19:00:00-05:00"
    assert "not engineering advice" in r["disclaimer"] and r["thresholds"]["temperature_f"]["cold"] == 40
    assert any("ACI PRC-306" in ref for ref in r["thresholds"]["references"])


def test_rain_during_pour_is_no_go_with_better_window(monkeypatch):
    r = run(monkeypatch, lambda t: {"pop": 80, "mm": 2.0} if P0 <= t < P1 else {})
    assert r["decision"] == "no_go" and r["rain_probability_percent"] == 80 and r["probability_percent"] == 20
    assert check(r, "rain_during_pour")["status"] == "no_go" and "80% rain chance during the pour" in r["reason"]
    alt = r["best_alternative"]
    assert alt["decision"] == "go" and alt["start"].endswith("-05:00") and "Better window" in r["reason"]


def test_rain_in_early_set_window_is_wait(monkeypatch):
    r = run(monkeypatch, lambda t: {"pop": 40} if P1 <= t < C1 else {})
    assert r["decision"] == "wait" and check(r, "rain_during_early_set")["status"] == "wait"
    assert check(r, "rain_during_pour")["status"] == "ok"
    # high-early mix: 2 h window, rain later is outside it
    r2 = run(monkeypatch, lambda t: {"pop": 40} if local("2026-10-22 13:00") <= t < C1 else {}, mix="high_early")
    assert r2["early_set_window"]["hours"] == 2 and check(r2, "rain_during_early_set")["status"] == "ok"


def test_heavy_rain_later_in_cure(monkeypatch):
    late = lambda t: {"pop": 90, "mm": 5.0} if local("2026-10-22 20:00") <= t < local("2026-10-22 23:00") else {}  # noqa: E731
    assert run(monkeypatch, late)["decision"] == "wait"
    assert run(monkeypatch, late, protection=True)["decision"] == "go"


def test_thunderstorms_are_no_go(monkeypatch):
    r = run(monkeypatch, lambda t: {"code": 95, "pop": 20} if P0 <= t < P1 else {})
    assert r["decision"] == "no_go" and check(r, "thunderstorms")["status"] == "no_go"


def test_freeze_cold_and_heat(monkeypatch):
    freeze = lambda t: {"temp": 29.0} if local("2026-10-23 03:00") <= t < local("2026-10-23 06:00") else {"temp": 45.0}  # noqa: E731
    r = run(monkeypatch, freeze)
    assert r["decision"] == "no_go" and check(r, "freeze")["value"] == 29.0
    assert run(monkeypatch, freeze, protection=True)["decision"] == "wait"
    cold = run(monkeypatch, lambda t: {"temp": 38.0})
    assert cold["decision"] == "wait" and "ACI 306" in check(cold, "cold")["detail"]
    hot = run(monkeypatch, lambda t: {"temp": 95.0, "rh": 60})
    assert hot["decision"] == "wait" and check(hot, "heat")["status"] == "wait"


def test_evaporation_and_mix_specific_limit(monkeypatch):
    windy_dry = lambda t: {"temp": 80.0, "rh": 30, "wind": 10.0}  # noqa: E731  about 0.94 kg/m2/h
    std = run(monkeypatch, windy_dry)
    assert check(std, "evaporation")["value"] == 0.94
    assert check(std, "evaporation")["status"] == "ok"
    sf = run(monkeypatch, windy_dry, mix="low_wc_or_silica_fume")
    assert check(sf, "evaporation")["status"] == "wait" and sf["decision"] == "wait"


def test_wind_gusts(monkeypatch):
    r = run(monkeypatch, lambda t: {"gust": 40.0})
    assert check(r, "wind")["status"] == "wait"


def test_nws_rain_chance_is_primary_in_us(monkeypatch):
    r = run(monkeypatch, nws=lambda t: 95 if P0 <= t < P1 else 5)
    assert r["decision"] == "no_go" and r["rain_probability_percent"] == 95
    assert any("National Weather Service" in s["name"] for s in r["sources"])


def test_input_errors(monkeypatch):
    with pytest.raises(rain.RainError, match="pour_start is required"):
        run(monkeypatch, pour_start=None)
    with pytest.raises(rain.RainError, match="mix must be one of"):
        run(monkeypatch, mix="magic")
    with pytest.raises(rain.RainError, match="in the past"):
        run(monkeypatch, pour_start="2026-10-20 07:00")
    with pytest.raises(rain.RainError, match="forecast only reaches"):
        run(monkeypatch, pour_start="2026-10-27 07:00")
    with pytest.raises(rain.RainError, match="duration_hours"):
        run(monkeypatch, duration_hours=40)


def test_town_name_and_threshold_override(monkeypatch, tmp_path):
    r = run(monkeypatch, location="Austin, TX", lat=None, lon=None)
    assert r["location"]["precision"] == "place"
    over = tmp_path / "t.json"
    over.write_text(json.dumps({"concrete": {"temperature_f": {"cold": 70}}}))
    monkeypatch.setenv("RAINOUT_CONSTRUCTION_THRESHOLDS", str(over))
    r = run(monkeypatch)
    assert r["decision"] == "wait" and r["thresholds"]["temperature_f"]["cold"] == 70


def test_rest_and_mcp(monkeypatch):
    monkeypatch.setattr(weather, "_http_get_json", SiteFake(lambda t: {"pop": 80, "mm": 2.0} if P0 <= t < P1 else {}))
    monkeypatch.setattr(concrete, "_now", lambda: NOW)
    s, _, body, cc = handle_construction_request("GET", "/v1/construction/concrete-pour", "lat=30.27&lon=-97.74&pour_start=2026-10-22%2007:00")
    data = json.loads(body)
    assert s == 200 and data["decision"] == "no_go" and data["best_alternative"]["decision"] == "go" and "max-age" in cc
    s, _, body, cc = handle_construction_request("POST", "/v1/construction/concrete-pour/", "",
                                                 json.dumps({"location": "30.27,-97.74", "pour_start": "2026-10-22T07:00:00-05:00",
                                                             "search_days": 0, "mix": "high_early"}).encode(), "application/json")
    data = json.loads(body)
    assert s == 200 and data["best_alternative"] is None and data["early_set_window"]["hours"] == 2 and cc == "no-store"
    s, _, body, _ = handle_construction_request("GET", "/v1/construction/concrete-pour", "lat=30.27&lon=-97.74")
    assert s == 400 and "pour_start" in json.loads(body)["message"]
    assert handle_construction_request("POST", "/v1/construction/concrete-pour", "", b"[1]", "application/json")[0] == 400
    assert handle_construction_request("POST", "/v1/construction/concrete-pour", "", b"{bad", "application/json")[0] == 400
    assert handle_construction_request("GET", "/v1/construction/nope", "")[0] == 404
    res = mcp_server.call_tool("check_concrete_pour", {"lat": 30.27, "lon": -97.74, "pour_start": "2026-10-22 07:00"})
    assert res["isError"] is False and res["structuredContent"]["decision"] == "no_go"
    assert mcp_server.call_tool("check_concrete_pour", {"lat": 30.27, "lon": -97.74})["isError"] is True
    res = mcp_server.call_tool("check_concrete_pour", {"pour_start": "2026-10-22 07:00"})
    assert res["isError"] is True and "lat and lon" in res["content"][0]["text"]

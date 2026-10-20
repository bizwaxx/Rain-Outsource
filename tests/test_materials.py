"""Materials rain-damage risk (synthetic weather)."""
import json

import pytest

from rainout_agent import materials, mcp_server, rain, weather
from rainout_agent.cache import MemoryCache, set_cache
from rainout_agent.construction_api import handle_construction_request
from tests.construction_fakes import NOW, SiteFake, local

W0, W1 = local("2026-10-21 18:00"), local("2026-10-21 21:00")


def run(monkeypatch, hour=None, nws=False, **kw):
    set_cache(MemoryCache())
    monkeypatch.setattr(weather, "_http_get_json", SiteFake(hour, nws))
    kw.setdefault("materials", ["drywall", "lumber", "rebar"])
    return materials.check_materials_risk(kw.pop("location", None), lat=kw.pop("lat", 30.27), lon=kw.pop("lon", -97.74), now=NOW, **kw)


def mat(r, key):
    return next(x for x in r["materials"] if x["material"] == key)


def storm(t):
    return {"pop": 90, "mm": 6.0, "code": 63} if W0 <= t < W1 else {}


def test_dry_window_is_low(monkeypatch):
    r = run(monkeypatch)
    assert r["overall_risk"] == "low" and r["rain"]["level"] == "none" and r["window"]["hours"] == 24
    assert all(x["risk"] == "low" and x["actions"] == [] for x in r["materials"])
    assert r["summary"].startswith("Low risk for everything") and "not engineering advice" in r["disclaimer"]


def test_heavy_rain_ranks_materials_and_gives_actions(monkeypatch):
    r = run(monkeypatch, storm, materials=["drywall", "lumber", "rebar", "cement", "roof deck"])
    assert r["rain"]["level"] == "heavy" and r["rain"]["total_mm"] == 18.0 and r["rain"]["peak_intensity"] == "moderate"
    assert r["rain"]["wettest_hour"] == "2026-10-21T18:00:00-05:00"
    assert mat(r, "drywall")["risk"] == "severe" and mat(r, "lumber")["risk"] == "high" and mat(r, "rebar")["risk"] == "low"
    assert mat(r, "cement_bags")["risk"] == "severe" and mat(r, "open_roof_deck")["risk"] == "severe"
    assert r["overall_risk"] == "severe" and "Wettest hour: Wed 6 PM" in r["summary"]
    assert mat(r, "drywall")["actions"] and "Gypsum Association" in mat(r, "drywall")["reference"]


def test_covered_drops_one_step_unless_gusty(monkeypatch):
    r = run(monkeypatch, storm, covered=True)
    assert mat(r, "drywall")["risk"] == "high" and mat(r, "lumber")["risk"] == "moderate"
    gusty = run(monkeypatch, lambda t: {**storm(t), "gust": 30.0}, covered=True)
    assert mat(gusty, "drywall")["risk"] == "severe" and gusty["wind"]["covers_at_risk"]
    assert mat(gusty, "drywall")["actions"][0].startswith("Tie down") and "blow covers off" in mat(gusty, "drywall")["reason"]
    mixed = run(monkeypatch, storm, materials=[{"material": "drywall", "covered": True}, "drywall"])
    assert [x["risk"] for x in mixed["materials"]] == ["high", "severe"]


def test_possible_and_likely_levels(monkeypatch):
    possible = run(monkeypatch, lambda t: {"pop": 30} if W0 <= t < W1 else {})
    assert possible["rain"]["level"] == "possible" and mat(possible, "drywall")["risk"] == "high" and mat(possible, "lumber")["risk"] == "low"
    likely = run(monkeypatch, lambda t: {"pop": 60, "mm": 1.0} if W0 <= t < W1 else {})
    assert likely["rain"]["level"] == "likely" and mat(likely, "lumber")["risk"] == "moderate"


def test_humidity_bumps_sensitive_materials(monkeypatch):
    r = run(monkeypatch, lambda t: {"rh": 95}, materials=["drywall", "lumber"])
    assert mat(r, "drywall")["risk"] == "moderate" and mat(r, "lumber")["risk"] == "low" and r["humid_hours"] == 24


def test_window_options_and_errors(monkeypatch):
    r = run(monkeypatch, start="2026-10-22 06:00", end="2026-10-22 18:00")
    assert r["window"]["start"] == "2026-10-22T06:00:00-05:00" and r["window"]["hours"] == 12
    r = run(monkeypatch, hours=200 - 40)
    assert r["window"]["hours"] == 160 or "note" in r["window"]
    with pytest.raises(rain.RainError, match="materials is required"):
        run(monkeypatch, materials=[])
    with pytest.raises(rain.RainError, match="Unknown material 'unobtainium'"):
        run(monkeypatch, materials=["unobtainium"])
    with pytest.raises(rain.RainError, match="end must be after start"):
        run(monkeypatch, start="2026-10-22 06:00", end="2026-10-22 05:00")
    with pytest.raises(rain.RainError, match="at most 7 days"):
        run(monkeypatch, start="2026-10-22 06:00", end="2026-10-30 06:00")
    with pytest.raises(rain.RainError, match="hours must be"):
        run(monkeypatch, hours=500)
    assert [x["material"] for x in run(monkeypatch, materials="Drywall, plywood, OSB, bricks")["materials"]] == ["drywall", "osb", "masonry_units"]


def test_rest_and_mcp(monkeypatch):
    monkeypatch.setattr(weather, "_http_get_json", SiteFake(storm))
    monkeypatch.setattr(materials, "_now", lambda: NOW)
    s, _, body, _ = handle_construction_request("GET", "/v1/construction/materials", "lat=30.27&lon=-97.74&materials=drywall,lumber")
    assert s == 200 and json.loads(body)["overall_risk"] == "severe"
    s, _, body, _ = handle_construction_request("POST", "/v1/construction/materials", "", json.dumps(
        {"lat": 30.27, "lon": -97.74, "materials": [{"material": "lumber", "covered": True}]}).encode(), "application/json")
    assert s == 200 and json.loads(body)["materials"][0]["risk"] == "moderate"
    s, _, body, _ = handle_construction_request("GET", "/v1/construction/materials", "lat=30.27&lon=-97.74")
    assert s == 400
    res = mcp_server.call_tool("check_materials_risk", {"lat": 30.27, "lon": -97.74, "materials": ["drywall", "lumber"],
                                                        "covered_materials": ["lumber"]})
    sc = res["structuredContent"]
    assert res["isError"] is False and mat(sc, "lumber")["covered"] and not mat(sc, "drywall")["covered"]
    assert mcp_server.call_tool("check_materials_risk", {"lat": 30.27, "lon": -97.74})["isError"] is True
    assert mcp_server.call_tool("check_materials_risk", {"materials": ["drywall"]})["isError"] is True

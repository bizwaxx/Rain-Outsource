"""Two-hour nowcast: Open-Meteo minutely_15 + NWS gridpoints + station obs, REST and MCP."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import mcp_server, nowcast, rain, weather
from rainout_agent.construction_api import handle_construction_request
from rainout_agent.http_app import application

NOW = datetime(2026, 10, 21, 13, 7, tzinfo=timezone.utc)  # Wed 8:07 AM CDT
SLOT0 = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)

GEO = {"austin": [{"name": "Austin", "admin1": "Texas", "country": "United States", "country_code": "US",
                   "latitude": 30.26715, "longitude": -97.74306, "timezone": "America/Chicago", "population": 961855}],
       "springfield": [
           {"name": "Springfield", "admin1": "Missouri", "country": "United States", "country_code": "US",
            "latitude": 37.2, "longitude": -93.3, "population": 170188},
           {"name": "Springfield", "admin1": "Illinois", "country": "United States", "country_code": "US",
            "latitude": 39.8, "longitude": -89.6, "population": 114394}]}


def m15(mm, pop=None, tzname="America/Chicago"):
    # past_minutely_15=1 -> first timestamp is SLOT0 (covers 12:45-13:00), then 12 more.
    times = [int((SLOT0 + timedelta(minutes=15 * i)).timestamp()) for i in range(13)]
    return {"timezone": tzname, "utc_offset_seconds": -18000,
            "minutely_15": {"time": times, "precipitation": [0.0] + mm + [0.0] * (12 - len(mm)),
                            "precipitation_probability": pop or [5] * 13, "weather_code": [3] * 13}}


class Fake:
    def __init__(self, mm=None, pop=40, om_fail=False, nws_fail=False, obs_mm=1.3):
        self.calls, self.mm, self.pop = [], mm or [0.0] * 12, pop
        self.om_fail, self.nws_fail, self.obs_mm = om_fail, nws_fail, obs_mm

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        if url.startswith(rain.GEOCODE_URL):
            return {"results": GEO.get(url.split("name=")[1].split("&")[0].lower(), [])}
        if url.startswith(weather.OPEN_METEO_URL):
            if self.om_fail:
                raise OSError("down")
            assert "minutely_15=" in url
            return m15(self.mm)
        if url.startswith("https://api.precip.ai/"):
            assert headers["Authorization"] == "Bearer test-key"
            return {"type": "FeatureCollection", "features": [{"properties": {"minutes": [
                {"startTime": (SLOT0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "precip_rate": 8.0 if m >= 30 else 0}
                for m in range(0, 120)]}}]}
        if url.startswith("https://api.weather.gov/"):
            if self.nws_fail:
                raise TimeoutError("nws")
            if "/points/" in url:
                return {"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/EWX/156,91",
                                       "observationStations": "https://api.weather.gov/gridpoints/EWX/156,91/stations",
                                       "timeZone": "America/Chicago", "forecastHourly": "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly"}}
            if url.endswith("/stations?limit=1"):
                return {"features": [{"properties": {"stationIdentifier": "KATT", "name": "Camp Mabry"}}]}
            if url.endswith("/observations/latest"):
                return {"properties": {"timestamp": datetime.now(timezone.utc).isoformat(), "textDescription": "Light Rain",
                                       "precipitationLastHour": {"value": self.obs_mm}}}
            if url.endswith("/gridpoints/EWX/156,91"):
                return {"properties": {"updateTime": "2026-10-21T12:00:00+00:00",
                                       "probabilityOfPrecipitation": {"values": [
                                           {"validTime": "2026-10-21T12:00:00+00:00/PT1H", "value": 20},
                                           {"validTime": "2026-10-21T13:00:00+00:00/PT2H", "value": self.pop}]},
                                       "quantitativePrecipitation": {"values": [
                                           {"validTime": "2026-10-21T12:00:00+00:00/PT6H", "value": 6.0}]}}}
        raise AssertionError(url)


@pytest.fixture
def fake(monkeypatch):
    f = Fake(mm=[0.0, 0.0, 0.3, 1.2, 2.5, 0.4])
    monkeypatch.setattr(weather, "_http_get_json", f)
    monkeypatch.delenv("RAINOUT_PRECIP_API_KEY", raising=False)
    return f


def test_parse_valid_time_and_intensity():
    s, e = nowcast.parse_valid_time("2026-10-20T01:00:00+00:00/P4DT17H")
    assert e - s == timedelta(days=4, hours=17)
    assert nowcast.parse_valid_time("garbage") is None
    assert [nowcast.intensity(x) for x in (0, 1, 5, 10)] == ["none", "light", "moderate", "heavy"]


def test_nowcast_rain_starts_and_merges_sources(fake):
    r = nowcast.nowcast(30.27, -97.74, label="Krieg", now=NOW)
    assert len(r["intervals"]) == 8 and r["interval_minutes"] == 15
    first = r["intervals"][0]
    assert first["start"] == "2026-10-21T08:00:00-05:00" and first["probability_source"] == "nws"
    assert first["probability_percent"] == 40 and first["precip_mm"] == 0.0
    # Open-Meteo value at 13:45Z covers 13:30-13:45 -> slot 2
    assert r["intervals"][2]["precip_mm"] == 0.3 and r["intervals"][2]["amount_source"] == "open_meteo"
    assert r["intervals"][4]["intensity"] == "heavy"  # 2.5 mm in 15 min = 10 mm/h
    assert r["rain_expected"] and r["rain_starts_at"] == "2026-10-21T08:30:00-05:00"
    assert r["peak_intensity"] == "heavy" and r["total_precip_mm"] == 4.4
    assert "expected to start around 8:30 AM" in r["summary"] and "Krieg" in r["summary"]
    assert r["observed"]["station_id"] == "KATT" and r["observed"]["precip_last_hour_in"] == 0.05
    assert "measured 0.05 in" in r["summary"]
    assert {s["name"].split(" ")[0] for s in r["sources"]} == {"Open-Meteo", "National", "NWS"}
    assert r["recheck_after_minutes"] == 15


def test_nowcast_dry_and_cached(fake):
    fake.mm = [0.0] * 12
    fake.pop = 10
    r = nowcast.nowcast(30.27, -97.74, now=NOW)
    assert not r["rain_expected"] and r["summary"].startswith("No rain expected") and r["recheck_after_minutes"] == 30
    n = len(fake.calls)
    nowcast.nowcast(30.27, -97.74, now=NOW)
    assert len(fake.calls) == n  # everything cached


def test_open_meteo_down_uses_spread_nws_qpf(fake):
    fake.om_fail = True
    r = nowcast.nowcast(30.27, -97.74, now=NOW)
    assert r["intervals"][0]["amount_source"] == "nws_qpf_spread" and r["intervals"][0]["precip_mm"] == 0.25
    assert any("spread evenly" in n for n in r["notes"])


def test_outside_us_uses_open_meteo_only(fake):
    r = nowcast.nowcast(51.5, -0.12, now=NOW)
    assert r["intervals"][0]["probability_source"] == "open_meteo" and r["observed"] is None
    assert not any("weather.gov" in u for u, _ in fake.calls)


def test_everything_down_raises(fake):
    fake.om_fail = fake.nws_fail = True
    with pytest.raises(weather.WeatherUnavailable):
        nowcast.nowcast(30.27, -97.74, now=NOW)


def test_optional_precip_provider_only_with_key(fake, monkeypatch):
    nowcast.nowcast(30.27, -97.74, now=NOW)
    assert not any("precip.ai" in u for u, _ in fake.calls)
    monkeypatch.setenv("RAINOUT_PRECIP_API_KEY", "test-key")
    from rainout_agent.cache import MemoryCache
    r = nowcast.nowcast(30.27, -97.74, now=NOW, cache=MemoryCache())
    assert r["intervals"][2]["amount_source"] == "precip_ai" and r["intervals"][2]["precip_mm"] == 2.0
    assert any(s["name"].startswith("Precip") for s in r["sources"])


def test_place_names_addresses_and_ambiguity(fake):
    r = nowcast.get_precipitation_nowcast("123 Main St, Austin", now=NOW)
    assert r["location"]["precision"] == "place" and "not the street address" in r["location"]["precision_note"]
    amb = nowcast.get_precipitation_nowcast("Springfield", now=NOW)
    assert amb["ambiguous"] and amb["next_step"] == "ask_user_which_place"
    assert nowcast.get_precipitation_nowcast(lat=30.27, lon=-97.74, now=NOW)["location"]["precision"] == "exact"


def test_rest_endpoint(fake):
    s, ct, body, cc = handle_construction_request("GET", "/v1/nowcast", "lat=30.27&lon=-97.74")
    assert s == 200 and json.loads(body)["interval_minutes"] == 15 and "max-age" in cc
    assert handle_construction_request("GET", "/v1/nowcast", "")[0] == 400
    assert handle_construction_request("GET", "/v1/nowcast", "lat=95&lon=0")[0] == 400
    assert handle_construction_request("GET", "/v1/nowcast", "q=Zzyzx")[0] == 404
    assert handle_construction_request("DELETE", "/v1/nowcast", "")[0] == 405
    fake.om_fail = fake.nws_fail = True
    s, _, body, _ = handle_construction_request("GET", "/v1/nowcast", "lat=31&lon=-97")
    assert s == 502 and json.loads(body)["error"] == "weather_unavailable"


def test_wsgi_route(fake):
    out = {}
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/nowcast", "QUERY_STRING": "lat=30.27&lon=-97.74"},
                                lambda st, h: out.update(status=st)))
    assert out["status"].startswith("200") and json.loads(body)["location"]["lat"] == 30.27


def test_mcp_tool(fake):
    res = mcp_server.call_tool("get_precipitation_nowcast", {"lat": 30.27, "lon": -97.74})
    assert res["isError"] is False and res["structuredContent"]["interval_minutes"] == 15
    assert mcp_server.call_tool("get_precipitation_nowcast", {})["isError"] is True
    assert mcp_server.call_tool("get_precipitation_nowcast", {"query": "Zzyzx"})["isError"] is True


def test_vercel_handler_and_route(fake):
    from pathlib import Path

    from api.construction import handler
    from tests.test_vercel_api import HandlerHarness

    code, headers, body = HandlerHarness(handler, "/v1/nowcast?lat=30.27&lon=-97.74").get_json()
    assert code == 200 and body["interval_minutes"] == 15 and headers["Access-Control-Allow-Origin"] == "*"
    cfg = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
    assert {"src": "/v1/nowcast/?", "dest": "/api/construction.py"} in cfg["routes"]
    assert {"src": "api/construction.py", "use": "@vercel/python"} in cfg["builds"]

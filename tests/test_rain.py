"""Shared worldwide rain module, /v1/rain, MCP get_rain_chance and the file cache."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import mcp_server, motocross, rain, weather
from rainout_agent.cache import FileCache, MemoryCache, build_cache, set_cache
from rainout_agent.rain_api import handle_rain_request

NOW = datetime(2026, 10, 17, 21, 30, tzinfo=timezone.utc)  # Sat 4:30 PM CDT
START = datetime(2026, 10, 14, 0, tzinfo=timezone.utc)  # Open-Meteo past_days=3

GEO = {
    "austin": [
        {"name": "Austin", "admin1": "Texas", "admin2": "Travis", "country": "United States", "country_code": "US",
         "latitude": 30.26715, "longitude": -97.74306, "timezone": "America/Chicago", "population": 961855},
        {"name": "Austin", "admin1": "Minnesota", "country": "United States", "country_code": "US",
         "latitude": 43.66663, "longitude": -92.97464, "timezone": "America/Chicago", "population": 24718},
    ],
    "london": [
        {"name": "London", "admin1": "England", "country": "United Kingdom", "country_code": "GB",
         "latitude": 51.50853, "longitude": -0.12574, "timezone": "Europe/London", "population": 8961989},
        {"name": "London", "admin1": "Ontario", "country": "Canada", "country_code": "CA",
         "latitude": 42.98339, "longitude": -81.23304, "timezone": "America/Toronto", "population": 346765},
    ],
    "springfield": [
        {"name": "Springfield", "admin1": "Missouri", "country": "United States", "country_code": "US",
         "latitude": 37.21533, "longitude": -93.29824, "timezone": "America/Chicago", "population": 170188},
        {"name": "Springfield", "admin1": "Illinois", "country": "United States", "country_code": "US",
         "latitude": 39.80172, "longitude": -89.64371, "timezone": "America/Chicago", "population": 114394},
        {"name": "Springfield", "admin1": "Massachusetts", "country": "United States", "country_code": "US",
         "latitude": 42.10148, "longitude": -72.58981, "timezone": "America/New_York", "population": 154341},
    ],
}


def om_payload(tzname="America/Chicago", offset=-18000, pop=lambda i: 10, mm=lambda i: 0.0):
    hours = 24 * 19
    return {
        "latitude": 30.27, "longitude": -97.74, "timezone": tzname, "utc_offset_seconds": offset,
        "hourly": {"time": [int((START + timedelta(hours=i)).timestamp()) for i in range(hours)],
                   "precipitation_probability": [pop(i) for i in range(hours)],
                   "precipitation": [mm(i) for i in range(hours)],
                   "weather_code": [61 if pop(i) >= 50 else 3 for i in range(hours)]},
        "daily": {"time": [int((datetime(2026, 10, 14, tzinfo=timezone.utc) + timedelta(days=d)).timestamp()) - offset for d in range(19)],
                  "precipitation_probability_max": [20 + d for d in range(19)],
                  "precipitation_sum": [1.5 for _ in range(19)], "weather_code": [61 for _ in range(19)]},
    }


def nws_forecast(pop=30):
    base = NOW.replace(minute=0)
    return {"properties": {"periods": [
        {"startTime": (base + timedelta(hours=i)).astimezone(timezone(timedelta(hours=-5))).isoformat(),
         "endTime": (base + timedelta(hours=i + 1)).astimezone(timezone(timedelta(hours=-5))).isoformat(),
         "probabilityOfPrecipitation": {"value": pop}, "shortForecast": "Chance Rain Showers"} for i in range(156)]}}


class FakeAPI:
    def __init__(self, om=None, nws_pop=30, nws_fail=False, om_fail=False):
        self.calls, self.om, self.nws_pop = [], om or om_payload(), nws_pop
        self.nws_fail, self.om_fail = nws_fail, om_fail

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers.get("User-Agent")))
        if url.startswith(rain.GEOCODE_URL):
            name = url.split("name=")[1].split("&")[0].lower()
            return {"results": GEO.get(name, [])}
        if url.startswith("https://api.weather.gov/"):
            if self.nws_fail:
                raise TimeoutError("nws")
            if "/points/" in url:
                return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly",
                                       "timeZone": "America/Chicago",
                                       "relativeLocation": {"properties": {"city": "Austin", "state": "TX"}}}}
            return nws_forecast(self.nws_pop)
        if url.startswith(weather.OPEN_METEO_URL):
            if self.om_fail:
                raise OSError("om down")
            return self.om
        raise AssertionError(url)

    def count(self, part):
        return sum(1 for u, _ in self.calls if part in u)


@pytest.fixture(autouse=True)
def mem():
    set_cache(MemoryCache())
    yield
    set_cache(None)


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(weather, "_http_get_json", fake)
    return fake


# ---------------------------------------------------------------- geocoding
def test_geocode_qualifiers_and_state_abbreviations(api):
    g = rain.geocode("Austin, TX")
    assert g["place"]["label"] == "Austin, Texas, United States" and not g["ambiguous"]
    assert rain.geocode("Austin, MN")["place"]["admin1"] == "Minnesota"
    assert rain.geocode("London, Canada")["place"]["country_code"] == "CA"
    assert rain.geocode("London, UK")["place"]["country_code"] == "GB"
    assert rain.geocode("Austin TX")["place"]["admin1"] == "Texas"  # no comma


def test_big_city_wins_but_similar_sizes_are_ambiguous(api):
    assert rain.geocode("London")["ambiguous"] is False
    s = rain.geocode("Springfield")
    assert s["ambiguous"] is True and len(s["matches"]) == 3
    res = rain.rain_chance("Springfield", now=NOW)
    assert res["ambiguous"] and res["next_step"] == "ask_user_which_place" and "hourly" not in res
    assert "1. Springfield, Missouri" in res["summary"]
    picked = rain.rain_chance("Springfield", pick=2, hours=3, now=NOW)
    assert picked["place"]["admin1"] == "Illinois" and len(picked["hourly"]) == 3


def test_unknown_place_and_bad_qualifier(api):
    with pytest.raises(rain.PlaceNotFound):
        rain.geocode("Zzyzxville")
    with pytest.raises(rain.PlaceNotFound) as exc:
        rain.geocode("Austin, Narnia")
    assert exc.value.suggestions[0]["name"] == "Austin"


def test_geocode_is_cached(api):
    rain.geocode("Austin, TX")
    rain.geocode("Austin, MN")
    assert api.count("geocoding-api") == 1


def test_latlon_parsing():
    assert rain.parse_latlon("30.27,-97.74") == (30.27, -97.74)
    assert rain.parse_latlon(" 51.5 -0.12 ") == (51.5, -0.12)
    assert rain.parse_latlon("Austin") is None
    with pytest.raises(rain.RainError):
        rain.parse_latlon("95,10")
    assert rain.in_us(30.27, -97.74) and rain.in_us(21.3, -157.8) and not rain.in_us(51.5, -0.12)


# ---------------------------------------------------------------- forecasts
def test_us_place_uses_nws_chance_and_open_meteo_amounts(api):
    api.om = om_payload(mm=lambda i: 0.5)
    r = rain.rain_chance("Austin, TX", hours=6, now=NOW)
    assert r["source"]["nws_used"] and r["units"] == "in" and r["timezone"] == "America/Chicago"
    assert [h["probability_percent"] for h in r["hourly"]] == [30] * 6
    assert {h["probability_source"] for h in r["hourly"]} == {"nws"}
    assert r["hourly"][0]["precip_mm"] == 0.5 and r["hourly"][0]["precip_in"] == 0.02
    assert r["hourly"][0]["time"] == "2026-10-17T16:00:00-05:00"
    assert "30% chance" in r["summary"] and "in" in r["summary"]
    # NWS identifies us with a contact
    assert all("rainout-agent-source.vercel.app" in ua for u, ua in api.calls if "weather.gov" in u)
    assert api.count("open-meteo.com/v1/forecast") == 1


def test_world_place_skips_nws(api):
    api.om = om_payload("Europe/London", 3600, pop=lambda i: 60 if i == 24 * 3 + 23 else 5, mm=lambda i: 2.0)
    r = rain.rain_chance("London", hours=4, now=NOW)
    assert api.count("weather.gov") == 0 and not r["source"]["nws_used"]
    assert r["units"] == "mm" and r["hourly"][0]["time"].endswith("+01:00")
    assert r["source"]["probability"] == "Open-Meteo"
    assert r["other_matches"] == ["London, Ontario, Canada"]
    assert "Open-Meteo.com" in r["source"]["attribution"]


def test_nws_failure_falls_back_to_open_meteo(api):
    api.nws_fail = True
    r = rain.rain_chance("30.27,-97.74", hours=3, now=NOW)
    assert not r["source"]["nws_used"] and r["hourly"][0]["probability_source"] == "open_meteo"
    assert "NWS unavailable" in r["source"]["probability"]
    assert api.count("open-meteo.com/v1/forecast") == 1  # weather.py fallback reused the shared payload


def test_latlon_us_label_uses_nws_relative_location(api):
    r = rain.rain_chance(None, lat="30.27", lon="-97.74", hours=2, now=NOW)
    assert r["place"]["label"] == "30.2700, -97.7400 (near Austin, TX)"


def test_daily_rows_and_today_rest_of_day(api):
    api.nws_fail = True
    r = rain.rain_chance("Austin, TX", days=3, now=NOW)
    assert [d["date"] for d in r["daily"]] == ["2026-10-17", "2026-10-18", "2026-10-19"]
    assert r["daily"][0]["note"] == "rest of today" and r["daily"][0]["max_probability_percent"] == 10
    assert r["daily"][1]["max_probability_percent"] == 24 and r["daily"][1]["precip_mm"] == 1.5
    assert r["hourly"] == [] and "Wettest day" in r["summary"]


def test_at_local_time(api):
    r = rain.rain_chance("Austin, TX", at="2026-10-18 19:00", now=NOW)
    assert r["at"]["time"] == "2026-10-18T19:00:00-05:00" and r["at"]["probability_percent"] == 30
    assert r["summary"].startswith("Sun 7 PM in Austin")
    with pytest.raises(rain.RainError):
        rain.rain_chance("Austin, TX", at="next tuesday", now=NOW)
    with pytest.raises(rain.RainError):
        rain.rain_chance("Austin, TX", at="2027-03-01 10:00", now=NOW)


def test_limits(api):
    with pytest.raises(rain.RainError):
        rain.rain_chance("Austin, TX", hours=500, now=NOW)
    with pytest.raises(rain.RainError):
        rain.rain_chance("Austin, TX", days=20, now=NOW)


def test_all_sources_down_raises(api):
    api.nws_fail = api.om_fail = True
    with pytest.raises(weather.WeatherUnavailable):
        rain.rain_chance("30.27,-97.74", now=NOW)


def test_motocross_and_rain_share_one_open_meteo_request(api):
    track = motocross.resolve_track("swan")
    field = motocross._field(track)
    lat, lon = field["coordinates"]["lat"], field["coordinates"]["lon"]
    amounts = motocross.get_amounts(field)
    assert amounts["rows"] and "precip_in" in amounts["rows"][0]
    rain.rain_chance(None, lat=lat, lon=lon, hours=3, now=NOW)
    weather.get_hourly_forecast(field)  # venue/team path
    assert api.count("open-meteo.com/v1/forecast") == 1


# --------------------------------------------------------------------- HTTP
def _get(qs):
    status, ctype, body, cache = handle_rain_request("GET", "/v1/rain", qs)
    return status, json.loads(body), cache


def test_http_rain(api):
    s, body, cache = _get("q=London&hours=3")
    assert s == 200 and body["place"]["country_code"] == "GB" and len(body["hourly"]) == 3 and "s-maxage" in cache
    s, body, _ = _get("lat=30.27&lon=-97.74&days=2")
    assert s == 200 and len(body["daily"]) == 2
    s, body, _ = _get("q=Springfield")
    assert s == 200 and body["ambiguous"] and body["matches"]


def test_http_rain_errors(api):
    assert _get("")[0] == 400
    assert _get("q=London&hours=abc")[0] == 400
    s, body, cache = _get("q=Zzyzxville")
    assert s == 404 and body["error"] == "place_not_found" and cache == "no-store"
    api.nws_fail = api.om_fail = True
    s, body, _ = _get("lat=30.27&lon=-97.74")
    assert s == 502 and body["spoken_answer"]
    assert handle_rain_request("POST", "/v1/rain", "q=x")[0] == 405


def test_wsgi_and_vercel_routes(api):
    from pathlib import Path

    from rainout_agent.http_app import application

    seen = {}
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/rain", "QUERY_STRING": "q=Tokyo"},
                                lambda st, h: seen.update(status=st)))
    assert seen["status"].startswith("404")  # Tokyo is not in the fake geocoder
    assert json.loads(body)["error"] == "place_not_found"
    vercel = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
    assert {"src": "/v1/rain/?", "dest": "/api/rain.py"} in vercel["routes"]
    assert {"src": "api/rain.py", "use": "@vercel/python"} in vercel["builds"]
    spec = (Path(__file__).parents[1] / "openapi.yaml").read_text()
    assert "operationId: getRainChance" in spec
    assert "get_rain_chance" in (Path(__file__).parents[1] / "llms.txt").read_text()


def test_mcp_get_rain_chance(api):
    def call(args):
        return mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "get_rain_chance", "arguments": args}})["result"]

    ok = call({"query": "London", "hours": 2})
    assert not ok["isError"] and ok["structuredContent"]["place"]["country_code"] == "GB"
    amb = call({"query": "Springfield"})
    assert not amb["isError"] and amb["structuredContent"]["ambiguous"]
    assert call({})["isError"]
    missing = call({"query": "Zzyzxville"})
    assert missing["isError"] and "No place matched" in missing["content"][0]["text"]


# --------------------------------------------------------------- file cache
def test_file_cache_roundtrip_and_expiry(tmp_path):
    clock = [1000.0]
    c = FileCache(str(tmp_path), clock=lambda: clock[0])
    c.set("k", {"a": 1}, 60)
    assert c.get("k") == {"a": 1}
    clock[0] += 61
    assert c.get("k") is None
    assert build_cache({"RAINOUT_CACHE_BACKEND": "file", "RAINOUT_CACHE_DIR": str(tmp_path)}).name == "memory+file"

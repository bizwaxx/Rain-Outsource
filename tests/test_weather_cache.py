"""Milestone 4: cache backends, cached NWS weather, Open-Meteo fallback, Cache-Control."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import cache as cache_mod
from rainout_agent import weather
from rainout_agent.cache import (
    MemoryCache,
    NullCache,
    SupabaseCache,
    TieredCache,
    UpstashCache,
    build_cache,
)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ----------------------------------------------------------------- memory cache
def test_memory_cache_ttl_expiry():
    clock = Clock()
    c = MemoryCache(clock=clock)
    c.set("a", {"x": 1}, 10)
    assert c.get("a") == {"x": 1}
    clock.t += 9.9
    assert c.get("a") == {"x": 1}
    clock.t += 0.2
    assert c.get("a") is None
    c.set("b", 1, 0)  # non-positive TTL is not stored
    assert c.get("b") is None


def test_memory_cache_lru_eviction():
    c = MemoryCache(max_entries=3)
    for k in "abc":
        c.set(k, k, 60)
    assert c.get("a") == "a"  # touch a so b is least recent
    c.set("d", "d", 60)
    assert c.get("b") is None
    assert {k: c.get(k) for k in "acd"} == {"a": "a", "c": "c", "d": "d"}
    assert len(c) == 3


# ---------------------------------------------------------------- remote caches
class FakeUpstash:
    def __init__(self, fail=False):
        self.store, self.calls, self.fail = {}, [], fail

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body)))
        assert timeout <= 2
        if self.fail:
            raise TimeoutError("slow")
        cmd = json.loads(body)
        if cmd[0] == "SET":
            assert cmd[3] == "EX"
            self.store[cmd[1]] = cmd[2]
            return 200, b'{"result":"OK"}'
        return 200, json.dumps({"result": self.store.get(cmd[1])}).encode()


def test_upstash_round_trip_uses_rest_commands_and_bearer_token():
    t = FakeUpstash()
    c = UpstashCache("https://kv.example.com/", "secret", transport=t)
    c.set("nws:points:1,2", {"forecastHourly": "u"}, 3600)
    assert c.get("nws:points:1,2") == {"forecastHourly": "u"}
    method, url, headers, cmd = t.calls[0]
    assert (method, url) == ("POST", "https://kv.example.com")
    assert headers["Authorization"] == "Bearer secret"
    assert cmd[:2] == ["SET", "rainout:v1:nws:points:1,2"] and cmd[3:] == ["EX", "3600"]
    assert c.get("missing") is None


def test_remote_failure_opens_circuit_breaker():
    clock = Clock()
    t = FakeUpstash(fail=True)
    c = UpstashCache("https://kv.example.com", "s", transport=t, clock=clock)
    assert c.get("k") is None
    assert len(t.calls) == 1 and c.errors == 1
    c.set("k", 1, 60)
    assert c.get("k") is None
    assert len(t.calls) == 1  # skipped during the cool-down
    clock.t += cache_mod.REMOTE_COOLDOWN_S + 1
    c.get("k")
    assert len(t.calls) == 2


def test_supabase_backend_upserts_and_filters_expired_rows():
    calls = []
    rows = {}

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers))
        assert headers["apikey"] == "svc"
        if method == "POST":
            row = json.loads(body)
            rows[row["key"]] = row
            return 201, b""
        return 200, json.dumps([{"value": r["value"]} for r in rows.values()]).encode()

    clock = Clock()
    c = SupabaseCache("https://abc.supabase.co", "svc", "rainout_cache", transport=transport, clock=clock)
    c.set("k", {"v": 1}, 600)
    assert c.get("k") == {"v": 1}
    post, get = calls
    assert post[1] == "https://abc.supabase.co/rest/v1/rainout_cache?on_conflict=key"
    assert "merge-duplicates" in post[2]["Prefer"]
    assert "key=eq.rainout%3Av1%3Ak" in get[1] and "expires_at=gt." in get[1]


def test_tiered_cache_fills_memory_from_remote_and_survives_remote_errors():
    t = FakeUpstash()
    remote = UpstashCache("https://kv", "s", transport=t)
    remote.set("k", {"v": 2}, 600)
    tiered = TieredCache(MemoryCache(), remote)
    assert tiered.get("k") == {"v": 2}
    n = len(t.calls)
    assert tiered.get("k") == {"v": 2}
    assert len(t.calls) == n  # second read served from memory
    t.fail = True
    tiered.set("z", 3, 60)  # remote write fails silently
    assert tiered.get("z") == 3


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "memory"),
        ({"RAINOUT_CACHE_BACKEND": "none"}, "none"),
        ({"KV_REST_API_URL": "https://kv", "KV_REST_API_TOKEN": "t"}, "memory+upstash"),
        ({"UPSTASH_REDIS_REST_URL": "https://kv", "UPSTASH_REDIS_REST_TOKEN": "t"}, "memory+upstash"),
        ({"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}, "memory+supabase"),
        ({"RAINOUT_CACHE_BACKEND": "memory", "KV_REST_API_URL": "https://kv", "KV_REST_API_TOKEN": "t"}, "memory"),
        ({"RAINOUT_CACHE_BACKEND": "supabase", "KV_REST_API_URL": "https://kv", "KV_REST_API_TOKEN": "t",
          "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "k"}, "memory+supabase"),
        ({"RAINOUT_CACHE_BACKEND": "upstash"}, "memory"),  # requested but not configured
        ({"RAINOUT_CACHE_BACKEND": "redis-cluster"}, "memory"),  # unknown value
        ({"KV_REST_API_URL": "https://kv"}, "memory"),  # half-configured
    ],
)
def test_backend_selection_from_env(env, expected):
    assert build_cache(env).name == expected


def test_describe_never_exposes_secrets():
    c = build_cache({"KV_REST_API_URL": "https://kv.example", "KV_REST_API_TOKEN": "topsecret"})
    assert "topsecret" not in json.dumps(c.describe())
    assert "kv.example" not in json.dumps(c.describe())


# ------------------------------------------------------------- weather layer
FIELD = {"id": "test-field", "coordinates": {"lat": 30.24641234, "lon": -97.71421234}}
HOURLY_URL = "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly"


def nws_forecast(pop=30, text="Chance Showers And Thunderstorms"):
    start = datetime(2026, 10, 14, 17, tzinfo=timezone.utc)
    return {
        "properties": {
            "periods": [
                {
                    "number": i + 1,
                    "startTime": (start + timedelta(hours=i)).isoformat(),
                    "endTime": (start + timedelta(hours=i + 1)).isoformat(),
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
                    "shortForecast": text,
                    "detailedForecast": "",
                    "temperature": 80,
                    "windSpeed": "10 mph",
                }
                for i in range(48)
            ]
        }
    }


def open_meteo_payload():
    start = int(datetime(2026, 10, 14, 17, tzinfo=timezone.utc).timestamp())
    return {
        "hourly": {
            "time": [start + 3600 * i for i in range(48)],
            "precipitation_probability": [70 if i == 3 else None if i == 4 else 5 for i in range(48)],
            "weather_code": [95 if i == 3 else 3 for i in range(48)],
        }
    }


class FakeHTTP:
    def __init__(self, nws_fail=False, om_fail=False):
        self.calls = []
        self.nws_fail, self.om_fail = nws_fail, om_fail

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        assert timeout > 0
        if url.startswith("https://api.weather.gov/"):
            assert "User-Agent" in headers
            if self.nws_fail:
                raise TimeoutError("NWS timed out")
            if "/points/" in url:
                return {"properties": {"forecastHourly": HOURLY_URL}}
            return nws_forecast()
        if url.startswith(weather.OPEN_METEO_URL):
            if self.om_fail:
                raise OSError("down")
            return open_meteo_payload()
        raise AssertionError(url)

    def count(self, part):
        return sum(1 for u in self.calls if part in u)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(weather, "_http_get_json", fake)
    return fake


def test_points_lookup_rounded_and_cached_long(http):
    c = MemoryCache()
    r = weather.get_hourly_forecast(FIELD, cache=c, now=0)
    assert r["source_id"] == "nws" and r["cache"] == "miss"
    assert http.calls[0] == "https://api.weather.gov/points/30.2464,-97.7142"
    assert c.get("nws:points:30.2464,-97.7142") == {"forecastHourly": HOURLY_URL}
    assert set(r["periods"][0]) == {"startTime", "endTime", "probabilityOfPrecipitation", "shortForecast", "detailedForecast"}


def test_forecast_cached_for_ttl_then_refetched_without_points(http, monkeypatch):
    monkeypatch.setenv("RAINOUT_FORECAST_TTL_S", "600")
    c = MemoryCache()
    weather.get_hourly_forecast(FIELD, cache=c, now=1000)
    hit = weather.get_hourly_forecast(FIELD, cache=c, now=1000 + 599)
    assert hit["cache"] == "hit" and hit["source_id"] == "nws"
    assert len(http.calls) == 2
    again = weather.get_hourly_forecast(FIELD, cache=c, now=1000 + 601)
    assert again["cache"] == "miss"
    assert http.count("/points/") == 1  # gridpoint reused
    assert http.count("/forecast/hourly") == 2


def test_stale_nws_served_when_nws_fails(http, monkeypatch):
    c = MemoryCache()
    weather.get_hourly_forecast(FIELD, cache=c, now=1000)
    http.nws_fail = True
    r = weather.get_hourly_forecast(FIELD, cache=c, now=1000 + 1800)
    assert (r["source_id"], r["cache"]) == ("nws_stale", "stale")
    assert "cached" in r["source"]
    assert http.count("open-meteo") == 0


def test_open_meteo_fallback_when_nws_fails_and_no_stale(http):
    http.nws_fail = True
    c = MemoryCache()
    r = weather.get_hourly_forecast(FIELD, cache=c, now=1000)
    assert r["source_id"] == "open_meteo" and r["cache"] == "miss"
    assert r["source"].startswith("Open-Meteo")
    url = [u for u in http.calls if "open-meteo" in u][0]
    assert "latitude=30.2464" in url and "hourly=precipitation_probability%2Cprecipitation%2Cweather_code" in url and "timezone=auto" in url
    second = weather.get_hourly_forecast(FIELD, cache=c, now=1100)
    assert second["cache"] == "hit" and http.count("open-meteo") == 1


def test_open_meteo_used_after_stale_window(http, monkeypatch):
    monkeypatch.setenv("RAINOUT_FORECAST_STALE_S", "3600")
    c = MemoryCache(clock=Clock(0))  # entries never expire in the memory tier here
    weather.get_hourly_forecast(FIELD, cache=c, now=0)
    http.nws_fail = True
    r = weather.get_hourly_forecast(FIELD, cache=c, now=3601)
    assert r["source_id"] == "open_meteo"


def test_all_sources_failing_raises(http):
    http.nws_fail = http.om_fail = True
    with pytest.raises(weather.WeatherUnavailable):
        weather.get_hourly_forecast(FIELD, cache=MemoryCache(), now=0)


def test_unexpected_forecast_url_is_rejected(monkeypatch):
    calls = []

    def fake(url, headers, timeout):
        calls.append(url)
        if "/points/" in url:
            return {"properties": {"forecastHourly": "http://169.254.169.254/latest"}}
        if "open-meteo" in url:
            return open_meteo_payload()
        raise AssertionError(url)

    monkeypatch.setattr(weather, "_http_get_json", fake)
    r = weather.get_hourly_forecast(FIELD, cache=MemoryCache(), now=0)
    assert r["source_id"] == "open_meteo"
    assert not any("169.254" in u for u in calls)


def test_open_meteo_conversion_maps_thunderstorms_and_nulls():
    periods = weather.open_meteo_periods(open_meteo_payload())
    assert periods[0]["startTime"] == "2026-10-14T17:00:00+00:00"
    assert periods[0]["endTime"] == "2026-10-14T18:00:00+00:00"
    assert periods[3]["shortForecast"] == "Thunderstorms"
    assert periods[3]["probabilityOfPrecipitation"]["value"] == 70
    assert periods[4]["probabilityOfPrecipitation"]["value"] is None
    from rainout_agent.status_api import weather_from_periods

    w = weather_from_periods(periods, "2026-10-14T15:10:00-05:00", source="Open-Meteo")
    assert w["rain_chance_percent"] == 70 and w["thunderstorm_likely"] is True
    assert weather_from_periods(periods, "2026-10-14T16:00:00-05:00")["rain_chance_percent"] == 0


# ------------------------------------------------- integration: status/schedule
def test_status_marks_fallback_source(http, monkeypatch):
    from rainout_agent.status_api import build_status_result

    http.nws_fail = True
    r = build_status_result("Krieg", "2026-10-14T15:10:00-05:00", official_status="on")
    assert r["weather_source_id"] == "open_meteo"
    assert r["weather_source"].startswith("Open-Meteo")
    assert r["weather_cache"] == "miss"
    assert "Open-Meteo" in r["source"]
    assert r["thunderstorm_likely"] is True


def test_status_reports_nws_and_cache_hit(http):
    from rainout_agent.status_api import build_status_result

    first = build_status_result("Krieg", "2026-10-14T15:10:00-05:00", official_status="on")
    second = build_status_result("Krieg", "2026-10-14T15:30:00-05:00", official_status="on")
    assert (first["weather_source_id"], first["weather_cache"]) == ("nws", "miss")
    assert (second["weather_source_id"], second["weather_cache"]) == ("nws", "hit")
    assert first["weather_source"] == "National Weather Service API"
    assert "weather: National Weather Service" in first["source"]


def test_schedule_marks_weather_source_per_game(http):
    from rainout_agent.schedule import check_schedule

    now = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)
    games = [{"venue_query": "Krieg", "start": datetime(2026, 10, 14, 20, 10, tzinfo=timezone.utc), "title": "G1"}]
    official = lambda field, timeout: {"official_status": "unknown", "checked": False}
    http.nws_fail = True
    report = check_schedule(games, now=now, official_provider=official)
    g = report["games"][0]
    assert g["weather_source_id"] == "open_meteo"
    assert g["basis"].startswith("open_meteo_forecast")
    assert g["thunderstorm_likely"] is True
    assert report["summary"]["with_fallback_forecast"] == 1
    assert report["summary"]["with_nws_forecast"] == 0


def test_schedule_accepts_plain_period_list_providers():
    from rainout_agent.schedule import _normalise_forecast

    assert _normalise_forecast([{"a": 1}])["source_id"] == "nws"
    assert _normalise_forecast({"periods": [1], "source": "X", "source_id": "open_meteo"})["source_id"] == "open_meteo"


# ----------------------------------------------------------------- headers
def wsgi(path, query="", method="GET", body=b""):
    from rainout_agent.http_app import application

    out = {}

    def start_response(status, headers):
        out["status"], out["headers"] = status, dict(headers)

    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
           "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(body)}
    data = b"".join(application(env, start_response))
    return out["status"], out["headers"], data


def test_cache_control_headers_on_wsgi_app(http, monkeypatch):
    _, h, _ = wsgi("/v1/fields")
    assert h["Cache-Control"].startswith("public") and "s-maxage=3600" in h["Cache-Control"]
    _, h, body = wsgi("/openapi.yaml")
    assert "max-age=3600" in h["Cache-Control"] and body.startswith(b"openapi")
    _, h, data = wsgi("/health")
    assert h["Cache-Control"] == "no-store"
    assert json.loads(data)["weather_cache"]["backend"] == "memory"
    _, h, _ = wsgi("/v1/status", "field_id=Krieg")
    assert h["Cache-Control"] == "no-store"
    monkeypatch.setattr("rainout_agent.status_api.fetch_official_rainout_status",
                        lambda field, **kw: {"official_status": "unknown", "checked": False})
    s, h, _ = wsgi("/v1/status", "field_id=Krieg&game_time=2026-10-14T15:10:00-05:00")
    assert s.startswith("200") and h["Cache-Control"].startswith("public, max-age=60")
    http.nws_fail = True
    from rainout_agent.cache import set_cache

    set_cache(MemoryCache())
    _, h, data = wsgi("/v1/status", "field_id=Krieg&game_time=2026-10-14T15:10:00-05:00")
    assert json.loads(data)["weather_source_id"] == "open_meteo"
    assert h["Cache-Control"] == "public, max-age=30, s-maxage=30"
    _, h, _ = wsgi("/v1/schedule/check", method="POST", body=b"[]")
    assert h["Cache-Control"] == "no-store"
    s, h, _ = wsgi("/v1/schedule/check", "")
    assert s.startswith("400") and h["Cache-Control"] == "no-store"


def test_cache_control_policy_helpers():
    from rainout_agent import http_cache

    assert http_cache.schedule_cache_control("GET", 200) == http_cache.SCHEDULE_GET
    assert http_cache.schedule_cache_control("POST", 200) == "no-store"
    assert http_cache.schedule_cache_control("GET", 502) == "no-store"
    assert http_cache.status_cache_control(404, {"error": "unknown_field"}) == "no-store"
    assert http_cache.status_cache_control(200, {"weather_source_id": "nws_stale"}) == http_cache.STATUS_DEGRADED
    assert http_cache.fields_cache_control(400) == "no-store"


def test_vercel_handlers_send_cache_control(monkeypatch):
    import importlib

    headers = {}

    def run(module_name, path):
        mod = importlib.import_module(module_name)
        h = mod.handler.__new__(mod.handler)
        h.path = path
        h.wfile = io.BytesIO()
        h.headers = {}
        sent = {}
        h.send_response = lambda code, *a: sent.__setitem__("code", code)
        h.send_header = lambda k, v: sent.__setitem__(k, v)
        h.end_headers = lambda: None
        h.do_GET()
        return sent

    assert "s-maxage=3600" in run("api.fields", "/v1/fields")["Cache-Control"]
    assert run("api.fields", "/v1/fields?sport=curling")["Cache-Control"] == "no-store"
    assert "max-age=3600" in run("api.openapi", "/openapi.yaml")["Cache-Control"]
    assert run("api.index", "/health")["Cache-Control"] == "no-store"
    assert run("api.index", "/")["Cache-Control"].startswith("public")
    assert run("api.stats", "/v1/stats")["Cache-Control"] == "no-store"
    assert run("api.status", "/v1/status?field_id=Krieg")["Cache-Control"] == "no-store"
    assert run("api.schedule", "/v1/schedule/check")["Cache-Control"] == "no-store"

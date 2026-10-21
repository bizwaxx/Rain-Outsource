"""Source policy: paid answers use NWS/NOAA (US) and MET Norway (non-US), never Open-Meteo.

All upstream responses are recorded files in tests/fixtures/sources (no network).
"""
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from rainout_agent import api_keys, premium_api, rain, source_policy, weather
from rainout_agent.cache import MemoryCache

FIX = Path(__file__).parent / "fixtures" / "sources"
AUSTIN = {"coordinates": {"lat": 30.2672, "lon": -97.7431}}
LONDON = {"coordinates": {"lat": 51.5074, "lon": -0.1278}}
OSLO = {"coordinates": {"lat": 59.9139, "lon": 10.7522}}
T0 = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc).timestamp()


def load(name):
    return json.loads((FIX / name).read_text())


class Upstream:
    """Records every upstream URL; serves recorded NWS JSON and MET Norway responses."""

    def __init__(self, nws_ok=True, met=None):
        self.urls, self.met_headers, self.nws_ok = [], [], nws_ok
        self.met = list(met or [])

    def json(self, url, headers, timeout):
        self.urls.append(url)
        if "open-meteo.com" in url:
            raise AssertionError(f"Open-Meteo must not be called here: {url}")
        if not self.nws_ok:
            raise TimeoutError("NWS down")
        if "/points/" in url:
            return load("nws_points_austin.json")
        if url.endswith("/forecast/hourly"):
            return load("nws_hourly_austin.json")
        raise AssertionError(url)

    def met_call(self, url, headers, timeout):
        self.urls.append(url)
        self.met_headers.append(dict(headers))
        status, hdrs, body = self.met.pop(0)
        if isinstance(status, Exception):
            raise status
        return status, hdrs, body


def met_ok(name, expires_in=1800, last_modified="Wed, 21 Oct 2026 14:35:02 GMT"):
    exp = format_datetime(datetime.fromtimestamp(T0 + expires_in, timezone.utc), usegmt=True)
    return 200, {"expires": exp, "last-modified": last_modified}, load(name)


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(weather, "_transport_json", up.json)
    monkeypatch.setattr(weather, "_met_transport", up.met_call)
    return up


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch):
    monkeypatch.delenv(source_policy.ENV, raising=False)


# ------------------------------------------------------------------ the switch
def test_mode_defaults_off_and_ignores_junk(monkeypatch):
    assert source_policy.mode() == "off"
    for raw, want in (("PAID", "paid"), (" all ", "all"), ("yes", "off"), ("", "off")):
        monkeypatch.setenv(source_policy.ENV, raw)
        assert source_policy.mode() == want
    monkeypatch.setenv(source_policy.ENV, "off")
    with source_policy.paid_request():
        assert not source_policy.commercial_only()  # flag off: paid requests keep today's sources


def test_paid_mark_only_counts_in_paid_mode(monkeypatch):
    monkeypatch.setenv(source_policy.ENV, "paid")
    assert not source_policy.commercial_only()
    with source_policy.paid_request():
        assert source_policy.commercial_only() and source_policy.tier_label() == "commercial"
    assert not source_policy.is_paid_request()  # the mark is reset after the request
    monkeypatch.setenv(source_policy.ENV, "all")
    assert source_policy.commercial_only()


def test_open_meteo_urls_refused_for_paid_answers(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    url = "https://api.open-meteo.com/v1/forecast?latitude=1&longitude=2"
    source_policy.check_url(url)  # free request: allowed
    with source_policy.paid_request():
        for u in (url, "https://geocoding-api.open-meteo.com/v1/search?name=x", "https://archive-api.open-meteo.com/v1/era5"):
            with pytest.raises(source_policy.SourceNotAllowed):
                weather._http_get_json(u, {}, 1)
        source_policy.check_url("https://api.weather.gov/points/1,2")  # NWS is fine
        with pytest.raises(rain.RainError, match="lat,lon"):
            rain.geocode("Austin, TX", cache=MemoryCache())
    assert upstream.urls == []


# ------------------------------------------------------------------ US: NWS only
def test_free_request_still_falls_back_to_open_meteo(monkeypatch):
    monkeypatch.setenv(source_policy.ENV, "paid")
    calls = []

    def fake(url, headers, timeout):
        calls.append(url)
        if "weather.gov" in url:
            raise TimeoutError("NWS down")
        return {"hourly": {"time": [int(T0)], "precipitation_probability": [40], "weather_code": [61]}}

    monkeypatch.setattr(weather, "_transport_json", fake)
    res = weather.get_hourly_forecast(AUSTIN, cache=MemoryCache(), now=T0)
    assert res["source_id"] == "open_meteo" and "source_policy" not in res
    assert any("open-meteo.com" in u for u in calls)


def test_paid_us_answer_uses_recorded_nws(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    with source_policy.paid_request():
        res = weather.get_hourly_forecast(AUSTIN, cache=MemoryCache(), now=T0)
    assert res["source_id"] == "nws" and res["source_policy"] == "commercial"
    assert len(res["periods"]) == 72 and res["periods"][0]["shortForecast"]
    assert all("api.weather.gov" in u for u in upstream.urls)
    assert all(u.startswith("https://api.weather.gov/") for u in upstream.urls)


def test_paid_us_answer_never_falls_back_to_open_meteo(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    upstream.nws_ok = False
    with source_policy.paid_request(), pytest.raises(weather.WeatherUnavailable, match="do not fall back"):
        weather.get_hourly_forecast(AUSTIN, cache=MemoryCache(), now=T0)
    assert not any("open-meteo" in u for u in upstream.urls)


def test_paid_us_answer_serves_stale_nws(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    cache = MemoryCache()
    with source_policy.paid_request():
        weather.get_hourly_forecast(AUSTIN, cache=cache, now=T0)
        upstream.nws_ok = False
        res = weather.get_hourly_forecast(AUSTIN, cache=cache, now=T0 + 1800)  # past the 10 min TTL
    assert res["source_id"] == "nws_stale" and res["cache"] == "stale" and res["source_policy"] == "commercial"


# ------------------------------------------------------------------ non-US: MET Norway
def test_met_norway_conversion_outside_nordic_estimates_chance_from_amount():
    periods = weather.met_norway_periods(load("metno_london_complete.json"))
    assert periods and all(p["probabilityKind"] == "estimated_from_amount" for p in periods)
    hourly = [p for p in periods if (datetime.fromisoformat(p["endTime"]) - datetime.fromisoformat(p["startTime"])).seconds == 3600]
    six = [p for p in periods if (datetime.fromisoformat(p["endTime"]) - datetime.fromisoformat(p["startTime"])).seconds == 6 * 3600]
    assert len(hourly) >= 40 and six  # 1-hour steps first, then 6-hour blocks
    for a, b in zip(periods, periods[1:]):
        assert a["endTime"] <= b["startTime"]  # no overlaps
    for p in periods:
        assert p["probabilityOfPrecipitation"]["value"] == weather.chance_from_amount(p["precipitationAmountMm"])


def test_met_norway_conversion_in_nordic_uses_forecast_chance():
    periods = weather.met_norway_periods(load("metno_oslo_complete.json"))
    first = periods[0]
    assert first["probabilityKind"] == "forecast"
    raw = load("metno_oslo_complete.json")["properties"]["timeseries"][0]["data"]["next_1_hours"]["details"]
    assert first["probabilityOfPrecipitation"]["value"] == raw["probability_of_precipitation"]


def test_chance_from_amount_table_and_symbols():
    assert [weather.chance_from_amount(x) for x in (None, 0, 0.1, 0.3, 0.7, 2, 4, 12)] == [None, 5, 25, 40, 55, 70, 80, 90]
    assert weather.met_symbol_text("heavyrainshowersandthunder_day") == "Heavy Rain Showers And Thunder"
    assert weather.met_symbol_text("lightssleetshowersandthunder_night") == "Light Sleet Showers And Thunder"
    assert weather.met_symbol_text("partlycloudy_night") == "Partly Cloudy"


def test_paid_non_us_answer_uses_met_norway_with_terms(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    upstream.met = [met_ok("metno_london_complete.json")]
    cache = MemoryCache()
    with source_policy.paid_request():
        res = weather.get_hourly_forecast(LONDON, cache=cache, now=T0, use_nws=False)
        again = weather.get_hourly_forecast(LONDON, cache=cache, now=T0 + 600, use_nws=False)
    assert res["source_id"] == "met_norway" and res["source"] == weather.MET_NORWAY_SOURCE
    assert res["attribution"].startswith("Data from MET Norway") and res["source_policy"] == "commercial"
    assert again["cache"] == "hit" and len(upstream.met_headers) == 1  # no new request before Expires
    url = upstream.urls[0]
    assert url == "https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=51.5074&lon=-0.1278"
    h = upstream.met_headers[0]
    assert "rainout-agent-source.vercel.app" in h["User-Agent"] and h["Accept-Encoding"] == "gzip"
    assert "If-Modified-Since" not in h


def test_met_norway_revalidates_with_if_modified_since_and_handles_304(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "all")
    exp = format_datetime(datetime.fromtimestamp(T0 + 4000, timezone.utc), usegmt=True)
    upstream.met = [met_ok("metno_london_complete.json", expires_in=60), (304, {"expires": exp}, None)]
    cache = MemoryCache()
    first = weather.fetch_met_norway(LONDON, cache, 5, now=T0)
    second = weather.fetch_met_norway(LONDON, cache, 5, now=T0 + 400)  # min TTL is 300 s even though Expires was 60 s
    assert second["cache"] == "revalidated" and second["periods"] == first["periods"]
    assert upstream.met_headers[1]["If-Modified-Since"] == "Wed, 21 Oct 2026 14:35:02 GMT"
    third = weather.fetch_met_norway(LONDON, cache, 5, now=T0 + 3000)
    assert third["cache"] == "hit" and len(upstream.met_headers) == 2


def test_met_norway_throttled_or_down_serves_cache_then_fails(monkeypatch, upstream):
    import urllib.error

    upstream.met = [met_ok("metno_london_complete.json", expires_in=0),
                    (urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None), None, None)]
    cache = MemoryCache()
    weather.fetch_met_norway(LONDON, cache, 5, now=T0)
    stale = weather.fetch_met_norway(LONDON, cache, 5, now=T0 + 900)
    assert stale["cache"] == "stale"
    upstream.met = [(TimeoutError("down"), None, None)]
    with pytest.raises(TimeoutError):
        weather.fetch_met_norway(OSLO, MemoryCache(), 5, now=T0)


def test_met_norway_deprecation_203_is_logged(monkeypatch, upstream, caplog):
    status, hdrs, body = met_ok("metno_oslo_complete.json")
    upstream.met = [(203, hdrs, body)]
    res = weather.fetch_met_norway(OSLO, MemoryCache(), 5, now=T0)
    assert res["periods"] and "deprecated" in caplog.text


def test_rain_forecast_paid_non_us_uses_met_norway_amounts(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    upstream.met = [met_ok("metno_london_complete.json")]
    now = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
    with source_policy.paid_request():
        res = rain.forecast(51.5074, -0.1278, hours=24, days=3, now=now, cache=MemoryCache())
    assert res["source"]["attribution"].startswith("Data from MET Norway")
    assert res["source"]["amounts"] == "MET Norway" and "no Open-Meteo" in res["source"]["policy"]
    assert res["hourly"] and all(r["probability_source"] == "met_norway" for r in res["hourly"])
    assert all(r.get("probability_kind") == "estimated_from_amount" for r in res["hourly"])
    assert all(r["precip_mm"] is not None for r in res["hourly"])
    assert res["daily"] and res["daily"][0]["probability_source"] == "met_norway"
    assert not any("open-meteo" in u for u in upstream.urls)


def test_rain_forecast_paid_us_is_nws_only(monkeypatch, upstream):
    monkeypatch.setenv(source_policy.ENV, "paid")
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    with source_policy.paid_request():
        res = rain.forecast(30.2672, -97.7431, hours=12, now=now, cache=MemoryCache())
    assert res["source"]["nws_used"] and res["source"]["attribution"] == "National Weather Service (NOAA)"
    assert res["hourly"] and all(r["probability_source"] == "nws" for r in res["hourly"])
    assert res["timezone"] == "America/Chicago"  # from the NWS points lookup, not Open-Meteo
    assert not any("open-meteo" in u for u in upstream.urls)


# ------------------------------------------------------------------ the paid endpoints set the mark
SECRET = "s" * 40


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("RAINOUT_API_KEY_SECRET", SECRET)
    api_keys._LOCAL_STORE.clear()
    monkeypatch.setattr(api_keys, "SUBSCRIPTION_RESOLVER", None)
    yield
    api_keys._LOCAL_STORE.clear()


def _seen_status_fn(seen):
    def status_fn(item):
        seen.append((source_policy.is_paid_request(), source_policy.commercial_only()))
        return {"field_id": item["field_id"], "game_time_play_probability_percent": 90}
    return status_fn


def _batch(items, headers, seen, path="/v1/batch", env=None):
    status, _, body = premium_api.handle_premium_request(
        "POST", path, "", json.dumps({"items": items}).encode(), headers, status_fn=_seen_status_fn(seen), env=env)
    return status, json.loads(body)


ITEMS = [{"field_id": f"f{i}", "game_time": "2026-10-24T18:30:00-05:00"} for i in range(3)]


def test_paid_key_batch_marks_every_worker_thread(monkeypatch, keys):
    monkeypatch.setenv(source_policy.ENV, "paid")
    seen = []
    status, body = _batch(ITEMS, {"X-API-Key": api_keys.mint_key("league")}, seen)
    assert status == 200 and body["data_sources"] == "commercial"
    assert seen == [(True, True)] * 3
    assert not source_policy.is_paid_request()


def test_free_and_anonymous_batches_keep_free_sources(monkeypatch, keys):
    monkeypatch.setenv(source_policy.ENV, "paid")
    seen = []
    status, body = _batch(ITEMS, {}, seen)
    assert status == 200 and body["data_sources"] == "free" and seen == [(False, False)] * 3
    seen.clear()
    status, body = _batch(ITEMS, {"X-API-Key": api_keys.mint_key("free_key")}, seen)
    assert status == 200 and body["data_sources"] == "free" and seen == [(False, False)] * 3


def test_flag_off_paid_batch_reports_free_sources(keys):
    seen = []
    status, body = _batch(ITEMS, {"X-API-Key": api_keys.mint_key("league")}, seen)
    assert status == 200 and body["data_sources"] == "free" and seen == [(True, False)] * 3


def test_x402_pay_per_call_batch_is_paid(monkeypatch, keys):
    import base64
    import time

    from rainout_agent import x402

    monkeypatch.setenv(source_policy.ENV, "paid")
    env = {"X402_PAY_TO": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"}

    def fac(url, body):
        if url.endswith("/verify"):
            return 200, {"isValid": True, "payer": "0xabc"}
        return 200, {"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:84532", "payer": "0xabc"}

    monkeypatch.setattr(x402, "TRANSPORT", fac)
    seen = []
    status, h, raw = premium_api.handle_premium_request("POST", "/v1/premium/batch", "", json.dumps({"items": ITEMS}).encode(),
                                                        {}, env=env, status_fn=_seen_status_fn(seen))
    req = json.loads(base64.b64decode(h["PAYMENT-REQUIRED"]))["accepts"][0]
    now = int(time.time())
    auth = {"from": "0x857b06519E91e3A54538791bDbb0E22373e36b66", "to": req["payTo"], "value": req["amount"],
            "validAfter": str(now - 10), "validBefore": str(now + 60), "nonce": "0x" + "42" * 32}
    sig = base64.b64encode(json.dumps({"x402Version": 2, "accepted": req,
                                       "payload": {"signature": "0x" + "cd" * 65, "authorization": auth}}).encode()).decode()
    status, _, raw = premium_api.handle_premium_request("POST", "/v1/premium/batch", "", json.dumps({"items": ITEMS}).encode(),
                                                        {"PAYMENT-SIGNATURE": sig}, env=env, status_fn=_seen_status_fn(seen))
    body = json.loads(raw)
    assert status == 200 and body["plan"] == "pay_per_call" and body["data_sources"] == "commercial"
    assert seen == [(True, True)] * 3


def test_no_new_vercel_function():
    root = Path(__file__).resolve().parents[1]
    builds = json.loads((root / "vercel.json").read_text())["builds"]
    assert len(builds) <= 13  # unchanged by this branch; Hobby allows 12 (see the function-count fix in #48/#49)
    assert not (root / "api" / "sources.py").exists()

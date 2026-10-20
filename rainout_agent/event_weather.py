"""Live inputs for the v2 whole-event model (rainout_agent.event_risk).

One cached Open-Meteo request per point (free, no key): hourly rain amounts for the past 2 days (model analysis,
so "rain that already fell"), plus rain chance, weather code, CAPE, gusts and temperature for the next days.
Everything degrades gracefully: if the fetch fails, callers keep the v1 answer and say so.

Set ``RAINOUT_EVENT_RISK=off`` to disable (the test suite does this by default so it never touches the network).
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent import event_risk as er
from rainout_agent import weather
from rainout_agent.cache import Cache, get_cache

TTL_S = 900
HOURLY = "precipitation,precipitation_probability,weather_code,cape,wind_gusts_10m,temperature_2m"
THUNDER_CODES = {95, 96, 99}
SOURCE = "Open-Meteo (past 2 days are model analysis, not a rain gauge)"


def enabled() -> bool:
    return os.environ.get("RAINOUT_EVENT_RISK", "on").strip().lower() not in {"0", "off", "false", "no"}


def fetch_rows(lat: float, lon: float, *, timeout: float = 4.0, cache: Cache | None = None) -> dict[datetime, dict[str, Any]]:
    """{utc hour start: {mm, pop, thunder, cape, gust_mph, temp_f}}. Raises on failure (callers catch)."""
    cache = get_cache() if cache is None else cache
    lat, lon = round(float(lat), 3), round(float(lon), 3)
    key = f"openmeteo:event:{lat},{lon}"
    payload = cache.get(key)
    if not (isinstance(payload, dict) and payload.get("hourly", {}).get("time")):
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": HOURLY, "past_days": 2, "forecast_days": 7,
                                    "timezone": "UTC", "timeformat": "unixtime", "temperature_unit": "fahrenheit",
                                    "wind_speed_unit": "mph"})
        raw = weather._http_get_json(f"{weather.OPEN_METEO_URL}?{q}", {"User-Agent": weather.USER_AGENT}, timeout)
        if not (raw or {}).get("hourly", {}).get("time"):
            raise ValueError("Open-Meteo returned no hourly data")
        payload = {"hourly": raw["hourly"]}
        cache.set(key, payload, TTL_S)
    return rows_from_payload(payload)


def rows_from_payload(payload: dict[str, Any]) -> dict[datetime, dict[str, Any]]:
    h = payload["hourly"]
    times = h["time"]
    col = lambda name: h.get(name) or [None] * len(times)
    rows: dict[datetime, dict[str, Any]] = {}
    for i, ts in enumerate(times):
        t = datetime.fromtimestamp(int(ts), timezone.utc)
        code = col("weather_code")[i]
        r = rows.setdefault(t, {})
        r.update({"pop": col("precipitation_probability")[i], "thunder": code in THUNDER_CODES if code is not None else False,
                  "cape": col("cape")[i], "gust_mph": col("wind_gusts_10m")[i], "temp_f": col("temperature_2m")[i]})
        # Open-Meteo precipitation at T is the preceding hour's total (same shift as the backtest).
        rows.setdefault(t - timedelta(hours=1), {})["mm"] = col("precipitation")[i]
    return rows


def evaluate(lat: float, lon: float, start: datetime, duration_h: float, drainage: str, *, thunder_forecast: bool = False,
             ball_sport: bool = True, soil_factor: float = 1.0, cache: Cache | None = None,
             timeout: float = 4.0) -> dict[str, Any] | None:
    """event_risk for one event, or None when disabled or the weather fetch fails (caller keeps v1)."""
    if not enabled():
        return None
    try:
        rows = fetch_rows(lat, lon, timeout=timeout, cache=cache)
    except Exception:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    feat = er.features(rows, rows, start, duration_h)
    if feat.get("window_pop") is None:
        return None                      # event is outside the fetched hours
    for k in ("earlier_today_in", "prior_24h_in", "prior_48h_in"):
        if feat.get(k) is not None and soil_factor:
            feat[k] = round(feat[k] / soil_factor, 3)
    if thunder_forecast:
        feat["thunder_window"] = True    # the NWS text forecast mentions thunder for the game
    base = er.base_percent(feat["window_pop"], drainage)
    out = er.adjust(base, feat, drainage, None, ball_sport)
    return {**out, "base_percent": base, "features": feat, "source": SOURCE, "model": "event_risk v2"}

"""Hourly job-site weather for the construction tools (cached; free sources only).

One Open-Meteo request per point (7 days: temperature, humidity, dew point, wind, gusts,
precipitation amount and probability, weather code; US units except precipitation in mm),
plus the NWS hourly rain chance in the US through ``weather.get_hourly_forecast`` so the
rain chance matches /v1/rain and the sports checks.
"""
from __future__ import annotations

import copy
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from rainout_agent import rain, weather
from rainout_agent.cache import Cache, get_cache

SITE_TTL_S = 600
FORECAST_DAYS = 7
THUNDER_CODES = {95, 96, 99}
THRESHOLDS_PATH = Path(__file__).resolve().parents[1] / "data" / "construction" / "thresholds.json"
DISCLAIMER = ("Weather screening only, not engineering advice. Follow your project specs, supplier, engineer of record "
              "and manufacturer instructions. Forecasts can be wrong, especially for pop-up storms.")


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    for k, v in over.items():
        base[k] = _merge(base.get(k, {}), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


@lru_cache(maxsize=4)
def _load(path_override: str) -> dict[str, Any]:
    cfg = json.loads(THRESHOLDS_PATH.read_text())
    if path_override:
        cfg = _merge(cfg, json.loads(Path(path_override).read_text()))
    return cfg


def thresholds() -> dict[str, Any]:
    """Construction thresholds (data/construction/thresholds.json, optional override file)."""
    return copy.deepcopy(_load(os.environ.get("RAINOUT_CONSTRUCTION_THRESHOLDS", "")))


def open_meteo_site(lat: float, lon: float, *, cache: Cache, timeout: float) -> dict[str, Any]:
    key = f"openmeteo:site:{lat},{lon}"
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("hourly"):
        return hit
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": ("temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m,"
                   "precipitation,precipitation_probability,weather_code"),
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "mm",
        "timezone": "auto", "timeformat": "unixtime", "forecast_days": FORECAST_DAYS})
    payload = weather._http_get_json(f"{weather.OPEN_METEO_URL}?{q}", {"User-Agent": weather.USER_AGENT}, timeout)
    if not ((payload or {}).get("hourly") or {}).get("time"):
        raise ValueError("Open-Meteo returned no hourly data")
    keep = {k: payload.get(k) for k in ("timezone", "utc_offset_seconds", "hourly")}
    cache.set(key, keep, SITE_TTL_S)
    return keep


def hourly(lat: float, lon: float, *, cache: Cache | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """{tz, rows: {utc hour: row}, sources}. Each row covers [hour, hour+1)."""
    cache = get_cache() if cache is None else cache
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    om = open_meteo_site(lat, lon, cache=cache, timeout=timeout * 0.6)
    h = om["hourly"]
    n = len(h["time"])

    def col(name: str) -> list[Any]:
        return h.get(name) or [None] * n

    rows: dict[datetime, dict[str, Any]] = {}
    for i, ts in enumerate(h["time"]):
        t = datetime.fromtimestamp(int(ts), timezone.utc)
        code = col("weather_code")[i]
        # Open-Meteo precipitation at time T is the sum of the hour before T; shift it onto [T-1h, T).
        rows.setdefault(t, {})
        rows[t].update({"pop": col("precipitation_probability")[i], "pop_source": "open_meteo",
                        "temp_f": col("temperature_2m")[i], "rh": col("relative_humidity_2m")[i],
                        "dew_f": col("dew_point_2m")[i], "wind_mph": col("wind_speed_10m")[i],
                        "gust_mph": col("wind_gusts_10m")[i], "code": code,
                        "thunder": code in THUNDER_CODES if code is not None else False})
        prev = t - timedelta(hours=1)
        rows.setdefault(prev, {})["mm"] = col("precipitation")[i]
    sources = [{"name": "Open-Meteo hourly forecast", "url": "https://open-meteo.com/",
                "used_for": "temperature, humidity, wind, gusts, rain amounts" + ("" if rain.in_us(lat, lon) else ", rain chance")}]
    if rain.in_us(lat, lon):
        try:
            res = weather.get_hourly_forecast({"coordinates": {"lat": lat, "lon": lon}}, timeout=timeout, cache=cache)
            if res["source_id"] in ("nws", "nws_stale"):
                for t, p in rain._nws_by_hour(res["periods"]).items():
                    if t in rows:
                        v = (p.get("probabilityOfPrecipitation") or {}).get("value")
                        rows[t]["pop"], rows[t]["pop_source"] = int(v or 0), "nws"
                        if "thunder" in (p.get("shortForecast") or "").lower():
                            rows[t]["thunder"] = True
                sources.append({"name": res["source"], "url": "https://api.weather.gov/", "used_for": "hourly rain chance (primary in the US)"})
        except Exception:
            pass
    rows = {t: r for t, r in rows.items() if "temp_f" in r}
    return {"tz": ZoneInfo(om.get("timezone") or "UTC"), "timezone": om.get("timezone") or "UTC", "rows": rows, "sources": sources}


def hours_between(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour starts whose [h, h+1) overlaps [start, end)."""
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def window_stats(rows: dict[datetime, dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    hs = [rows[t] for t in hours_between(start, end) if t in rows]
    if not hs or len(hs) < len(hours_between(start, end)):
        return None

    def vals(k: str) -> list[float]:
        return [r[k] for r in hs if r.get(k) is not None]

    mm = vals("mm")
    pops = vals("pop")
    temps = vals("temp_f")
    gusts = vals("gust_mph")
    return {"hours": len(hs), "max_pop": int(max(pops)) if pops else None, "total_mm": round(sum(mm), 2) if mm else None,
            "max_hourly_mm": round(max(mm), 2) if mm else None, "min_temp_f": round(min(temps), 1) if temps else None,
            "max_temp_f": round(max(temps), 1) if temps else None, "max_gust_mph": round(max(gusts), 1) if gusts else None,
            "max_wind_mph": round(max(vals("wind_mph")), 1) if vals("wind_mph") else None,
            "thunder": any(r.get("thunder") for r in hs), "min_rh": min(vals("rh")) if vals("rh") else None,
            "max_rh": max(vals("rh")) if vals("rh") else None,
            "min_dew_spread_f": (round(min(r["temp_f"] - r["dew_f"] for r in hs if r.get("temp_f") is not None and r.get("dew_f") is not None), 1)
                                 if any(r.get("dew_f") is not None for r in hs) else None)}


def mm_to_in(mm: float | None) -> float | None:
    return None if mm is None else round(mm / rain.MM_PER_IN, 2)


def parse_local(value: str, tz: ZoneInfo, field: str) -> datetime:
    try:
        return rain.parse_at(str(value), tz)
    except rain.RainError as exc:
        raise rain.RainError(f"{field} must look like 2026-10-21 07:00 (local time at the site) or ISO 8601 with offset.") from exc


def fmt_when(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%a %-I:%M %p").replace(":00 ", " ")

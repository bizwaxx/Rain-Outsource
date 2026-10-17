"""Cached hourly weather with an Open-Meteo fallback.

Lookup order for one venue:

1. A fresh cached NWS hourly forecast (``RAINOUT_FORECAST_TTL_S``, default 600 s).
2. A live NWS fetch: the points -> gridpoint lookup is cached for 7 days, so a
   cold forecast normally needs one NWS request, not two.
3. A stale cached NWS forecast up to ``RAINOUT_FORECAST_STALE_S`` old (default 2 h)
   when NWS errors or times out.
4. Open-Meteo (free, no key; cached ``RAINOUT_FALLBACK_TTL_S``, default 600 s),
   converted to NWS-shaped hourly periods.

Every result says which source served it (``source_id``: ``nws``, ``nws_stale``
or ``open_meteo``) and whether the cache was used (``cache``: ``hit``, ``miss``,
``stale``). If every source fails, ``WeatherUnavailable`` is raised.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from rainout_agent.cache import Cache, get_cache

log = logging.getLogger("rainout.weather")

# NWS asks every client for a User-Agent that identifies the app and a contact.
USER_AGENT = "RainoutSource/1.0 (JEEZ Labs; +https://rainout-agent-source.vercel.app; github.com/bizwaxx/Rain-Outsource)"
NWS_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

NWS_SOURCE = "National Weather Service API"
NWS_STALE_SOURCE = "National Weather Service API (cached; NWS currently unavailable)"
OPEN_METEO_SOURCE = "Open-Meteo (fallback; NWS unavailable)"
OPEN_METEO_WORLD_SOURCE = "Open-Meteo"

POINTS_TTL_S = 7 * 24 * 3600
MAX_BODY_BYTES = 4 * 1024 * 1024


def _env_seconds(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def forecast_ttl_s() -> int:
    return _env_seconds("RAINOUT_FORECAST_TTL_S", 600)


def stale_ttl_s() -> int:
    return max(forecast_ttl_s(), _env_seconds("RAINOUT_FORECAST_STALE_S", 7200))


def fallback_ttl_s() -> int:
    return _env_seconds("RAINOUT_FALLBACK_TTL_S", 600)


class WeatherUnavailable(RuntimeError):
    """Neither NWS (live or stale cache) nor the fallback produced a forecast."""


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    """GET a JSON document from a fixed, trusted API host (monkeypatched in tests)."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
        return json.loads(resp.read(MAX_BODY_BYTES).decode("utf-8"))


def _coords(field: dict[str, Any]) -> tuple[float, float]:
    return round(float(field["coordinates"]["lat"]), 4), round(float(field["coordinates"]["lon"]), 4)


def _slim_period(period: dict[str, Any]) -> dict[str, Any]:
    """Keep only what the estimator reads, so cached payloads stay small."""
    pop = (period.get("probabilityOfPrecipitation") or {}).get("value")
    return {
        "startTime": period.get("startTime"),
        "endTime": period.get("endTime"),
        "probabilityOfPrecipitation": {"value": pop},
        "shortForecast": period.get("shortForecast") or "",
        "detailedForecast": period.get("detailedForecast") or "",
    }


def _nws_gridpoint(field: dict[str, Any], cache: Cache, timeout: float) -> str:
    lat, lon = _coords(field)
    key = f"nws:points:{lat},{lon}"
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("forecastHourly"):
        return cached["forecastHourly"]
    points = _http_get_json(f"https://api.weather.gov/points/{lat},{lon}", NWS_HEADERS, timeout)
    props = points["properties"]
    hourly_url = props["forecastHourly"]
    if not str(hourly_url).startswith("https://api.weather.gov/"):
        raise ValueError("unexpected NWS forecastHourly URL")
    entry: dict[str, Any] = {"forecastHourly": hourly_url}
    rel = ((props.get("relativeLocation") or {}).get("properties") or {})
    if rel.get("city"):
        entry["near"] = {"city": rel.get("city"), "state": rel.get("state"), "timeZone": props.get("timeZone")}
    cache.set(key, entry, POINTS_TTL_S)
    return hourly_url


def _fetch_nws_live(field: dict[str, Any], cache: Cache, deadline: float) -> list[dict[str, Any]]:
    hourly_url = _nws_gridpoint(field, cache, deadline - time.monotonic())
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        raise TimeoutError("NWS time budget exhausted after points lookup")
    forecast = _http_get_json(hourly_url, NWS_HEADERS, remaining)
    periods = forecast["properties"]["periods"]
    if not periods:
        raise ValueError("NWS returned no forecast periods")
    return [_slim_period(p) for p in periods]


# WMO weather interpretation codes used by Open-Meteo.
_WMO_TEXT = {
    0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Cloudy",
    45: "Fog", 48: "Freezing Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle", 56: "Freezing Drizzle", 57: "Freezing Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 66: "Freezing Rain", 67: "Freezing Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains",
    80: "Rain Showers", 81: "Rain Showers", 82: "Heavy Rain Showers", 85: "Snow Showers", 86: "Snow Showers",
    95: "Thunderstorms", 96: "Thunderstorms With Hail", 99: "Thunderstorms With Hail",
}


def open_meteo_periods(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an Open-Meteo hourly response (unixtime, UTC) into NWS-shaped periods."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    pops = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    periods = []
    for i, ts in enumerate(times):
        start = datetime.fromtimestamp(int(ts), timezone.utc)
        end = datetime.fromtimestamp(int(ts) + 3600, timezone.utc)
        pop = pops[i] if i < len(pops) else None
        code = codes[i] if i < len(codes) else None
        periods.append(
            {
                "startTime": start.isoformat(),
                "endTime": end.isoformat(),
                "probabilityOfPrecipitation": {"value": pop},
                "shortForecast": _WMO_TEXT.get(int(code), "Unknown") if code is not None else "Unknown",
                "detailedForecast": "",
            }
        )
    if not periods:
        raise ValueError("Open-Meteo returned no hourly data")
    return periods


def _fetch_open_meteo(field: dict[str, Any], timeout: float, cache: Cache | None = None) -> list[dict[str, Any]]:
    """Fallback periods from the shared Open-Meteo payload (one request serves rain, amounts and fallback)."""
    from rainout_agent import rain

    lat, lon = _coords(field)
    return open_meteo_periods(rain.open_meteo_payload(lat, lon, timeout=timeout, cache=cache))


def get_hourly_forecast(
    field: dict[str, Any],
    timeout: float = 10.0,
    cache: Cache | None = None,
    now: float | None = None,
    use_nws: bool = True,
) -> dict[str, Any]:
    """Return ``{periods, source, source_id, cache, fetched_at}`` for a field.

    ``timeout`` is the total budget; roughly two thirds go to NWS and the rest is
    kept for the fallback.
    """
    cache = get_cache() if cache is None else cache
    wall_now = time.time() if now is None else now
    lat, lon = _coords(field)
    forecast_key = f"nws:hourly:{lat},{lon}"

    cached = cache.get(forecast_key)
    if isinstance(cached, dict) and cached.get("periods"):
        if wall_now - float(cached.get("fetched_at", 0)) < forecast_ttl_s():
            return _result(cached["periods"], NWS_SOURCE, "nws", "hit", cached["fetched_at"])
    else:
        cached = None

    start = time.monotonic()
    nws_budget = max(1.0, timeout * 2 / 3)
    nws_error: Exception | None = None
    if use_nws:
        try:
            periods = _fetch_nws_live(field, cache, start + nws_budget)
            cache.set(forecast_key, {"periods": periods, "fetched_at": wall_now}, stale_ttl_s())
            return _result(periods, NWS_SOURCE, "nws", "miss", wall_now)
        except Exception as exc:  # network errors, timeouts, bad JSON, 5xx
            nws_error = exc
            log.warning("NWS forecast failed for %s,%s: %s", lat, lon, exc.__class__.__name__)
    else:
        nws_error = RuntimeError("outside NWS coverage")

    if cached and wall_now - float(cached.get("fetched_at", 0)) < stale_ttl_s():
        return _result(cached["periods"], NWS_STALE_SOURCE, "nws_stale", "stale", cached["fetched_at"])

    fallback_key = f"openmeteo:hourly:{lat},{lon}"
    fb_cached = cache.get(fallback_key)
    om_label = OPEN_METEO_SOURCE if use_nws else OPEN_METEO_WORLD_SOURCE
    if isinstance(fb_cached, dict) and fb_cached.get("periods"):
        return _result(fb_cached["periods"], om_label, "open_meteo", "hit", fb_cached["fetched_at"])
    try:
        remaining = max(2.0, timeout - (time.monotonic() - start))
        periods = _fetch_open_meteo(field, remaining, cache)
    except Exception as exc:
        raise WeatherUnavailable(
            f"NWS failed ({nws_error.__class__.__name__}) and Open-Meteo failed ({exc.__class__.__name__})"
        ) from exc
    cache.set(fallback_key, {"periods": periods, "fetched_at": wall_now}, fallback_ttl_s())
    return _result(periods, om_label, "open_meteo", "miss", wall_now)


def _result(periods: list[dict[str, Any]], source: str, source_id: str, cache_state: str, fetched_at: float) -> dict[str, Any]:
    return {
        "periods": periods,
        "source": source,
        "source_id": source_id,
        "cache": cache_state,
        "fetched_at": datetime.fromtimestamp(float(fetched_at), timezone.utc).isoformat(),
    }

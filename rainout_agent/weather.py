"""Cached hourly weather with an Open-Meteo fallback.

Lookup order for one venue:

1. A fresh cached NWS hourly forecast (``RAINOUT_FORECAST_TTL_S``, default 600 s).
2. A live NWS fetch: the points -> gridpoint lookup is cached for 7 days, so a
   cold forecast normally needs one NWS request, not two.
3. A stale cached NWS forecast up to ``RAINOUT_FORECAST_STALE_S`` old (default 2 h)
   when NWS errors or times out.
4. Open-Meteo (free, no key; cached ``RAINOUT_FALLBACK_TTL_S``, default 600 s),
   converted to NWS-shaped hourly periods.

Every result says which source served it (``source_id``: ``nws``, ``nws_stale``,
``open_meteo`` or ``met_norway``) and whether the cache was used (``cache``: ``hit``,
``miss``, ``stale``). If every source fails, ``WeatherUnavailable`` is raised.

Commercial-only requests (paid answers with ``RAINOUT_COMMERCIAL_SOURCES`` on, see
``source_policy``) never touch Open-Meteo: US points use NWS (live or stale cache)
only, and points outside NWS coverage use MET Norway Locationforecast.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent import source_policy
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
# "complete" (not "compact"): it carries probability_of_precipitation where MET has one (the Nordic area).
MET_NORWAY_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MET_NORWAY_SOURCE = "MET Norway Locationforecast"
MET_NORWAY_STALE_SOURCE = "MET Norway Locationforecast (cached; MET Norway currently unavailable)"
MET_NORWAY_ATTRIBUTION = "Data from MET Norway (api.met.no), CC BY 4.0"
# MET Norway asks for an app name plus a contact link or company email in the User-Agent.
MET_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
MET_MIN_TTL_S = 300  # never re-ask sooner than this, even if Expires is earlier
MET_DEFAULT_TTL_S = 1800  # when the response has no usable Expires header
MET_KEEP_S = 6 * 3600  # how long a cached MET answer may be served stale or revalidated

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
    """GET a JSON document from a fixed, trusted API host (monkeypatched in tests).

    Refuses Open-Meteo URLs while a commercial-only (paid) request is being answered.
    """
    source_policy.check_url(url)
    return _transport_json(url, headers, timeout)


def _transport_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
        return json.loads(resp.read(MAX_BODY_BYTES).decode("utf-8"))


def _met_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], Any]:
    """GET for api.met.no: returns (status, headers, json or None). 304 comes back as status 304."""
    import urllib.error

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
            raw = resp.read(MAX_BODY_BYTES)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if hdrs.get("content-encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, hdrs, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, {k.lower(): v for k, v in exc.headers.items()}, None
        raise


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


# ------------------------------------------------------------------ MET Norway
_MET_SYMBOL_TEXT = {
    "clearsky": "Clear", "fair": "Mostly Clear", "partlycloudy": "Partly Cloudy", "cloudy": "Cloudy", "fog": "Fog",
}


def met_symbol_text(code: str | None) -> str:
    """Plain words for a MET Norway symbol_code ('lightrainshowersandthunder_day' -> 'Light Rain Showers And Thunder')."""
    if not code:
        return "Unknown"
    base = str(code).split("_")[0].lower()
    if base in _MET_SYMBOL_TEXT:
        return _MET_SYMBOL_TEXT[base]
    rest = base.replace("lightss", "lights")  # MET's documented typo: lightssleet..., lightssnow...
    out = []
    while rest:
        token = next((t for t in ("heavy", "light", "rain", "sleet", "snow", "showers", "and", "thunder") if rest.startswith(t)), None)
        if token is None:
            return base.title()
        out.append(token.title())
        rest = rest[len(token):]
    return " ".join(out)


# MET Norway gives a rain chance (probability_of_precipitation) only for the Nordic area. Elsewhere we
# estimate one from the forecast amount for the period. This table is a plain, uncalibrated heuristic,
# always labelled ``estimated_from_amount``; backtest it before trusting it (see docs/data-sources.md).
_AMOUNT_TO_CHANCE = ((0.05, 5), (0.2, 25), (0.5, 40), (1.0, 55), (2.5, 70), (5.0, 80))


def chance_from_amount(mm: float | None) -> int | None:
    if mm is None:
        return None
    for limit, pct in _AMOUNT_TO_CHANCE:
        if mm < limit:
            return pct
    return 90


def met_norway_periods(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a Locationforecast 2.0 (compact or complete) JSON body into NWS-shaped periods.

    Uses ``next_1_hours`` while it exists, then ``next_6_hours`` blocks that start after the
    last covered hour. Each period also carries ``precipitationAmountMm`` and
    ``probabilityKind`` (``forecast`` when MET gave a rain chance, ``estimated_from_amount`` otherwise).
    """
    series = ((payload or {}).get("properties") or {}).get("timeseries") or []
    periods: list[dict[str, Any]] = []
    covered_until: datetime | None = None
    for entry in series:
        try:
            start = datetime.fromisoformat(str(entry["time"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        data = entry.get("data") or {}
        block, hours = None, 0
        if data.get("next_1_hours"):
            block, hours = data["next_1_hours"], 1
        elif data.get("next_6_hours"):
            block, hours = data["next_6_hours"], 6
        if not block or (covered_until and start < covered_until):
            continue
        details = block.get("details") or {}
        mm = details.get("precipitation_amount")
        pop = details.get("probability_of_precipitation")
        kind = "forecast"
        if pop is None:
            pop, kind = chance_from_amount(mm), "estimated_from_amount"
        thunder = details.get("probability_of_thunder")
        text = met_symbol_text((block.get("summary") or {}).get("symbol_code"))
        if thunder is not None and thunder >= 30 and "thunder" not in text.lower():
            text += " And Thunder"
        end = start + timedelta(hours=hours)
        periods.append({
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "probabilityOfPrecipitation": {"value": pop},
            "shortForecast": text,
            "detailedForecast": "",
            "precipitationAmountMm": mm,
            "probabilityKind": kind,
        })
        covered_until = end
    if not periods:
        raise ValueError("MET Norway returned no forecast periods")
    return periods


def _met_expiry(headers: dict[str, str], wall_now: float) -> float:
    from email.utils import parsedate_to_datetime

    try:
        expires = parsedate_to_datetime(headers.get("expires", "")).timestamp()
    except (TypeError, ValueError, IndexError):
        expires = wall_now + MET_DEFAULT_TTL_S
    return max(expires, wall_now + MET_MIN_TTL_S)


def fetch_met_norway(field: dict[str, Any], cache: Cache, timeout: float, now: float | None = None) -> dict[str, Any]:
    """MET Norway Locationforecast for a field, following the api.met.no terms.

    Identifies us in the User-Agent, sends at most 4 decimals, does not ask again before
    the ``Expires`` header, revalidates with ``If-Modified-Since`` and asks for gzip.
    Returns ``{periods, fetched_at, cache}``; serves a cached copy (stale) on errors or 429.
    """
    wall_now = time.time() if now is None else now
    lat, lon = _coords(field)
    key = f"metno:complete:{lat:.4f},{lon:.4f}"
    cached = cache.get(key)
    if not (isinstance(cached, dict) and cached.get("periods")):
        cached = None
    if cached and wall_now < float(cached.get("expires", 0)):
        return {"periods": cached["periods"], "fetched_at": cached["fetched_at"], "cache": "hit"}
    url = f"{MET_NORWAY_URL}?{urllib.parse.urlencode({'lat': f'{lat:.4f}', 'lon': f'{lon:.4f}'})}"
    headers = dict(MET_HEADERS)
    if cached and cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    try:
        status, resp_headers, body = _met_transport(url, headers, timeout)
        if status == 304 and cached:
            cached = {**cached, "expires": _met_expiry(resp_headers, wall_now)}
            cache.set(key, cached, MET_KEEP_S)
            return {"periods": cached["periods"], "fetched_at": cached["fetched_at"], "cache": "revalidated"}
        if status == 203:
            log.warning("MET Norway Locationforecast 2.0 is deprecated (HTTP 203); plan the upgrade")
        if status not in (200, 203):
            raise RuntimeError(f"MET Norway HTTP {status}")
        periods = met_norway_periods(body)
    except Exception as exc:
        if cached:
            log.warning("MET Norway failed for %s,%s (%s); serving cached", lat, lon, exc.__class__.__name__)
            return {"periods": cached["periods"], "fetched_at": cached["fetched_at"], "cache": "stale"}
        raise
    entry = {"periods": periods, "fetched_at": wall_now, "expires": _met_expiry(resp_headers, wall_now),
             "last_modified": resp_headers.get("last-modified")}
    cache.set(key, entry, MET_KEEP_S)
    return {"periods": periods, "fetched_at": wall_now, "cache": "miss"}


def _commercial_forecast(field: dict[str, Any], timeout: float, cache: Cache, wall_now: float, use_nws: bool,
                         forecast_key: str, cached: dict[str, Any] | None) -> dict[str, Any]:
    """Paid answers: NWS (live or stale) in the US, MET Norway elsewhere. Never Open-Meteo."""
    lat, lon = _coords(field)
    if use_nws:
        try:
            periods = _fetch_nws_live(field, cache, time.monotonic() + max(1.0, timeout))
            cache.set(forecast_key, {"periods": periods, "fetched_at": wall_now}, stale_ttl_s())
            return _result(periods, NWS_SOURCE, "nws", "miss", wall_now, tier="commercial")
        except Exception as exc:
            log.warning("NWS forecast failed for %s,%s (commercial): %s", lat, lon, exc.__class__.__name__)
            if cached and wall_now - float(cached.get("fetched_at", 0)) < stale_ttl_s():
                return _result(cached["periods"], NWS_STALE_SOURCE, "nws_stale", "stale", cached["fetched_at"], tier="commercial")
            raise WeatherUnavailable(f"NWS failed ({exc.__class__.__name__}); paid answers do not fall back to Open-Meteo") from exc
    try:
        met = fetch_met_norway(field, cache, timeout, now=wall_now)
    except Exception as exc:
        raise WeatherUnavailable(f"MET Norway failed ({exc.__class__.__name__})") from exc
    source = MET_NORWAY_STALE_SOURCE if met["cache"] == "stale" else MET_NORWAY_SOURCE
    return _result(met["periods"], source, "met_norway", met["cache"], met["fetched_at"], tier="commercial")


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
    commercial = source_policy.commercial_only()
    if isinstance(cached, dict) and cached.get("periods"):
        if use_nws and wall_now - float(cached.get("fetched_at", 0)) < forecast_ttl_s():
            return _result(cached["periods"], NWS_SOURCE, "nws", "hit", cached["fetched_at"],
                           tier="commercial" if commercial else "free")
    else:
        cached = None
    if commercial:
        return _commercial_forecast(field, timeout, cache, wall_now, use_nws, forecast_key, cached)

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


def _result(periods: list[dict[str, Any]], source: str, source_id: str, cache_state: str, fetched_at: float,
            tier: str | None = None) -> dict[str, Any]:
    out = {
        "periods": periods,
        "source": source,
        "source_id": source_id,
        "cache": cache_state,
        "fetched_at": datetime.fromtimestamp(float(fetched_at), timezone.utc).isoformat(),
    }
    if tier == "commercial":
        out["source_policy"] = "commercial"  # paid answer: NWS/NOAA (US) or MET Norway only
    if source_id == "met_norway":
        out["attribution"] = MET_NORWAY_ATTRIBUTION
    return out

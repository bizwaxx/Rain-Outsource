"""Shared worldwide rain-chance module (used by /v1/rain, MCP, the box CLI, motocross and venues).

Sources (all free, no key):

* Open-Meteo forecast API for every point on Earth: hourly precipitation
  probability, precipitation amount and weather code, daily max probability and
  totals, plus the local timezone. One request per location (cached 10 min)
  also feeds the NWS fallback in ``weather.py`` and motocross prior-rain totals.
* Open-Meteo geocoding for place names (cached 7 days).
* NWS api.weather.gov for US points: the primary hourly rain chance, through
  ``weather.get_hourly_forecast`` (cache, stale cache, Open-Meteo fallback).
  NWS has no hourly amounts, so amounts always come from Open-Meteo.

Open-Meteo's free tier is for non-commercial use (under 10,000 calls a day);
the caches keep us far below that.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from rainout_agent import weather
from rainout_agent.cache import Cache, get_cache

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_TTL_S = 600
GEOCODE_TTL_S = 7 * 24 * 3600
MAX_HOURS = 16 * 24
MAX_DAYS = 16
AMBIGUITY_RATIO = 0.25
MM_PER_IN = 25.4

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
COUNTRY_ALIASES = {"usa": "us", "united states": "us", "uk": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
                   "great britain": "gb", "united kingdom": "gb"}
# Rough boxes for NWS coverage: CONUS, Alaska, Hawaii, Puerto Rico/USVI, Guam.
US_BOXES = ((24.4, 49.5, -125.0, -66.8), (51.0, 71.6, -179.9, -129.9), (18.8, 22.4, -160.6, -154.7),
            (17.6, 18.6, -67.4, -64.5), (13.2, 13.7, 144.6, 145.0))
LATLON_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


class RainError(ValueError):
    """Bad input (message is safe to show)."""


class PlaceNotFound(LookupError):
    def __init__(self, query: str, suggestions: list[dict[str, Any]] | None = None):
        super().__init__(query)
        self.query = query
        self.suggestions = suggestions or []


# ------------------------------------------------------------------ places
def parse_latlon(text: str | None) -> tuple[float, float] | None:
    m = LATLON_RE.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RainError("Latitude must be -90..90 and longitude -180..180.")
    return lat, lon


def in_us(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in US_BOXES)


def _geo_search(name: str, cache: Cache, timeout: float) -> list[dict[str, Any]]:
    key = f"geo:v1:{name.lower()}"
    hit = cache.get(key)
    if isinstance(hit, list):
        return hit
    url = f"{GEOCODE_URL}?{urllib.parse.urlencode({'name': name, 'count': 10, 'language': 'en', 'format': 'json'})}"
    payload = weather._http_get_json(url, {"User-Agent": weather.USER_AGENT}, timeout)
    results = [{k: r.get(k) for k in ("name", "admin1", "admin2", "country", "country_code", "latitude", "longitude",
                                       "timezone", "population", "feature_code")}
               for r in (payload or {}).get("results") or []]
    cache.set(key, results, GEOCODE_TTL_S)
    return results


def _label(r: dict[str, Any]) -> str:
    parts = [r.get("name")]
    if r.get("admin1") and r.get("admin1") != r.get("name"):
        parts.append(r["admin1"])
    if r.get("country"):
        parts.append(r["country"])
    return ", ".join(p for p in parts if p)


def _place(r: dict[str, Any]) -> dict[str, Any]:
    return {"name": r.get("name"), "label": _label(r), "admin1": r.get("admin1"), "admin2": r.get("admin2"),
            "country": r.get("country"), "country_code": (r.get("country_code") or "").upper() or None,
            "latitude": r.get("latitude"), "longitude": r.get("longitude"), "timezone": r.get("timezone"),
            "population": r.get("population")}


def _matches_qualifier(r: dict[str, Any], qual: str) -> bool:
    q = qual.strip().lower()
    if not q:
        return True
    q = COUNTRY_ALIASES.get(q, q)
    state = US_STATES.get(q.upper(), "").lower()
    fields = [(r.get(k) or "").lower() for k in ("admin1", "admin2", "country")]
    cc = (r.get("country_code") or "").lower()
    return q == cc or (state and state == fields[0] and cc == "us") or any(f == q or (len(q) > 3 and f.startswith(q)) for f in fields)


def geocode(query: str, *, cache: Cache | None = None, timeout: float = 6.0) -> dict[str, Any]:
    """Resolve a place name. Returns {place, ambiguous, matches}; raises PlaceNotFound."""
    cache = get_cache() if cache is None else cache
    query = re.sub(r"\s+", " ", (query or "").strip())
    if not query:
        raise RainError("Give a place name or lat,lon.")
    if len(query) > 120:
        raise RainError("Place name is too long.")
    parts = [p.strip() for p in query.split(",") if p.strip()]
    name, quals = parts[0], parts[1:]
    results = _geo_search(name, cache, timeout)
    if not results and not quals and " " in name:
        # "Austin TX": try the last word as a qualifier.
        head, tail = name.rsplit(" ", 1)
        results, name, quals = _geo_search(head, cache, timeout), head, [tail]
    pool = [r for r in results if all(_matches_qualifier(r, q) for q in quals)]
    if not pool:
        raise PlaceNotFound(query, [_place(r) for r in results[:5]])
    top = pool[0]
    second = pool[1] if len(pool) > 1 else None
    ambiguous = False
    if second is not None:
        p1, p2 = top.get("population") or 0, second.get("population") or 0
        far_apart = abs((top.get("latitude") or 0) - (second.get("latitude") or 0)) + abs((top.get("longitude") or 0) - (second.get("longitude") or 0)) > 0.5
        ambiguous = far_apart and (p2 >= AMBIGUITY_RATIO * p1 if p1 else True)
    return {"query": query, "place": _place(top), "ambiguous": ambiguous, "matches": [_place(r) for r in pool[:5]]}


# --------------------------------------------------------------- forecasts
def open_meteo_payload(lat: float, lon: float, *, timeout: float = 6.0, cache: Cache | None = None) -> dict[str, Any]:
    """One cached Open-Meteo request per point: hourly + daily rain, local timezone, 3 past days."""
    cache = get_cache() if cache is None else cache
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    key = f"openmeteo:full:{lat},{lon}"
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("hourly"):
        return hit
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": "precipitation_probability,precipitation,weather_code",
        "daily": "precipitation_probability_max,precipitation_sum,weather_code",
        "timezone": "auto", "timeformat": "unixtime", "past_days": 3, "forecast_days": MAX_DAYS,
    })
    payload = weather._http_get_json(f"{weather.OPEN_METEO_URL}?{query}", {"User-Agent": weather.USER_AGENT}, timeout)
    if not (payload or {}).get("hourly", {}).get("time"):
        raise ValueError("Open-Meteo returned no hourly data")
    keep = {k: payload.get(k) for k in ("latitude", "longitude", "timezone", "utc_offset_seconds", "elevation", "hourly", "daily")}
    cache.set(key, keep, FORECAST_TTL_S)
    return keep


def amounts_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Hourly rows in UTC with inches (the shape motocross scoring reads)."""
    h = payload["hourly"]
    n = len(h["time"])
    pops = h.get("precipitation_probability") or [None] * n
    pr = h.get("precipitation") or [None] * n
    codes = h.get("weather_code") or [None] * n
    rows = [{"time": datetime.fromtimestamp(int(t), timezone.utc).isoformat(),
             "precip_in": None if pr[i] is None else round(pr[i] / MM_PER_IN, 3), "pop": pops[i], "code": codes[i]}
            for i, t in enumerate(h["time"])]
    return {"rows": rows, "source": "Open-Meteo (model estimates; past hours are model analysis, not a rain gauge)"}


def get_amounts(lat: float, lon: float, *, cache: Cache | None = None, timeout: float = 6.0) -> dict[str, Any] | None:
    try:
        return amounts_rows(open_meteo_payload(lat, lon, timeout=timeout, cache=cache))
    except Exception:
        return None


def _weather_text(code: Any) -> str | None:
    return weather._WMO_TEXT.get(int(code), "Unknown") if code is not None else None


def _hour(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _nws_by_hour(periods: list[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
    out = {}
    for p in periods or []:
        try:
            start = datetime.fromisoformat(p["startTime"])
            end = datetime.fromisoformat(p["endTime"]) if p.get("endTime") else start + timedelta(hours=1)
        except (KeyError, TypeError, ValueError):
            continue
        t = _hour(start)
        while t < end:
            out[t] = p
            t += timedelta(hours=1)
    return out


def _amount(mm: float | None) -> dict[str, Any]:
    return {"precip_mm": None if mm is None else round(mm, 1), "precip_in": None if mm is None else round(mm / MM_PER_IN, 2)}


def _fmt_amount(mm: float | None, units: str) -> str:
    if mm is None:
        return "an unknown amount"
    if units == "in":
        inches = mm / MM_PER_IN
        return "no measurable rain" if inches < 0.005 else f"about {inches:.2f} in"
    return "no measurable rain" if mm < 0.05 else f"about {mm:.1f} mm"


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%a %-I %p").replace(" 12 PM", " noon").replace(" 12 AM", " midnight")


def parse_at(value: str, tz: ZoneInfo) -> datetime:
    v = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(v, "%Y-%m-%d %I:%M %p")
        except ValueError as exc:
            raise RainError("at must look like 2026-10-18 19:00 (local time at the place) or an ISO time with offset.") from exc
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def forecast(lat: float, lon: float, *, place: dict[str, Any] | None = None, hours: int | None = None,
             days: int | None = None, at: str | None = None, now: datetime | None = None,
             cache: Cache | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """Rain chance and amounts for a point. NWS is primary in the US; Open-Meteo everywhere else."""
    cache = get_cache() if cache is None else cache
    now = now or datetime.now(timezone.utc)
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    if hours is None and days is None and at is None:
        hours = 24
    if hours is not None and not 1 <= int(hours) <= MAX_HOURS:
        raise RainError(f"hours must be 1-{MAX_HOURS}.")
    if days is not None and not 1 <= int(days) <= MAX_DAYS:
        raise RainError(f"days must be 1-{MAX_DAYS}.")
    us = (place.get("country_code") == "US") if place and place.get("country_code") else in_us(lat, lon)

    om, om_error = None, None
    try:
        om = open_meteo_payload(lat, lon, timeout=timeout * 0.6, cache=cache)
    except Exception as exc:  # NWS may still answer
        om_error = exc
    nws_hours: dict[datetime, dict[str, Any]] = {}
    nws_source = None
    if us:
        try:
            res = weather.get_hourly_forecast({"coordinates": {"lat": lat, "lon": lon}}, timeout=timeout, cache=cache)
            if res["source_id"] in ("nws", "nws_stale"):
                nws_hours, nws_source = _nws_by_hour(res["periods"]), res["source"]
        except Exception:
            pass
    if om is None and not nws_hours:
        raise weather.WeatherUnavailable(f"Open-Meteo failed ({om_error.__class__.__name__ if om_error else 'no data'})")

    tzname = (om or {}).get("timezone") or (place or {}).get("timezone")
    near = None
    if not tzname or not place:
        pts = cache.get(f"nws:points:{lat},{lon}") or {}
        near = pts.get("near")
        tzname = tzname or (near or {}).get("timeZone") or "UTC"
    tz = ZoneInfo(tzname)
    units = "in" if us else "mm"

    om_hours: dict[datetime, dict[str, Any]] = {}
    if om:
        h = om["hourly"]
        n = len(h["time"])
        for i, t in enumerate(h["time"]):
            om_hours[datetime.fromtimestamp(int(t), timezone.utc)] = {
                "pop": (h.get("precipitation_probability") or [None] * n)[i],
                "mm": (h.get("precipitation") or [None] * n)[i], "code": (h.get("weather_code") or [None] * n)[i]}

    def hour_row(t: datetime) -> dict[str, Any] | None:
        o, nw = om_hours.get(t), nws_hours.get(t)
        nws_pop = ((nw or {}).get("probabilityOfPrecipitation") or {}).get("value") if nw else None
        if nw is not None:
            prob, src = int(nws_pop or 0), "nws"
            sky = nw.get("shortForecast") or _weather_text((o or {}).get("code"))
        elif o is not None and o["pop"] is not None:
            prob, src, sky = int(o["pop"]), "open_meteo", _weather_text(o["code"])
        else:
            return None
        return {"time": t.astimezone(tz).isoformat(), "probability_percent": prob, **_amount((o or {}).get("mm")),
                "weather": sky, "probability_source": src}

    start = _hour(now)
    hourly = []
    if hours:
        for i in range(int(hours)):
            row = hour_row(start + timedelta(hours=i))
            if row:
                hourly.append(row)

    daily = []
    if days:
        today = now.astimezone(tz).date()
        dd = (om or {}).get("daily") or {}
        om_days = {}
        for i, t in enumerate(dd.get("time") or []):
            d = datetime.fromtimestamp(int(t) + int((om or {}).get("utc_offset_seconds") or 0), timezone.utc).date()
            om_days[d] = {"pop": (dd.get("precipitation_probability_max") or [None])[i] if i < len(dd.get("precipitation_probability_max") or []) else None,
                          "mm": (dd.get("precipitation_sum") or [None])[i] if i < len(dd.get("precipitation_sum") or []) else None,
                          "code": (dd.get("weather_code") or [None])[i] if i < len(dd.get("weather_code") or []) else None}
        for k in range(int(days)):
            d = today + timedelta(days=k)
            first = max(start, datetime.combine(d, datetime.min.time(), tz).astimezone(timezone.utc))
            last = datetime.combine(d + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc)
            span = []
            t = first
            while t < last:
                span.append(t)
                t += timedelta(hours=1)
            nws_vals = [((nws_hours[t].get("probabilityOfPrecipitation") or {}).get("value") or 0) for t in span if t in nws_hours]
            o = om_days.get(d, {})
            mm_left = None
            if om_hours and k == 0:
                vals = [om_hours[t]["mm"] for t in span if t in om_hours and om_hours[t]["mm"] is not None]
                mm_left = sum(vals) if vals else None
            om_left = [om_hours[t]["pop"] for t in span if t in om_hours and om_hours[t]["pop"] is not None] if k == 0 else []
            if span and len(nws_vals) == len(span):
                prob, src = int(max(nws_vals)), "nws"
            elif om_left:
                prob, src = int(max(om_left)), "open_meteo"
            elif o.get("pop") is not None:
                prob, src = int(o["pop"]), "open_meteo"
            else:
                continue
            mm = mm_left if k == 0 and mm_left is not None else o.get("mm")
            daily.append({"date": d.isoformat(), "weekday": d.strftime("%a"), "max_probability_percent": prob, **_amount(mm),
                          "weather": _weather_text(o.get("code")), "probability_source": src,
                          **({"note": "rest of today"} if k == 0 else {})})

    at_row = None
    if at:
        when = parse_at(at, tz)
        t = _hour(when)
        row = hour_row(t)
        if row is None:
            raise RainError("That time is outside the forecast range (now through 16 days).")
        around = [r for r in (hour_row(t + timedelta(hours=d)) for d in (-1, 0, 1)) if r]
        at_row = {**row, "requested": when.astimezone(tz).isoformat(),
                  "window_max_probability_percent": max(r["probability_percent"] for r in around)}

    label = (place or {}).get("label")
    if not label:
        label = f"{lat:.4f}, {lon:.4f}" + (f" (near {near['city']}, {near['state']})" if near else "")
    sources = {
        "probability": (f"{nws_source} (US primary); Open-Meteo beyond NWS range" if nws_hours else
                        ("Open-Meteo (NWS unavailable)" if us else "Open-Meteo")),
        "amounts": "Open-Meteo" if om else "unavailable",
        "nws_used": bool(nws_hours),
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)" + ("; National Weather Service" if nws_hours else ""),
    }
    result = {
        "place": {**(place or {}), "label": label, "latitude": lat, "longitude": lon, "timezone": tzname,
                  "in_nws_coverage": us},
        "timezone": tzname, "units": units, "source": sources, "as_of": now.isoformat(),
        "hourly": hourly, "daily": daily, "at": at_row,
    }
    result["summary"] = summarize(result, hours=hours, days=days)
    return result


def summarize(r: dict[str, Any], *, hours: int | None, days: int | None) -> str:
    label, units = r["place"]["label"], r["units"]
    if r.get("at"):
        a = r["at"]
        t = datetime.fromisoformat(a["time"])
        return (f"{_fmt_time(t)} in {label}: {a['probability_percent']}% chance of rain"
                f" ({_fmt_amount(a['precip_mm'], units)} that hour).")
    if r["hourly"]:
        rows = r["hourly"]
        peak = max(rows, key=lambda x: x["probability_percent"])
        total = sum(x["precip_mm"] or 0 for x in rows) if any(x["precip_mm"] is not None for x in rows) else None
        span = f"the next {len(rows)} hours"
        if peak["probability_percent"] < 15:
            return f"Rain is unlikely in {label} over {span} (max {peak['probability_percent']}% chance)."
        return (f"Up to {peak['probability_percent']}% chance of rain in {label} over {span}, highest around "
                f"{_fmt_time(datetime.fromisoformat(peak['time']))}, with {_fmt_amount(total, units)} expected in total.")
    if r["daily"]:
        wet = max(r["daily"], key=lambda x: x["max_probability_percent"])
        if wet["max_probability_percent"] < 15:
            return f"Rain is unlikely in {label} over the next {len(r['daily'])} days."
        return (f"Wettest day in {label} over the next {len(r['daily'])} days is {wet['weekday']} {wet['date']} "
                f"with up to {wet['max_probability_percent']}% chance and {_fmt_amount(wet['precip_mm'], units)} of rain.")
    return f"No forecast data for {label}."


def rain_chance(q: str | None = None, *, lat: float | str | None = None, lon: float | str | None = None,
                hours: int | None = None, days: int | None = None, at: str | None = None, pick: int | None = None,
                now: datetime | None = None, cache: Cache | None = None) -> dict[str, Any]:
    """Entry point for the API, MCP and CLI. Ambiguous names return matches instead of a forecast."""
    place = None
    if lat not in (None, "") or lon not in (None, ""):
        try:
            la, lo = float(lat), float(lon)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise RainError("lat and lon must both be numbers.") from exc
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            raise RainError("Latitude must be -90..90 and longitude -180..180.")
    elif parse_latlon(q):
        la, lo = parse_latlon(q)  # type: ignore[misc]
    else:
        geo = geocode(q or "", cache=cache)
        if geo["ambiguous"] and not pick:
            return {"query": geo["query"], "ambiguous": True, "matches": geo["matches"], "next_step": "ask_user_which_place",
                    "summary": (f"\"{geo['query']}\" matches several places: "
                                + "; ".join(f"{i}. {m['label']}" for i, m in enumerate(geo["matches"], 1))
                                + ". Add a state or country, or use lat,lon.")}
        if pick:
            if not 1 <= int(pick) <= len(geo["matches"]):
                raise RainError(f"pick must be 1-{len(geo['matches'])}.")
            chosen = geo["matches"][int(pick) - 1]
        else:
            chosen = geo["place"]
        place, la, lo = chosen, chosen["latitude"], chosen["longitude"]
        res = forecast(la, lo, place=place, hours=hours, days=days, at=at, now=now, cache=cache)
        res["query"], res["ambiguous"] = geo["query"], False
        if len(geo["matches"]) > 1:
            res["other_matches"] = [m["label"] for m in geo["matches"] if m is not chosen][:4]
        return res
    res = forecast(la, lo, hours=hours, days=days, at=at, now=now, cache=cache)
    res["query"], res["ambiguous"] = q or f"{la},{lo}", False
    return res

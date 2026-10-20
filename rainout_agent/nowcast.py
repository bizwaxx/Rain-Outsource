"""Two-hour precipitation nowcast in 15-minute steps (GET /v1/nowcast, MCP get_precipitation_nowcast).

Free sources only, no keys:

* Open-Meteo ``minutely_15`` precipitation (amounts). Over North America this comes
  from NOAA's HRRR model at 15-minute steps; elsewhere it is interpolated from hourly
  models. Cached 5 minutes per point.
* NWS gridpoint data (api.weather.gov/gridpoints/...): probability of precipitation and
  quantitative precipitation for US points. Primary rain chance in the US, matching
  /v1/rain. Cached 10 minutes.
* NWS latest station observation (US): what the nearest airport/ASOS station saw in the
  last hour. Best effort, cached 5 minutes.

Optional, off by default: the Precip API (precip.ai, observation-based 1-minute rates).
It is a paid service; set ``RAINOUT_PRECIP_API_KEY`` only if the operator already has a
key. Without it, no request is made.

Inspiration, not integrated: Buienradar's 2-hour radar nowcast (Netherlands only) and
NOAA MRMS radar (GRIB2 files; too heavy for this serverless function).
"""
from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from rainout_agent import rain, weather
from rainout_agent.cache import Cache, get_cache

SLOT_MIN = 15
SLOTS = 8  # 2 hours
M15_TTL_S = 300
GRID_TTL_S = 600
OBS_TTL_S = 300
STATIONS_TTL_S = 7 * 24 * 3600
POINTS_TTL_S = 7 * 24 * 3600
PRECIP_URL = "https://api.precip.ai/api/v1/minutely"
RAIN_MM = 0.1  # 15-min amount that counts as "rain in this slot" (~0.4 mm/h)
# Rain-rate classes from the AMS Glossary of Meteorology ("rain"): light < 2.5 mm/h,
# moderate 2.5-7.6 mm/h, heavy > 7.6 mm/h.
LIGHT_MAX_MMH, MODERATE_MAX_MMH = 2.5, 7.6
MM_PER_IN = 25.4

_DUR_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def intensity(rate_mm_h: float | None) -> str | None:
    if rate_mm_h is None:
        return None
    if rate_mm_h < 0.1:
        return "none"
    if rate_mm_h < LIGHT_MAX_MMH:
        return "light"
    if rate_mm_h <= MODERATE_MAX_MMH:
        return "moderate"
    return "heavy"


def parse_valid_time(value: str) -> tuple[datetime, datetime] | None:
    """NWS validTime '2026-10-20T01:00:00+00:00/PT6H' -> (start, end)."""
    try:
        start_s, dur = value.split("/")
        start = datetime.fromisoformat(start_s)
        m = _DUR_RE.match(dur)
        if not m:
            return None
        d, h, mi = (int(x or 0) for x in m.groups())
        return start, start + timedelta(days=d, hours=h, minutes=mi)
    except (ValueError, AttributeError):
        return None


def _floor_slot(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % SLOT_MIN)


# ------------------------------------------------------------------ sources
def open_meteo_m15(lat: float, lon: float, *, cache: Cache, timeout: float) -> dict[str, Any]:
    key = f"openmeteo:m15:{lat},{lon}"
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("minutely_15"):
        return hit
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "minutely_15": "precipitation,precipitation_probability,weather_code",
        "forecast_minutely_15": 12, "past_minutely_15": 1, "timezone": "auto", "timeformat": "unixtime"})
    payload = weather._http_get_json(f"{weather.OPEN_METEO_URL}?{query}", {"User-Agent": weather.USER_AGENT}, timeout)
    if not ((payload or {}).get("minutely_15") or {}).get("time"):
        raise ValueError("Open-Meteo returned no minutely_15 data")
    keep = {k: payload.get(k) for k in ("timezone", "utc_offset_seconds", "minutely_15")}
    cache.set(key, keep, M15_TTL_S)
    return keep


def _nws_points(lat: float, lon: float, *, cache: Cache, timeout: float) -> dict[str, Any]:
    key = f"nws:points:grid:{lat},{lon}"
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("forecastGridData"):
        return hit
    props = weather._http_get_json(f"https://api.weather.gov/points/{lat},{lon}", weather.NWS_HEADERS, timeout)["properties"]
    entry = {"forecastGridData": props.get("forecastGridData"), "observationStations": props.get("observationStations"),
             "timeZone": props.get("timeZone")}
    for k in ("forecastGridData", "observationStations"):
        if entry[k] and not str(entry[k]).startswith("https://api.weather.gov/"):
            raise ValueError("unexpected NWS URL")
    cache.set(key, entry, POINTS_TTL_S)
    return entry


def nws_grid(lat: float, lon: float, *, cache: Cache, timeout: float, now: datetime) -> dict[str, Any]:
    """PoP and QPF intervals from NWS gridpoint data (slimmed to the next 3 days)."""
    key = f"nws:grid:{lat},{lon}"
    hit = cache.get(key)
    if isinstance(hit, dict) and "pop" in hit:
        return hit
    pts = _nws_points(lat, lon, cache=cache, timeout=timeout)
    if not pts.get("forecastGridData"):
        raise ValueError("no NWS grid for this point")
    props = weather._http_get_json(pts["forecastGridData"], weather.NWS_HEADERS, timeout)["properties"]
    horizon = now + timedelta(days=3)

    def slim(name: str) -> list[list[Any]]:
        out = []
        for v in (props.get(name) or {}).get("values") or []:
            span = parse_valid_time(v.get("validTime", ""))
            if span and span[1] > now - timedelta(hours=1) and span[0] < horizon and v.get("value") is not None:
                out.append([span[0].isoformat(), span[1].isoformat(), v["value"]])
        return out

    entry = {"pop": slim("probabilityOfPrecipitation"), "qpf_mm": slim("quantitativePrecipitation"),
             "updated": props.get("updateTime")}
    cache.set(key, entry, GRID_TTL_S)
    return entry


def _grid_at(series: list[list[Any]], t: datetime) -> tuple[float, float] | None:
    """(value, interval_hours) of the grid interval covering t."""
    for s, e, v in series:
        start, end = datetime.fromisoformat(s), datetime.fromisoformat(e)
        if start <= t < end:
            return float(v), (end - start).total_seconds() / 3600
    return None


def nws_observation(lat: float, lon: float, *, cache: Cache, timeout: float) -> dict[str, Any] | None:
    pts = _nws_points(lat, lon, cache=cache, timeout=timeout)
    st_url = pts.get("observationStations")
    if not st_url:
        return None
    skey = f"nws:stations:{lat},{lon}"
    station = cache.get(skey)
    if not isinstance(station, dict):
        feats = (weather._http_get_json(f"{st_url}?limit=1", weather.NWS_HEADERS, timeout) or {}).get("features") or []
        if not feats:
            return None
        p = feats[0].get("properties") or {}
        station = {"id": p.get("stationIdentifier"), "name": p.get("name")}
        if not station["id"] or not re.fullmatch(r"[A-Z0-9]{3,6}", station["id"]):
            return None
        cache.set(skey, station, STATIONS_TTL_S)
    okey = f"nws:obs:{station['id']}"
    obs = cache.get(okey)
    if not isinstance(obs, dict):
        p = weather._http_get_json(f"https://api.weather.gov/stations/{station['id']}/observations/latest",
                                   weather.NWS_HEADERS, timeout)["properties"]
        mm = (p.get("precipitationLastHour") or {}).get("value")
        obs = {"station_id": station["id"], "station_name": station.get("name"), "observed_at": p.get("timestamp"),
               "conditions": p.get("textDescription") or None,
               "precip_last_hour_mm": None if mm is None else round(float(mm), 1),
               "precip_last_hour_in": None if mm is None else round(float(mm) / MM_PER_IN, 2)}
        cache.set(okey, obs, OBS_TTL_S)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(obs["observed_at"])).total_seconds() / 60
        obs = {**obs, "age_minutes": int(age), "stale": age > 90}
    except (TypeError, ValueError, KeyError):
        pass
    return obs


def precip_api_slots(lat: float, lon: float, start: datetime, *, cache: Cache, timeout: float) -> list[float | None] | None:
    """Optional paid provider: per-slot mm from Precip's 1-minute rates. None when not configured."""
    key_value = os.environ.get("RAINOUT_PRECIP_API_KEY", "").strip()
    if not key_value:
        return None
    ckey = f"precip:minutely:{lat},{lon}:{start.isoformat()}"
    hit = cache.get(ckey)
    if isinstance(hit, list):
        return hit
    end = start + timedelta(minutes=SLOT_MIN * SLOTS)
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "format": "json",
                                "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.strftime("%Y-%m-%dT%H:%M:%S")})
    payload = weather._http_get_json(f"{PRECIP_URL}?{q}", {"User-Agent": weather.USER_AGENT,
                                                            "Authorization": f"Bearer {key_value}"}, timeout)
    feats = (payload or {}).get("features")
    minutes = ((feats[0].get("properties") or {}).get("minutes") if feats else (payload or {}).get("minutes")) or []
    buckets: list[list[float]] = [[] for _ in range(SLOTS)]
    for m in minutes:
        try:
            t = datetime.strptime(m["startTime"].replace("+0000", "+00:00"), "%Y-%m-%dT%H:%M:%S%z")
            rate = float(m.get("precip_rate") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        i = int((t - start).total_seconds() // (SLOT_MIN * 60))
        if 0 <= i < SLOTS:
            buckets[i].append(rate)
    out = [round(sum(b) / len(b) / 4, 2) if b else None for b in buckets]  # mean mm/h over 15 min -> mm
    cache.set(ckey, out, M15_TTL_S)
    return out


# ------------------------------------------------------------------ nowcast
def _fmt(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").replace(":00 ", " ")


def nowcast(lat: float, lon: float, *, label: str | None = None, now: datetime | None = None,
            cache: Cache | None = None, timeout: float = 8.0) -> dict[str, Any]:
    cache = get_cache() if cache is None else cache
    now = now or datetime.now(timezone.utc)
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    us = rain.in_us(lat, lon)
    start = _floor_slot(now)
    slots = [start + timedelta(minutes=SLOT_MIN * i) for i in range(SLOTS)]
    sources, notes = [], []

    m15, m15_err = None, None
    try:
        m15 = open_meteo_m15(lat, lon, cache=cache, timeout=timeout * 0.5)
    except Exception as exc:
        m15_err = exc
    grid = None
    obs = None
    if us:
        try:
            grid = nws_grid(lat, lon, cache=cache, timeout=timeout * 0.5, now=now)
        except Exception:
            notes.append("NWS gridpoint data was unavailable; rain chance comes from Open-Meteo.")
        try:
            obs = nws_observation(lat, lon, cache=cache, timeout=min(3.0, timeout * 0.3))
        except Exception:
            obs = None
    precip_slots = None
    try:
        precip_slots = precip_api_slots(lat, lon, start, cache=cache, timeout=timeout * 0.4)
    except Exception:
        notes.append("The optional Precip provider failed; using free model data.")
    if m15 is None and grid is None and precip_slots is None:
        raise weather.WeatherUnavailable(f"Open-Meteo failed ({m15_err.__class__.__name__ if m15_err else 'no data'})")

    om_mm: dict[datetime, float | None] = {}
    om_pop: dict[datetime, float | None] = {}
    if m15:
        m = m15["minutely_15"]
        n = len(m["time"])
        prs = m.get("precipitation") or [None] * n
        pops = m.get("precipitation_probability") or [None] * n
        for i, ts in enumerate(m["time"]):
            end_t = datetime.fromtimestamp(int(ts), timezone.utc)
            # Open-Meteo 15-minute sums cover the 15 minutes before the timestamp.
            om_mm[end_t - timedelta(minutes=SLOT_MIN)] = prs[i]
            # Probability is an instantaneous-style value at the timestamp; use it for the slot it starts.
            om_pop[end_t] = pops[i]
        sources.append({"name": "Open-Meteo 15-minute forecast", "url": "https://open-meteo.com/",
                        "used_for": "15-minute amounts" + ("" if us else " and rain chance"),
                        "note": "NOAA HRRR at 15-minute steps over North America; interpolated hourly models elsewhere."})
    if grid:
        sources.append({"name": "National Weather Service gridpoint forecast", "url": "https://api.weather.gov/",
                        "used_for": "rain chance (primary in the US)" + ("" if m15 else " and amounts"),
                        "updated": grid.get("updated")})
    if precip_slots is not None:
        sources.append({"name": "Precip (precip.ai) minutely rates", "url": "https://precip.ai/",
                        "used_for": "15-minute amounts (observation-based, operator-supplied key)"})
    if obs:
        sources.append({"name": f"NWS station {obs['station_id']} latest observation",
                        "url": f"https://api.weather.gov/stations/{obs['station_id']}/observations/latest",
                        "used_for": "what fell in the last hour"})

    tzname = (m15 or {}).get("timezone") or "UTC"
    tz = ZoneInfo(tzname)
    intervals = []
    for s in slots:
        mm, amount_src = None, None
        if precip_slots is not None and precip_slots[slots.index(s)] is not None:
            mm, amount_src = precip_slots[slots.index(s)], "precip_ai"
        elif s in om_mm and om_mm[s] is not None:
            mm, amount_src = float(om_mm[s]), "open_meteo"
        elif grid:
            q = _grid_at(grid["qpf_mm"], s)
            if q:
                mm, amount_src = q[0] / max(q[1] * 4, 1), "nws_qpf_spread"
        prob, prob_src = None, None
        if grid:
            p = _grid_at(grid["pop"], s)
            if p:
                prob, prob_src = int(round(p[0])), "nws"
        if prob is None and om_pop.get(s) is not None:
            prob, prob_src = int(om_pop[s]), "open_meteo"
        rate = None if mm is None else round(mm * 4, 2)
        intervals.append({
            "start": s.astimezone(tz).isoformat(), "end": (s + timedelta(minutes=SLOT_MIN)).astimezone(tz).isoformat(),
            "precip_mm": None if mm is None else round(mm, 2), "precip_in": None if mm is None else round(mm / MM_PER_IN, 3),
            "rate_mm_per_hr": rate, "intensity": intensity(rate), "probability_percent": prob,
            "probability_source": prob_src, "amount_source": amount_src})
    if any(i["amount_source"] == "nws_qpf_spread" for i in intervals):
        notes.append("Some amounts are NWS multi-hour totals spread evenly, not true 15-minute values.")

    wet = [i for i in intervals if (i["precip_mm"] or 0) >= RAIN_MM]
    probs = [i["probability_percent"] for i in intervals if i["probability_percent"] is not None]
    total = sum(i["precip_mm"] or 0 for i in intervals) if any(i["precip_mm"] is not None for i in intervals) else None
    rates = [i["rate_mm_per_hr"] for i in intervals if i["rate_mm_per_hr"] is not None]
    peak = intensity(max(rates)) if rates else None
    max_prob = max(probs) if probs else None
    place = label or f"{lat:.3f},{lon:.3f}"
    units_in = us
    tot_txt = (f"about {total / MM_PER_IN:.2f} in" if units_in else f"about {total:.1f} mm") if total else "a trace"
    if wet:
        first = datetime.fromisoformat(wet[0]["start"])
        last = datetime.fromisoformat(wet[-1]["end"])
        now_wet = wet[0] is intervals[0]
        when = "now" if now_wet else f"around {_fmt(first)}"
        summary = (f"Rain {'is falling' if now_wet else 'is expected to start'} {when} at {place}, {peak} at its heaviest, "
                   f"with {tot_txt} by {_fmt(last)}.")
    elif max_prob is not None and max_prob >= 50:
        summary = (f"Rain chance is up to {max_prob}% at {place} in the next 2 hours, but the 15-minute model shows no "
                   "measurable rain. Recheck in 30 minutes.")
    else:
        summary = f"No rain expected at {place} in the next 2 hours" + (f" (max {max_prob}% chance)." if max_prob is not None else ".")
    if obs and obs.get("precip_last_hour_mm") and not obs.get("stale"):
        summary += f" The nearest station measured {obs['precip_last_hour_in']} in in the last hour."

    return {
        "location": {"lat": lat, "lon": lon, "label": place, "timezone": tzname},
        "issued_at": now.astimezone(tz).isoformat(timespec="seconds"),
        "interval_minutes": SLOT_MIN,
        "window": {"start": intervals[0]["start"], "end": intervals[-1]["end"]},
        "intervals": intervals,
        "rain_expected": bool(wet),
        "rain_starts_at": wet[0]["start"] if wet else None,
        "rain_ends_at": wet[-1]["end"] if wet else None,
        "peak_intensity": peak,
        "total_precip_mm": None if total is None else round(total, 2),
        "total_precip_in": None if total is None else round(total / MM_PER_IN, 2),
        "max_probability_percent": max_prob,
        "observed": obs,
        "summary": summary,
        "recheck_after_minutes": 15 if wet or (max_prob or 0) >= 30 else 30,
        "sources": sources,
        "notes": notes + ["Model nowcast, not live radar. Thunderstorms can pop up between updates."],
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0) and the U.S. National Weather Service.",
    }


def get_precipitation_nowcast(query: str | None = None, *, lat: Any = None, lon: Any = None, pick: Any = None,
                              now: datetime | None = None, cache: Cache | None = None) -> dict[str, Any]:
    """Entry point for REST and MCP. Ambiguous place names return matches instead of a nowcast."""
    from rainout_agent.sites import resolve_site

    site = resolve_site(query, lat=lat, lon=lon, pick=pick, cache=cache)
    if site.get("ambiguous"):
        return site
    res = nowcast(site["lat"], site["lon"], label=site["label"], now=now, cache=cache)
    res["location"]["precision"] = site["precision"]
    if site.get("precision_note"):
        res["location"]["precision_note"] = site["precision_note"]
    return res

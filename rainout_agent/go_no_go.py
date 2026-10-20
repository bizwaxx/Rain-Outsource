"""Unified go / wait / no-go for outdoor events and work (GET|POST /v1/go-no-go, MCP go_no_go).

One call, one answer shape for every activity:
``{activity, decision, probability_percent, reason, recheck_at, sources, routed_to, ...}``.

* softball, baseball, soccer with a ``venue`` -> the sports status model (official status,
  NWS forecast, NOAA history), exactly what /v1/status returns.
* motocross with a ``track_id`` -> the motocross race checker.
* sports or motocross with only a location -> weather-only play odds over the event window
  (same unknown-status model as the sports core, no official status).
* concrete_pour -> check_concrete_pour.
* roofing, painting, general_outdoor_work -> workability rules (rain during and after,
  thunder, gusts, temperature, humidity/dew point, heat index) from
  data/construction/thresholds.json.

Workability framing inspired by Rain Check (AU field-service go/no-go); event framing by
WeatherNews' venue go/no-go support. Construction answers are weather screening only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent import play_probability, rain
from rainout_agent import site_weather as sw
from rainout_agent.cache import Cache

SPORTS = ("softball", "baseball", "soccer")
WORK = ("roofing", "painting", "general_outdoor_work")
ACTIVITIES = SPORTS + ("motocross", "concrete_pour") + WORK
ALIASES = {"concrete": "concrete_pour", "pour": "concrete_pour", "roof": "roofing", "paint": "painting",
           "exterior_painting": "painting", "outdoor_work": "general_outdoor_work", "work": "general_outdoor_work",
           "construction": "general_outdoor_work", "mx": "motocross", "football": "soccer"}
DEFAULT_EVENT_HOURS = {"softball": 1.5, "baseball": 2.5, "soccer": 2.0, "motocross": 9.0}
PENALTY = {"no_go": 0.3, "wait": 0.85}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def heat_index_f(t: float, rh: float) -> float:
    """NWS heat index (Rothfusz regression with the NWS adjustments)."""
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if (simple + t) / 2 < 80:
        return round(simple, 1)
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 6.83783e-3 * t * t
          - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return round(hi, 1)


def decision_from_percent(p: int | None, cfg: dict[str, Any]) -> str:
    b = cfg["go_no_go"]["decision_bands"]
    if p is None:
        return "wait"
    return "go" if p >= b["go_min_percent"] else "wait" if p >= b["wait_min_percent"] else "no_go"


def _recheck(start: datetime, now: datetime, tz: Any) -> str:
    hours_out = (start - now).total_seconds() / 3600
    t = start - timedelta(hours=24 if hours_out > 30 else 12 if hours_out > 14 else 2)
    return max(t, now + timedelta(minutes=30)).replace(second=0, microsecond=0).astimezone(tz).isoformat()


def _num(v: Any, name: str, lo: float, hi: float) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise rain.RainError(f"{name} must be a number.") from exc
    if not lo <= x <= hi:
        raise rain.RainError(f"{name} must be {lo:g}-{hi:g}.")
    return x


def _parse_time(value: str | None, tz: Any, now: datetime) -> datetime:
    if not value or str(value).strip().lower() == "now":
        return now
    return sw.parse_local(value, tz, "time")


# ------------------------------------------------------------------ workability
def workability(activity: str, rows: dict[datetime, dict[str, Any]], start: datetime, hours: float, cfg: dict[str, Any]) -> dict[str, Any]:
    a = cfg["go_no_go"]["activities"][activity]
    end = start + timedelta(hours=hours)
    st = sw.window_stats(rows, start, end)
    if st is None:
        raise rain.RainError("No forecast for that window yet (about 7 days ahead at most).")
    checks = []
    pop, mm = st["max_pop"] or 0, st["total_mm"] or 0
    if st["thunder"]:
        checks.append({"check": "thunderstorms", "status": "no_go", "detail": "thunderstorms are possible (stop work for lightning)"})
    rs = "no_go" if pop >= a["rain_no_go_pop"] else "wait" if pop >= a["rain_wait_pop"] or mm >= a["rain_wait_mm"] else "ok"
    checks.append({"check": "rain", "status": rs, "value": {"max_rain_chance_percent": pop, "rain_mm": mm},
                   "detail": f"{pop}% rain chance during the work" + (f", about {sw.mm_to_in(mm)} in" if mm >= 0.25 else "")})
    if a.get("dry_after_hours"):
        after = sw.window_stats(rows, end, end + timedelta(hours=a["dry_after_hours"]))
        if after:
            ap = after["max_pop"] or 0
            checks.append({"check": "rain_after", "status": "wait" if ap >= a["rain_wait_pop"] else "ok", "value": ap,
                           "detail": f"{ap}% rain chance in the {a['dry_after_hours']} h after finishing"
                                     + (" (fresh paint needs dry time)" if activity == "painting" else " (dry in the deck first)")})
    g = st["max_gust_mph"] or 0
    gs = "no_go" if a.get("gust_no_go_mph") and g >= a["gust_no_go_mph"] else "wait" if g >= a["gust_wait_mph"] else "ok"
    checks.append({"check": "wind", "status": gs, "value": g, "detail": f"gusts to {g:.0f} mph"})
    if a.get("min_temp_wait_f") is not None and st["min_temp_f"] is not None:
        cold = st["min_temp_f"] < a["min_temp_wait_f"]
        checks.append({"check": "cold", "status": "wait" if cold else "ok", "value": st["min_temp_f"],
                       "detail": f"{st['min_temp_f']:.0f}°F is below the usual {a['min_temp_wait_f']}°F minimum" if cold else "temperature is OK"})
    if a.get("max_temp_wait_f") is not None and st["max_temp_f"] is not None:
        hot = st["max_temp_f"] > a["max_temp_wait_f"]
        checks.append({"check": "heat", "status": "wait" if hot else "ok", "value": st["max_temp_f"],
                       "detail": f"{st['max_temp_f']:.0f}°F is hot for this work" if hot else "not too hot"})
    if a.get("max_rh_wait") is not None and st["max_rh"] is not None:
        humid = st["max_rh"] > a["max_rh_wait"]
        checks.append({"check": "humidity", "status": "wait" if humid else "ok", "value": st["max_rh"],
                       "detail": f"humidity up to {st['max_rh']}%" if humid else "humidity is OK"})
    if a.get("min_dew_spread_f") is not None and st["min_dew_spread_f"] is not None:
        dew = st["min_dew_spread_f"] < a["min_dew_spread_f"]
        checks.append({"check": "dew_point", "status": "wait" if dew else "ok", "value": st["min_dew_spread_f"],
                       "detail": f"air within {st['min_dew_spread_f']:.0f}°F of the dew point (surfaces may be damp)" if dew else "dew point is OK"})
    if a.get("heat_index_wait_f") is not None:
        his = [heat_index_f(rows[t]["temp_f"], rows[t]["rh"]) for t in sw.hours_between(start, end)
               if t in rows and rows[t].get("temp_f") is not None and rows[t].get("rh") is not None]
        hi = max(his) if his else None
        hs = "ok" if hi is None else "no_go" if hi >= a["heat_index_no_go_f"] else "wait" if hi >= a["heat_index_wait_f"] else "ok"
        checks.append({"check": "heat_index", "status": hs, "value": hi,
                       "detail": f"heat index up to {hi:.0f}°F (OSHA {'very high' if hs == 'no_go' else 'high'} risk)" if hs != "ok" else "heat index is OK"})
    statuses = [c["status"] for c in checks]
    decision = "no_go" if "no_go" in statuses else "wait" if "wait" in statuses else "go"
    prob = (100 - pop) / 100
    for c in checks:
        if c["check"] not in ("rain", "thunderstorms"):
            prob *= PENALTY.get(c["status"], 1.0)
    if decision == "go":
        reason = f"Go for {a['label']}: {pop}% rain chance, {st['min_temp_f']:.0f}-{st['max_temp_f']:.0f}°F, gusts to {g:.0f} mph."
    else:
        bad = [c["detail"] for c in checks if c["status"] == decision]
        reason = f"{'No-go' if decision == 'no_go' else 'Wait'} for {a['label']}: " + "; ".join(bad[:3]) + "."
    return {"decision": decision, "probability_percent": int(round(prob * 100)), "reason": reason, "checks": checks,
            "conditions": st, "window": (start, end)}


# ------------------------------------------------------------------ entry point
def go_no_go(activity: str | None, *, venue: str | None = None, track_id: str | None = None, location: str | None = None,
             lat: Any = None, lon: Any = None, pick: Any = None, time: str | None = None, duration_hours: Any = None,
             options: dict[str, Any] | None = None, now: datetime | None = None, cache: Cache | None = None) -> dict[str, Any]:
    act = (activity or "").strip().lower().replace("-", "_").replace(" ", "_")
    act = ALIASES.get(act, act)
    if act not in ACTIVITIES:
        raise rain.RainError(f"activity must be one of: {', '.join(ACTIVITIES)}.")
    options = options or {}
    now = (now or _now()).replace(microsecond=0)
    cfg = sw.thresholds()
    dur = _num(duration_hours, "duration_hours", 0.5, 16)

    if act in SPORTS and venue:
        return _venue(act, venue, time, now, cfg)
    if act == "motocross" and track_id:
        return _track(track_id, time, now, cfg, cache)
    if not (location or (lat not in (None, "") and lon not in (None, ""))):
        need = "venue (field name or field_id from find_venue)" if act in SPORTS else "track_id (from find_track)" if act == "motocross" else "lat and lon for the site"
        raise rain.RainError(f"Pass {need}, or a location (town name or lat/lon).")

    if act == "concrete_pour":
        from rainout_agent.concrete import check_concrete_pour

        r = check_concrete_pour(location, lat=lat, lon=lon, pick=pick, pour_start=time or None, duration_hours=dur,
                                now=now, cache=cache, **{k: options.get(k) for k in ("cure_hours", "critical_hours", "mix", "protection",
                                                                                      "concrete_temp_f", "search_days")})
        if r.get("ambiguous"):
            return r
        return {"activity": act, "decision": r["decision"], "probability_percent": r["probability_percent"], "reason": r["reason"],
                "recheck_at": r["recheck_at"], "sources": r["sources"], "routed_to": "check_concrete_pour",
                "time": {"start": r["pour"]["start"], "end": r["pour"]["end"]}, "location": r["location"],
                "best_alternative": r["best_alternative"], "details": {"checks": r["checks"], "mix": r["mix"]},
                "disclaimer": r["disclaimer"]}

    from rainout_agent.sites import resolve_site

    site = resolve_site(location, lat=lat, lon=lon, pick=pick, cache=cache)
    if site.get("ambiguous"):
        return site
    wx = sw.hourly(site["lat"], site["lon"], cache=cache)
    tz, rows = wx["tz"], wx["rows"]
    start = _parse_time(time, tz, now)
    if start < now - timedelta(hours=1):
        raise rain.RainError("time is in the past.")
    loc = {"lat": site["lat"], "lon": site["lon"], "label": site["label"], "timezone": wx["timezone"], "precision": site["precision"]}

    if act in WORK:
        hours = dur or cfg["go_no_go"]["activities"][act]["default_hours"]
        w = workability(act, rows, start, hours, cfg)
        return {"activity": act, "decision": w["decision"], "probability_percent": w["probability_percent"], "reason": w["reason"],
                "recheck_at": _recheck(start, now, tz), "sources": wx["sources"], "routed_to": "workability",
                "time": {"start": start.astimezone(tz).isoformat(), "end": w["window"][1].astimezone(tz).isoformat()},
                "location": loc, "details": {"checks": w["checks"], "conditions": w["conditions"],
                                             "thresholds": {k: v for k, v in cfg["go_no_go"]["activities"][act].items() if k != "source"},
                                             "references": [cfg["sources"][s] for s in cfg["go_no_go"]["activities"][act]["source"]]},
                "disclaimer": sw.DISCLAIMER}

    # Sports or motocross with a location only: weather-only odds, same unknown-status model as the sports core.
    hours = dur or DEFAULT_EVENT_HOURS[act]
    st = sw.window_stats(rows, start, start + timedelta(hours=hours))
    if st is None:
        raise rain.RainError("No forecast for that time yet (about 7 days ahead at most).")
    pop, thunder = st["max_pop"] or 0, bool(st["thunder"])
    p = play_probability.unknown_status_play_percent(pop, thunder, float(pop))
    decision = decision_from_percent(p, cfg)
    reason = (f"About {p}% chance the {act} {'race' if act == 'motocross' else 'game'} is played: {pop}% rain chance"
              f"{' with thunderstorms possible' if thunder else ''} at {sw.fmt_when(start, tz)}. No official status for a "
              "location without a venue; check the league or track before leaving.")
    return {"activity": act, "decision": decision, "probability_percent": p, "reason": reason, "recheck_at": _recheck(start, now, tz),
            "sources": wx["sources"], "routed_to": "weather_only_play_odds",
            "time": {"start": start.astimezone(tz).isoformat(), "end": (start + timedelta(hours=hours)).astimezone(tz).isoformat()},
            "location": loc, "details": {"rain_chance_percent": pop, "thunderstorm_likely": thunder, "official_status": "unknown",
                                         "next_step": "Pass venue (find_venue) or track_id (find_track) for official status and NOAA history."}}


def _venue(act: str, venue: str, time: str | None, now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.schedule import venue_timezone
    from rainout_agent.status_api import build_status_result, load_field, resolve_field_id

    fid = resolve_field_id(venue)
    if not fid:
        raise rain.RainError(f"Unknown venue '{venue}'. Use find_venue (or /v1/fields?q=) and pass its field_id.")
    tz = venue_timezone(load_field(fid))
    start = _parse_time(time, tz, now)
    r = build_status_result(field_query=fid, game_time=start.astimezone(tz).isoformat())
    if r.get("error"):
        raise rain.RainError(r.get("spoken_answer") or "Unknown venue.")
    p = r.get("game_time_play_probability_percent")
    status = r.get("official_status") or "unknown"
    decision = "no_go" if status in ("cancelled", "canceled", "closed", "field_closed", "field closed") else decision_from_percent(p, cfg)
    sources = [{"name": r.get("weather_source"), "used_for": "game-time rain chance"}]
    if r.get("official_status_source_url"):
        sources.append({"name": r.get("official_status_source_name") or "Official status page", "url": r["official_status_source_url"],
                        "used_for": "official status"})
    if (r.get("historical_calibration") or {}).get("source"):
        sources.append({"name": r["historical_calibration"]["source"], "used_for": "NOAA rain history calibration"})
    return {"activity": act, "decision": decision, "probability_percent": p, "reason": r.get("reason") or r.get("spoken_answer"),
            "spoken_answer": r.get("spoken_answer"), "recheck_at": _recheck(start, now, tz), "sources": sources, "routed_to": "get_venue_status",
            "time": {"start": start.astimezone(tz).isoformat()}, "location": {"venue": r.get("field"), "field_id": fid, "address": r.get("address")},
            "details": {"official_status": status, "rain_chance_percent": r.get("rain_chance_percent"),
                        "thunderstorm_likely": r.get("thunderstorm_likely"), "recommendation": r.get("recommendation")}}


def _track(track_id: str, time: str | None, now: datetime, cfg: dict[str, Any], cache: Cache | None) -> dict[str, Any]:
    from rainout_agent import motocross

    try:
        track = motocross.resolve_track(track_id)
    except motocross.UnknownTrack as exc:
        raise rain.RainError("Unknown track_id. Call find_track first.") from exc
    tz = motocross._tz(track)
    start = _parse_time(time, tz, now)
    try:
        r = motocross.check_race(track["field_id"], date_param=start.astimezone(tz).date().isoformat(), now=now)
    except motocross.TracksError as exc:
        raise rain.RainError(str(exc)) from exc
    ev = r.get("event") or {}
    cond = r.get("conditions_if_racing") or {}
    p = ev.get("play_probability") if r.get("found") else cond.get("play_probability")
    status = ev.get("official_status") or "unknown"
    decision = "no_go" if status == "cancelled" else decision_from_percent(p, cfg)
    sources = [{"name": s.get("name"), "url": s.get("url"), "used_for": "event listing"} for s in (ev.get("sources") or []) if isinstance(s, dict)]
    sources.append({"name": "National Weather Service / Open-Meteo", "used_for": "race-day rain odds"})
    return {"activity": "motocross", "decision": decision, "probability_percent": p, "reason": r.get("answer"),
            "recheck_at": _recheck(start, now, tz), "sources": sources, "routed_to": "check_race",
            "time": {"start": ev.get("start") or start.astimezone(tz).isoformat(), "end": ev.get("end")},
            "location": {"track": r["track"]["name"], "track_id": r["track"]["track_id"]},
            "details": {"official_status": status, "event_found": bool(r.get("found")), "next_step": r.get("next_step")}}

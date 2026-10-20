"""Concrete pour go / wait / no-go (POST|GET /v1/construction/concrete-pour, MCP check_concrete_pour).

Checks the pour itself, the critical early-set window right after it (rain marks or washes a
fresh surface), and the rest of the cure window: rain, thunderstorms, cold (< 40 F, ACI 306),
freeze, heat (> 90 F, ACI 305 / common specs), wind gusts and the ACI 305 evaporation rate
(plastic shrinkage). Thresholds live in data/construction/thresholds.json. Then it scans the
next few days for the best alternative start. Not engineering advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent import rain
from rainout_agent import site_weather as sw
from rainout_agent.cache import Cache

RANK = {"go": 2, "wait": 1, "no_go": 0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def evaporation_rate(air_f: float, rh_pct: float, wind_mph: float, concrete_f: float | None = None) -> float:
    """ACI 305 nomograph equation (Uno 1998), kg/m2/h. Uses 10 m wind, which errs on the high (cautious) side."""
    ta = (air_f - 32) / 1.8
    tc = ((concrete_f if concrete_f is not None else air_f) - 32) / 1.8
    v = max(wind_mph, 0) * 1.609
    e = 5 * ((tc + 18) ** 2.5 - (rh_pct / 100) * (ta + 18) ** 2.5) * (v + 4) * 1e-6
    return round(max(e, 0.0), 2)


def _max_evap(rows: dict[datetime, dict[str, Any]], start: datetime, end: datetime, concrete_f: float | None) -> float | None:
    vals = []
    for t in sw.hours_between(start, end):
        r = rows.get(t)
        if r and None not in (r.get("temp_f"), r.get("rh"), r.get("wind_mph")):
            vals.append(evaporation_rate(r["temp_f"], r["rh"], r["wind_mph"], concrete_f))
    return max(vals) if vals else None


def _rain_check(name: str, label: str, st: dict[str, Any], rc: dict[str, Any], prefix: str) -> dict[str, Any]:
    pop, mm = st["max_pop"] or 0, st["total_mm"] or 0
    status = "ok"
    if pop >= rc[f"{prefix}_no_go_pop"] or (mm >= rc[f"{prefix}_no_go_mm"] and pop >= rc[f"{prefix}_wait_pop"]):
        status = "no_go"
    elif pop >= rc[f"{prefix}_wait_pop"] or mm >= rc[f"{prefix}_wait_mm"]:
        status = "wait"
    amt = f", about {sw.mm_to_in(mm):.2f} in" if mm >= 0.25 else ""
    return {"check": name, "status": status, "value": {"max_rain_chance_percent": pop, "rain_mm": mm, "rain_in": sw.mm_to_in(mm)},
            "threshold": {"wait_percent": rc[f"{prefix}_wait_pop"], "no_go_percent": rc[f"{prefix}_no_go_pop"],
                          "wait_mm": rc[f"{prefix}_wait_mm"], "no_go_mm": rc[f"{prefix}_no_go_mm"]},
            "detail": f"{pop}% rain chance {label}{amt}"}


def evaluate(rows: dict[datetime, dict[str, Any]], start: datetime, cfg: dict[str, Any], *, duration_h: float, cure_h: float,
             critical_h: float, mix: dict[str, Any], protection: bool, concrete_f: float | None) -> dict[str, Any] | None:
    """Score one start time. None when the forecast doesn't cover the whole cure window."""
    c = cfg["concrete"]
    rc, tc = c["rain"], c["temperature_f"]
    pour_end = start + timedelta(hours=duration_h)
    crit_end = pour_end + timedelta(hours=critical_h)
    cure_end = pour_end + timedelta(hours=cure_h)
    pour = sw.window_stats(rows, start, pour_end)
    crit = sw.window_stats(rows, pour_end, crit_end)
    cure = sw.window_stats(rows, crit_end, cure_end) if cure_end > crit_end else None
    if pour is None or crit is None or (cure_end > crit_end and cure is None):
        return None
    checks = []
    if pour["thunder"] and rc.get("thunder_no_go", True):
        checks.append({"check": "thunderstorms", "status": "no_go", "value": True,
                       "detail": "thunderstorms are possible during the pour (stop work for lightning)"})
    checks.append(_rain_check("rain_during_pour", "during the pour", pour, rc, "pour"))
    checks.append(_rain_check("rain_during_early_set", f"in the first {critical_h:g} hours after the pour", crit, rc, "critical"))
    if cure:
        heavy = (cure["total_mm"] or 0) >= rc["cure_wait_total_mm"] or (cure["max_hourly_mm"] or 0) >= rc["cure_wait_hourly_mm"]
        checks.append({"check": "rain_later_in_cure", "status": "wait" if heavy and not protection else "ok",
                       "value": {"rain_mm": cure["total_mm"], "max_hourly_mm": cure["max_hourly_mm"], "max_rain_chance_percent": cure["max_pop"]},
                       "threshold": {"total_mm": rc["cure_wait_total_mm"], "hourly_mm": rc["cure_wait_hourly_mm"]},
                       "detail": (f"heavy rain later in the cure window (about {sw.mm_to_in(cure['total_mm'])} in)"
                                  + (", covers on site" if protection else ", keep plastic or covers ready")) if heavy
                       else "no heavy rain later in the cure window"})
    lows = [x["min_temp_f"] for x in (pour, crit, cure) if x and x["min_temp_f"] is not None]
    low = min(lows) if lows else None
    high = pour["max_temp_f"]
    if low is not None and low <= tc["freeze"]:
        checks.append({"check": "freeze", "status": "wait" if protection else "no_go", "value": low, "threshold": tc["freeze"],
                       "detail": f"freezing temperatures ({low:.0f}°F) before the concrete gains strength"
                                 + (" - insulated blankets or heat required" if protection else "")})
    elif low is not None and low < tc["cold"]:
        checks.append({"check": "cold", "status": "wait", "value": low, "threshold": tc["cold"],
                       "detail": f"cold weather ({low:.0f}°F) - needs a cold-weather plan (blankets, heat, accelerator) per ACI 306"})
    else:
        checks.append({"check": "cold", "status": "ok", "value": low, "threshold": tc["cold"], "detail": "no cold-weather concerns"})
    if high is not None and high > tc["hot"]:
        checks.append({"check": "heat", "status": "wait", "value": high, "threshold": tc["hot"],
                       "detail": f"hot weather ({high:.0f}°F) during the pour - hot-weather plan per ACI 305, or start earlier"})
    else:
        checks.append({"check": "heat", "status": "ok", "value": high, "threshold": tc["hot"], "detail": "no hot-weather concerns"})
    evap = _max_evap(rows, start, crit_end, concrete_f)
    ev_lim = mix.get("evaporation_wait", c["evaporation_kg_m2_h"]["wait"])
    checks.append({"check": "evaporation", "status": "wait" if evap is not None and evap >= ev_lim else "ok",
                   "value": evap, "threshold": ev_lim, "unit": "kg/m2/h",
                   "detail": (f"fast surface drying ({evap} kg/m²/h) - plastic shrinkage cracking risk; use evaporation retarder, "
                              "fogging or windbreaks" if evap is not None and evap >= ev_lim else "evaporation rate is OK")})
    gust = max(x for x in (pour["max_gust_mph"], 0) if x is not None)
    gl = c["wind_mph"]["gust_wait"]
    checks.append({"check": "wind", "status": "wait" if gust >= gl else "ok", "value": gust, "threshold": gl,
                   "detail": f"gusts to {gust:.0f} mph (check the pump boom limit)" if gust >= gl else "wind is OK"})

    statuses = [x["status"] for x in checks]
    decision = "no_go" if "no_go" in statuses else "wait" if "wait" in statuses else "go"
    rain_pop = max(pour["max_pop"] or 0, crit["max_pop"] or 0)
    prob = (100 - rain_pop) / 100
    for x in checks:
        if x["check"] in ("rain_during_pour", "rain_during_early_set", "thunderstorms"):
            continue
        prob *= {"no_go": 0.3, "wait": 0.85}.get(x["status"], 1.0)
    return {"decision": decision, "probability_percent": int(round(prob * 100)), "rain_probability_percent": rain_pop,
            "checks": checks, "start": start, "pour_end": pour_end, "critical_end": crit_end, "cure_end": cure_end,
            "conditions": {"pour": pour, "early_set": crit, "rest_of_cure": cure}, "max_evaporation_kg_m2_h": evap}


def _reason(ev: dict[str, Any], tz: Any) -> str:
    bad = [x["detail"] for x in ev["checks"] if x["status"] == ev["decision"]] if ev["decision"] != "go" else []
    if ev["decision"] == "go":
        p, cr = ev["conditions"]["pour"], ev["conditions"]["early_set"]
        lo, hi = round(p["min_temp_f"]), round(p["max_temp_f"])
        temps = f"{lo}°F" if lo == hi else f"{lo}-{hi}°F"
        return (f"Go: {max(p['max_pop'] or 0, cr['max_pop'] or 0)}% rain chance during the pour and early set, "
                f"{temps} during the pour.")
    word = "No-go" if ev["decision"] == "no_go" else "Wait"
    return f"{word}: " + "; ".join(bad[:3]) + "."


def _summary(ev: dict[str, Any], tz: Any) -> dict[str, Any]:
    return {"start": ev["start"].astimezone(tz).isoformat(), "start_spoken": sw.fmt_when(ev["start"], tz), "decision": ev["decision"],
            "probability_percent": ev["probability_percent"], "rain_probability_percent": ev["rain_probability_percent"],
            "reason": _reason(ev, tz)}


def check_concrete_pour(query: str | None = None, *, lat: Any = None, lon: Any = None, pick: Any = None,
                        pour_start: str | None = None, duration_hours: Any = None, cure_hours: Any = None,
                        critical_hours: Any = None, mix: str | None = None, protection: Any = False,
                        concrete_temp_f: Any = None, search_days: Any = None, now: datetime | None = None,
                        cache: Cache | None = None) -> dict[str, Any]:
    from rainout_agent.sites import resolve_site

    cfg = sw.thresholds()
    c = cfg["concrete"]
    d = c["defaults"]
    mix_key = (mix or d["mix"]).strip().lower().replace("-", "_").replace(" ", "_")
    if mix_key not in c["mixes"]:
        raise rain.RainError(f"mix must be one of: {', '.join(c['mixes'])}.")
    m = c["mixes"][mix_key]

    def num(v: Any, default: float, lo: float, hi: float, name: str) -> float:
        if v in (None, ""):
            return float(default)
        try:
            x = float(v)
        except (TypeError, ValueError) as exc:
            raise rain.RainError(f"{name} must be a number.") from exc
        if not lo <= x <= hi:
            raise rain.RainError(f"{name} must be {lo:g}-{hi:g}.")
        return x

    duration = num(duration_hours, d["duration_hours"], 0.5, 16, "duration_hours")
    cure = num(cure_hours, d["cure_hours"], 2, 72, "cure_hours")
    critical = num(critical_hours, m["critical_hours"], 1, 8, "critical_hours")
    critical = min(critical, cure)
    days = int(num(search_days, d["search_days"], 0, 5, "search_days"))
    concrete_f = None if concrete_temp_f in (None, "") else num(concrete_temp_f, 0, 20, 120, "concrete_temp_f")
    protect = protection in (True, "true", "1", "yes", 1)
    if not pour_start:
        raise rain.RainError("pour_start is required, e.g. '2026-10-21 07:00' (local time at the site).")

    site = resolve_site(query, lat=lat, lon=lon, pick=pick, cache=cache)
    if site.get("ambiguous"):
        return site
    now = now or _now()
    wx = sw.hourly(site["lat"], site["lon"], cache=cache)
    tz, rows = wx["tz"], wx["rows"]
    start = sw.parse_local(pour_start, tz, "pour_start")
    if start < now - timedelta(hours=1):
        raise rain.RainError("pour_start is in the past.")
    ev = evaluate(rows, start, cfg, duration_h=duration, cure_h=cure, critical_h=critical, mix=m, protection=protect, concrete_f=concrete_f)
    last = max(rows) + timedelta(hours=1) if rows else now
    if ev is None:
        raise rain.RainError(f"The forecast only reaches {sw.fmt_when(last, tz)}; the pour plus {cure:g} h cure must fit before that. "
                             "Pick an earlier start or a shorter cure_hours.")

    # Scan for alternatives: hourly starts in working hours over the next `days` days.
    cands = []
    first = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    t = first
    end_scan = now + timedelta(days=max(days, 0))
    want_h = start.astimezone(tz).hour

    def score(e: dict[str, Any]) -> tuple[int, int, int, float]:
        # Best decision, then odds, then closest to the requested time of day, then earliest.
        return (RANK[e["decision"]], e["probability_percent"], -abs(e["start"].astimezone(tz).hour - want_h), -e["start"].timestamp())

    while t <= end_scan and days > 0:
        lh = t.astimezone(tz).hour
        if d["earliest_start_hour"] <= lh <= d["latest_start_hour"] and t != start:
            e = evaluate(rows, t, cfg, duration_h=duration, cure_h=cure, critical_h=critical, mix=m, protection=protect, concrete_f=concrete_f)
            if e:
                cands.append(e)
        t += timedelta(hours=1)
    best = max(cands, key=score, default=None)
    better = best is not None and (RANK[best["decision"]], best["probability_percent"]) > (RANK[ev["decision"]], ev["probability_percent"])
    by_day: dict[Any, dict[str, Any]] = {}
    for e in cands:
        k = e["start"].astimezone(tz).date()
        if k not in by_day or score(e) > score(by_day[k]):
            by_day[k] = e

    reason = _reason(ev, tz)
    if ev["decision"] != "go" and better:
        reason += f" Better window: {sw.fmt_when(best['start'], tz)} ({best['decision'].replace('_', '-')}, {best['probability_percent']}%)."
    elif ev["decision"] != "go" and best is not None:
        reason += " No better window in the next few days."
    hours_out = (start - now).total_seconds() / 3600
    # A day ahead for far-off pours, the evening before for tomorrow's, then 2 hours before (nowcast).
    recheck = start - timedelta(hours=24 if hours_out > 30 else 12 if hours_out > 14 else 2)
    if recheck < now:
        recheck = now + timedelta(minutes=30)
    tzf = lambda x: x.astimezone(tz).isoformat()  # noqa: E731
    return {
        "activity": "concrete_pour",
        "decision": ev["decision"],
        "probability_percent": ev["probability_percent"],
        "rain_probability_percent": ev["rain_probability_percent"],
        "reason": reason,
        "location": {"lat": site["lat"], "lon": site["lon"], "label": site["label"], "timezone": wx["timezone"],
                     "precision": site["precision"], **({"precision_note": site["precision_note"]} if site.get("precision_note") else {})},
        "pour": {"start": tzf(start), "end": tzf(ev["pour_end"]), "duration_hours": duration},
        "early_set_window": {"start": tzf(ev["pour_end"]), "end": tzf(ev["critical_end"]), "hours": critical,
                             "note": "Fresh concrete is most sensitive to rain until final set."},
        "cure_window": {"start": tzf(ev["pour_end"]), "end": tzf(ev["cure_end"]), "hours": cure},
        "mix": {"key": mix_key, "label": m["label"]},
        "protection_on_site": protect,
        "checks": ev["checks"],
        "conditions": ev["conditions"],
        "best_alternative": _summary(best, tz) if better else None,
        "best_window_per_day": [_summary(by_day[k], tz) for k in sorted(by_day)],
        "recheck_at": tzf(recheck),
        "recheck_tip": "Run get_precipitation_nowcast about 2 hours before the pour for the 15-minute picture.",
        "sources": wx["sources"],
        "thresholds": {"version": cfg["version"], "rain": {k: v for k, v in c["rain"].items() if k != "source"},
                       "temperature_f": {k: v for k, v in c["temperature_f"].items() if k != "source"},
                       "wind_gust_wait_mph": c["wind_mph"]["gust_wait"], "evaporation_wait_kg_m2_h": m["evaporation_wait"],
                       "references": [cfg["sources"][k] for k in ("aci306", "aci305", "aci305_1", "field_practice")]},
        "disclaimer": sw.DISCLAIMER,
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0) and the U.S. National Weather Service.",
    }

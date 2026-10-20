"""Rain damage risk for materials on a job site (GET|POST /v1/construction/materials, MCP check_materials_risk).

For a time window it rates the rain as none / possible / likely / heavy (rain chance, total and
hourly intensity), maps that to a risk per material (low / moderate / high / severe) from
data/construction/thresholds.json, lowers it one step for covered materials unless gusts can
blow covers off, and lists plain protective actions. Not engineering advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent import nowcast, rain
from rainout_agent import site_weather as sw
from rainout_agent.cache import Cache

MAX_WINDOW_H = 7 * 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _truthy(v: Any) -> bool:
    return v in (True, 1, "1", "true", "yes", "True")


def parse_materials(value: Any, default_covered: bool, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    m = cfg["materials"]
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    if not isinstance(value, list) or not value:
        raise rain.RainError("materials is required: a list like ['drywall', 'lumber'] or a comma-separated string. "
                             f"Known: {', '.join(m['items'])}.")
    if len(value) > 20:
        raise rain.RainError("At most 20 materials per call.")
    out, seen = [], set()
    for item in value:
        covered = default_covered
        if isinstance(item, dict):
            name, covered = item.get("material") or item.get("name") or "", _truthy(item.get("covered", default_covered))
        else:
            name = str(item)
        key = name.strip().lower().replace("-", "_").replace(" ", "_")
        for cand in (key, key[:-1] if key.endswith("s") else key):
            cand = m["aliases"].get(cand, cand)
            if cand in m["items"]:
                key = cand
                break
        if key not in m["items"]:
            raise rain.RainError(f"Unknown material '{name}'. Known: {', '.join(m['items'])}.")
        if (key, covered) not in seen:
            seen.add((key, covered))
            out.append({"key": key, "covered": covered})
    return out


def rain_level(stats: dict[str, Any], rl: dict[str, Any]) -> str:
    pop, mm, hourly = stats["max_pop"] or 0, stats["total_mm"] or 0, stats["max_hourly_mm"] or 0
    if pop < rl["none_max_pop"] and mm < rl["none_max_mm"]:
        return "none"
    if pop >= rl["heavy_min_pop"] and (mm >= rl["heavy_total_mm"] or hourly >= rl["heavy_hourly_mm"]):
        return "heavy"
    if pop >= rl["likely_pop"] or mm >= rl["likely_mm"]:
        return "likely"
    return "possible"


def check_materials_risk(query: str | None = None, *, lat: Any = None, lon: Any = None, pick: Any = None,
                         materials: Any = None, covered: Any = False, start: str | None = None, end: str | None = None,
                         hours: Any = None, now: datetime | None = None, cache: Cache | None = None) -> dict[str, Any]:
    from rainout_agent.sites import resolve_site

    cfg = sw.thresholds()
    m = cfg["materials"]
    items = parse_materials(materials, _truthy(covered), cfg)
    if hours not in (None, ""):
        try:
            hours_f = float(hours)
        except (TypeError, ValueError) as exc:
            raise rain.RainError("hours must be a number.") from exc
        if not 1 <= hours_f <= MAX_WINDOW_H:
            raise rain.RainError(f"hours must be 1-{MAX_WINDOW_H}.")
    else:
        hours_f = None
    site = resolve_site(query, lat=lat, lon=lon, pick=pick, cache=cache)
    if site.get("ambiguous"):
        return site
    now = now or _now()
    wx = sw.hourly(site["lat"], site["lon"], cache=cache)
    tz, rows = wx["tz"], wx["rows"]
    t0 = sw.parse_local(start, tz, "start") if start else now
    if end:
        t1 = sw.parse_local(end, tz, "end")
    else:
        t1 = t0 + timedelta(hours=hours_f or 24)
    if t1 <= t0:
        raise rain.RainError("end must be after start.")
    if t1 < now:
        raise rain.RainError("The window is in the past.")
    if (t1 - t0).total_seconds() > MAX_WINDOW_H * 3600:
        raise rain.RainError("The window can be at most 7 days.")
    t0 = max(t0, now)
    last = max(rows) + timedelta(hours=1) if rows else now
    t1c = min(t1, last)
    stats = sw.window_stats(rows, t0, t1c)
    if stats is None:
        raise rain.RainError(f"No forecast for that window; the forecast reaches {sw.fmt_when(last, tz)}.")
    level = rain_level(stats, m["rain_level"])
    gust = stats["max_gust_mph"] or 0
    covers_at_risk = gust >= m["cover_wind_gust_mph"]
    humid_hours = sum(1 for t in sw.hours_between(t0, t1c) if t in rows and (rows[t].get("rh") or 0) >= m["humid_rh"])
    order = m["risk_order"]
    lvl_i = m["levels"].index(level)

    # Wettest hour for the summary.
    wet_t = max((t for t in sw.hours_between(t0, t1c) if t in rows),
                key=lambda t: ((rows[t].get("mm") or 0), rows[t].get("pop") or 0), default=None)

    results = []
    for it in items:
        spec = m["items"][it["key"]]
        base = order.index(spec["risk"][lvl_i])
        risk_i = base
        notes = []
        if it["covered"] and base > 0:
            if covers_at_risk:
                notes.append(f"gusts to {gust:.0f} mph can blow covers off, so covering doesn't lower the risk unless tied down")
            else:
                risk_i = max(0, base - int(spec.get("cover_reduction", 1)))
        if spec.get("humidity_sensitive") and humid_hours >= 6 and risk_i == 0:
            risk_i = 1
            notes.append(f"{humid_hours} hours of very humid air (90%+)")
        risk = order[risk_i]
        actions = [] if risk == "low" and not notes else list(spec["actions"])
        if it["covered"] and covers_at_risk and risk != "low":
            actions.insert(0, "Tie down and weight every tarp edge")
        why = {"none": "no rain expected", "possible": f"rain possible (up to {stats['max_pop']}%)",
               "likely": f"rain likely (up to {stats['max_pop']}%, about {sw.mm_to_in(stats['total_mm'])} in)",
               "heavy": f"heavy rain expected (about {sw.mm_to_in(stats['total_mm'])} in)"}[level]
        reason = f"{risk.capitalize()} risk for {spec['label']}{' (covered)' if it['covered'] else ''}: {why}"
        reason += ("; " + "; ".join(notes) if notes else "") + "."
        results.append({"material": it["key"], "label": spec["label"], "covered": it["covered"], "risk": risk,
                        "reason": reason, "actions": actions, "reference": cfg["sources"].get(spec.get("source", ""), "")})

    overall = max((r["risk"] for r in results), key=order.index)
    worst = [r for r in results if r["risk"] == overall]
    if overall == "low":
        summary = f"Low risk for everything listed at {site['label']}: {('no rain expected' if level == 'none' else 'only a small chance of rain')}."
    else:
        names = ", ".join(r["label"] for r in worst[:3])
        summary = f"{overall.capitalize()} risk for {names}. " + worst[0]["actions"][0] + "."
        if wet_t is not None and (rows[wet_t].get("mm") or 0) > 0:
            summary += f" Wettest hour: {sw.fmt_when(wet_t, tz)}."
    peak_rate = stats["max_hourly_mm"]
    hours_out = (t0 - now).total_seconds() / 3600
    recheck = now + timedelta(hours=3) if hours_out < 6 else t0 - timedelta(hours=3)
    return {
        "activity": "materials_protection",
        "overall_risk": overall,
        "summary": summary,
        "materials": results,
        "rain": {"level": level, "max_probability_percent": stats["max_pop"], "total_mm": stats["total_mm"],
                 "total_in": sw.mm_to_in(stats["total_mm"]), "max_hourly_mm": peak_rate,
                 "max_hourly_in": sw.mm_to_in(peak_rate), "peak_intensity": nowcast.intensity(peak_rate),
                 "wettest_hour": wet_t.astimezone(tz).isoformat() if wet_t is not None else None},
        "wind": {"max_gust_mph": stats["max_gust_mph"], "covers_at_risk": covers_at_risk, "cover_gust_limit_mph": m["cover_wind_gust_mph"]},
        "humid_hours": humid_hours,
        "window": {"start": t0.astimezone(tz).isoformat(), "end": t1c.astimezone(tz).isoformat(), "hours": round((t1c - t0).total_seconds() / 3600, 1),
                   **({"note": "The forecast ends before the requested window does."} if t1c < t1 else {})},
        "location": {"lat": site["lat"], "lon": site["lon"], "label": site["label"], "timezone": wx["timezone"],
                     "precision": site["precision"], **({"precision_note": site["precision_note"]} if site.get("precision_note") else {})},
        "recheck_at": max(recheck, now + timedelta(minutes=30)).astimezone(tz).isoformat(),
        "sources": wx["sources"],
        "thresholds": {"version": cfg["version"], "rain_level": {k: v for k, v in m["rain_level"].items() if k != "source"},
                       "cover_wind_gust_mph": m["cover_wind_gust_mph"], "humid_rh": m["humid_rh"]},
        "disclaimer": sw.DISCLAIMER,
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0) and the U.S. National Weather Service.",
    }

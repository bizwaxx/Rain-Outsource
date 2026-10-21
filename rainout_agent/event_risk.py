"""Whole-event weather risk: rain that already fell (soak), storms across the event window, wind and cold.

The old model looked only at the forecast for the start hour. Most real rainouts in the backtest happened for
other reasons: rain earlier that day or the day before had soaked the field, storms threatened part of the
event, or (in Lubbock) wind gusts above 45 mph. This module turns hourly weather around an event into a few
features and applies a short list of explainable rules on top of the base play probability.

Hourly rows are ``{utc hour start: row}`` with any of ``mm`` (rain in that hour), ``pop`` (chance %),
``thunder`` (bool), ``cape`` (J/kg), ``gust_mph``, ``temp_f``. Rain that already fell comes from ``observed``
rows (observations or a recent-past model run); the event window comes from ``forecast`` rows.

Rules (``PARAMS``, calibrated with leave-one-out on staging/fixtures/known_outcomes.json; see
staging/calibrate.py and docs/model.md):

1. Rain chance: the highest chance from 1 h before the start to the end (not only the start hour). On
   artificial turf, plain rain counts at ``turf_rain_factor`` (turf stays playable in rain; lightning doesn't).
2. Soak: ``earlier today + w24 x prior 24 h + w48 x the 24 h before``, times the surface's drainage factor
   (turf drains fast; grass and tarped college infields slower; community dirt and motocross dirt slowest),
   then a points-off table.
3. Storms: thunder in the window (forecast text, or an unstable hour, CAPE >= ``storm_cape`` with a rain
   chance >= 20%) caps the odds; storms elsewhere that day or the evening before take points off.
4. Wind gusts >= ``gust_mph`` or (ball sports) a start temperature <= ``cold_f`` cap the odds.
5. Humility cap: with any real risk signal present (``humble_soak_in`` of rain already fell, a 30% rain chance,
   storms or 35 mph gusts), never say more than ``humble_cap`` (so not "confident").
6. Turf floor: artificial turf without any storm signal never drops below ``turf_floor``.
7. Forecast-threat cap: with any threat signal from the day before through the event (storms, a 30% chance in the
   window, a 40% chance the day before, unstable air the evening before, or cold), never say more than
   ``threat_cap``, because organizers cancel on threats that never arrive. Motocross answers with a threat also
   carry ``FORECAST_CANCEL_NOTE`` ("organizers here sometimes cancel on the forecast alone").
8. Unstable-air cap: with very unstable air in the window or that day (CAPE >= ``unstable_cape``), never say more than
   ``UNSTABLE_CAP``, because pop-up storms and cancels happen even when the rain chance is low.
9. Cancel floor: weather alone never says less than ``cancel_floor`` (so never "confident cancel"), because organizers
   sometimes play through a bad forecast. This model only runs when no official status is posted; a posted
   cancellation still decides (0%).
10. Status ceiling (optional, off by default): with no official status yet, never say more than ``status_ceiling``,
   because organizers can still cancel for reasons the weather doesn't show. Held out on 154 fixtures it removes the
   last 2 confident wrong calls, but answers at 80%+ were right 95% of the time (37 of 39), so it is a policy
   choice, not a calibration fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

PARAMS: dict[str, Any] = {
    # Chosen by staging/calibrate.py (Oct 21 retest, 129 scored fixtures, 1,327,104 grid points, with the
    # unstable-air cap and cancel-floor axes). Held out (leave-one-out) with the caps fixed as below: 84% directional,
    # 2 confident wrong (new 68 alone: 93% / 0). Same set with every cap off: 79% / 7. Previous PARAMS: 80% / 5.
    "drainage": {"fast": 0.3, "medium": 0.6, "slow": 1.2},
    "soak_weights": (1.0, 0.6, 0.3),       # earlier today, prior 24 h, 24-48 h before
    "soak_scale": 1.0,
    "turf_rain_factor": 0.7,
    "storm_cape": None,                     # None = only explicit thunder forecasts
    "gust_mph": 45,                         # None = off
    "cold_f": 35,                           # None = off
    "humble_cap": 79,                       # None = off
    "humble_soak_in": 0.15,                 # raw weighted rain that counts as a risk signal for the humility cap
    "turf_floor": None,                     # turf without storms never drops below this (None = off; not chosen)
    # Ground-wetness bucket (PlayCondition / DryLies style): hourly over the 7 days before the start,
    # W = max(0, W + rain - et_mult x ET0 - drain), capped at WET_CAP_MM. "soak_model" picks which soak is used.
    "soak_model": "window",                 # "window" (2.5-day weighted rain), "wetness" (7-day bucket) or "hybrid" (max)
    "wet_drain_mm_h": {"fast": 1.0, "medium": 0.1, "slow": 0.05, "mx": 0.08},
    "wet_et_mult": 1.0,
    "wet_scale": 1.0,
    # Venue maintenance (rainout_agent.venue_profile): soak at maintained venues (tarp or grounds crew, college/pro)
    # is multiplied by this. None = the older rule (maintained dirt infields count as "medium" drainage).
    "maint_soak_factor": 0.2,
    # Forecast-threat caps (cancellations on a storm threat that never arrives). With any threat signal from the
    # day before through the event (see ``threat_signal``), never say more than this (None = off).
    "threat_cap": 79,
    # Motocross organizers often cancel on the forecast alone: with any threat signal, cap at this (None = off).
    "mx_forecast_cap": None,
    # Unstable air (CAPE in the window or that day) at or above this caps at UNSTABLE_CAP (None = off).
    "unstable_cape": 2500,
    # Weather alone never goes below this (None = off); a posted official cancellation still gives 0%.
    "cancel_floor": 21,
    # Optional ceiling for weather-only answers while no official status is posted (None = off; Jimmy's call).
    "status_ceiling": None,
    # Rain tolerance for college/pro soccer (tune/soccer-pro-rain, OFF by default): pro and college soccer play through
    # plain rain (lightning still stops them). For a rain-tolerant event, the window rain chance counts at
    # ``pro_rain_factor`` and the soak at ``pro_soak_factor`` (None = off). Storm, wind and cold rules are unchanged.
    "pro_rain_factor": None,
    "pro_soak_factor": None,
    # NWS watches/warnings (rainout_agent.nws_alerts; feature-flagged by RAINOUT_NWS_ALERTS, OFF by default). With the
    # feature present, cap the odds at these (None = off): a warning at the start, a watch at the start, tropical storm
    # or hurricane alerts in the 24 h before.
    "alert_warn_cap": None,
    "alert_watch_cap": None,
    "alert_tropical_cap": None,
}
ALERT_RULES = (("alert_tropical", "alert_tropical_cap", "A tropical storm or hurricane watch or warning covers this area"),
               ("alert_warn", "alert_warn_cap", "A National Weather Service warning covers this area at the start"),
               ("alert_watch", "alert_watch_cap", "A National Weather Service watch covers this area at the start"))
RAIN_TOLERANT_NOTE = "College and pro soccer usually play through rain (lightning still stops play)."
RAIN_TOLERANT_SPORTS = {"soccer"}


def rain_tolerant(sport: str | None, level: str | None) -> bool:
    """True for sports that play through plain rain at this level (college/pro soccer)."""
    return (sport or "").lower() in RAIN_TOLERANT_SPORTS and level == "college_pro"
UNSTABLE_CAP = 79
UNSTABLE_NOTE = "Unstable air: pop-up storms can form even with a low rain chance, and organizers may call it early."
STATUS_CEILING_NOTE = "No official status yet; organizers can still cancel for non-weather reasons."
CANCEL_FLOOR_NOTE = ("Weather alone can't make a cancellation certain (organizers sometimes play through a bad "
                     "forecast); check the official status.")
THREAT_SIGNALS = {"pop": 30, "prev_pop": 40}
FORECAST_CANCEL_NOTE = "Organizers here sometimes cancel on the forecast alone, even if the storm never arrives."
WET_CAP_MM = 75.0
WET_DAYS = 7
SOAK_PENALTY = ((0.0, 0), (0.1, 0), (0.3, 12), (0.6, 30), (1.0, 45), (2.0, 65), (3.0, 75))
STORM_WINDOW_CAP, STORM_WINDOW_DROP, STORM_DAY_DROP = 45, 10, 10
CAP_OTHER = 45                               # wind / cold cap
HUMBLE_SIGNALS = {"window_pop": 30, "gust_mph": 35}

FIELD_TYPE_DRAINAGE = {"turf": "fast", "artificial_turf": "fast", "grass_soccer": "medium", "football": "medium",
                       "grass": "medium", "grass_with_dirt_infield": "slow", "dirt_infield": "slow", "dirt": "slow",
                       "motocross": "slow", "mixed": "medium"}
MM_PER_IN = 25.4


def drainage_class(field: dict[str, Any] | None = None, field_type: str | None = None, maintained: bool = False) -> str:
    """'fast' | 'medium' | 'slow'. Tarped, crew-maintained infields (college/pro) count one step faster."""
    prof_type = None
    if field and isinstance(field.get("venue_profile_resolved"), dict):
        from rainout_agent import venue_profile
        prof_type = venue_profile.field_type(field["venue_profile_resolved"])
        sport = " ".join(str(x) for x in (field.get("sports") or [field.get("sport") or ""])).lower()
        if prof_type == "grass" and "ball" in sport and "foot" not in sport:
            prof_type = "grass_with_dirt_infield"   # a grass ball field still has a skinned dirt infield
    if field_type or prof_type:
        cls = FIELD_TYPE_DRAINAGE.get(field_type or prof_type, "slow")
    else:
        field = field or {}
        surface = str(field.get("surface") or "unknown").lower()
        if surface in FIELD_TYPE_DRAINAGE:
            cls = FIELD_TYPE_DRAINAGE[surface]
        else:
            sport = " ".join(str(s) for s in (field.get("sports") or [field.get("sport") or ""])).lower()
            # Unknown surface: baseball/softball fields almost always have skinned dirt infields.
            cls = "slow" if ("ball" in sport and "foot" not in sport and "basket" not in sport) else "medium"
        drainage = str(field.get("drainage") or "unknown").lower()
        if drainage == "good":
            maintained = True
        elif drainage == "poor" and cls != "slow":
            cls = {"fast": "medium", "medium": "slow"}[cls]
    if maintained and cls == "slow":
        cls = "medium"
    return cls


def _hours(a: datetime, b: datetime) -> Iterable[datetime]:
    t = a.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    while t < b:
        yield t
        t += timedelta(hours=1)


def _sum_mm(rows: dict[datetime, dict[str, Any]], a: datetime, b: datetime) -> float | None:
    vals = [rows.get(h, {}).get("mm") for h in _hours(a, b)]
    if not vals or any(v is None for v in vals):
        return None
    return float(sum(vals))


def _max(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return max(vals) if vals else None


def features(observed: dict[datetime, dict[str, Any]], forecast: dict[datetime, dict[str, Any]],
             start: datetime, duration_h: float) -> dict[str, Any]:
    """Event features (rain amounts in inches).

    * earlier_today_in: rain from 12 h before the start until 1 h before it (observed);
    * prior_24h_in / prior_48h_in: the 24 h before that, and the 24 h before that (observed);
    * window_pop: highest forecast rain chance from 1 h before the start to the end;
    * storm hours: forecast thunder, or CAPE with a rain chance >= 20% (see ``storm_flags``);
    * gust_mph: highest forecast gust in the window; temp_f: forecast temperature at the start.
    """
    end = start + timedelta(hours=duration_h)
    early = _sum_mm(observed, start - timedelta(hours=12), start - timedelta(hours=1))
    p24 = _sum_mm(observed, start - timedelta(hours=36), start - timedelta(hours=12))
    p48 = _sum_mm(observed, start - timedelta(hours=60), start - timedelta(hours=36))
    win = [forecast[h] for h in _hours(start - timedelta(hours=1), end) if h in forecast]
    near = [forecast[h] for h in _hours(start - timedelta(hours=2), end) if h in forecast]
    day = [forecast.get(h) or observed.get(h) or {} for h in _hours(start - timedelta(hours=18), end + timedelta(hours=3))]
    prev = [forecast.get(h) or observed.get(h) or {} for h in _hours(start - timedelta(hours=36), start - timedelta(hours=1))]
    first = forecast.get(start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)) or {}
    to_in = lambda mm: None if mm is None else round(mm / MM_PER_IN, 2)

    def unstable(rows: list[dict[str, Any]]) -> float | None:
        vals = [r["cape"] for r in rows if r.get("cape") is not None and (r.get("pop") or 0) >= 20]
        return max(vals) if vals else (0.0 if any(r.get("cape") is not None for r in rows) else None)

    return {
        "earlier_today_in": to_in(early), "prior_24h_in": to_in(p24), "prior_48h_in": to_in(p48),
        "window_pop": int(_max(win, "pop")) if _max(win, "pop") is not None else None,
        "window_rain_in": to_in(sum((x.get("mm") or 0) for x in win)) if win else None,
        "thunder_window": any(r.get("thunder") for r in near), "thunder_day": any(r.get("thunder") for r in day),
        "cape_window": unstable(near), "cape_day": unstable(day),
        "gust_mph": _max(win, "gust_mph"), "temp_f": first.get("temp_f"),
        "prev_pop": int(_max(prev, "pop")) if _max(prev, "pop") is not None else None,
        "thunder_prev": any(r.get("thunder") for r in prev), "cape_prev": unstable(prev),
    }


def wet_series(observed: dict[datetime, dict[str, Any]], start: datetime, soil_factor: float = 1.0) -> dict[str, Any] | None:
    """Hourly rain and ET0 (mm) for the WET_DAYS before the start (to 1 h before it), or None if rain is missing."""
    hrs = list(_hours(start - timedelta(days=WET_DAYS), start - timedelta(hours=1)))
    rain = [observed.get(h, {}).get("mm") for h in hrs]
    if not rain or sum(v is None for v in rain) > len(rain) * 0.05:
        return None
    et0 = [observed.get(h, {}).get("et0") for h in hrs]
    sf = soil_factor or 1.0
    return {"rain": [(v or 0.0) / sf for v in rain], "et0": [v or 0.0 for v in et0], "has_et0": any(v is not None for v in et0)}


def wet_totals(series: dict[str, Any] | None) -> dict[str, Any]:
    if not series:
        return {"rain_72h_in": None, "rain_7d_in": None, "et0_72h_in": None, "et0_7d_in": None}
    r, e = series["rain"], series["et0"]
    to_in = lambda mm: round(mm / MM_PER_IN, 2)
    return {"rain_72h_in": to_in(sum(r[-72:])), "rain_7d_in": to_in(sum(r)),
            "et0_72h_in": to_in(sum(e[-72:])) if series["has_et0"] else None, "et0_7d_in": to_in(sum(e)) if series["has_et0"] else None}


def wetness_in(series: dict[str, Any] | None, surface: str, params: dict[str, Any] | None = None) -> float | None:
    """Ground-water bucket at the start, inches. ``surface`` is fast/medium/slow, or "mx" (motocross dry-out curve)."""
    if not series:
        return None
    p = params or PARAMS
    drain = p["wet_drain_mm_h"][surface]
    et_mult = p["wet_et_mult"]
    w = 0.0
    for rain, et in zip(series["rain"], series["et0"]):
        w = min(WET_CAP_MM, max(0.0, w + rain - et_mult * et - drain))
    return round(w / MM_PER_IN, 2)


def add_wetness(feat: dict[str, Any], series: dict[str, Any] | None, drainage: str, ball_sport: bool,
                params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Adds 72 h / 7 d rain, ET0 and the wetness bucket to the features (in place)."""
    feat.update(wet_totals(series))
    feat["wetness_in"] = wetness_in(series, drainage if ball_sport else "mx", params)
    return feat


def effective_drainage(cls: str, maintained: bool, params: dict[str, Any] | None = None) -> str:
    """Surface drainage class, with the older maintained-infield bump when ``maint_soak_factor`` is None."""
    p = params or PARAMS
    if maintained and p.get("maint_soak_factor") is None and cls == "slow":
        return "medium"
    return cls


def soak_index(feat: dict[str, Any], drainage: str, params: dict[str, Any] | None = None,
               maintained: bool = False) -> tuple[float | None, float]:
    """(effective soak inches after drainage and venue maintenance, raw weighted inches)."""
    p = params or PARAMS
    soak, raw = _soak_index(feat, drainage, p)
    f = p.get("maint_soak_factor")
    if soak is not None and maintained and f is not None:
        soak = round(soak * f, 2)
    return soak, raw


def _soak_index(feat: dict[str, Any], drainage: str, p: dict[str, Any]) -> tuple[float | None, float]:
    model = p.get("soak_model")
    if model == "wetness" and feat.get("wetness_in") is not None:
        w = feat["wetness_in"]
        return round(w * p.get("wet_scale", 1.0), 2), w
    if model == "hybrid" and feat.get("wetness_in") is not None:
        p_win = {**p, "soak_model": "window"}
        win_soak, win_raw = _soak_index(feat, drainage, p_win)
        w = feat["wetness_in"]
        return round(max(win_soak or 0.0, w * p.get("wet_scale", 1.0)), 2), max(win_raw, w)
    parts = (feat.get("earlier_today_in"), feat.get("prior_24h_in"), feat.get("prior_48h_in"))
    if all(v is None for v in parts):
        return None, 0.0
    raw = sum(w * (v or 0.0) for w, v in zip(p["soak_weights"], parts))
    return round(raw * p["drainage"][drainage] * p.get("soak_scale", 1.0), 2), round(raw, 2)


def interp(x: float, table) -> float:
    if x <= table[0][0]:
        return float(table[0][1])
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(table[-1][1])


def storm_flags(feat: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, bool]:
    p = params or PARAMS
    cape = p.get("storm_cape")
    win = bool(feat.get("thunder_window")) or (cape is not None and (feat.get("cape_window") or 0) >= cape)
    day = bool(feat.get("thunder_day")) or (cape is not None and (feat.get("cape_day") or 0) >= cape)
    return win, day and not win


def threat_signal(feat: dict[str, Any], params: dict[str, Any] | None = None) -> bool:
    """Any storm or rain threat from the day before through the event (forecast chance, thunder or instability),
    or a start temperature at or below ``cold_f`` (snow/ice threat)."""
    p = params or PARAMS
    cape = p.get("storm_cape") or 1500
    cold = p.get("cold_f")
    if cold is not None and feat.get("temp_f") is not None and feat["temp_f"] <= cold:
        return True
    storm_win, storm_day = storm_flags(feat, p)
    return bool(storm_win or storm_day or feat.get("thunder_prev")
                or (feat.get("window_pop") or 0) >= THREAT_SIGNALS["pop"]
                or (feat.get("prev_pop") or 0) >= THREAT_SIGNALS["prev_pop"]
                or (feat.get("cape_prev") or 0) >= cape)


def base_percent(window_pop: int, drainage: str, params: dict[str, Any] | None = None, tolerant: bool = False) -> int:
    """Unknown-status odds from the window's rain chance (same curve as play_probability)."""
    from rainout_agent.play_probability import unknown_status_play_percent

    p = params or PARAMS
    eff = window_pop * (p["turf_rain_factor"] if drainage == "fast" else 1.0)
    if tolerant and p.get("pro_rain_factor") is not None:
        eff *= p["pro_rain_factor"]
    return unknown_status_play_percent(int(round(window_pop)), False, eff)


def adjust(base: int, feat: dict[str, Any], drainage: str, params: dict[str, Any] | None = None,
           ball_sport: bool = True, maintained: bool = False, soak_memory: float = 1.0, tolerant: bool = False) -> dict[str, Any]:
    """Apply soak, storm, wind, cold and humility rules to a base play probability.

    ``soak_memory`` is the venue's learned drainage memory (rainout_agent.outcomes), a bounded multiplier."""
    p = params or PARAMS
    pct = float(base)
    reasons: list[str] = []
    soak, raw = soak_index(feat, drainage, p, maintained)
    if soak is not None and soak_memory != 1.0:
        soak = round(soak * soak_memory, 2)
    if tolerant and (p.get("pro_rain_factor") is not None or p.get("pro_soak_factor") is not None):
        if soak is not None and p.get("pro_soak_factor") is not None:
            soak = round(soak * p["pro_soak_factor"], 2)
        reasons.append(RAIN_TOLERANT_NOTE)
    soak_pts = round(interp(soak, SOAK_PENALTY)) if soak is not None else 0
    if soak_pts:
        pct -= soak_pts
        surface = {"fast": "turf drains fast", "medium": "grass or a tarped infield drains slowly",
                   "slow": "dirt holds water"}[drainage]
        if maintained and p.get("maint_soak_factor") is not None:
            surface += "; a maintained venue with a tarp or grounds crew, so it counts much less"
        if p.get("soak_model") in ("wetness", "hybrid") and feat.get("wetness_in") is not None:
            et = feat.get("et0_7d_in")
            reasons.append(f"{feat.get('rain_72h_in') or 0:.2f} in of rain in the last 72 h ({feat.get('rain_7d_in') or 0:.2f} in over 7 days"
                           + (f", minus about {et:.2f} in of evaporation" if et is not None else "")
                           + f") leaves the ground wet ({surface}): -{soak_pts} points.")
        else:
            fell = sum(v or 0 for v in (feat.get("earlier_today_in"), feat.get("prior_24h_in"), feat.get("prior_48h_in")))
            reasons.append(f"{fell:.2f} in of rain fell in the 2.5 days before the start ({surface}): -{soak_pts} points.")
    storm_win, storm_day = storm_flags(feat, p)
    if storm_win:
        pct = min(pct - STORM_WINDOW_DROP, STORM_WINDOW_CAP)
        reasons.append(f"Thunderstorms are possible during the event (lightning rules): capped at {STORM_WINDOW_CAP}%.")
    elif storm_day:
        pct -= STORM_DAY_DROP
        reasons.append(f"Storms are possible that day or the evening before: -{STORM_DAY_DROP} points.")
    gust = feat.get("gust_mph")
    if p.get("gust_mph") is not None and gust is not None and gust >= p["gust_mph"]:
        pct = min(pct, CAP_OTHER)
        reasons.append(f"Wind gusts near {round(gust)} mph are forecast: capped at {CAP_OTHER}%.")
    temp = feat.get("temp_f")
    if ball_sport and p.get("cold_f") is not None and temp is not None and temp <= p["cold_f"]:
        pct = min(pct, CAP_OTHER)
        reasons.append(f"About {round(temp)}°F at the start (games are often called for cold): capped at {CAP_OTHER}%.")
    if p.get("humble_cap") is not None:
        signal = (raw >= p.get("humble_soak_in", 0.25) or (feat.get("window_pop") or 0) >= HUMBLE_SIGNALS["window_pop"]
                  or storm_win or storm_day or (gust or 0) >= HUMBLE_SIGNALS["gust_mph"])
        if signal and pct > p["humble_cap"]:
            pct = p["humble_cap"]
            reasons.append(f"Some weather risk is present, so we don't go above {p['humble_cap']}%.")
    threat = None
    for key, applies, why in (("threat_cap", True, "A storm or rain threat is in the forecast for the day before or the day of the event"),
                              ("mx_forecast_cap", not ball_sport, FORECAST_CANCEL_NOTE)):
        cap = p.get(key)
        if cap is None or not applies:
            continue
        threat = threat_signal(feat, p) if threat is None else threat
        if threat and pct > cap:
            pct = cap
            reasons.append(f"{why.rstrip('.')}: not above {cap}%.")
    forecast_note = None
    if not ball_sport:
        threat = threat_signal(feat, p) if threat is None else threat
        if threat:
            forecast_note = FORECAST_CANCEL_NOTE
            if not any(FORECAST_CANCEL_NOTE.rstrip('.') in r for r in reasons):
                reasons.append(FORECAST_CANCEL_NOTE)
    if p.get("turf_floor") is not None and drainage == "fast" and not (storm_win or storm_day) and pct < p["turf_floor"]:
        pct = p["turf_floor"]
        reasons.append(f"Artificial turf stays playable in rain without lightning, so we don't go below {p['turf_floor']}%.")
    for fkey, pkey, why in ALERT_RULES:
        cap = p.get(pkey)
        if cap is not None and feat.get(fkey) and pct > cap:
            pct = cap
            names = ", ".join(feat.get("alert_names") or [])
            reasons.append(f"{why}{f' ({names})' if names else ''}: not above {cap}%.")
    cape_hi = max(feat.get("cape_window") or 0, feat.get("cape_day") or 0)
    if p.get("unstable_cape") is not None and cape_hi >= p["unstable_cape"] and pct > UNSTABLE_CAP:
        pct = UNSTABLE_CAP
        reasons.append(f"{UNSTABLE_NOTE.rstrip('.')}: not above {UNSTABLE_CAP}%.")
    if p.get("cancel_floor") is not None and pct < p["cancel_floor"]:
        pct = p["cancel_floor"]
        reasons.append(f"{CANCEL_FLOOR_NOTE.rstrip('.')}: not below {p['cancel_floor']}%.")
    if p.get("status_ceiling") is not None and pct > p["status_ceiling"]:
        pct = p["status_ceiling"]
        reasons.append(f"{STATUS_CEILING_NOTE.rstrip('.')}: not above {p['status_ceiling']}%.")
    pct = int(max(0, min(100, round(pct))))
    return {"play_probability_percent": pct, "soak_index_in": soak, "soak_raw_in": raw, "soak_penalty": soak_pts,
            "storm_window": storm_win, "storm_day": storm_day, "drainage": drainage, "reasons": reasons,
            "threat_signal": threat, "forecast_cancel_note": forecast_note}


def evaluate(observed: dict[datetime, dict[str, Any]], forecast: dict[datetime, dict[str, Any]], start: datetime,
             duration_h: float, drainage: str, params: dict[str, Any] | None = None, ball_sport: bool = True,
             maintained: bool = False) -> dict[str, Any]:
    """features -> base -> adjust, in one call."""
    drainage = effective_drainage(drainage, maintained, params)
    feat = features(observed, forecast, start, duration_h)
    add_wetness(feat, wet_series(observed, start), drainage, ball_sport, params)
    base = base_percent(feat.get("window_pop") or 0, drainage, params)
    out = adjust(base, feat, drainage, params, ball_sport, maintained)
    return {**out, "base_percent": base, "features": feat}

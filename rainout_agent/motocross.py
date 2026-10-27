"""Motocross race checker: track events, race odds, official notices and rain history.

* ``list_tracks`` / ``resolve_track``: the supported motocross tracks.
* ``track_events``: public events at a track (track site, series calendars, MX
  Parks feed, or a schedule link from the user) with per-day race odds.
* ``check_race``: one answer for "will the race at X on <date> happen?".
* ``track_history``: monthly NOAA rain stats plus documented rain cancellations.

Race odds reuse the play-probability model with motocross settings and add the
thing that actually cancels dirt-track races: a soaked track. Rain from the 72
hours before the race (estimated from Open-Meteo model data) and heavy rain
forecast for race day lower the odds; sandy soil drains faster than clay. See
``docs/motocross.md``.

The API never writes to calendars. ``calendar_event`` is a ready-made Google
Calendar body an agent may use *after the user agrees*.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rainout_agent import mx_sources as mx
from rainout_agent.historical import calibration_for, load_historical_profile
from rainout_agent.play_probability import MONTH_NAMES, calculate_play_probability
from rainout_agent.safe_fetch import safe_get

BASE_URL = "https://rainout-agent-source.vercel.app"
FORECAST_DAYS = 16          # Open-Meteo horizon; NWS covers about 7 days
DEFAULT_RANGE_DAYS = 90
MAX_RANGE_DAYS = 400
NOTICE_LOOKAHEAD_DAYS = 10

# Effective-rain multiplier: how much rain a track's dirt takes before it is
# unrideable, relative to average loam. Sand drains fast, clay holds water.
SOIL_FACTORS = {"sand": 1.4, "sand_assumed": 1.25, "loam_sand": 1.15, "loam": 1.0, "clay": 0.8, "unknown": 1.0}
SOIL_WORDS = {"sand": "sandy soil that drains fast", "sand_assumed": "sandy Florida soil", "loam_sand": "loam and sand",
              "loam": "loam", "clay": "clay that holds water", "unknown": "soil of unknown type"}
# Prior rain weights: last 24 h counts fully, the day before 60%, the day before that 35%.
SOAK_WEIGHTS = (1.0, 0.6, 0.35)
# (effective inches, odds points off) breakpoints, linearly interpolated.
SOAK_PENALTY = ((0.5, 0), (1.0, 8), (2.0, 25), (3.0, 45), (5.0, 65))
RACE_DAY_PENALTY = ((0.25, 0), (0.5, 5), (1.0, 15), (2.0, 35), (3.0, 45))
CLIMATE_BASELINE = 96
CLIMATE_CANCEL_SHARE = 0.75  # share of heavy-rain race days/eves that actually cancel
MIN_ODDS, MAX_ODDS = 2, 96
PRACTICE_RE = re.compile(r"\b(practice|school|camp|lessons?|test.?n.?tune|open ride|ride day|e-?bike night|kids night)\b", re.I)
RACE_WORDS_RE = re.compile(r"\b(race|racing|round|rd\.?|series|championship|classic|qualifier|national|moto)\b", re.I)
SERIES_KEYWORDS = (
    (r"lone star", "Lone Star Day & Night Series"),
    (r"texas winter series|\btws\b", "FMF Texas Winter Series"),
    (r"texas night series|\btns\b", "FMF Texas Night Series"),
    (r"\bnma\b|texas state (motocross )?championship", "NMA Texas State Championship"),
    (r"ama texas state", "AMA Texas State Championship Series"),
    (r"\bi-?4 ?mx\b|\bi-4\b", "I4MX Series"),
    (r"florida motocross series|florida series", "Florida Motocross Series"),
    (r"gold ?cup", "Florida Gold Cup Series"),
    (r"fl(orida)? state championship", "Florida State Championship"),
    (r"oklahoma fall mx", "Oklahoma Fall MX Series"),
    (r"loretta|llaq|area qualifier|regional qualifier", "Loretta Lynn's qualifier"),
    (r"saturday night championship|dcmx", "Dade City MX Points Championship"),
    (r"saturday night lights", "3 Palms Saturday Night Lights"),
    (r"usa mx championships|usa motocross championships", "USA Motocross Championships"),
    (r"great american team race|gatr", "Great American Team Race"),
    (r"spring championship|js7", "JS7 Spring Championship"),
    (r"winter-?am", "Florida Winter-AM Series"),
)
SOURCE_PRIORITY = {"user_url": 0, "track_site": 0, "series": 1, "mxparks": 2}


class TracksError(ValueError):
    """Bad request input (400)."""


class UnknownTrack(LookupError):
    """No supported track matches."""


# ------------------------------------------------------------------ tracks
def _field(track: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.status_api import load_field

    return load_field(track["field_id"])


def _tz(track: dict[str, Any]) -> ZoneInfo:
    from rainout_agent.schedule import venue_timezone

    return venue_timezone(_field(track))


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("&", " and ").replace("'", "")).strip()


def _match_score(track: dict[str, Any], field: dict[str, Any], query: str) -> float:
    q = _norm(query)
    if not q:
        return 0.0
    names = [track["name"], track["short_id"], track["field_id"], *track.get("aliases", []), field.get("city", "")]
    best = 0.0
    for name in names:
        n = _norm(name)
        if not n:
            continue
        if q == n:
            return 1.0
        if re.search(rf"\b{re.escape(n)}\b", q) and len(n) >= 4:
            best = max(best, 0.9)
        elif re.search(rf"\b{re.escape(q)}\b", n) and len(q) >= 4:
            best = max(best, 0.85)
        best = max(best, SequenceMatcher(None, q, n).ratio() * 0.9)
    return round(best, 2)


def tracks() -> list[dict[str, Any]]:
    return mx.load_config()["tracks"]


OSM_TRACK_NOTE = ("From OpenStreetMap only: the name and location are community data, no event calendar or official "
                  "status source is configured, and no rain cancellations have been researched for this track.")


def osm_track(v: dict[str, Any]) -> dict[str, Any]:
    """A track record for an OpenStreetMap motocross venue (no event sources, soil unknown)."""
    return {"field_id": v["id"], "short_id": v["id"], "name": v["name"], "aliases": [v["name"]], "soil": "unknown",
            "event_sources": [], "series_sources": [], "unparsed_sources": [], "social": {}, "history": [],
            "verification_level": "osm_only", "history_coverage": OSM_TRACK_NOTE}


def _osm_track_matches(q: str, state: str | None = None) -> list[tuple[float, dict[str, Any]]]:
    from rainout_agent import osm_venues

    out = []
    for v in osm_venues.motocross_tracks():
        if state and v["state"] != state:
            continue
        s = osm_venues.name_score(v, q) if q else 0.0
        if q and s < 0.6:
            continue
        out.append((s, v))
    out.sort(key=lambda x: (-x[0], x[1]["name"]))
    return out


def resolve_track(track_id: str | None) -> dict[str, Any]:
    from rainout_agent import osm_venues

    tid = (track_id or "").strip()
    for t in tracks():
        if tid in (t["field_id"], t["short_id"]):
            return t
    if osm_venues.is_osm_id(tid):
        v = osm_venues.get(tid)
        if v and ("motocross" in v["sports"] or v["kind"] == "motocross_track"):
            return osm_track(v)
        raise UnknownTrack(tid)
    scored = sorted(((_match_score(t, _field(t), tid), t) for t in tracks()), key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.8:
        return scored[0][1]
    osm = _osm_track_matches(tid)
    # Only take an OSM track by name when it is a clear, unique match.
    if osm and osm[0][0] >= 0.9 and (len(osm) == 1 or osm[1][0] < osm[0][0]):
        return osm_track(osm[0][1])
    raise UnknownTrack(tid)


def public_track(track: dict[str, Any], confidence: float | None = None) -> dict[str, Any]:
    field = _field(track)
    out = {
        "track_id": track["field_id"],
        "short_id": track["short_id"],
        "name": track["name"],
        "city": field.get("city"), "state": field.get("state"), "address": field.get("address"),
        "coordinates": field.get("coordinates"),
        "timezone": str(_tz(track)),
        "website": track.get("website"),
        "phone": field.get("rainout_phone"),
        "soil": track.get("soil", "unknown"),
        "social": track.get("social", {}),
        "event_sources": [{"name": s["name"], "url": s.get("page_url") or s["url"]} for s in track.get("event_sources", [])]
                         + [{"name": mx.load_config()["series"][sid]["name"], "url": mx.load_config()["series"][sid]["url"]}
                            for sid in track.get("series_sources", [])],
        "unreadable_sources": track.get("unparsed_sources", []),
        "verification_level": track.get("verification_level", "confirmed"),
        "links": {
            "events": f"{BASE_URL}/v1/tracks/{track['field_id']}/events",
            "check": f"{BASE_URL}/v1/tracks/{track['field_id']}/check?date=YYYY-MM-DD",
            "history": f"{BASE_URL}/v1/tracks/{track['field_id']}/history",
            "calendar_ics": f"{BASE_URL}/v1/tracks/{track['field_id']}/calendar.ics",
        },
    }
    if track.get("verification_level") == "osm_only":
        from rainout_agent import osm_venues

        out["verification_note"] = OSM_TRACK_NOTE
        out["osm_elements"] = field.get("osm", {}).get("elements", [])
        out["attribution"] = osm_venues.ATTRIBUTION
    if confidence is not None:
        out["confidence"] = confidence
    return out


def list_tracks(q: str | None = None, state: str | None = None, verification: str | None = None,
                limit: int | str | None = None, offset: int | str | None = None) -> dict[str, Any]:
    """Supported tracks. Confirmed tracks first; OpenStreetMap tracks (osm_only) are added when
    searching (q), filtering by state, or asking for verification=all|osm_only."""
    q = (q or "").strip()
    if len(q) > 120:
        raise TracksError("q is too long.")
    st = (state or "").strip().upper() or None
    level = (verification or ("all" if (q or st) else "confirmed")).strip().lower()
    if level not in ("confirmed", "osm_only", "all"):
        raise TracksError("verification must be confirmed, osm_only or all.")
    try:
        lim = int(limit) if limit not in (None, "") else 100
        off = int(offset) if offset not in (None, "") else 0
    except ValueError as exc:
        raise TracksError("limit and offset must be numbers.") from exc
    if not 1 <= lim <= 500 or off < 0:
        raise TracksError("limit must be 1-500 and offset >= 0.")
    rows = []
    for t in tracks():
        field = _field(t)
        if st and field.get("state") != st:
            continue
        score = _match_score(t, field, q) if q else None
        if q and score < 0.6:
            continue
        if level != "osm_only":
            rows.append(((score if score is not None else 0) + 0.05, public_track(t, score)))
    n_confirmed = len(rows)
    osm_rows = _osm_track_matches(q, st) if level != "confirmed" else []
    confirmed_names = {_norm(r[1]["name"]) for r in rows}
    for s_, v in osm_rows:
        if _norm(v["name"]) in confirmed_names:
            continue
        rows.append((s_, {"_osm": v, "name": v["name"], "confidence": s_ if q else None}))
    rows.sort(key=lambda r: (-r[0], r[1]["name"]))
    page = [public_track(osm_track(r[1]["_osm"]), r[1]["confidence"]) if "_osm" in r[1] else r[1] for r in rows[off:off + lim]]
    result = {"query": q or None, "state": st, "count": len(page), "total": len(rows), "offset": off, "limit": lim,
              "verification": level, "counts": {"confirmed": n_confirmed, "osm_only": len(rows) - n_confirmed},
              "tracks": page, "as_of": datetime.now(timezone.utc).isoformat()}
    if off + lim < len(rows):
        result["next_offset"] = off + lim
    if level != "confirmed":
        from rainout_agent import osm_venues

        result["attribution"] = osm_venues.ATTRIBUTION
    elif not q:
        n_osm = len(_osm_track_matches("", st))
        if n_osm:
            result["osm_tracks_available"] = n_osm
            result["note"] = (f"Showing the {n_confirmed} confirmed tracks only (event calendars and researched history). "
                              f"Add state=, q= or verification=all to include {n_osm} OpenStreetMap motocross tracks "
                              "(verification_level osm_only, weather-only odds).")
    if q and not rows:
        result["next_step"] = "ask_user_for_events_url"
        result["message"] = ("That track isn't one of the supported motocross tracks. Supported tracks are listed at /v1/tracks; "
                             "for other venues, ask the user for the track's event calendar link.")
    elif q:
        top = page[0] if page else {}
        result["next_step"] = "confirm_with_user" if (top.get("confidence") or 0) < 1 or top.get("verification_level") == "osm_only" else "use_track"
    return result


# ----------------------------------------------------------------- events
def _src_kind(raw: dict[str, Any]) -> str:
    sid = raw["source"]["id"]
    if sid in ("mxparks", "series", "user_url"):
        return sid
    return "track_site"


def _kind(name: str) -> str:
    if PRACTICE_RE.search(name) and not re.search(r"\b(race|racing|round|rd\.?|series|championship|qualifier)\b", name, re.I):
        return "practice"
    return "race"


def _series(names: list[str]) -> str | None:
    blob = " ".join(names).lower()
    for pattern, label in SERIES_KEYWORDS:
        if re.search(pattern, blob):
            return label
    return None


def _shouting(name: str) -> bool:
    letters = [c for c in name if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.45


ACRONYMS = {"AMA", "NMA", "FMF", "USA", "GNCC", "MXGP", "TSC", "ATV", "UTV", "MX", "DCMX", "OMC", "GATR", "KTM", "YZ", "LL", "AMSOIL", "TX", "FL", "OK", "II", "III", "IV"}
CASE_WORDS = {"THE", "AND", "OF", "AT", "FOR"}


def _soften(w: str, first: bool) -> str:
    core = re.sub(r"[^A-Za-z]", "", w)
    if not (core.isupper() and core not in ACRONYMS and (len(core) >= 4 or core in CASE_WORDS)):
        return w
    i = w.index(core[0])
    w = w[:i] + core.capitalize() + w[i + len(core):] if w.count(core) == 1 else w.capitalize()
    return w.lower() if core in CASE_WORDS and not first else w


def _clean_name(name: str) -> str:
    """Tidy whitespace and soften ALL-CAPS words, keeping known acronyms."""
    name = re.sub(r"\s+", " ", name).strip(" -|:")
    words = ["-".join(_soften(p, n == 0 and k == 0) for k, p in enumerate(w.split("-"))) for n, w in enumerate(name.split())]
    return re.sub(r"^\d{4}\s+(?=[A-Za-z])", "", " ".join(words).replace("PRO-AM", "Pro-Am"))


def merge_events(raws: list[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Group listings of the same event from different sources (overlapping dates, same kind)."""
    groups: list[dict[str, Any]] = []
    for raw in sorted(raws, key=lambda r: r["start"]):
        kind = _kind(raw["name"])
        s, e = raw["start"].astimezone(tz).date(), raw["end"].astimezone(tz).date()
        for g in groups:
            if g["kind"] == kind and s <= g["end_date"] and e >= g["start_date"]:
                g["parts"].append(raw)
                g["start_date"], g["end_date"] = min(g["start_date"], s), max(g["end_date"], e)
                break
        else:
            groups.append({"kind": kind, "start_date": s, "end_date": e, "parts": [raw]})
    return groups


def _group_to_event(track: dict[str, Any], g: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    parts = g["parts"]
    def covers(r: dict[str, Any]) -> bool:
        return r["start"].astimezone(tz).date() <= g["start_date"] and r["end"].astimezone(tz).date() >= g["end_date"]

    best = sorted(parts, key=lambda r: (_shouting(r["name"]), not covers(r), SOURCE_PRIORITY[_src_kind(r)], -len(r["name"])))[0]
    timed = [p for p in parts if not p["time_estimated"]]
    start = min((p["start"] for p in timed), default=min(p["start"] for p in parts)).astimezone(tz)
    end = max((p["end"] for p in timed), default=max(p["end"] for p in parts)).astimezone(tz)
    if end.date() < g["end_date"]:
        end = datetime.combine(g["end_date"], end.time(), tz)
    if start.date() > g["start_date"]:
        start = datetime.combine(g["start_date"], start.time(), tz)
    names = [p["name"] for p in parts]
    sources, seen = [], set()
    for p in sorted(parts, key=lambda r: SOURCE_PRIORITY[_src_kind(r)]):
        key = (p["source"]["name"], p["source"].get("event_url") or p["source"]["url"])
        if key in seen:
            continue
        seen.add(key)
        sources.append({"name": p["source"]["name"], "url": p["source"]["url"], "event_url": p["source"].get("event_url"),
                        "listed_as": _clean_name(p["name"]), "type": _src_kind(p)})
    day_labels = {}
    for p in timed:
        if p["start"].astimezone(tz).date() == p["end"].astimezone(tz).date():
            day_labels[p["start"].astimezone(tz).date()] = (_clean_name(p["name"]), p["start"].astimezone(tz), p["end"].astimezone(tz))
    status_texts = [p.get("status_text") or "" for p in parts] + names
    return {
        "name": _clean_name(best["name"]),
        "kind": g["kind"],
        "series": _series(names),
        "start": start, "end": end,
        "time_estimated": not timed,
        "time_note": None if timed else best.get("time_note"),
        "sources": sources,
        "day_labels": day_labels,
        "listing_cancelled": any(p.get("status_text") == "cancelled" or re.search(r"\bcancel+ed\b", p["name"], re.I) for p in parts),
        "rescheduled_to_this_date": any(re.search(r"\breschedul", t, re.I) for t in status_texts),
    }


def _event_id(track: dict[str, Any], ev: dict[str, Any], used: set[str]) -> str:
    base = f"{track['short_id']}-{ev['start'].strftime('%Y%m%d')}-{ev['kind'][0]}"
    eid = base
    if eid in used:
        eid = f"{base}-{mx.stable_id(ev['name'])[:6]}"
    used.add(eid)
    return eid


def gather_events(track: dict[str, Any], *, today: date, events_url: str | None = None,
                  fetcher: Callable[[str], Any] = safe_get) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every configured source (and the user's link). Returns (events, sources_checked)."""
    tz = _tz(track)
    raws: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    cfg = mx.load_config()
    plan = [dict(s) for s in track.get("event_sources", [])] + [dict(cfg["series"][sid], id="series") for sid in track.get("series_sources", [])]
    for src in plan:
        try:
            found = mx.read_source(track, src, tz, today)
            raws.extend(found)
            checked.append({"name": src["name"], "url": src.get("page_url") or src["url"], "ok": True, "events_found": len(found)})
        except Exception as exc:  # one bad source never sinks the answer
            checked.append({"name": src["name"], "url": src.get("page_url") or src["url"], "ok": False, "error": str(exc)[:160]})
    if events_url:
        try:
            found = mx.events_from_user_url(events_url, track, tz, today, fetcher)
            raws.extend(found)
            checked.append({"name": "Schedule link you provided", "url": events_url, "ok": True, "events_found": len(found)})
        except Exception as exc:
            checked.append({"name": "Schedule link you provided", "url": events_url, "ok": False, "error": str(exc)[:160]})
    for u in track.get("unparsed_sources", []):
        checked.append({"name": u["name"], "url": u["url"], "ok": False, "error": u["why"], "readable": False})
    used: set[str] = set()
    events = []
    for g in merge_events(raws, tz):
        ev = _group_to_event(track, g, tz)
        ev["event_id"] = _event_id(track, ev, used)
        events.append(ev)
    return events, checked


# ----------------------------------------------------------------- weather
def get_amounts(field: dict[str, Any]) -> dict[str, Any] | None:
    """Hourly rain amounts (past 3 days + 16 days) from the shared rain module."""
    from rainout_agent import rain

    return rain.get_amounts(field["coordinates"]["lat"], field["coordinates"]["lon"])


def get_hourly(field: dict[str, Any]) -> dict[str, Any] | None:
    from rainout_agent.weather import get_hourly_forecast

    try:
        return get_hourly_forecast(field, timeout=6.0)
    except Exception:
        return None


def _window_stats(day: date, ws: datetime, we: datetime, hourly: dict[str, Any] | None, amounts: dict[str, Any] | None,
                  now: datetime | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"rain_chance_percent": None, "thunderstorm_likely": False, "race_window_rain_in": None,
                           "prior_rain_in": None, "prior_rain_by_day_in": None, "pop_source": None}
    if hourly and hourly.get("periods"):
        pops, thunder, covered = [], False, False
        for p in hourly["periods"]:
            try:
                ps = datetime.fromisoformat(p["startTime"])
                pe = datetime.fromisoformat(p.get("endTime") or p["startTime"]) if p.get("endTime") else ps + timedelta(hours=1)
            except (KeyError, ValueError):
                continue
            if pe <= ws or ps >= we:
                continue
            covered = True
            pops.append((p.get("probabilityOfPrecipitation") or {}).get("value") or 0)
            thunder = thunder or "thunder" in (p.get("shortForecast") or "").lower()
        if covered:
            out.update(rain_chance_percent=int(round(max(pops))) if pops else 0, thunderstorm_likely=thunder, pop_source=hourly.get("source"))
    if amounts and amounts.get("rows"):
        rows = [(datetime.fromisoformat(r["time"]), r) for r in amounts["rows"]]
        first, last = rows[0][0], rows[-1][0]

        def total(a: datetime, b: datetime) -> float | None:
            if a < first - timedelta(hours=1) or b > last + timedelta(hours=1):
                return None
            return round(sum((r["precip_in"] or 0) for t, r in rows if a <= t < b), 2)

        out["race_window_rain_in"] = total(ws, we)
        by_day = [total(ws - timedelta(hours=24 * (i + 1)), ws - timedelta(hours=24 * i)) for i in range(3)]
        if all(v is not None for v in by_day):
            out["prior_rain_by_day_in"] = by_day
            out["prior_rain_in"] = round(sum(by_day[:2]), 2)
            out["soak_index_in"] = round(sum(w * v for w, v in zip(SOAK_WEIGHTS, by_day)), 2)
        if out["rain_chance_percent"] is None:
            win = [r for t, r in rows if ws <= t < we]
            if win and any(r["pop"] is not None for r in win):
                out["rain_chance_percent"] = int(max(r["pop"] or 0 for r in win))
                # Model thunder codes are noisy: count them only inside a week, when at least
                # two race hours show thunder and the rain chance is meaningful.
                storm_hours = sum(1 for r in win if (r["code"] or 0) >= 95)
                out["thunderstorm_likely"] = bool(storm_hours >= 2 and out["rain_chance_percent"] >= 40 and (ws - (now or datetime.now(timezone.utc))).days <= 7)
                out["pop_source"] = "Open-Meteo"
    return out


def _interp(x: float, table: tuple[tuple[float, float], ...]) -> float:
    if x <= table[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(table[-1][1])


def race_odds(official_status: str, rain_chance: int, thunderstorm: bool, hours_until: float,
              calibration: dict[str, Any] | None, soak_in: float | None, race_rain_in: float | None, soil: str) -> dict[str, Any]:
    """Motocross race odds for one race day (0-100)."""
    base = calculate_play_probability(official_status, rain_chance, thunderstorm, hours_until, calibration)
    status = (official_status or "unknown").lower()
    factor = SOIL_FACTORS.get(soil, 1.0)
    soak_pen = _interp((soak_in or 0) / factor, SOAK_PENALTY) if soak_in is not None else 0.0
    day_pen = _interp((race_rain_in or 0) / factor, RACE_DAY_PENALTY) if race_rain_in is not None else 0.0
    odds = base["play_probability_percent"]
    if status in {"cancelled", "canceled", "closed", "field closed", "field_closed"}:
        odds = 0
    elif status in {"on", "open", "playing"}:
        odds = max(0, min(98, odds - 0.5 * day_pen))
    else:
        odds = odds - soak_pen - day_pen
        cap = 50 if status in {"delayed", "postponed", "uncertain", "conflicting"} else MAX_ODDS
        odds = max(MIN_ODDS, min(cap, odds))
    return {"play_probability": int(round(odds)), "base_percent": base["play_probability_percent"],
            "soaked_track_penalty": round(soak_pen, 1), "race_day_rain_penalty": round(day_pen, 1), "model_reason": base["reason"]}


def climate_odds(track: dict[str, Any], day: date) -> dict[str, Any] | None:
    cal = calibration_for(track["field_id"], datetime.combine(day, dtime(12)))
    if not cal or cal.get("rainout_day_frac") is None:
        return None
    factor = SOIL_FACTORS.get(track.get("soil", "unknown"), 1.0)
    p = min(1.0, float(cal["rainout_day_frac"]) / factor)
    p2 = 1 - (1 - p) ** 2
    odds = int(round(CLIMATE_BASELINE * (1 - CLIMATE_CANCEL_SHARE * p2)))
    return {"play_probability": odds, "heavy_rain_day_percent": round(100 * float(cal["rainout_day_frac"]), 1),
            "month": MONTH_NAMES[day.month - 1], "station": cal.get("station_name"), "threshold_in": cal.get("rain_to_cancel_threshold_in")}


# ----------------------------------------------------------------- status
def _event_tokens(ev: dict[str, Any]) -> set[str]:
    stop = {"the", "and", "series", "round", "race", "night", "day", "of", "at", "mx", "rd", "annual", "texas", "a", "for", "championship"}
    toks = set()
    for s in [ev["name"]] + [x["listed_as"] for x in ev["sources"]]:
        toks |= {w for w in _norm(s).split() if w not in stop and not w.isdigit() and len(w) > 2}
    return toks


def official_status(track: dict[str, Any], ev: dict[str, Any], notices: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Decide the official status of one event from listings and recent notices.

    Conservative: a notice only counts as "cancelled" when it is recent, clearly
    about racing, and points at this event's dates. Anything vaguer makes the
    status "uncertain" (odds capped at 50%) with the notice quoted.
    """
    evidence = []
    if ev["listing_cancelled"]:
        return {"status": "cancelled", "evidence": [{"source": s["name"], "url": s["url"], "text": s["listed_as"]} for s in ev["sources"]]}
    status = "unknown"
    toks = _event_tokens(ev)
    practice_event = bool(PRACTICE_RE.search(ev["name"] or ""))
    first_day, last_day = _days(ev)[0], _days(ev)[-1]
    for n in notices:
        text = f"{n.get('title', '')} {n.get('text', '')}".strip()
        if not mx.NOTICE_RE.search(text) and not mx.GOOD_RE.search(text):
            continue
        posted = n.get("posted")
        if posted and not (ev["start"] - timedelta(days=7) <= posted <= ev["end"] + timedelta(hours=12)):
            continue
        if posted is None and n.get("source_name") and "home page" not in n["source_name"].lower() and "schedule" not in n["source_name"].lower():
            continue
        when = notice_dates(n.get("title", ""), n.get("text", ""), posted, ev)
        if when == "other":
            continue  # about a different date (a make-up date or a different weekend never counts as this one)
        if _other_track_or_round(text, track, ev):
            continue
        practice_only = (bool(re.search(r"\bpractice\b", text, re.I)) and not re.search(r"\b(race|racing|round|moto|event)\b", text, re.I)
                         and not practice_event)
        about_event = bool(toks & set(_norm(text).split())) or bool(re.search(r"\b(race|racing|event|this weekend|tonight|tomorrow)\b", text, re.I))
        if practice_only or not about_event:
            continue
        # Undated post: it can still be about this event when it was posted on race day or up to two days before
        # and names the event (series, round) - e.g. "Round 6 of the Texas Night Series is canceled due to the weather".
        implied = (when == "none" and posted is not None and first_day - timedelta(days=2) <= _posted_day(posted) <= last_day
                   and bool(toks & set(_norm(text).split())))
        dated = when == "match" or implied
        hedged = bool(re.search(r"\b(may|might|could|possibly)\s+(?:be\s+|get\s+)?(?:cancel|postpon|rain)", text, re.I))
        item = {"source": n.get("source_name"), "url": n.get("source_url"), "text": text[:300],
                "posted": posted.isoformat() if posted else None}
        if mx.GOOD_RE.search(text) and not re.search(r"cancel|postpon|rained out", text, re.I):
            evidence.append(item)
            status = "on" if status == "unknown" else status
        elif re.search(r"cancel+ed|canceled|canceling|cancelling|rained out|no racing", text, re.I) and dated and posted and not hedged:
            evidence.append(item)
            status = "cancelled"
        elif re.search(r"postpon", text, re.I) and dated and not hedged:
            evidence.append(item)
            status = "postponed"
        else:
            evidence.append(item)
            status = "uncertain" if status in ("unknown", "on") else status
    return {"status": status, "evidence": evidence}


_MAKEUP_RE = re.compile(r"\b(reschedul\w*|re-scheduled|make[- ]?up|rain date|moved? (?:on )?to|next round|next race|next weekend|"
                        r"double points|new date|instead)\b", re.I)
_DATELINE_AFTER_RE = re.compile(r"^,?\s*(?:20\d\d)?\s*\([^)]{2,60}\)\s*[-\u2013\u2014]")
_TONIGHT_RE = re.compile(r"\b(tonight\W?s?|today\W?s?|this evening)\b", re.I)


def _posted_day(posted: datetime) -> date:
    # WordPress "date" is the site's local wall time (notices_from_wp tags it UTC), so use the wall date as-is.
    return posted.date()


def _date_mentions(text: str, year: int) -> list[tuple[int, int, set[date]]]:
    """(start, end, days) for every date phrase in ``text``."""
    out = []
    for m in mx.DATE_RE.finditer(text):
        found = mx.find_date_range(m.group(0))
        if not found:
            continue
        y = found.get("year") or year
        span = mx._make_dates(found, y)
        days = set()
        if span:
            d = span[0]
            while d <= span[1]:
                days.add(d)
                d += timedelta(days=1)
        else:
            for mon, day in mx._tokens(m.group("body")):
                try:
                    days.add(date(y, mon or found["start"][0], day))
                except ValueError:
                    pass
        out.append((m.start(), m.end(), days))
    for m in mx.NUMERIC_RE.finditer(text):
        mo, d1 = int(m.group("m")), int(m.group("d"))
        d2 = int(m.group("d2")) if m.group("d2") else d1
        y = int(m.group("year")) if m.group("year") else year
        try:
            days = {date(y, mo, d) for d in range(d1, d2 + 1)} if 1 <= mo <= 12 and d2 - d1 <= 10 else set()
        except ValueError:
            days = set()
        if days:
            out.append((m.start(), m.end(), days))
    return out


def notice_dates(title: str, body: str, posted: datetime | None, ev: dict[str, Any]) -> str:
    """Does a notice point at this event's days? Returns "match", "other" or "none" (no usable date).

    Every date in the post is checked, not just the first:
    * a press dateline ("August 18, 2012 (Wortham, Texas) -") is the posting date; it only counts as the event
      date when the post says tonight/today ("Tonight's AllStar Amateurs has been rained out ...");
    * dates after "rescheduled for", "make-up", "rain date", "next round" ... are make-up or other events and are
      ignored, so "rescheduled for Sunday, December 2nd" never hides (or creates) a cancellation;
    * tonight/today/tomorrow/this weekend are resolved against the post date.
    """
    ev_days = set(_days(ev))
    year = ev["start"].year
    hits: set[date] = set()
    content_dates = False
    tonight = bool(_TONIGHT_RE.search(f"{title} {body}"))
    for part in (title, body):
        for s, e, days in _date_mentions(part, year):
            if _DATELINE_AFTER_RE.match(part[e:]):
                if tonight:
                    hits |= days
                continue
            sentence = re.split(r"[.!?]\s", part[max(0, s - 80):s])[-1]
            if _MAKEUP_RE.search(sentence):
                continue
            content_dates = True
            hits |= days
    if posted is not None:
        pd = _posted_day(posted)
        text = f"{title} {body}"
        rel: set[date] = set()
        if _TONIGHT_RE.search(text):
            rel.add(pd)
        if re.search(r"\btomorrow\b", text, re.I):
            rel.add(pd + timedelta(days=1))
        if re.search(r"\bthis weekend\b", text, re.I):
            rel |= {pd} | {pd + timedelta(days=i) for i in range(0, 7 - pd.weekday()) if (pd + timedelta(days=i)).weekday() >= 4}
        if rel and not content_dates:
            hits |= rel
            content_dates = True
    if hits & ev_days:
        return "match"
    return "other" if content_dates else "none"


def _other_track_or_round(text: str, track: dict[str, Any], ev: dict[str, Any]) -> bool:
    """True when the notice names a different track (and not this one) or a different round number."""
    low = _norm(text.replace("\u2019", "'"))
    def named(t: dict[str, Any]) -> bool:
        names = {t.get("name", "")} | set(t.get("aliases") or [])
        return any(len(_norm(a)) > 3 and re.search(rf"\b{re.escape(_norm(a))}\b", low) for a in names if a)
    if not named(track):
        try:
            if any(named(t) for t in tracks() if t.get("short_id") != track.get("short_id")):
                return True
        except Exception:  # pragma: no cover - track list unavailable
            pass
    rnd = lambda s: {int(x) for x in re.findall(r"\b(?:rd|rnd|round)\.?\s*(\d{1,2})\b", s or "", re.I)}
    ev_r, n_r = rnd(ev.get("name")), rnd(text)
    return bool(ev_r and n_r and not (ev_r & n_r))


def _days(ev: dict[str, Any]) -> list[date]:
    d, out = ev["start"].date(), []
    while d <= ev["end"].date() and len(out) < 12:
        out.append(d)
        d += timedelta(days=1)
    return out


def _day_window(ev: dict[str, Any], day: date, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    label = ev["day_labels"].get(day)
    if label:
        return label[1], label[2], label[0]
    days = _days(ev)
    if len(days) == 1:
        return ev["start"], ev["end"], ev["name"]
    if day == days[0]:
        return ev["start"], max(ev["start"] + timedelta(hours=2), datetime.combine(day, dtime(23), tz)), "first day"
    if day == days[-1]:
        return datetime.combine(day, dtime(7), tz), max(datetime.combine(day, dtime(8), tz), ev["end"]), "last day"
    return datetime.combine(day, dtime(8), tz), datetime.combine(day, dtime(23), tz), "middle day"


def _role(ev: dict[str, Any], day: date) -> str:
    """'race' or 'arrival_practice' (the Thursday/Friday opener of a 3+ day weekend)."""
    days = _days(ev)
    label = ev["day_labels"].get(day)
    if label:
        name = label[0]
        if re.search(r"\brace\b", name, re.I) or not PRACTICE_RE.search(name):
            return "race"
        return "arrival_practice"
    if len(days) >= 3 and day == days[0] and day.weekday() in (3, 4):
        return "arrival_practice"
    return "race"


# ------------------------------------------------------------------ scoring
def score_event(track: dict[str, Any], ev: dict[str, Any], status: dict[str, Any], now: datetime,
                hourly: dict[str, Any] | None, amounts: dict[str, Any] | None) -> list[dict[str, Any]]:
    tz = _tz(track)
    soil = track.get("soil", "unknown")
    out = []
    for day in _days(ev):
        ws, we, label = _day_window(ev, day, tz)
        entry: dict[str, Any] = {"date": day.isoformat(), "label": label, "role": _role(ev, day),
                                 "window_start": ws.isoformat(), "window_end": we.isoformat()}
        if we < now:
            entry.update(play_probability=None, basis="past")
            out.append(entry)
            continue
        if status["status"] in ("cancelled",):
            entry.update(play_probability=0, basis="official")
            out.append(entry)
            continue
        days_out = (day - now.astimezone(tz).date()).days
        stats = _window_stats(day, ws, we, hourly, amounts, now) if days_out <= FORECAST_DAYS else None
        if stats and stats["rain_chance_percent"] is not None:
            cal = calibration_for(track["field_id"], ws)
            hours = max(0.0, (ws - now).total_seconds() / 3600)
            odds = race_odds(status["status"], stats["rain_chance_percent"], stats["thunderstorm_likely"], hours, cal,
                             stats.get("soak_index_in"), stats.get("race_window_rain_in"), soil)
            entry.update(play_probability=odds["play_probability"], basis="forecast", weather={
                "rain_chance_percent": stats["rain_chance_percent"], "thunderstorm_likely": stats["thunderstorm_likely"],
                "race_window_rain_in": stats.get("race_window_rain_in"), "prior_48h_rain_in": stats.get("prior_rain_in"),
                "prior_rain_by_day_in": stats.get("prior_rain_by_day_in"), "soak_index_in": stats.get("soak_index_in"),
                "rain_chance_source": stats.get("pop_source"), "rain_amount_source": (amounts or {}).get("source"),
            }, model={k: v for k, v in odds.items() if k != "play_probability"})
            if status["status"] == "unknown":
                _apply_event_risk(track, entry, ws, we, stats, soil)
        else:
            clim = climate_odds(track, day)
            if clim:
                odds_val = clim["play_probability"]
                if status["status"] in ("uncertain", "postponed"):
                    odds_val = min(50, odds_val)
                entry.update(play_probability=odds_val, basis="climatology", climatology=clim)
            else:
                entry.update(play_probability=None, basis="unavailable")
        out.append(entry)
    return out


def _apply_event_risk(track: dict[str, Any], entry: dict[str, Any], ws: datetime, we: datetime, stats: dict[str, Any],
                      soil: str) -> None:
    """v2 whole-event model for a race day with nothing official posted; keeps v1 if the weather fetch fails."""
    from rainout_agent import event_weather

    entry["model"]["version"] = "v1"
    try:
        loc = _field(track)["coordinates"]
        v2 = event_weather.evaluate(loc["lat"], loc["lon"], ws, max(1.0, (we - ws).total_seconds() / 3600), "slow",
                                    thunder_forecast=bool(stats.get("thunderstorm_likely")), ball_sport=False,
                                    soil_factor=SOIL_FACTORS.get(soil, 1.0))
    except Exception:  # pragma: no cover - never break a race answer
        v2 = None
    if v2 is None:
        entry["model"]["note"] = "event weather (Open-Meteo) unavailable or disabled; v1 race model used"
        return
    entry["model"].update(version="v2", v1_play_probability=entry["play_probability"], reasons=v2["reasons"],
                          soak_penalty=v2["soak_penalty"], storm_window=v2["storm_window"], features=v2["features"])
    entry["play_probability"] = max(MIN_ODDS, min(MAX_ODDS, v2["play_probability_percent"]))


def _spoken_day(day: date, now: datetime, tz: ZoneInfo) -> str:
    delta = (day - now.astimezone(tz).date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return day.strftime("%A")
    return f"{day.strftime('%a %b')} {day.day}"


def _inches(x: float | None) -> str:
    if x is None:
        return "an unknown amount of"
    if x < 0.05:
        return "almost no"
    return f"about {x:.1f} inches of" if x >= 0.95 else f"about {x:.2f} inches of"


def spoken_reason(track: dict[str, Any], ev: dict[str, Any], day: dict[str, Any] | None, status: dict[str, Any], now: datetime) -> str:
    tz = _tz(track)
    name = ev["name"]
    if status["status"] == "cancelled":
        src = (status["evidence"] or [{}])[0].get("source") or "the event listing"
        return f"{name} at {track['name']} is listed as cancelled by {src}, so don't make the drive."
    if day is None:
        return f"{name} at {track['name']} has already happened."
    when = _spoken_day(date.fromisoformat(day["date"]), now, tz)
    odds = day.get("play_probability")
    if status["status"] in ("uncertain", "postponed"):
        quote = (status["evidence"] or [{}])[0].get("text", "")[:120]
        return (f"A recent notice from {(status['evidence'] or [{}])[0].get('source', 'the track')} may affect {name} {when} "
                f"(\"{quote}\"), so treat it as uncertain until the track confirms.")
    if day["basis"] == "climatology":
        c = day["climatology"]
        return (f"{name} {when} is too far out for a forecast, so this is the {c['month']} average for {track['name']}: "
                f"about {c['heavy_rain_day_percent']:g}% of days get an inch or more of rain, which works out to roughly {odds}% odds the race runs.")
    if day["basis"] != "forecast" or odds is None:
        return f"I found {name} {when} at {track['name']}, but I couldn't get a forecast, so check the track's page or social posts before leaving."
    w = day["weather"]
    rain, storms = w["rain_chance_percent"], w["thunderstorm_likely"]
    soak = w.get("soak_index_in")
    prior = w.get("prior_48h_rain_in")
    race_rain = w.get("race_window_rain_in")
    if storms:
        sky = f"thunderstorms are possible ({rain}% chance of rain)"
    elif rain <= 15:
        sky = "rain looks unlikely" + (f" ({rain}% chance)" if rain else "")
    elif race_rain is not None and race_rain >= 0.5:
        sky = f"{_inches(race_rain)} rain is forecast during the race ({rain}% chance)"
    else:
        sky = f"there's a {rain}% chance of rain"
    soil_factor = SOIL_FACTORS.get(track.get("soil", "unknown"), 1.0)
    if soak is None:
        ground = "I couldn't get recent rain totals for the track"
    elif soak / soil_factor >= 1.0:
        ground = f"the track has taken {_inches(prior)} rain in the last two days, the kind of soaking that gets dirt tracks cancelled"
    elif prior and prior >= 0.25:
        ground = f"the ground has had {_inches(prior)} rain in the last two days"
    else:
        ground = "the track has stayed mostly dry the last two days"
    if odds >= 85:
        lead = "looks good to run"
    elif odds >= 60:
        lead = "will probably run"
    elif odds >= 35:
        lead = "is at real risk of a rain cancellation"
    else:
        lead = "is likely to be called off for rain"
    tail = "the track has posted that racing is on" if status["status"] == "on" else "nothing official has been posted"
    return f"{name} at {track['name']} {when} {lead} ({odds}%): {sky}, {ground}, and {tail}."


# ----------------------------------------------------------------- calendar
def _uid(event_id: str) -> str:
    return f"{event_id}@rainout-agent-source.vercel.app"


def calendar_event(track: dict[str, Any], ev: dict[str, Any], odds: int | None, reason: str, now: datetime) -> dict[str, Any]:
    field = _field(track)
    tzname = str(_tz(track))
    lines = [f"Race odds: {odds}%" if odds is not None else "Race odds: not available yet", reason,
             f"Checked {now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC by Rainout Source; odds change with the forecast."]
    if ev["time_estimated"] and ev.get("time_note"):
        lines.append(ev["time_note"])
    for s in ev["sources"][:3]:
        lines.append(f"Listed by {s['name']}: {s.get('event_url') or s['url']}")
    social = track.get("social", {})
    if social:
        lines.append("Track posts weather calls on: " + ", ".join(social.values()))
    if field.get("rainout_phone") and field["rainout_phone"] not in ("not_published", "unknown"):
        lines.append(f"Track phone: {field['rainout_phone']}")
    body = {
        "summary": f"{ev['name']} - {track['name']}",
        "location": f"{track['name']}, {field.get('address')}" if field.get("address") else track["name"],
        "description": "\n".join(lines),
        "iCalUID": _uid(ev["event_id"]),
        "source": {"title": "Rainout Source", "url": f"{BASE_URL}/v1/tracks/{track['field_id']}/events"},
        "reminders": {"useDefault": True},
    }
    if ev["time_estimated"]:
        body["start"] = {"date": ev["start"].date().isoformat()}
        body["end"] = {"date": (ev["end"].date() + timedelta(days=1)).isoformat()}
    else:
        body["start"] = {"dateTime": ev["start"].isoformat(), "timeZone": tzname}
        body["end"] = {"dateTime": ev["end"].isoformat(), "timeZone": tzname}
    return body


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def events_ics(items: list[dict[str, Any]], name: str | None = None) -> str:
    from rainout_agent.schedule import _fold

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JEEZ Labs//Rainout Source//EN", "CALSCALE:GREGORIAN"]
    if name:
        lines.append(f"X-WR-CALNAME:{_esc(name)}")
    for it in items:
        ev = it["calendar_event"]
        lines += ["BEGIN:VEVENT", f"UID:{ev['iCalUID']}", f"DTSTAMP:{stamp}"]
        if "date" in ev["start"]:
            lines += [f"DTSTART;VALUE=DATE:{ev['start']['date'].replace('-', '')}", f"DTEND;VALUE=DATE:{ev['end']['date'].replace('-', '')}"]
        else:
            to_utc = lambda v: datetime.fromisoformat(v).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            lines += [f"DTSTART:{to_utc(ev['start']['dateTime'])}", f"DTEND:{to_utc(ev['end']['dateTime'])}"]
        lines += [f"SUMMARY:{_esc(ev['summary'])}", f"LOCATION:{_esc(ev['location'])}", f"DESCRIPTION:{_esc(ev['description'])}",
                  "STATUS:CANCELLED" if it.get("official_status") == "cancelled" else "STATUS:CONFIRMED", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


# ----------------------------------------------------------------- public API
def _parse_day(value: str | None, tz: ZoneInfo, now: datetime) -> date | None:
    if not value:
        return None
    v = value.strip().lower()
    today = now.astimezone(tz).date()
    if v == "today":
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    if v in ("saturday", "sunday", "this weekend", "weekend"):
        target = 5 if v != "sunday" else 6
        return today + timedelta(days=(target - today.weekday()) % 7)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise TracksError("date must be today, tomorrow, saturday, sunday, this weekend or YYYY-MM-DD.")


def _resolve_range(date_p, from_p, to_p, tz, now) -> tuple[date, date]:
    today = now.astimezone(tz).date()
    if date_p:
        d = _parse_day(date_p, tz, now)
        end = d + timedelta(days=1) if (date_p or "").lower() in ("this weekend", "weekend") else d
        return d, end
    start = _parse_day(from_p, tz, now) if from_p else today
    end = _parse_day(to_p, tz, now) if to_p else start + timedelta(days=DEFAULT_RANGE_DAYS)
    if end < start:
        raise TracksError("to must be on or after from.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise TracksError(f"Ask for at most {MAX_RANGE_DAYS} days at a time.")
    return start, end


def build_events(track: dict[str, Any], events: list[dict[str, Any]], now: datetime, *, with_status: bool = True) -> list[dict[str, Any]]:
    tz = _tz(track)
    soon = [e for e in events if e["end"] >= now and e["start"] <= now + timedelta(days=FORECAST_DAYS + 1)]
    notices, notice_sources = (mx.read_notices(track, now) if with_status and any(
        e["start"] <= now + timedelta(days=NOTICE_LOOKAHEAD_DAYS) for e in soon) else ([], []))
    hourly = get_hourly(_field(track)) if soon else None
    amounts = get_amounts(_field(track)) if soon else None
    out = []
    for ev in events:
        status = official_status(track, ev, notices, now) if ev["end"] >= now else {"status": "past", "evidence": []}
        days = score_event(track, ev, status, now, hourly, amounts)
        upcoming = [d for d in days if d["basis"] != "past"]
        race_days = [d for d in upcoming if d["role"] == "race"] or upcoming
        head = race_days[0] if race_days else None
        odds = head["play_probability"] if head else None
        reason = spoken_reason(track, ev, head, status, now)
        item = {
            "event_id": ev["event_id"], "track_id": track["field_id"], "track": track["name"],
            "name": ev["name"], "kind": ev["kind"], "series": ev["series"],
            "start": ev["start"].isoformat(), "end": ev["end"].isoformat(), "timezone": str(tz),
            "time_estimated": ev["time_estimated"], "time_note": ev.get("time_note"),
            "rescheduled_to_this_date": ev["rescheduled_to_this_date"],
            "play_probability": odds, "headline_date": head["date"] if head else None,
            "reason": reason,
            "official_status": status["status"], "status_evidence": status["evidence"],
            "days": days, "sources": ev["sources"],
            "status_checked": notice_sources,
            "social_not_checked": [{"name": k, "url": v, "why": "needs a login"} for k, v in track.get("social", {}).items()],
            "as_of": now.isoformat(),
        }
        item["calendar_event"] = calendar_event(track, {**ev}, odds, reason, now)
        item["ics"] = events_ics([item])
        out.append(item)
    return out


def track_events(track_id: str, *, date_param=None, from_param=None, to_param=None, include_practice: bool = False,
                 events_url: str | None = None, now: datetime | None = None, fetcher=safe_get) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    track = resolve_track(track_id)
    tz = _tz(track)
    start, end = _resolve_range(date_param, from_param, to_param, tz, now)
    events, checked = gather_events(track, today=now.astimezone(tz).date(), events_url=events_url, fetcher=fetcher)
    in_range = [e for e in events if e["end"].date() >= start and e["start"].date() <= end and (include_practice or e["kind"] == "race")]
    items = build_events(track, in_range, now)
    future_races = [e for e in events if e["kind"] == "race" and e["end"] >= now]
    result = {
        "track": public_track(track), "range": {"from": start.isoformat(), "to": end.isoformat()},
        "count": len(items), "events": items, "sources_checked": checked,
        "calendar_ics_url": f"{BASE_URL}/v1/tracks/{track['field_id']}/calendar.ics",
        "calendar_policy": "Never add an event to a calendar without asking the user first. Rainout Source never writes to calendars.",
        "as_of": now.isoformat(),
    }
    if not items:
        nxt = min(future_races, key=lambda e: e["start"], default=None)
        result["next_event"] = build_events(track, [nxt], now)[0] if nxt else None
        if not future_races:
            result["next_step"] = "ask_user_for_events_url"
            result["message"] = (f"I couldn't find any upcoming races at {track['name']} in its public calendars. "
                                 "If you have a link to the event or the series schedule, send it and I'll check that.")
        else:
            result["message"] = f"No {'events' if include_practice else 'races'} at {track['name']} in that range; next_event has the next one."
    return result


def check_race(track_id: str, *, date_param: str | None = None, event_id: str | None = None, events_url: str | None = None,
               now: datetime | None = None, fetcher=safe_get) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    track = resolve_track(track_id)
    tz = _tz(track)
    if not date_param and not event_id:
        raise TracksError("Provide date (YYYY-MM-DD, today, saturday, this weekend) or event_id.")
    events, checked = gather_events(track, today=now.astimezone(tz).date(), events_url=events_url, fetcher=fetcher)
    chosen = None
    if event_id:
        chosen = next((e for e in events if e["event_id"] == event_id), None)
        if not chosen:
            raise TracksError("Unknown event_id for this track; call get_track_events first.")
        target = None
    else:
        start, end = _resolve_range(date_param, None, None, tz, now)
        cands = [e for e in events if e["end"].date() >= start and e["start"].date() <= end]
        cands.sort(key=lambda e: (e["kind"] != "race", e["start"]))
        chosen = cands[0] if cands else None
        target = start
    base = {"track": public_track(track), "sources_checked": checked, "as_of": now.isoformat(),
            "calendar_policy": "Ask the user before adding this to their calendar. Rainout Source never writes to calendars."}
    if not chosen:
        future = sorted((e for e in events if e["kind"] == "race" and e["end"] >= now), key=lambda e: e["start"])
        nxt = build_events(track, future[:1], now)[0] if future else None
        when = target.strftime("%a %b %d").replace(" 0", " ") if target else "that date"
        if track.get("verification_level") == "osm_only":
            answer = (f"{track['name']} comes from OpenStreetMap, so I have no event calendar for it and can't confirm a race on {when}.")
        else:
            answer = f"I don't see a race at {track['name']} on {when} in its public calendars."
        if nxt:
            answer += f" The next one I can find is {nxt['name']} starting {nxt['start'][:10]}."
        cond = conditions_if_racing(track, target, now) if target else None
        if cond and cond.get("play_probability") is not None:
            answer += f" If there is racing that day, the weather odds are about {cond['play_probability']}%: {cond['reason']}"
        return {**base, "found": False, "event": None, "next_event": nxt, "conditions_if_racing": cond,
                "answer": answer + " If you have a link to the event, send it and I'll check it.",
                "next_step": "ask_user_for_events_url"}
    item = build_events(track, [chosen], now)[0]
    if target:
        day = next((d for d in item["days"] if d["date"] == target.isoformat()), None)
        if day and day["basis"] != "past":
            item["play_probability"] = day["play_probability"]
            item["headline_date"] = day["date"]
            item["reason"] = spoken_reason(track, chosen, day, {"status": item["official_status"], "evidence": item["status_evidence"]}, now)
    return {**base, "found": True, "event": item, "answer": item["reason"],
            "next_step": "offer_calendar_add" if item["official_status"] != "cancelled" else "tell_user_cancelled"}


def conditions_if_racing(track: dict[str, Any], day: date, now: datetime) -> dict[str, Any] | None:
    """Weather-only race odds for a day with no listed event (assumes racing 8am-5pm local)."""
    tz = _tz(track)
    if datetime.combine(day, dtime(17), tz) < now:
        return None
    ev = {"name": "Racing", "kind": "race", "series": None, "start": datetime.combine(day, dtime(8), tz),
          "end": datetime.combine(day, dtime(17), tz), "time_estimated": True,
          "time_note": "No listed event; assumes racing 8am-5pm local.", "sources": [], "day_labels": {},
          "listing_cancelled": False, "rescheduled_to_this_date": False,
          "event_id": f"{track['short_id']}-{day.strftime('%Y%m%d')}-assumed"}
    try:
        item = build_events(track, [ev], now, with_status=False)[0]
    except Exception:  # weather outages never sink the answer
        return None
    head = item["days"][0] if item["days"] else {}
    return {"assumed": True, "date": day.isoformat(), "window": "08:00-17:00 local (assumed)",
            "play_probability": item["play_probability"], "basis": head.get("basis"),
            "reason": item["reason"], "day": head,
            "note": "Weather-only estimate: no event is listed, so nothing official was checked."}


def track_calendar(track_id: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    track = resolve_track(track_id)
    tz = _tz(track)
    events, _ = gather_events(track, today=now.astimezone(tz).date())
    keep = [e for e in events if e["kind"] == "race" and e["end"] >= now - timedelta(days=7) and e["start"] <= now + timedelta(days=180)]
    return events_ics(build_events(track, keep, now), name=f"{track['name']} races - Rainout Source")


def track_history(track_id: str) -> dict[str, Any]:
    track = resolve_track(track_id)
    profile = load_historical_profile(track["field_id"]) or {}
    noaa = profile.get("noaa_history") or {}
    monthly = []
    for key, m in sorted((noaa.get("monthly") or {}).items()):
        if not m.get("days_observed"):
            continue
        dim = {2: 28.25, 4: 30, 6: 30, 9: 30, 11: 30}.get(int(key), 31)
        monthly.append({
            "month": int(key), "name": MONTH_NAMES[int(key) - 1],
            "mean_rain_in": m.get("mean_precip_in"),
            "rainy_day_percent": round(100 * m.get("wet_day_frac", 0), 1),
            "half_inch_day_percent": round(100 * m.get("p_ge_0_50", 0), 1),
            "inch_day_percent": round(100 * m.get("p_ge_1_00", 0), 1),
            "inch_days_per_month": round(m.get("p_ge_1_00", 0) * dim, 1),
            "share_of_rainy_days_reaching_an_inch": round(100 * m.get("cancel_given_wet", 0), 1),
            "climatology_race_odds": climate_odds(track, date(2026, int(key), 15))["play_probability"] if climate_odds(track, date(2026, int(key), 15)) else None,
        })
    wettest = sorted(monthly, key=lambda x: -x["inch_day_percent"])[:3]
    history = track.get("history", [])
    rainish = {"rain", "rain_forecast", "rain_and_cold", "flooding", "weather", "severe_weather_forecast"}
    races = [h for h in history if h.get("kind") == "race"]
    station = noaa.get("station") or {}
    return {
        "track": public_track(track),
        "noaa_rain_history": {
            "source": noaa.get("source"), "source_url": noaa.get("source_url"),
            "station": {"id": station.get("id"), "name": station.get("name"), "distance_km": station.get("distance_km")},
            "period": noaa.get("period"), "coverage_percent": noaa.get("coverage_percent"),
            "heavy_rain_threshold_in": noaa.get("rain_to_cancel_threshold_in"),
            "annual": noaa.get("annual"), "monthly": monthly,
            "wettest_months": [w["name"] for w in wettest],
            "recent_inch_plus_days": (noaa.get("rainout_level_days") or [])[-10:],
        },
        "documented_events": history,
        "summary": {
            "documented_race_cancellations_or_moves": len(races),
            "weather_related": sum(1 for h in races if h.get("cause") in rainish),
            "practice_cancellations": sum(1 for h in history if h.get("kind") == "practice"),
        },
        "coverage": {
            "note": track.get("history_coverage"),
            "how_found": ("Searched the track's own site and news, series promoter news (the FMF Texas series posts on freestonemx.com), "
                          "MX Parks and RideMX event pages, and race reports. Facebook and Instagram posts need a login and were not read."),
            "unreadable_sources": track.get("unparsed_sources", []) + [{"name": k, "url": v, "why": "needs a login"} for k, v in track.get("social", {}).items()],
            "gaps": "An empty or short list means nothing was publicly documented, not that races never got rained out.",
        },
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

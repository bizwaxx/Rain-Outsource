"""Nationwide OpenStreetMap venues (osm_only) and shared NOAA station history.

Storage (built by ``scripts/osm_venues.py``):

* ``data/osm/<ST>.json``: one compact file per state, ``{"row_fields": [...], "rows": [[...], ...]}``.
  A state file loads in milliseconds and is cached per serverless instance.
* ``data/osm/motocross.json``: every OSM motocross track nationwide (for track search).
* ``data/osm/index.json``: counts, OSM data date, license.
* ``data/noaa/stations.json``: monthly rain stats per NOAA GHCN-Daily station (2016-2025),
  shared by every venue that uses the station. Venue rows only store the station id.

Venue ids look like ``osm-tx-zilker-park-w123456`` (state, name slug, OSM element), so a
lookup only opens one state file. Hand-verified venues (``data/<city>/*.json``) stay the
"confirmed" tier and win over OSM duplicates.

Map data (c) OpenStreetMap contributors, available under the Open Database License (ODbL).
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from rainout_agent.historical import REPORT_THRESHOLDS_IN, SPORT_THRESHOLDS_IN, threshold_key

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OSM_DIR = DATA_DIR / "osm"
STATIONS_FILE = DATA_DIR / "noaa" / "stations.json"
ID_RE = re.compile(r"^osm-([a-z]{2})-[a-z0-9-]*-[nwr]\d+$")
KINDS = {"b": "ball_field", "s": "soccer_complex", "m": "multipurpose_field", "x": "motocross_track"}
ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
BASE_URL = "https://rainout-agent-source.vercel.app"

STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver", "PR": "America/Puerto_Rico",
}


def timezone_for(state: str, lat: float, lon: float) -> str:
    """IANA zone from state, with approximate splits for states that span two zones."""
    tz = STATE_TZ.get(state, "America/Chicago")
    if state == "TX" and lon < -104.9:
        return "America/Denver"
    if state == "FL" and lon < -85.0 and lat > 29.6:
        return "America/Chicago"
    if state in ("KS", "NE") and lon < -101.3:
        return "America/Denver"
    if state == "SD" and lon < -100.5:
        return "America/Denver"
    if state == "ND" and lon < -101.0 and lat < 47.4:
        return "America/Denver"
    if state == "ID" and lat > 45.6:
        return "America/Los_Angeles"
    if state == "OR" and lon > -117.9 and lat < 44.4:
        return "America/Boise"
    if state == "TN" and lon > -85.3:
        return "America/New_York"
    if state == "KY" and lon < -85.9:
        return "America/Chicago"
    if state == "IN" and ((lon < -86.8 and lat > 41.0) or (lon < -86.9 and lat < 38.4)):
        return "America/Chicago"
    if state == "MI" and lon < -87.6 and lat > 45.5:
        return "America/Menominee"
    return tz


# ------------------------------------------------------------------ loading
@lru_cache(maxsize=1)
def index() -> dict[str, Any]:
    path = OSM_DIR / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"states": {}, "totals": {}}


def coverage() -> dict[str, Any]:
    """Which states have every venue kind vs motocross tracks only (the rollout is state by state)."""
    states = index().get("states", {})
    return {"osm_data_as_of": index().get("osm_data_as_of"),
            "full_states": sorted(st for st, d in states.items() if d.get("scope") != "motocross_only"),
            "motocross_only_states": sorted(st for st, d in states.items() if d.get("scope") == "motocross_only"),
            "venues": index().get("totals", {}).get("venues", 0)}


def available_states() -> list[str]:
    return sorted(index().get("states", {}))


@lru_cache(maxsize=64)
def load_state(state: str) -> tuple[dict[str, Any], ...]:
    path = OSM_DIR / f"{state.upper()}.json"
    if not re.fullmatch(r"[A-Z]{2}", state.upper()) or not path.exists():
        return ()
    doc = json.loads(path.read_text(encoding="utf-8"))
    return tuple(_venue(dict(zip(doc["row_fields"], r)), state.upper()) for r in doc["rows"])


@lru_cache(maxsize=1)
def motocross_tracks() -> tuple[dict[str, Any], ...]:
    path = OSM_DIR / "motocross.json"
    if not path.exists():
        return ()
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in doc["rows"]:
        row = dict(zip(doc["row_fields"], r))
        out.append(_venue(row, row.pop("state")))
    return tuple(out)


def _venue(row: dict[str, Any], state: str) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "sports": row["sports"].split(","), "kind": KINDS.get(row["kind"], "multipurpose_field"),
        "lat": row["lat"], "lon": row["lon"], "city": row.get("city"), "state": state, "address": row.get("address"),
        "pitches": row.get("pitches"), "osm": row["osm"].split(";") if row.get("osm") else [], "station": row.get("station"),
    }


GENERIC_WORDS = frozenset("park field fields sports complex school high middle elementary the and of at city county "
                          "community recreation center centre stadium memorial ballpark athletic soccer baseball softball".split())


def _search_keys(v: dict[str, Any]) -> tuple[str, frozenset[str], frozenset[str]]:
    keys = v.get("_keys")
    if keys is None:
        name = _norm(v["name"])
        keys = (name, frozenset(name.split()) | frozenset(_norm(v.get("city")).split()),
                frozenset(t[:3] for t in name.split() if len(t) >= 3))
        if "id" in v:  # cache on stored venues only
            v["_keys"] = keys
    return keys


def is_osm_id(value: str | None) -> bool:
    return bool(ID_RE.match((value or "").strip().lower()))


def get(venue_id: str) -> dict[str, Any] | None:
    vid = (venue_id or "").strip().lower()
    m = ID_RE.match(vid)
    if not m:
        return None
    return next((v for v in load_state(m.group(1).upper()) if v["id"] == vid), None)


# ------------------------------------------------------------------ search
def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("&", " and ")).split())


def _dist_km(lat1, lon1, lat2, lon2) -> float:
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


@lru_cache(maxsize=256)
def _query_parts(q: str) -> tuple[str, frozenset[str], frozenset[str]]:
    qn = _norm(q)
    toks = frozenset(qn.split())
    return qn, toks, frozenset(t[:3] for t in toks if len(t) >= 3 and t not in GENERIC_WORDS)


def name_score(v: dict[str, Any], q: str) -> float:
    """0..1 match of a query against a venue's name and city."""
    from difflib import SequenceMatcher

    qn, q_tokens, q_pref = _query_parts(q)
    if not qn:
        return 0.0
    name, full_tokens, prefixes = _search_keys(v)
    if qn == name:
        return 1.0
    if q_tokens <= full_tokens:
        return 0.9
    if len(qn) >= 4 and qn in name:
        return 0.85
    overlap = len(q_tokens & full_tokens) / max(1, len(q_tokens)) * 0.8
    if not (q_pref & prefixes) or abs(len(qn) - len(name)) > max(len(qn), len(name)) * 0.6:
        return round(overlap, 3)  # no distinctive word starts alike: skip the fuzzy compare
    sm = SequenceMatcher(None, qn, name, autojunk=False)
    if sm.real_quick_ratio() * 0.85 <= overlap or sm.quick_ratio() * 0.85 <= max(overlap, 0.55):
        return round(overlap, 3)
    return round(max(sm.ratio() * 0.85, overlap), 3)


def search(q: str | None = None, *, states: list[str] | None = None, sports: list[str] | None = None,
           near: tuple[float, float] | None = None, radius_km: float = 50.0, kinds: set[str] | None = None,
           min_score: float = 0.6) -> list[tuple[float, dict[str, Any]]]:
    """Return (score, venue) pairs, best first. Score is name match or nearness."""
    pool_states = states or available_states()
    out = []
    for st in pool_states:
        for v in load_state(st):
            if sports and not set(sports) & set(v["sports"]):
                continue
            if kinds and v["kind"] not in kinds:
                continue
            d = _dist_km(near[0], near[1], v["lat"], v["lon"]) if near else None
            if near and d > radius_km:
                continue
            if q:
                s = name_score(v, q)
                if s < min_score:
                    continue
            else:
                s = 1.0 - (d / radius_km if d is not None else 0)
            out.append((round(s, 3), v, d))
    out.sort(key=lambda x: (-x[0], x[2] if x[2] is not None else 0, x[1]["name"]))
    return [(s, v) for s, v, _ in out]


@lru_cache(maxsize=1)
def surfaces() -> dict[str, list[str]]:
    """{venue_id: [profile surface, raw OSM tag, element URL]} from data/osm/surfaces.json (scripts/osm_surfaces.py)."""
    path = OSM_DIR / "surfaces.json"
    return json.loads(path.read_text(encoding="utf-8")).get("venues", {}) if path.exists() else {}


# ------------------------------------------------------------------ shapes
def primary_sport(v: dict[str, Any]) -> str:
    if v["kind"] == "motocross_track":
        return "motocross"
    if v["kind"] == "soccer_complex":
        return "soccer"
    if v["kind"] == "ball_field":
        return "softball" if v["sports"] == ["softball"] else "baseball"
    return "multipurpose fields"


def osm_url(ref: str) -> str:
    kind = {"n": "node", "w": "way", "r": "relation"}.get(ref[:1], "way")
    return f"https://www.openstreetmap.org/{kind}/{ref[1:]}"


def to_field(v: dict[str, Any]) -> dict[str, Any]:
    """The field-definition shape the status, schedule and motocross code reads."""
    sport = primary_sport(v)
    return {
        "id": v["id"], "field_name": v["name"], "city": v.get("city") or "", "state": v["state"],
        "address": v.get("address") or v["state"], "sport": sport, "sports": v["sports"], "venue_type": v["kind"],
        "coordinates": {"lat": v["lat"], "lon": v["lon"], "method": "openstreetmap"},
        "timezone": timezone_for(v["state"], v["lat"], v["lon"]),
        "rainout_phone": "not_published", "weather_source": "National Weather Service API",
        "official_status_source_url": None, "official_status_source_name": None, "poll_official_status": False,
        "status_source_type": "none", "ownership_type": "unknown", "surface": "unknown", "drainage": "unknown",
        "source_reliability": "low", "aliases": [v["name"]], "verification_level": "osm_only",
        "rain_to_cancel_threshold_in": SPORT_THRESHOLDS_IN.get(sport, 0.5),
        "osm": {"elements": [osm_url(r) for r in v["osm"][:3]], "pitches": v.get("pitches"), "attribution": ATTRIBUTION,
                "surface": _surface(v)},
        "noaa_station_id": v.get("station"),
    }


def _surface(v: dict[str, Any]) -> dict[str, str] | None:
    hit = surfaces().get(v["id"])
    if hit:
        return {"value": hit[0], "tag": f"{hit[1]}", "element": hit[2]}
    if v["kind"] == "motocross_track":
        return {"value": "motocross_dirt", "tag": "(sport=motocross)", "element": osm_url(v["osm"][0]) if v.get("osm") else "OpenStreetMap"}
    return None


def public(v: dict[str, Any], score: float | None = None, distance_km: float | None = None) -> dict[str, Any]:
    """Item for /v1/fields (same keys as confirmed venues, plus OSM labels)."""
    f = to_field(v)
    out = {
        "field_id": v["id"], "name": v["name"], "city": f["city"], "state": v["state"], "address": f["address"],
        "sport": f["sport"], "sports": v["sports"], "venue_type": v["kind"], "coordinates": {"lat": v["lat"], "lon": v["lon"]},
        "timezone": f["timezone"], "pitches": v.get("pitches"),
        "rainout_phone": "not_published", "official_status_source_url": None, "status_source_type": "none",
        "source_reliability": "low", "verification_level": "osm_only",
        "verification_note": "From OpenStreetMap only: name and location are community data; no official rainout source is configured.",
        "osm_elements": f["osm"]["elements"], "noaa_station_id": v.get("station"), "attribution": ATTRIBUTION,
        "status_url": f"{BASE_URL}/v1/status?field_id={v['id']}&game_time={{ISO-8601-game-time}}",
    }
    if score is not None:
        out["match_score"] = score
    if distance_km is not None:
        out["distance_km"] = round(distance_km, 1)
    return out


# ------------------------------------------------------------------ NOAA stations
@lru_cache(maxsize=1)
def stations() -> dict[str, Any]:
    if not STATIONS_FILE.exists():
        return {"stations": {}, "monthly_fields": []}
    return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))


def station_profile(station_id: str | None, threshold_in: float, lat: float | None = None,
                    lon: float | None = None) -> dict[str, Any] | None:
    """A ``noaa_history`` block (same shape as hand-verified venues) built from shared station stats."""
    doc = stations()
    st = doc.get("stations", {}).get(station_id or "")
    if not st:
        return None
    fields = doc["monthly_fields"]
    key = threshold_key(min(REPORT_THRESHOLDS_IN, key=lambda t: abs(t - threshold_in)))
    monthly = {}
    for m, vals in st["monthly"].items():
        e = dict(zip(fields, vals))
        wet = e.get("wet_day_frac") or 0
        heavy = e.get(key) or 0
        e["rainout_day_frac"] = heavy
        e["cancel_given_wet"] = round(heavy / wet, 3) if wet else 0.0
        monthly[m] = e
    dist = round(_dist_km(lat, lon, st["lat"], st["lon"]), 1) if lat is not None else None
    period = doc.get("period") or {}
    return {
        "source": doc.get("source"), "source_url": f"https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations={station_id}&dataTypes=PRCP",
        "station": {"id": station_id, "name": st["name"], "distance_km": dist, "lat": st["lat"], "lon": st["lon"]},
        "period": f"{period.get('start')} to {period.get('end')}" if period else None,
        "coverage_percent": st.get("coverage_percent"), "rain_to_cancel_threshold_in": threshold_in,
        "annual": {"mean_precip_in": st.get("annual_mean_precip_in")}, "monthly": monthly,
        "rainout_level_days": [], "shared_station": True,
    }


def history_profile(field_id: str) -> dict[str, Any] | None:
    """Historical profile for an OSM venue (station stats only; no documented cancellations)."""
    v = get(field_id)
    if not v:
        return None
    f = to_field(v)
    noaa = station_profile(v.get("station"), f["rain_to_cancel_threshold_in"], v["lat"], v["lon"])
    return {"field_id": field_id, "noaa_history": noaa, "historical_cancellations": []} if noaa else None

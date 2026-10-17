#!/usr/bin/env python3
"""Nationwide venue ingestion from OpenStreetMap (Geofabrik extracts) + NOAA station history.

Reproducible pipeline, stdlib Python plus the ``osmium`` command (osmium-tool):

  1. download   Geofabrik US extract (one file) and the per-state .poly boundaries.
  2. filter     ``osmium tags-filter`` keeps only sport-tagged features, named
                containers (parks, sports centres, schools, raceways) and place nodes.
  3. extract    ``osmium extract -p <state>.poly`` cuts the small filtered file per state,
                then ``osmium export`` writes GeoJSON lines.
  4. build      clusters pitches into venues (container polygon first, else 150 m
                single-linkage), names them, finds the nearest town, dedupes against
                the hand-verified venues, and writes compact ``data/osm/<ST>.json``
                plus ``data/osm/motocross.json`` and ``data/osm/index.json``.
  5. stations   picks the nearest NOAA GHCN-Daily station with 2016-2025 PRCP for each
                venue, batch-downloads daily PRCP (25 stations per request), keeps
                stations with >= 75% coverage, and writes monthly stats to
                ``data/noaa/stations.json``; venue rows get their station id.

Usage:
  scripts/osm_venues.py all [--states TX,OK] [--pbf PATH]
  scripts/osm_venues.py build --states TX
  scripts/osm_venues.py stations [--states TX,OK]

Data (c) OpenStreetMap contributors, ODbL 1.0. NOAA data is public domain.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from rainout_agent.historical import REPORT_THRESHOLDS_IN, compute_climatology, threshold_key  # noqa: E402

WORK = Path("/workspace/shared/osm")
OUT_DIR = ROOT / "data" / "osm"
NOAA_DIR = ROOT / "data" / "noaa"
GEOFABRIK = "https://download.geofabrik.de/north-america"
UA = "RainoutSource/1.0 (JEEZ Labs; +https://rainout-agent-source.vercel.app; github.com/bizwaxx/Rain-Outsource)"
ADS_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
GHCN_BASE = "https://www.ncei.noaa.gov/pub/data/ghcn/daily"

STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "DC": "district-of-columbia", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico",
    "NY": "new-york", "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west-virginia", "WI": "wisconsin", "WY": "wyoming", "PR": "puerto-rico",
}
BALL = {"baseball", "softball"}
TARGET = BALL | {"soccer"}
MX = {"motocross", "supercross"}
CONTAINER_LEISURE = {"park", "sports_centre", "recreation_ground", "stadium", "track"}
CONTAINER_AMENITY = {"school", "college", "university"}
MAX_CONTAINER_DIAG_M = 3000
LINK_M = 150
MX_LINK_M = 800
GENERIC_PITCH = re.compile(r"^(?:(?:field|diamond|pitch|court|ball ?field|soccer field|baseball field|softball field|"
                           r"practice field|t-?ball field|little league field|multi-?purpose field)\b[\s#\w-]{0,6}|[\w#-]{1,4})$", re.I)
KIND_CODES = {"ball_field": "b", "soccer_complex": "s", "multipurpose_field": "m", "motocross_track": "x"}
ROW_FIELDS = ["id", "name", "sports", "kind", "lat", "lon", "city", "address", "pitches", "osm", "station"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd):
    log("  $", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        dest.write_bytes(r.read())
    time.sleep(0.5)
    return dest


# ------------------------------------------------------------------ geometry
def dist_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


def _rings(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return [[c]]
    if t == "LineString":
        return [c]
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    if t == "MultiLineString":
        return c
    return []


def centroid_bbox(geom):
    pts = [p for ring in _rings(geom) for p in ring]
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (sum(lats) / len(lats), sum(lons) / len(lons)), (min(lats), min(lons), max(lats), max(lons))


def parse_poly(text: str):
    """Geofabrik .poly -> list of (outer ring, [holes])."""
    rings, cur, name = [], None, None
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        if line == "END":
            if cur is None:
                break
            rings.append((name, cur))
            cur = None
            continue
        if cur is None:
            name, cur = line, []
            continue
        lon, lat = (float(x) for x in line.split()[:2])
        cur.append((lon, lat))
    return rings


def _in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def in_poly(lon, lat, rings):
    hit = False
    for name, ring in rings:
        if _in_ring(lon, lat, ring):
            hit = not name.startswith("!") if not hit else (False if name.startswith("!") else True)
    return hit


class Grid:
    def __init__(self, cell=0.01):
        self.cell, self.cells = cell, defaultdict(list)

    def key(self, lat, lon):
        return int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))

    def add(self, lat, lon, item):
        self.cells[self.key(lat, lon)].append(item)

    def near(self, lat, lon, r=1):
        a, b = self.key(lat, lon)
        for i in range(a - r, a + r + 1):
            for j in range(b - r, b + r + 1):
                yield from self.cells.get((i, j), ())


# ------------------------------------------------------------------ text
def slug(text: str, n: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:n].strip("-") or "venue"


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def sports_of(tags) -> set[str]:
    return {s.strip().lower() for s in re.split(r"[;,]", tags.get("sport", "")) if s.strip()}


def address_of(tags) -> str | None:
    street = tags.get("addr:street")
    if not street:
        return None
    num = tags.get("addr:housenumber")
    parts = [f"{num} {street}" if num else street]
    if tags.get("addr:city"):
        parts.append(tags["addr:city"])
    tail = " ".join(x for x in (tags.get("addr:state"), tags.get("addr:postcode")) if x)
    if tail:
        parts.append(tail)
    return ", ".join(parts)


# ------------------------------------------------------------------ steps 1-3
def step_download(pbf: Path):
    if not pbf.exists():
        log(f"downloading {GEOFABRIK}/us-latest.osm.pbf -> {pbf}")
        run(["curl", "-sS", "-L", "--retry", "10", "-C", "-", "-A", UA, "-o", pbf, f"{GEOFABRIK}/us-latest.osm.pbf"])
    for st, name in STATES.items():
        fetch(f"{GEOFABRIK}/us/{name}.poly", WORK / "poly" / f"{name}.poly")


def step_filter(pbf: Path) -> Path:
    out = WORK / "us-filtered.osm.pbf"
    if out.exists() and out.stat().st_mtime > pbf.stat().st_mtime:
        return out
    run(["osmium", "tags-filter", "-O", "-o", out, pbf, "nwr/sport",
         "nwr/leisure=park,sports_centre,recreation_ground,stadium,track",
         "nwr/amenity=school,college,university", "n/place=city,town,village,hamlet,suburb", "w/highway=raceway"])
    return out


def step_extract(filtered: Path, st: str) -> Path:
    name = STATES[st]
    pbf = WORK / "states" / f"{st}.osm.pbf"
    gj = WORK / "states" / f"{st}.geojsonseq"
    pbf.parent.mkdir(parents=True, exist_ok=True)
    if gj.exists() and gj.stat().st_mtime > filtered.stat().st_mtime:
        return gj
    run(["osmium", "extract", "-O", "-p", WORK / "poly" / f"{name}.poly", "-o", pbf, filtered])
    run(["osmium", "export", "-O", "-f", "geojsonseq", "--geometry-types=point,linestring,polygon",
         "-a", "type,id", "-o", gj, pbf])
    return gj


# ------------------------------------------------------------------ step 4
def load_features(gj: Path):
    pitches, mx, containers, places = [], [], [], []
    with gj.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            f = json.loads(line)
            p = f.get("properties") or {}
            tags = {k: v for k, v in p.items() if not k.startswith("@")}
            ref = f"{p.get('@type', 'x')[0]}{p.get('@id')}"
            cb = centroid_bbox(f.get("geometry") or {})
            if not cb:
                continue
            (lat, lon), bbox = cb
            item = {"ref": ref, "tags": tags, "lat": lat, "lon": lon, "bbox": bbox, "name": (tags.get("name") or "").strip()}
            sp = sports_of(tags)
            if sp & MX or (MX_NAME.search(item["name"]) and not sp & NOT_MX
                           and (tags.get("leisure") in ("sports_centre", "track", "pitch") or tags.get("highway") == "raceway" or "motor" in sp)):
                mx.append(item)
            if tags.get("leisure") == "pitch" and sp & TARGET:
                item["sports"] = sp & TARGET
                pitches.append(item)
            elif tags.get("leisure") in ("sports_centre", "stadium") and sp & TARGET and item["name"]:
                item["sports"] = sp & TARGET
                item["complex"] = True
                pitches.append(item)
            if item["name"] and (tags.get("leisure") in CONTAINER_LEISURE or tags.get("amenity") in CONTAINER_AMENITY
                                 or tags.get("highway") == "raceway") and f["geometry"]["type"] != "Point":
                diag = dist_m(bbox[0], bbox[1], bbox[2], bbox[3])
                if diag <= MAX_CONTAINER_DIAG_M:
                    containers.append({**item, "area": (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])})
            elif item["name"] and (tags.get("leisure") in CONTAINER_LEISURE or tags.get("amenity") in CONTAINER_AMENITY):
                containers.append({**item, "area": 0.0, "point": True})
            if tags.get("place") and item["name"] and f["geometry"]["type"] == "Point":
                places.append(item)
    return pitches, mx, containers, places


class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


PLACE_RANK = {"city": 0, "town": 1, "village": 2, "suburb": 3, "hamlet": 4}


def nearest_town(places_grid, lat, lon):
    """Nearest city/town within 10 km, else nearest village within 10 km, else the nearest named place."""
    cands = []
    for r in (1, 2):
        cands = [(dist_m(lat, lon, pl["lat"], pl["lon"]), pl) for pl in places_grid.near(lat, lon, r)]
        if cands:
            break
    if not cands:
        return None
    for allowed in (("city", "town"), ("village",)):
        pool = [(d, pl) for d, pl in cands if pl["tags"].get("place") in allowed and d <= 10000]
        if pool:
            d, pl = min(pool, key=lambda x: x[0])
            return (d, pl, d)
    d, pl = min(cands, key=lambda x: x[0])
    return (d, pl, d)


def contained_in(cgrid, item, kinds=None):
    best = None
    for c in cgrid.near(item["lat"], item["lon"], 2):
        b = c["bbox"]
        if c.get("point"):
            continue
        if b[0] <= item["lat"] <= b[2] and b[1] <= item["lon"] <= b[3]:
            if kinds and not kinds(c):
                continue
            if best is None or c["area"] < best["area"]:
                best = c
    return best


def nearest_named(cgrid, lat, lon, max_m, kinds=None):
    best = None
    for c in cgrid.near(lat, lon, 2):
        if kinds and not kinds(c):
            continue
        d = dist_m(lat, lon, c["lat"], c["lon"])
        if d <= max_m and (best is None or d < best[0]):
            best = (d, c)
    return best[1] if best else None


def cluster(items, link_m, cell):
    g = Grid(cell)
    for i, it in enumerate(items):
        g.add(it["lat"], it["lon"], i)
    uf = UF(len(items))
    for i, it in enumerate(items):
        for j in g.near(it["lat"], it["lon"]):
            if j > i and dist_m(it["lat"], it["lon"], items[j]["lat"], items[j]["lon"]) <= link_m:
                uf.union(i, j)
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[uf.find(i)].append(items[i])
    return list(groups.values())


def good_name(name: str) -> bool:
    return bool(name) and not GENERIC_PITCH.match(name.strip())


MX_NAME = re.compile(r"\b(mx|motocross|moto-?x|motoplex|supercross|motorcross)\b", re.I)
NOT_MX_NAME = re.compile(r"\bbmx\b|pump ?track|bicycle|\br/?c\b|snowmobile|\bdrags?\b|climb|riding academy|ironman|\bgym\b|\btank\b|singletrack", re.I)
GENERIC_MX = re.compile(r"^(private |intermediate |pro |mini ?bikes? |minibike )?(motocross|mx|motorcross|dirt bike|moto/atv practice|"
                        r"motorcycle practice|atv)( practice)? ?(track|course|area|park|range|tracks|track area)?s?$", re.I)
MX_PLACE_NAME = re.compile(r"\b(mx|moto|motocross|motorcross|motoplex|supercross|raceway|racing|race|cycle|motorcycle|"
                           r"orv|ohv|off-?road|dirt|speedway|track|motorsports?|park)\b", re.I)
NOT_MX = {"bmx", "rc_car", "cycling", "mtb", "disc_golf", "skateboard"}
SKIP_NAME = re.compile(r"batting cage|cage|bullpen|practice net|driving range", re.I)


def make_venue(st, members, container, kind_sports, places_grid, is_mx):
    named = [m for m in members if good_name(m["name"]) and not m.get("complex")]
    complexes = [m for m in members if m.get("complex")]
    if container is not None:
        name, rep = container["name"], container
    elif complexes:
        name, rep = complexes[0]["name"], complexes[0]
    elif named:
        rep = sorted(named, key=lambda m: (-len(m["name"]), m["ref"]))[0]
        name = rep["name"]
    else:
        return None
    if SKIP_NAME.search(name) or (is_mx and NOT_MX_NAME.search(name)):
        return None
    pts = [m for m in members if not m.get("complex")] or members
    lat = sum(m["lat"] for m in pts) / len(pts)
    lon = sum(m["lon"] for m in pts) / len(pts)
    addr = None
    for src in [container] + complexes + members:
        if src is not None:
            addr = address_of(src["tags"])
            if addr:
                break
    city_tag = next((s["tags"].get("addr:city") for s in [container] + members if s is not None and s["tags"].get("addr:city")), None)
    town = nearest_town(places_grid, lat, lon)
    city = city_tag or (town[1]["name"] if town else None)
    if addr and not re.search(r"\b[A-Z]{2}\b", addr):
        m_zip = re.search(r",?\s*(\d{5}(?:-\d{4})?)$", addr)
        addr = f"{addr[:m_zip.start()]}, {st} {m_zip.group(1)}" if m_zip else f"{addr}, {st}"
    if not addr:
        addr = (f"near {town[1]['name']}, {st}" if town and town[2] > 1500 else f"{city}, {st}") if city else f"{st}"
    if is_mx and GENERIC_MX.match(name.strip()) and town:
        name = f"{name.strip()} near {town[1]['name']}"
    if is_mx:
        kind = "motocross_track"
        sports = ["motocross"]
    else:
        sp = set()
        for m in members:
            sp |= m["sports"]
        sports = sorted(sp)
        kind = "multipurpose_field" if "soccer" in sp and sp & BALL else ("soccer_complex" if "soccer" in sp else "ball_field")
    refs = sorted({m["ref"] for m in members})
    rep_ref = rep["ref"]
    return {
        "id": f"osm-{st.lower()}-{slug(name)}-{rep_ref}",
        "name": re.sub(r"\s+", " ", name).strip(), "sports": sports, "kind": kind,
        "lat": round(lat, 5), "lon": round(lon, 5), "city": city, "address": addr,
        "pitches": sum(1 for m in members if not m.get("complex")) or 1,
        "osm": [rep_ref] + [r for r in refs if r != rep_ref][:7], "station": None,
    }


def build_state(st, gj, confirmed):
    pitches, mx, containers, places = load_features(gj)
    cgrid = Grid(0.02)
    for c in containers:
        cgrid.add(c["lat"], c["lon"], c)
    pgrid = Grid(0.1)
    for pl in places:
        pgrid.add(pl["lat"], pl["lon"], pl)
    stats = defaultdict(int)
    venues = []

    # ---- ball / soccer
    by_container = defaultdict(list)
    loose = []
    for p in pitches:
        c = None if p.get("complex") else contained_in(cgrid, p, lambda c: c["tags"].get("highway") != "raceway")
        (by_container[c["ref"]] if c else loose).append(p)
    cont_by_ref = {c["ref"]: c for c in containers}
    for ref, members in by_container.items():
        v = make_venue(st, members, cont_by_ref[ref], None, pgrid, False)
        if v:
            venues.append(v)
    for grp in cluster(loose, LINK_M, 0.002):
        complexes = [m for m in grp if m.get("complex")]
        container = None
        if not complexes and not any(good_name(m["name"]) for m in grp):
            lat = sum(m["lat"] for m in grp) / len(grp)
            lon = sum(m["lon"] for m in grp) / len(grp)
            container = nearest_named(cgrid, lat, lon, 250, lambda c: c["tags"].get("highway") != "raceway")
        v = make_venue(st, grp, container, None, pgrid, False)
        if v:
            venues.append(v)
        else:
            stats["unnamed_pitch_clusters_dropped"] += 1
    # merge venues that ended up with the same id (same container reached two ways)
    merged = {}
    for v in venues:
        if v["id"] in merged:
            m = merged[v["id"]]
            m["pitches"] += v["pitches"]
            m["sports"] = sorted(set(m["sports"]) | set(v["sports"]))
            m["osm"] = (m["osm"] + [r for r in v["osm"] if r not in m["osm"]])[:8]
        else:
            merged[v["id"]] = v
    venues = list(merged.values())
    for v in venues:
        sp = set(v["sports"])
        v["kind"] = "multipurpose_field" if "soccer" in sp and sp & BALL else ("soccer_complex" if "soccer" in sp else "ball_field")

    # ---- motocross
    for grp in cluster(mx, MX_LINK_M, 0.01):
        lat = sum(m["lat"] for m in grp) / len(grp)
        lon = sum(m["lon"] for m in grp) / len(grp)
        named = [m for m in grp if good_name(m["name"])]
        container = None
        if not named:
            container = nearest_named(cgrid, lat, lon, 1000,
                                      lambda c: (c["tags"].get("leisure") in ("sports_centre", "track") or c["tags"].get("highway") == "raceway")
                                      and bool(MX_PLACE_NAME.search(c["name"])))
        for m in grp:
            m["sports"] = {"motocross"}
        v = make_venue(st, grp, container, None, pgrid, True)
        if v:
            venues.append(v)
        else:
            stats["unnamed_motocross_dropped"] += 1

    # ---- dedupe against hand-verified venues
    kept = []
    for v in venues:
        dup = None
        for c in confirmed:
            if c["state"] and c["state"] != st:
                continue
            radius = 1500 if v["kind"] == "motocross_track" else 500
            d = dist_m(v["lat"], v["lon"], c["lat"], c["lon"])
            same_sport = bool(set(v["sports"]) & set(c["sports"])) or "multipurpose" in c["sports"]
            if same_sport and (d <= radius or (d <= 3000 and _similar(v["name"], c["name"]))):
                dup = c
                break
        if dup:
            stats["duplicates_of_confirmed"] += 1
            dup.setdefault("osm_matches", []).append(v["id"])
            continue
        kept.append(v)
    kept.sort(key=lambda v: v["id"])
    return kept, dict(stats)


def _similar(a, b):
    from difflib import SequenceMatcher

    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8)


def load_confirmed():
    from rainout_agent import motocross
    from rainout_agent.status_api import _load_all_fields
    from rainout_agent.venue_schema import sports_for

    out = []
    for f in _load_all_fields():
        c = f.get("coordinates") or {}
        if c.get("lat") is None:
            continue
        out.append({"id": f["id"], "name": f["field_name"], "state": f.get("state"), "lat": float(c["lat"]),
                    "lon": float(c["lon"]), "sports": sports_for(f) or ["multipurpose"]})
    known = {o["id"] for o in out}
    for t in motocross.tracks():
        if t["field_id"] not in known:
            log(f"  warning: motocross track {t['field_id']} has no field file")
    return out


def resolve_border_duplicates(per_state):
    """A feature crossing a state line lands in both extracts: keep the state containing it."""
    owner = defaultdict(list)
    for st, venues in per_state.items():
        for v in venues:
            owner[v["osm"][0]].append(st)
    polys = {}
    drop = set()
    for ref, sts in owner.items():
        if len(sts) < 2:
            continue
        v = next(x for x in per_state[sts[0]] if x["osm"][0] == ref)
        winner = None
        for st in sts:
            if st not in polys:
                polys[st] = parse_poly((WORK / "poly" / f"{STATES[st]}.poly").read_text())
            if in_poly(v["lon"], v["lat"], polys[st]):
                winner = st
                break
        winner = winner or sts[0]
        drop |= {(st, ref) for st in sts if st != winner}
    for st in per_state:
        per_state[st] = [v for v in per_state[st] if (st, v["osm"][0]) not in drop]
    return len(drop)


def write_outputs(per_state, stats, osm_timestamp):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "osm_data_as_of": osm_timestamp, "source": "OpenStreetMap via Geofabrik US extract",
             "license": "Open Database License (ODbL) 1.0; (c) OpenStreetMap contributors",
             "attribution_url": "https://www.openstreetmap.org/copyright",
             "row_fields": ROW_FIELDS, "kind_codes": KIND_CODES, "states": {}}
    mx_rows = []
    for st in sorted(per_state):
        venues = per_state[st]
        rows = [_row(v) for v in venues]
        doc = {"state": st, "row_fields": ROW_FIELDS, "rows": rows}
        (OUT_DIR / f"{st}.json").write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
        counts = defaultdict(int)
        for v in venues:
            counts[v["kind"]] += 1
            for s in v["sports"]:
                counts["sport:" + s] += 1
        index["states"][st] = {"venues": len(venues), "by_kind": {k: counts[k] for k in KIND_CODES if counts[k]},
                               "by_sport": {k[6:]: n for k, n in sorted(counts.items()) if k.startswith("sport:")},
                               "with_station": sum(1 for v in venues if v["station"]), **stats.get(st, {})}
        mx_rows += [[st] + _row(v) for v in venues if v["kind"] == "motocross_track"]
    (OUT_DIR / "motocross.json").write_text(json.dumps({"row_fields": ["state"] + ROW_FIELDS, "rows": mx_rows},
                                                       separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    index["totals"] = {"venues": sum(s["venues"] for s in index["states"].values()),
                       "motocross_tracks": len(mx_rows)}
    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")


def _row(v):
    return [v["id"], v["name"], ",".join(v["sports"]), KIND_CODES[v["kind"]], v["lat"], v["lon"], v["city"], v["address"],
            v["pitches"], ";".join(v["osm"]), v["station"]]


def read_state(st):
    doc = json.loads((OUT_DIR / f"{st}.json").read_text(encoding="utf-8"))
    inv = {c: k for k, c in KIND_CODES.items()}
    out = []
    for r in doc["rows"]:
        v = dict(zip(doc["row_fields"], r))
        v["sports"] = v["sports"].split(",")
        v["kind"] = inv[v["kind"]]
        v["osm"] = v["osm"].split(";")
        out.append(v)
    return out


def osm_timestamp(pbf: Path) -> str | None:
    try:
        out = subprocess.run(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(pbf)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return out or None
    except Exception:
        return None


# ------------------------------------------------------------------ step 5
def parse_ads_multi(text):
    out = defaultdict(dict)
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get("PRCP") or "").strip()
        if not raw or not row.get("DATE"):
            continue
        attrs = (row.get("PRCP_ATTRIBUTES") or "").split(",")
        q = attrs[1].strip() if len(attrs) > 1 else ""
        try:
            out[row["STATION"]][date.fromisoformat(row["DATE"][:10])] = None if q else float(raw)
        except ValueError:
            continue
    return out


def station_stats(values, start, end):
    """Monthly stats only (no per-day list): enough to derive any sport's threshold."""
    days = []
    d = start
    while d <= end:
        days.append((d, values.get(d)))
        d += timedelta(days=1)
    clim = compute_climatology(days, 1.0, period_days=(end - start).days + 1)
    monthly = {}
    for m, e in clim["monthly"].items():
        if not e.get("days_observed"):
            continue
        monthly[m] = [e["days_observed"], e["mean_precip_in"], e["wet_day_frac"]] + [e[threshold_key(t)] for t in REPORT_THRESHOLDS_IN]
    return clim["coverage_percent"], clim["annual"]["mean_precip_in"], monthly


def step_stations(states, start=date(2016, 1, 1), end=date(2025, 12, 31), batch=25, min_cov=75.0, workers=3):
    sys.path.insert(0, str(ROOT / "scripts"))
    import fetch_noaa_history as fnh

    cache = WORK / "noaa"
    cache.mkdir(parents=True, exist_ok=True)
    stations = fnh.parse_stations(fetch(f"{GHCN_BASE}/ghcnd-stations.txt", cache / "ghcnd-stations.txt").read_text())
    inv = fnh.parse_prcp_inventory(fetch(f"{GHCN_BASE}/ghcnd-inventory.txt", cache / "ghcnd-inventory.txt").read_text())
    good = [s for sid, (a, b) in inv.items() if a <= start.year and b >= end.year and (s := stations.get(sid))
            and (sid.startswith("US") or sid.startswith("RQ") or sid.startswith("CA") or sid.startswith("MX"))]
    sgrid = Grid(0.25)
    for s in good:
        sgrid.add(s["lat"], s["lon"], s)
    path = NOAA_DIR / "stations.json"
    # Every station ever computed is kept in the work dir (stations_all.json), so rebuilding a
    # subset is a cache hit; the repo copy only carries stations that venues reference.
    full_path = cache / "stations_all.json"
    seed = full_path if full_path.exists() else path
    doc = json.loads(seed.read_text()) if seed.exists() else {
        "version": 1, "source": "NOAA NCEI GHCN-Daily (Access Data Service, daily-summaries)",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "monthly_fields": ["days_observed", "mean_precip_in", "wet_day_frac"] + [threshold_key(t) for t in REPORT_THRESHOLDS_IN],
        "stations": {}, "rejected": {}}
    known, rejected = doc["stations"], doc.setdefault("rejected", {})
    all_states = sorted(p.stem for p in OUT_DIR.glob("[A-Z][A-Z].json"))
    venues = {st: read_state(st) for st in all_states}
    states = set(states)

    def selected(st, v):  # the chosen states, plus motocross tracks everywhere
        return st in states or v["kind"] == "motocross_track"

    def candidates(v):
        out = []
        for s in sgrid.near(v["lat"], v["lon"], 1):
            d = dist_m(v["lat"], v["lon"], s["lat"], s["lon"]) / 1000
            if d <= 40 and s["id"] not in rejected:
                out.append((d, s))
        # Prefer long-record COOP/ASOS stations (USC/USW) over a slightly nearer volunteer gauge,
        # so nearby venues share a station (fewer downloads, steadier records).
        out.sort(key=lambda x: x[0] * (1.0 if x[1]["id"][:3] in ("USC", "USW") else 1.6))
        return out

    for rnd in range(4):
        want = {}
        for st, vs in venues.items():
            for v in vs:
                if not selected(st, v):
                    continue
                c = candidates(v)
                if c and c[0][1]["id"] not in known:
                    want[c[0][1]["id"]] = c[0][1]
        log(f"round {rnd}: {len(want)} stations to fetch")
        if not want:
            break
        ids = sorted(want)
        chunks = [ids[i:i + batch] for i in range(0, len(ids), batch)]

        def get_chunk(chunk):
            params = {"dataset": "daily-summaries", "stations": ",".join(chunk), "dataTypes": "PRCP",
                      "startDate": start.isoformat(), "endDate": end.isoformat(), "units": "standard",
                      "includeAttributes": "true", "format": "csv"}
            cpath = cache / f"ads_{fnh_hash(chunk)}.csv"
            for attempt in range(3):
                try:
                    return chunk, fetch(f"{ADS_URL}?{urllib.parse.urlencode(params)}", cpath).read_text(encoding="utf-8")
                except Exception as exc:  # noqa: PERF203
                    err = exc
                    time.sleep(5 * (attempt + 1))
            return chunk, err

        from concurrent.futures import ThreadPoolExecutor

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:  # a few polite parallel requests
            for chunk, text in pool.map(get_chunk, chunks):
                done += len(chunk)
                if isinstance(text, Exception):
                    log(f"  batch failed ({text})")
                    for sid in chunk:
                        rejected[sid] = "fetch_failed"
                    continue
                data = parse_ads_multi(text)
                for sid in chunk:
                    cov, annual, monthly = station_stats(data.get(sid, {}), start, end)
                    if (cov or 0) >= min_cov:
                        s = want[sid]
                        known[sid] = {"name": s["name"], "lat": s["lat"], "lon": s["lon"], "elevation_m": s["elevation_m"],
                                      "coverage_percent": cov, "annual_mean_precip_in": annual, "monthly": monthly}
                    else:
                        rejected[sid] = f"coverage {cov}%"
                if done % (batch * 20) < batch or done == len(ids):
                    log(f"  {done}/{len(ids)} fetched; {len(known)} good, {len(rejected)} rejected")
                    NOAA_DIR.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
        NOAA_DIR.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")

    # assign the nearest good station to every venue (40 km max)
    for st, vs in venues.items():
        for v in vs:
            if not selected(st, v) and v.get("station") in known:
                continue
            c = [x for x in candidates(v) if x[1]["id"] in known]
            v["station"] = c[0][1]["id"] if c else None
    full_path.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    used = {v["station"] for vs in venues.values() for v in vs if v.get("station")}
    pruned = {k: v for k, v in doc.items() if k not in ("stations", "rejected")}
    pruned["stations"] = {sid: known[sid] for sid in sorted(used)}
    pruned["stations_rejected"] = len(rejected)
    path.write_text(json.dumps(pruned, separators=(",", ":")) + "\n", encoding="utf-8")
    return venues


def fnh_hash(chunk):
    import hashlib

    return hashlib.sha1(",".join(chunk).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["download", "build", "stations", "all"])
    ap.add_argument("--states", default="all")
    ap.add_argument("--pbf", default=str(WORK / "us-latest.osm.pbf"))
    ap.add_argument("--full-states", default="all",
                    help="states that keep every venue kind; other built states keep only motocross tracks (default: all)")
    ap.add_argument("--station-states", default=None, help="states to batch NOAA stats for (default: same as --states)")
    args = ap.parse_args(argv)
    states = sorted(STATES) if args.states == "all" else [s.strip().upper() for s in args.states.split(",")]
    pbf = Path(args.pbf)
    if args.step in ("download", "all"):
        step_download(pbf)
    if args.step in ("build", "all"):
        for st, name in STATES.items():
            fetch(f"{GEOFABRIK}/us/{name}.poly", WORK / "poly" / f"{name}.poly")
        filtered = step_filter(pbf)
        confirmed = load_confirmed()
        per_state, stats = {}, {}
        for st in states:
            gj = step_extract(filtered, st)
            per_state[st], stats[st] = build_state(st, gj, confirmed)
            log(f"{st}: {len(per_state[st])} venues {stats[st]}")
        n = resolve_border_duplicates(per_state)
        log(f"border duplicates removed: {n}")
        if args.full_states != "all":
            full = {x.strip().upper() for x in args.full_states.split(",") if x.strip()}
            for st in per_state:
                if st not in full:
                    per_state[st] = [v for v in per_state[st] if v["kind"] == "motocross_track"]
                    stats[st] = {**stats[st], "scope": "motocross_only"}
        # keep station ids from a previous run
        old = {}
        for st in per_state:
            if (OUT_DIR / f"{st}.json").exists():
                old.update({v["id"]: v["station"] for v in read_state(st)})
        for vs in per_state.values():
            for v in vs:
                v["station"] = old.get(v["id"])
        write_outputs(per_state, stats, osm_timestamp(pbf))
        (OUT_DIR / "confirmed_matches.json").write_text(json.dumps(
            {c["id"]: c.get("osm_matches", []) for c in confirmed if c.get("osm_matches")}, indent=1) + "\n")
    if args.step in ("stations", "all"):
        sstates = states if not args.station_states else [s.strip().upper() for s in args.station_states.split(",")]
        venues = step_stations(sstates)
        per_state = {st: read_state(st) for st in sorted(p.stem for p in OUT_DIR.glob("[A-Z][A-Z].json"))}
        for st, vs in venues.items():
            per_state[st] = vs
        idx = json.loads((OUT_DIR / "index.json").read_text())
        stats = {st: {k: v for k, v in d.items() if k not in ("venues", "by_kind", "by_sport", "with_station")}
                 for st, d in idx["states"].items()}
        write_outputs(per_state, stats, idx.get("osm_data_as_of"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

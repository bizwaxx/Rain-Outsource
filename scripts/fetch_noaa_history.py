#!/usr/bin/env python3
"""Fetch real NOAA daily precipitation history for every supported field.

For each field definition in data/ this script:

1. Finds GHCN-Daily stations near the field that report precipitation (PRCP)
   for the whole study period, using NOAA's public station and inventory files.
2. Downloads daily PRCP for the nearest candidates from NCEI's free, keyless
   Access Data Service (dataset=daily-summaries) and keeps the nearest station
   with good coverage (>= --min-coverage, default 90%).
3. Writes a ``noaa_history`` block (station, coverage, monthly rain
   frequencies, days at the rain-to-cancel level) into the field's
   data/historical/*-historical.json, preserving hand-entered cancellation
   records.

Raw downloads are cached in .cache/noaa/ (git-ignored), so re-running is cheap
and the output is reproducible for a fixed period.

Usage:
    python scripts/fetch_noaa_history.py                 # all fields
    python scripts/fetch_noaa_history.py --field Krieg   # one field (id or substring)
    python scripts/fetch_noaa_history.py --start 2016-01-01 --end 2025-12-31
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rainout_agent.historical import compute_climatology, rain_to_cancel_threshold_in  # noqa: E402

DATA_DIR = ROOT / "data"
HIST_DIR = DATA_DIR / "historical"
CACHE_DIR = ROOT / ".cache" / "noaa"
GHCN_BASE = "https://www.ncei.noaa.gov/pub/data/ghcn/daily"
ADS_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
USER_AGENT = "Rainout Source by JEEZ Labs (public pilot; github.com/bizwaxx/Rain-Outsource)"
SOURCE_NAME = "NOAA NCEI GHCN-Daily (Access Data Service, daily-summaries)"


# --------------------------------------------------------------------------- io
def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # network hiccups / 5xx: back off and retry
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


def _cached(name: str, url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if not path.exists():
        print(f"  downloading {url}", file=sys.stderr)
        path.write_bytes(_get(url))
    return path


# ---------------------------------------------------------------- station data
def parse_stations(text: str) -> dict[str, dict]:
    """Parse ghcnd-stations.txt (fixed width)."""
    stations = {}
    for line in text.splitlines():
        if len(line) < 71:
            continue
        sid = line[0:11].strip()
        stations[sid] = {
            "id": sid,
            "lat": float(line[12:20]),
            "lon": float(line[21:30]),
            "elevation_m": float(line[31:37]),
            "state": line[38:40].strip(),
            "name": line[41:71].strip(),
        }
    return stations


def parse_prcp_inventory(text: str) -> dict[str, tuple[int, int]]:
    """Parse ghcnd-inventory.txt and return {station_id: (first_year, last_year)} for PRCP."""
    inv = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 6 and parts[3] == "PRCP":
            inv[parts[0]] = (int(parts[4]), int(parts[5]))
    return inv


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def candidate_stations(lat, lon, stations, inventory, start_year, end_year, max_km, limit):
    out = []
    for sid, (first, last) in inventory.items():
        if first > start_year or last < end_year:
            continue
        st = stations.get(sid)
        if not st:
            continue
        # Cheap bounding box before the exact distance.
        if abs(st["lat"] - lat) > 1.0 or abs(st["lon"] - lon) > 1.2:
            continue
        d = haversine_km(lat, lon, st["lat"], st["lon"])
        if d <= max_km:
            out.append({**st, "distance_km": round(d, 1)})
    out.sort(key=lambda s: s["distance_km"])
    return out[:limit]


# ------------------------------------------------------------ precipitation data
def parse_daily_csv(text: str) -> dict[date, float | None]:
    """Parse an Access Data Service CSV (with attributes) into {date: inches}.

    Values whose quality flag (2nd attribute) is set failed NOAA QC and are
    treated as missing.
    """
    values: dict[date, float | None] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        raw = (row.get("PRCP") or "").strip()
        if not raw or not row.get("DATE"):
            continue
        attrs = (row.get("PRCP_ATTRIBUTES") or "").split(",")
        qflag = attrs[1].strip() if len(attrs) > 1 else ""
        try:
            day = date.fromisoformat(row["DATE"][:10])
            values[day] = None if qflag else float(raw)
        except ValueError:
            continue
    return values


def fetch_station_prcp(station_id: str, start: date, end: date) -> dict[date, float | None]:
    params = {
        "dataset": "daily-summaries",
        "stations": station_id,
        "dataTypes": "PRCP",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "units": "standard",
        "includeAttributes": "true",
        "format": "csv",
    }
    url = f"{ADS_URL}?{urllib.parse.urlencode(params)}"
    path = _cached(f"{station_id}_{start}_{end}.csv", url)
    return parse_daily_csv(path.read_text(encoding="utf-8"))


def coverage(values: dict[date, float | None], start: date, end: date) -> float:
    total = (end - start).days + 1
    good = sum(1 for d, v in values.items() if v is not None and start <= d <= end)
    return good / total if total else 0.0


def iter_period(values, start, end):
    d = start
    while d <= end:
        yield d, values.get(d)
        d += timedelta(days=1)


# ------------------------------------------------------------- historical files
def canonical_hist_name(field_id: str) -> str:
    # Same naming scheme as scripts/generate_historical.py.
    return field_id.replace("austin-tx-", "").replace("tx-", "") + "-historical.json"


def load_field_definitions() -> list[dict]:
    fields = []
    for path in sorted(DATA_DIR.glob("**/*.json")):
        if "historical" in path.relative_to(DATA_DIR).parts:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "id" in data and data.get("coordinates"):
            fields.append(data)
    return fields


def existing_hist_docs(field_id: str) -> list[tuple[Path, dict]]:
    docs = []
    for path in sorted(HIST_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("field_id") == field_id:
            docs.append((path, data))
    return docs


def merge_cancellations(docs: list[dict]) -> list[dict]:
    seen, merged = set(), []
    for doc in docs:
        for rec in doc.get("historical_cancellations", []) or []:
            key = (rec.get("date"), rec.get("game_time"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)
    merged.sort(key=lambda r: (r.get("date") or "", r.get("game_time") or ""))
    return merged


def build_noaa_block(field, station, values, start, end, cov):
    threshold = rain_to_cancel_threshold_in(field)
    clim = compute_climatology(iter_period(values, start, end), threshold, period_days=(end - start).days + 1)
    return {
        "source": SOURCE_NAME,
        "source_url": ADS_URL + "?dataset=daily-summaries&stations=" + station["id"] + "&dataTypes=PRCP",
        "station": {
            "id": station["id"],
            "name": station["name"],
            "lat": station["lat"],
            "lon": station["lon"],
            "elevation_m": station["elevation_m"],
            "distance_km": station["distance_km"],
        },
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "coverage_ok": cov >= 0.9,
        **clim,
    }


def write_hist(field, noaa_block, values):
    field_id = field["id"]
    docs = existing_hist_docs(field_id)
    target = HIST_DIR / canonical_hist_name(field_id)
    base = next((d for p, d in docs if p == target), docs[0][1] if docs else {})
    cancellations = merge_cancellations([d for _, d in docs])
    if noaa_block is not None:
        checks = []
        for rec in cancellations:
            try:
                d = date.fromisoformat(rec["date"])
            except (KeyError, ValueError):
                continue
            checks.append({
                "date": rec["date"],
                "recorded_rain_in": rec.get("rain_amount_in"),
                "station_prcp_in": values.get(d),
                "station_prcp_prev_day_in": values.get(d - timedelta(days=1)),
            })
        if checks:
            noaa_block["recorded_cancellations_check"] = checks
    doc = {
        "field_id": field_id,
        "historical_cancellations": cancellations,
        "typical_cancellation_threshold_in": base.get("typical_cancellation_threshold_in", 1.5),
        "notes": (
            "historical_cancellations are hand-entered official rainouts. noaa_history is generated by "
            "scripts/fetch_noaa_history.py from NOAA GHCN-Daily observations at the nearest station with "
            "good precipitation coverage; it is used to calibrate play probability."
            if noaa_block is not None
            else "No GHCN-Daily station with good precipitation coverage was found near this field; "
                 "play probability falls back to the uncalibrated model."
        ),
    }
    if noaa_block is not None:
        doc["noaa_history"] = noaa_block
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    removed = []
    for path, _ in docs:
        if path != target:
            path.unlink()  # duplicate doc for the same field; records were merged above
            removed.append(path.name)
    return target, removed


# ------------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--max-km", type=float, default=40.0, help="search radius for stations")
    ap.add_argument("--candidates", type=int, default=8, help="nearest stations to try per field")
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--fallback-coverage", type=float, default=0.75,
                    help="accept the best station above this if none meets --min-coverage")
    ap.add_argument("--field", help="only process fields whose id contains this text")
    ap.add_argument("--report", default=None, help="optional path for a JSON run report")
    args = ap.parse_args(argv)

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    stations = parse_stations(_cached("ghcnd-stations.txt", f"{GHCN_BASE}/ghcnd-stations.txt").read_text())
    inventory = parse_prcp_inventory(_cached("ghcnd-inventory.txt", f"{GHCN_BASE}/ghcnd-inventory.txt").read_text())

    fields = load_field_definitions()
    if args.field:
        q = args.field.lower()
        fields = [f for f in fields if q in f["id"].lower() or q in f.get("field_name", "").lower()]

    report = []
    for field in fields:
        lat, lon = field["coordinates"]["lat"], field["coordinates"]["lon"]
        cands = candidate_stations(lat, lon, stations, inventory, start.year, end.year, args.max_km, args.candidates)
        chosen = best = None
        for st in cands:
            try:
                values = fetch_station_prcp(st["id"], start, end)
            except RuntimeError as exc:
                print(f"  {st['id']}: {exc}", file=sys.stderr)
                continue
            cov = coverage(values, start, end)
            if best is None or cov > best[2]:
                best = (st, values, cov)
            if cov >= args.min_coverage:
                chosen = (st, values, cov)
                break
            time.sleep(0.3)
        if chosen is None and best is not None and best[2] >= args.fallback_coverage:
            chosen = best
        if chosen:
            st, values, cov = chosen
            block = build_noaa_block(field, st, values, start, end, cov)
            target, removed = write_hist(field, block, values)
            line = f"{field['id']}: {st['id']} {st['name']} {st['distance_km']} km, coverage {cov:.1%}"
        else:
            values = {}
            target, removed = write_hist(field, None, values)
            line = f"{field['id']}: NO STATION with good coverage within {args.max_km} km"
        if removed:
            line += f" (merged duplicates: {', '.join(removed)})"
        print(line)
        report.append({
            "field_id": field["id"],
            "station_id": chosen[0]["id"] if chosen else None,
            "station_name": chosen[0]["name"] if chosen else None,
            "distance_km": chosen[0]["distance_km"] if chosen else None,
            "coverage": round(chosen[2], 4) if chosen else None,
            "file": target.name,
        })
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for r in report if r["station_id"])
    print(f"Done: {ok}/{len(report)} fields have NOAA history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Add NOAA rain observations around each documented motocross cancellation.

For every dated entry in data/motocross/tracks.json -> tracks[].history, fetch
GHCN-Daily precipitation for the race day and the three days before it at the
same station the track's NOAA climatology uses, and store it as
``noaa_observed``. Run by hand when history entries change:

    python scripts/enrich_mx_history.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rainout_agent.historical import load_historical_profile  # noqa: E402

API = "https://www.ncei.noaa.gov/access/services/data/v1"


def fetch(station: str, start: date, end: date) -> dict[str, float | None]:
    url = (f"{API}?dataset=daily-summaries&stations={station}&startDate={start}&endDate={end}"
           "&dataTypes=PRCP&units=standard&format=json")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "RainoutSource/1.0"}), timeout=60) as r:
        rows = json.loads(r.read().decode())
    return {row["DATE"]: (float(row["PRCP"]) if row.get("PRCP") not in (None, "") else None) for row in rows}


def main() -> None:
    path = ROOT / "data" / "motocross" / "tracks.json"
    doc = json.loads(path.read_text())
    for track in doc["tracks"]:
        noaa = (load_historical_profile(track["field_id"]) or {}).get("noaa_history") or {}
        station = noaa.get("station") or {}
        for entry in track["history"]:
            if not entry.get("date") or not station.get("id"):
                continue
            day = date.fromisoformat(entry["date"])
            obs = fetch(station["id"], day - timedelta(days=3), day)
            days = [{"date": (day - timedelta(days=i)).isoformat(), "prcp_in": obs.get((day - timedelta(days=i)).isoformat())} for i in (3, 2, 1, 0)]
            known = [d["prcp_in"] for d in days if d["prcp_in"] is not None]
            entry["noaa_observed"] = {
                "station_id": station["id"], "station_name": station.get("name"), "distance_km": station.get("distance_km"),
                "days": days, "four_day_total_in": round(sum(known), 2) if known else None,
                "note": "Daily totals at the nearest NOAA station; rain at the track itself can differ.",
            }
            print(track["short_id"], entry["date"], [d["prcp_in"] for d in days])
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

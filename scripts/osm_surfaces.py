"""Build data/osm/surfaces.json: the OpenStreetMap ``surface`` tag of each OSM venue's own pitch elements.

Reads the per-state GeoJSON-seq extracts made by scripts/osm_venues.py (``/workspace/shared/osm/states``) and
the venue rows in data/osm/<ST>.json. Only venues whose elements carry an explicit, consistent surface tag
are written; everything else stays "unknown" (never guessed).

    python scripts/osm_surfaces.py [--states TX,OK] [--src /workspace/shared/osm/states]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OSM_DIR = ROOT / "data" / "osm"
TURF = {"artificial_turf", "artificial_grass", "turf", "astroturf"}
DIRT = {"dirt", "clay", "ground", "earth", "compacted"}


def mapped(tag: str, kind: str) -> str | None:
    t = tag.strip().lower()
    if t in TURF:
        return "turf"
    if t == "grass":
        return "grass"
    if t in DIRT and kind == "ball_field":
        return "dirt_infield"
    return None


def build(states: list[str], src: Path) -> dict:
    out: dict[str, list[str]] = {}
    for st in states:
        path, geo = OSM_DIR / f"{st}.json", src / f"{st}.geojsonseq"
        if not path.exists() or not geo.exists():
            continue
        doc = json.loads(path.read_text())
        rows = [dict(zip(doc["row_fields"], r)) for r in doc["rows"]]
        want: dict[str, list[dict]] = {}
        for r in rows:
            for ref in str(r.get("osm") or "").split(";"):
                if ref:
                    want.setdefault(ref, []).append(r)
        tags: dict[str, str] = {}
        with geo.open() as fh:
            for line in fh:
                if '"surface"' not in line:
                    continue
                p = json.loads(line.lstrip("\x1e"))["properties"]
                ref = {"node": "n", "way": "w", "relation": "r"}.get(p.get("@type"), "?") + str(p.get("@id"))
                if ref in want and p.get("surface"):
                    tags[ref] = p["surface"]
        for r in rows:
            refs = [x for x in str(r.get("osm") or "").split(";") if x in tags]
            vals = {mapped(tags[x], {"b": "ball_field"}.get(r.get("kind"), r.get("kind") or "")) for x in refs}
            vals.discard(None)
            if len(vals) == 1:
                ref = refs[0]
                kind = {"n": "node", "w": "way", "r": "relation"}[ref[0]]
                out[r["id"]] = [vals.pop(), tags[ref], f"https://www.openstreetmap.org/{kind}/{ref[1:]}"]
    return {"note": "OpenStreetMap surface tags of each venue's own elements (ODbL). Only explicit, consistent tags; "
                    "value = profile surface, tag = raw OSM value, element = where it came from.",
            "row_fields": ["surface", "tag", "element"], "venues": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=None)
    ap.add_argument("--src", default="/workspace/shared/osm/states")
    a = ap.parse_args()
    states = a.states.split(",") if a.states else sorted(p.stem for p in OSM_DIR.glob("[A-Z][A-Z].json"))
    doc = build(states, Path(a.src))
    (OSM_DIR / "surfaces.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    from collections import Counter
    print(len(doc["venues"]), Counter(v[0] for v in doc["venues"].values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

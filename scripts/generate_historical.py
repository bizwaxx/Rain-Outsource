#!/usr/bin/env python3
"""Generate placeholder historical JSON for all fields without one yet."""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
HIST_DIR = DATA_DIR / "historical"
HIST_DIR.mkdir(exist_ok=True)

existing = {p.stem.replace("-historical", "") for p in HIST_DIR.glob("*.json")}

for field_file in DATA_DIR.rglob("*.json"):
    if "historical" in str(field_file):
        continue
    try:
        field = json.loads(field_file.read_text(encoding="utf-8"))
    except Exception:
        continue
    fid = field.get("id")
    if not fid or fid in existing:
        continue

    hist = {
        "field_id": fid,
        "historical_cancellations": [],
        "typical_cancellation_threshold_in": 1.5,
        "notes": "Placeholder - add real cancellation events with rain amounts"
    }
    out_name = fid.replace("austin-tx-", "").replace("tx-", "") + "-historical.json"
    (HIST_DIR / out_name).write_text(json.dumps(hist, indent=2), encoding="utf-8")
    print(f"Created {out_name}")

print("Done.")

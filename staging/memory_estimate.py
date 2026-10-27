"""Held-out estimate: what the per-venue drainage memory would have done if seeded with the known-outcome fixtures.

For every fixture j, nothing about j is used:
  1. the model parameters are the leave-one-out choice for j (fitted on the other fixtures, staging/calibrate.py);
  2. the venue's memory is built from the *other* fixtures at the same venue, each "snapshotted" with those
     parameters (its odds and soak penalty), exactly as rainout_agent.outcomes.memory_from_events does live;
  3. j is predicted with those parameters and that memory multiplier.

This is an estimate only: real memory comes from user reports, and most fixture venues have few events.

    python -m staging.memory_estimate --weather-cache /tmp/wxcache [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rainout_agent import event_risk as er  # noqa: E402
from rainout_agent import outcomes  # noqa: E402
from staging import backtest as bt  # noqa: E402
from staging import calibrate as cal  # noqa: E402


def venue_key(fx: dict) -> str:
    v = fx.get("track") or fx.get("venue") or fx["id"]
    return re.split(r"[,(]", v)[0].strip().lower()


def run_one(inp: dict, params: dict, memory: float = 1.0) -> dict:
    maintained = bool(inp.get("maintained"))
    drainage = er.effective_drainage(inp["drainage"], maintained, params)
    base = er.base_percent(inp["features"].get("window_pop") or 0, drainage, params)
    return er.adjust(base, inp["features"], drainage, params, inp["ball_sport"], maintained, memory)


def estimate(inputs: list[dict], venues: list[str], loo_points: list[dict]) -> dict:
    preds_without, preds_with, rows = [], [], []
    for j, inp in enumerate(inputs):
        params = cal.params_from(loo_points[j])
        events = []
        for i, other in enumerate(inputs):
            if i == j or venues[i] != venues[j]:
                continue
            o = run_one(other, params)
            events.append({"outcome": "played" if other["outcome"] == "played" else "not_played",
                           "prediction": {"play_probability_percent": o["play_probability_percent"],
                                          "soak_penalty": o["soak_penalty"], "official_status": "unknown"}})
        mem = outcomes.memory_from_events(events)
        p0 = run_one(inp, params)["play_probability_percent"]
        p1 = run_one(inp, params, mem["soak_multiplier"] if mem["active"] else 1.0)["play_probability_percent"]
        preds_without.append(p0)
        preds_with.append(p1)
        rows.append({"id": inp["id"], "venue": venues[j], "outcome": inp["outcome"], "p_without_memory": p0, "p_with_memory": p1,
                     "memory": {k: mem[k] for k in ("wet_events", "played", "expected_played", "soak_multiplier", "active")}})
    return {"without_memory": cal.score(inputs, preds_without), "with_memory": cal.score(inputs, preds_with),
            "changed": [r for r in rows if r["p_without_memory"] != r["p_with_memory"]],
            "venues_with_active_memory": sorted({r["venue"] for r in rows if r["memory"]["active"]}), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    from rainout_agent import motocross as mx

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weather-cache", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    fetch = cal.caching_fetch(a.weather_cache)
    tracks = {t["short_id"]: t for t in mx.tracks()}
    pairs = [(fx, cal.fixture_inputs(fx, fetch, tracks)) for fx in bt.load_fixtures()["fixtures"]]
    pairs = [(fx, i) for fx, i in pairs if i]
    inputs = [i for _, i in pairs]
    res_cal = cal.calibrate(inputs)
    res = estimate(inputs, [venue_key(fx) for fx, _ in pairs], res_cal["loo_points"])
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("without_memory", "with_memory", "changed", "venues_with_active_memory")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Calibrate rainout_agent.event_risk on the known-outcome fixtures, honestly.

* Features are computed once per fixture from the same free Open-Meteo archives the backtest uses.
* A small grid of explainable parameters is scored on: 3 x confident-wrong calls + directional misses.
* Reported: the in-sample score of the chosen parameters AND a leave-one-out score, where every fixture is
  predicted with parameters chosen on the other fixtures only. The leave-one-out number is the honest one.
* Ties go to the simplest grid point (rules off, factors closest to the defaults), so the data has to earn
  every rule.

Construction fixtures are scored by concrete.evaluate and are not part of this calibration.

    python -m staging.calibrate --weather-cache /tmp/wxcache [--json out.json]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rainout_agent import event_risk as er  # noqa: E402
from staging import backtest as bt  # noqa: E402

# Grid: first value of each axis is the "simplest" (used to break ties).
GRID: dict[str, tuple] = {
    "humble_cap": (None, 79),
    "humble_soak_in": (0.25, 0.15),
    "turf_floor": (None, 25),
    "gust_mph": (None, 45),
    "cold_f": (None, 35),
    "storm_cape": (None, 2500, 1500),
    "turf_rain_factor": (1.0, 0.7),
    "fast": (0.3, 0.15, 0.5),
    "medium": (0.6, 0.3, 1.0),
    "slow": (1.2, 0.8, 1.6),
    "w24": (0.6, 0.3),
    "w48": (0.3, 0.15),
}


def caching_fetch(cache_dir: str | None) -> Callable[[str], dict]:
    if not cache_dir:
        return bt.http_json
    os.makedirs(cache_dir, exist_ok=True)

    def fetch(url: str) -> dict:
        path = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".json")
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
        for attempt in range(3):
            try:
                data = bt.http_json(url)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return data
    return fetch


def fixture_inputs(fx: dict[str, Any], fetch: Callable[[str], dict], tracks: dict[str, Any]) -> dict[str, Any] | None:
    """Everything event_risk needs for one fixture, or None (construction / no weather)."""
    from rainout_agent import motocross as mx

    if fx["field_type"] == "construction":
        return None
    if fx["field_type"] == "motocross":
        track = tracks[fx["track"]]
        loc = mx._field(track)["coordinates"]
        lat, lon, tz = loc["lat"], loc["lon"], ZoneInfo("America/Chicago")
        start = datetime.fromisoformat(fx["start"]).replace(tzinfo=tz)
        duration = (datetime.fromisoformat(fx["end"]).replace(tzinfo=tz) - start).total_seconds() / 3600
        soil = mx.SOIL_FACTORS.get(track.get("soil") or "unknown", 1.0)
    else:
        lat, lon, tz = fx["lat"], fx["lon"], ZoneInfo(fx["tz"])
        start = datetime.fromisoformat(fx["start"]).replace(tzinfo=tz)
        duration, soil = float(fx.get("duration_h", 3)), 1.0
    day = start.date()
    wx = bt.fetch_weather(lat, lon, day - timedelta(days=4), day + timedelta(days=2), fetch)
    rows, grade = bt.pick_rows(wx, start - timedelta(hours=2), start + timedelta(hours=duration))
    if not rows:
        return None
    feat = er.features(wx.get("observed") or {}, rows, start, duration)
    for k in ("earlier_today_in", "prior_24h_in", "prior_48h_in"):
        if feat.get(k) is not None:
            feat[k] = round(feat[k] / soil, 3)   # sandy motocross soil drains faster
    drainage = er.drainage_class(field_type=fx["field_type"], maintained=bool(fx.get("maintained")))
    return {"id": fx["id"], "field_type": fx["field_type"], "outcome": fx["outcome"], "grade": grade, "features": feat,
            "drainage": drainage, "ball_sport": fx["field_type"] != "motocross"}


def params_from(point: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(json.dumps(er.PARAMS))
    p["drainage"] = {"fast": point["fast"], "medium": point["medium"], "slow": point["slow"]}
    p["soak_weights"] = (1.0, point["w24"], point["w48"])
    for k in ("humble_cap", "humble_soak_in", "turf_floor", "gust_mph", "cold_f", "storm_cape", "turf_rain_factor"):
        p[k] = point[k]
    return p


def predict(inp: dict[str, Any], params: dict[str, Any]) -> int:
    base = er.base_percent(inp["features"].get("window_pop") or 0, inp["drainage"], params)
    return er.adjust(base, inp["features"], inp["drainage"], params, inp["ball_sport"])["play_probability_percent"]


def loss(outcome: str, p: int) -> int:
    g = bt.grade(outcome, p)
    return 3 * int(g["confident_wrong"]) + int(g["directional_ok"] is False)


def score(inputs: list[dict[str, Any]], preds: list[int]) -> dict[str, Any]:
    gs = [bt.grade(i["outcome"], p) for i, p in zip(inputs, preds)]
    dirs = [g for g in gs if g["directional_ok"] is not None]
    return {"n": len(gs), "directional_accuracy": round(100 * sum(g["directional_ok"] for g in dirs) / len(dirs)) if dirs else None,
            "confident_wrong": sum(g["confident_wrong"] for g in gs),
            "band_accuracy": round(100 * sum(g["result"] == "correct" for g in gs) / len(gs)) if gs else None}


def calibrate(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    keys = list(GRID)
    points = [dict(zip(keys, vals)) for vals in itertools.product(*GRID.values())]
    preds = [[predict(i, params_from(pt)) for i in inputs] for pt in points]
    losses = [[loss(i["outcome"], p) for i, p in zip(inputs, row)] for row in preds]
    totals = [sum(row) for row in losses]
    best = min(range(len(points)), key=lambda k: (totals[k], k))
    loo_preds, loo_choice = [], []
    for j in range(len(inputs)):
        k = min(range(len(points)), key=lambda k: (totals[k] - losses[k][j], k))
        loo_preds.append(preds[k][j])
        loo_choice.append(k)
    default = params_from({**{k: v[0] for k, v in GRID.items()}})
    return {"chosen": points[best], "chosen_params": params_from(points[best]),
            "in_sample": score(inputs, preds[best]), "leave_one_out": score(inputs, loo_preds),
            "loo_distinct_choices": len(set(loo_choice)),
            "per_fixture": [{"id": i["id"], "outcome": i["outcome"], "grade": i["grade"], "p_in_sample": preds[best][n],
                             "p_leave_one_out": loo_preds[n], "drainage": i["drainage"]} for n, i in enumerate(inputs)],
            "grid_points": len(points), "simplest_point_in_sample": score(inputs, [predict(i, default) for i in inputs])}


def main(argv: list[str] | None = None) -> int:
    from rainout_agent import motocross as mx

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weather-cache", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    fetch = caching_fetch(args.weather_cache)
    tracks = {t["short_id"]: t for t in mx.tracks()}
    inputs = [x for x in (fixture_inputs(fx, fetch, tracks) for fx in bt.load_fixtures()["fixtures"]) if x]
    res = calibrate(inputs)
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("chosen", "in_sample", "leave_one_out", "loo_distinct_choices", "grid_points",
                                          "simplest_point_in_sample")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    # Venue maintenance: soak multiplier at maintained venues (None = older "maintained infield counts as medium").
    "maint": (None, 0.6, 0.35, 0.2),
    # Forecast-threat caps (event_risk.threat_signal): any threat the day before or day of -> not "confident".
    "threat_cap": (None, 79),
    "mx_forecast_cap": (None, 79),
}


# Ground-wetness family (7-day bucket minus FAO-56 ET0, by surface, motocross dry-out curve). The window
# family above stays in the search, so the wetness model has to beat it to be chosen.
COMMON = ("humble_cap", "humble_soak_in", "turf_floor", "gust_mph", "cold_f", "storm_cape", "turf_rain_factor", "maint",
          "threat_cap", "mx_forecast_cap")
GRID_WET: dict[str, tuple] = {
    **{k: GRID[k] for k in COMMON},
    "d_fast": (1.0, 0.5, 2.0),
    "d_medium": (0.1, 0.05, 0.2),
    "d_slow": (0.05, 0.02, 0.1),
    "d_mx": (0.08, 0.03, 0.15),
    "et_mult": (1.0, 0.6),
    "wet_scale": (1.0, 0.6, 1.5),
}


FAMILIES = ("window",)          # --families window,wetness,hybrid to also search the ground-wetness families


def grid_points(families: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    families = families or FAMILIES
    out = []
    if "window" in families:
        out += [{"soak_model": "window", **dict(zip(GRID, v))} for v in itertools.product(*GRID.values())]
    wet = [{"soak_model": "wetness", **dict(zip(GRID_WET, v))} for v in itertools.product(*GRID_WET.values())]
    if "wetness" in families:
        out += wet
    if "hybrid" in families:
        # Hybrid: the larger of the window soak (fixed at its calibrated drainage) and the wetness bucket.
        out += [{**pt, "soak_model": "hybrid", "fast": 0.15, "medium": 0.6, "slow": 1.6, "w24": 0.6, "w48": 0.3} for pt in wet]
    return out


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
    track = tracks.get(fx.get("track")) if fx["field_type"] == "motocross" else None
    if track:
        loc = mx._field(track)["coordinates"]
        lat, lon = loc["lat"], loc["lon"]
    else:
        lat, lon = fx["lat"], fx["lon"]
    day = datetime.fromisoformat(fx["start"]).date()
    wx = bt.fetch_weather(lat, lon, day - timedelta(days=4), day + timedelta(days=2), fetch)
    return bt.event_inputs(fx, wx, track)


def params_from(point: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(json.dumps(er.PARAMS))
    p["soak_model"] = point.get("soak_model", "window")
    if p["soak_model"] in ("wetness", "hybrid"):
        p["wet_drain_mm_h"] = {"fast": point["d_fast"], "medium": point["d_medium"], "slow": point["d_slow"], "mx": point["d_mx"]}
        p["wet_et_mult"], p["wet_scale"] = point["et_mult"], point["wet_scale"]
    if p["soak_model"] in ("window", "hybrid"):
        p["drainage"] = {"fast": point["fast"], "medium": point["medium"], "slow": point["slow"]}
        p["soak_weights"] = (1.0, point["w24"], point["w48"])
    for k in COMMON:
        if k == "maint":
            p["maint_soak_factor"] = point.get("maint")
        else:
            p[k] = point.get(k)
    return p


_WET_MEMO: dict[tuple, float | None] = {}


def predict(inp: dict[str, Any], params: dict[str, Any]) -> int:
    if params.get("soak_model") in ("wetness", "hybrid"):
        maintained = bool(inp.get("maintained"))
        drainage = er.effective_drainage(inp["drainage"], maintained, params)
        surface = drainage if inp["ball_sport"] else "mx"
        key = (inp["id"], surface, params["wet_drain_mm_h"][surface], params["wet_et_mult"])
        if key not in _WET_MEMO:
            _WET_MEMO[key] = er.wetness_in(inp.get("series"), surface, params)
        feat = {**inp["features"], "wetness_in": _WET_MEMO[key]}
        base = er.base_percent(feat.get("window_pop") or 0, drainage, params)
        return er.adjust(base, feat, drainage, params, inp["ball_sport"], maintained)["play_probability_percent"]
    return bt.score_event_risk(inp, params)["p"]


def loss(outcome: str, p: int) -> int:
    g = bt.grade(outcome, p)
    return 3 * int(g["confident_wrong"]) + int(g["directional_ok"] is False)


def score(inputs: list[dict[str, Any]], preds: list[int], split: bool = True) -> dict[str, Any]:
    out = _score(inputs, preds)
    if split:
        types = sorted({i["field_type"] for i in inputs})
        out["by_field_type"] = {t: _score([i for i in inputs if i["field_type"] == t],
                                          [p for i, p in zip(inputs, preds) if i["field_type"] == t]) for t in types}
    return out


def _score(inputs: list[dict[str, Any]], preds: list[int]) -> dict[str, Any]:
    gs = [bt.grade(i["outcome"], p) for i, p in zip(inputs, preds)]
    dirs = [g for g in gs if g["directional_ok"] is not None]
    return {"n": len(gs), "directional_accuracy": round(100 * sum(g["directional_ok"] for g in dirs) / len(dirs)) if dirs else None,
            "confident_wrong": sum(g["confident_wrong"] for g in gs),
            "confident_wrong_ids": [i["id"] for i, g in zip(inputs, gs) if g["confident_wrong"]],
            "band_accuracy": round(100 * sum(g["result"] == "correct" for g in gs) / len(gs)) if gs else None}


def calibrate(inputs: list[dict[str, Any]], families: tuple[str, ...] | None = None) -> dict[str, Any]:
    points = grid_points(families)
    preds = [[predict(i, prm) for i in inputs] for prm in (params_from(pt) for pt in points)]
    losses = [[loss(i["outcome"], p) for i, p in zip(inputs, row)] for row in preds]
    totals = [sum(row) for row in losses]
    best = min(range(len(points)), key=lambda k: (totals[k], k))
    loo_preds, loo_choice = [], []
    for j in range(len(inputs)):
        k = min(range(len(points)), key=lambda k: (totals[k] - losses[k][j], k))
        loo_preds.append(preds[k][j])
        loo_choice.append(k)
    default = params_from({**{k: v[0] for k, v in GRID.items()}})
    fam = lambda name: [k for k, pt in enumerate(points) if pt["soak_model"] == name]

    def family_loo(idx: list[int]) -> dict[str, Any]:
        best_f = min(idx, key=lambda k: (totals[k], k))
        lp = [preds[min(idx, key=lambda k: (totals[k] - losses[k][j], k))][j] for j in range(len(inputs))]
        return {"chosen": points[best_f], "in_sample": score(inputs, preds[best_f], split=False), "leave_one_out": score(inputs, lp)}
    return {"chosen": points[best], "chosen_params": params_from(points[best]),
            "in_sample": score(inputs, preds[best]), "leave_one_out": score(inputs, loo_preds),
            "loo_distinct_choices": len(set(loo_choice)), "loo_points": [points[k] for k in loo_choice],
            "per_fixture": [{"id": i["id"], "outcome": i["outcome"], "grade": i["grade"], "p_in_sample": preds[best][n],
                             "p_leave_one_out": loo_preds[n], "drainage": i["drainage"]} for n, i in enumerate(inputs)],
            "grid_points": len(points), "simplest_point_in_sample": score(inputs, [predict(i, default) for i in inputs]),
            "families": {f: family_loo(fam(f)) for f in (families or FAMILIES)},
            "maintenance": {str(m): family_loo([k for k, pt in enumerate(points) if pt.get("maint") == m])
                            for m in GRID["maint"]},
            "threat_caps": {f"threat_cap={a},mx_forecast_cap={b}": family_loo(
                [k for k, pt in enumerate(points) if pt.get("threat_cap") == a and pt.get("mx_forecast_cap") == b])
                for a in GRID["threat_cap"] for b in GRID["mx_forecast_cap"]}}


def main(argv: list[str] | None = None) -> int:
    from rainout_agent import motocross as mx

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weather-cache", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--families", default=",".join(FAMILIES))
    args = ap.parse_args(argv)
    fetch = caching_fetch(args.weather_cache)
    tracks = {t["short_id"]: t for t in mx.tracks()}
    inputs = [x for x in (fixture_inputs(fx, fetch, tracks) for fx in bt.load_fixtures()["fixtures"]) if x]
    res = calibrate(inputs, tuple(args.families.split(",")))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("chosen", "in_sample", "leave_one_out", "loo_distinct_choices", "grid_points",
                                          "simplest_point_in_sample", "families", "maintenance", "threat_caps")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

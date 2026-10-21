"""Nested leave-one-out for NWS alert caps (+ the college/pro soccer rain tolerance), streaming over settings.

Base grid as on #47 (caps fixed: threat 79, unstable-air 2500, cancel floor 21; every other axis re-chosen per fold).
Rule grid (first value = off): alert_warn_cap x alert_watch_cap x alert_tropical_cap x pro-rain. For each held-out
fixture the base point AND the rule setting are chosen on the other 174 only; ties go to the earlier setting (off)."""
import sys, pickle, json, itertools
import numpy as np
from multiprocessing import Pool
sys.path.insert(0, "/tmp/bt")
from staging import calibrate as cal, backtest as bt
from rainout_agent import event_risk as er, nws_alerts as na
pairs = pickle.load(open("/workspace/soccer-bt/pairs175.pkl", "rb"))
H = json.load(open("/workspace/soccer-bt/alerts/history.json"))
NAME = {"SV.W": "Severe Thunderstorm Warning", "TO.W": "Tornado Warning", "FF.W": "Flash Flood Warning", "TR.W": "Tropical Storm Warning",
        "HU.W": "Hurricane Warning", "SV.A": "Severe Thunderstorm Watch", "TO.A": "Tornado Watch", "FF.A": "Flash Flood Watch",
        "FA.A": "Flood Watch", "TR.A": "Tropical Storm Watch", "HU.A": "Hurricane Watch"}
def hist_features(h):
    if h.get("data") != "iem":
        return {"alert_warn": False, "alert_watch": False, "alert_tropical": False, "alert_names": [], "alert_data": h.get("data")}
    from datetime import datetime
    items = []
    for e in h["events"]:
        n = NAME.get(f'{e["phenomena"]}.{e["significance"]}')
        if n: items.append({"event": n, "kind": "warning" if e["significance"] == "W" else "watch", "onset": e["issue"], "ends": e["expire"]})
    f = na.features_from(items, datetime.fromisoformat(h["start"]))
    return {**f, "alert_data": "iem"}
inputs = []
for fx, i in pairs:
    i = dict(i); i["features"] = {**i["features"], **hist_features(H[fx["id"]])}
    i["rain_tolerant"] = er.rain_tolerant(fx.get("activity"), (i.get("venue_profile") or {}).get("level")); i["_activity"] = fx.get("activity")
    inputs.append(i)
points = [pt for pt in cal.grid_points() if (pt.get("threat_cap"), pt.get("mx_forecast_cap"), pt.get("unstable_cape"), pt.get("cancel_floor")) == (79, None, 2500, 21)
          and pt.get("status_ceiling") is None and pt.get("cold_wet_f") is None]
WARN = (None, 45, 21); WATCH = (None, 69, 50); TROP = (None, 45, 21); PRO = ((None, None), (0.5, 0.5), (0.0, 0.0))
SETTINGS = [(w, a, t, pr) for pr in PRO for w in WARN for a in WATCH for t in TROP]   # (off, off, off, off) first
flag = np.array([any(i["features"].get(k) for k in ("alert_warn", "alert_watch", "alert_tropical")) for i in inputs])
tol = np.array([i["rain_tolerant"] for i in inputs])
def cols_for(s):
    w, a, t, pr = SETTINGS[s]
    m = np.zeros(len(inputs), bool)
    if pr != (None, None): m |= tol
    if (w, a, t) != (None, None, None): m |= flag
    return np.where(m)[0]
def chunk(args):
    a, b, s, cols = args
    w, wa, t, (rf, sf) = SETTINGS[s]
    out = []
    for k in range(a, b):
        prm = cal.params_from(points[k])
        prm.update({"alert_warn_cap": w, "alert_watch_cap": wa, "alert_tropical_cap": t, "pro_rain_factor": rf, "pro_soak_factor": sf})
        out.append([cal.predict(inputs[j], prm) for j in cols])
    return np.array(out, dtype=np.int16)
if __name__ == "__main__":
    K, n = len(points), len(inputs)
    played = np.array([i["outcome"] == "played" for i in inputs])
    best_v = np.full(n, np.iinfo(np.int64).max); best_p = np.zeros(n, np.int16); best_s = np.zeros(n, int)
    ins_best = (np.iinfo(np.int64).max, None, None)
    with Pool(8) as pool:
        P0 = np.vstack(pool.map(chunk, [(a, min(a + 2000, K), 0, np.arange(n)) for a in range(0, K, 2000)]))
        before = None
        for s in range(len(SETTINGS)):
            if s == 0:
                P = P0
            else:
                cols = cols_for(s)
                P = P0.copy()
                P[:, cols] = np.vstack(pool.map(chunk, [(a, min(a + 2000, K), s, cols) for a in range(0, K, 2000)]))
            cw = (~played & (P >= bt.CONFIDENT_PLAY)) | (played & (P <= bt.CONFIDENT_CANCEL))
            L = (3 * cw + ((P >= 50) != played)).astype(np.int32); T = L.sum(1).astype(np.int64)
            v = T[:, None] - L                         # K x n: fold totals
            kb = np.argmin(v, axis=0); vb = v[kb, np.arange(n)]
            if s == 0:
                before = P[kb, np.arange(n)].copy()
            upd = vb < best_v
            best_v[upd] = vb[upd]; best_p[upd] = P[kb, np.arange(n)][upd]; best_s[upd] = s
            kt = int(np.argmin(T))
            if T[kt] < ins_best[0]: ins_best = (int(T[kt]), s, kt)
            print("setting", s, SETTINGS[s], "in-sample loss", int(T[kt]), flush=True)
    after = best_p
    acts = np.array([i["_activity"] for i in inputs])
    def sc(pred, m=None):
        ins = [i for j, i in enumerate(inputs) if m is None or m[j]]; pr = [int(p) for j, p in enumerate(pred) if m is None or m[j]]
        s = cal._score(ins, pr); return {"n": len(ins), "dir": s["directional_accuracy"], "cw": s["confident_wrong"], "cw_ids": s["confident_wrong_ids"], "band": s["band_accuracy"]}
    groups = {"all": None, "soccer": acts == "soccer", "baseball": acts == "baseball", "softball": acts == "softball", "motocross": acts == "motocross", "any_alert": flag}
    res = {"rule_settings": len(SETTINGS), "base_points": K, "combinations": len(SETTINGS) * K,
           "before": {g: sc(before, m) for g, m in groups.items()}, "after": {g: sc(after, m) for g, m in groups.items()},
           "fold_choices": {str(SETTINGS[s]): int((best_s == s).sum()) for s in sorted(set(best_s.tolist()))},
           "in_sample_best": {"loss": ins_best[0], "setting": SETTINGS[ins_best[1]], "point": points[ins_best[2]]},
           "conf_play": {"before": [int((before >= 80).sum()), int(((before >= 80) & played).sum())], "after": [int((after >= 80).sum()), int(((after >= 80) & played).sum())]},
           "changed": [{"id": inputs[j]["id"], "sport": acts[j], "outcome": inputs[j]["outcome"], "before": int(before[j]), "after": int(after[j]),
                        "setting": SETTINGS[best_s[j]], "alerts": inputs[j]["features"].get("alert_names")} for j in range(n) if before[j] != after[j]],
           "soccer": [{"id": inputs[j]["id"], "outcome": inputs[j]["outcome"], "before": int(before[j]), "after": int(after[j]), "setting": SETTINGS[best_s[j]],
                       "alerts": inputs[j]["features"].get("alert_names")} for j in range(n) if acts[j] == "soccer"],
           "two_cw": {i["id"]: i["features"].get("alert_names") for i in inputs if i["id"] in ("ut-baseball-2023-04-23", "cbb-bobcat-2019-02-19")}}
    json.dump(res, open("/workspace/soccer-bt/alerts/results.json", "w"), indent=1, default=str)
    for k in ("rule_settings", "base_points", "combinations", "fold_choices", "in_sample_best", "conf_play", "two_cw"): print(k, res[k])
    for g in groups: print(g, "before", res["before"][g], "| after", res["after"][g])
    for c in res["changed"]: print("changed", c)

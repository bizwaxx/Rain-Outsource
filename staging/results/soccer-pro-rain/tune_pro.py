"""Nested leave-one-out for the college/pro soccer rain-tolerance rule.

Base grid: same as the Oct 21 runs (caps fixed: threat 79, unstable-air 2500, cancel floor 21; no ceiling, no cold-wet),
every other axis re-chosen per fold. Rule grid: pro_rain_factor x pro_soak_factor (off first = simplest).
For each held-out fixture, BOTH the base point and the rule setting are chosen on the other 174 only."""
import sys, pickle, json, itertools
import numpy as np
from multiprocessing import Pool
sys.path.insert(0, "/tmp/bt")
from staging import calibrate as cal, backtest as bt
from rainout_agent import event_risk as er
pairs = pickle.load(open("/workspace/soccer-bt/pairs175.pkl", "rb"))
inputs = []
for fx, i in pairs:
    i = dict(i); i["rain_tolerant"] = er.rain_tolerant(fx.get("activity"), (i.get("venue_profile") or {}).get("level"))
    i["_activity"] = fx.get("activity"); inputs.append(i)
NEWIDS = {f["id"] for f in json.load(open("/workspace/shared/rain-outsource/fixtures-staged/soccer-2026-10-21.json"))["fixtures"]}
points = [pt for pt in cal.grid_points() if (pt.get("threat_cap"), pt.get("mx_forecast_cap"), pt.get("unstable_cape"), pt.get("cancel_floor")) == (79, None, 2500, 21)
          and pt.get("status_ceiling") is None and pt.get("cold_wet_f") is None]
RF = (None, 0.7, 0.5, 0.3, 0.0); SF = (None, 0.5, 0.2, 0.0)
SETTINGS = list(itertools.product(RF, SF))          # (None, None) first = rule off
tol = np.array([i["rain_tolerant"] for i in inputs]); TI = np.where(tol)[0]
def chunk(args):
    a, b, s = args
    rf, sf = SETTINGS[s]
    cols = range(len(inputs)) if s == 0 else TI
    out = []
    for k in range(a, b):
        prm = cal.params_from(points[k]); prm["pro_rain_factor"], prm["pro_soak_factor"] = rf, sf
        out.append([cal.predict(inputs[j], prm) for j in cols])
    return np.array(out, dtype=np.int16)
if __name__ == "__main__":
    K, n, S = len(points), len(inputs), len(SETTINGS)
    with Pool(8) as pool:
        P0 = np.vstack(pool.map(chunk, [(a, min(a + 2000, K), 0) for a in range(0, K, 2000)]))
        P = np.repeat(P0[None], S, axis=0)
        for s in range(1, S):
            P[s][:, TI] = np.vstack(pool.map(chunk, [(a, min(a + 2000, K), s) for a in range(0, K, 2000)]))
    played = np.array([i["outcome"] == "played" for i in inputs])
    cw = (~played & (P >= bt.CONFIDENT_PLAY)) | (played & (P <= bt.CONFIDENT_CANCEL))
    L = (3 * cw + ((P >= 50) != played)).astype(np.int32)       # S x K x n
    tot = L.sum(2)                                                # S x K
    flat = tot.reshape(-1)                                         # index = s*K + k (s=0 first -> ties prefer rule off)
    Lf = L.reshape(S * K, n)
    def loo(allowed_s):
        idx = np.concatenate([np.arange(s * K, (s + 1) * K) for s in allowed_s])
        sub = flat[idx][:, None] - Lf[idx]                         # (|idx| x n)
        pick = idx[np.argmin(sub, axis=0)]
        return Lf.shape and P.reshape(S * K, n)[pick, np.arange(n)], pick // K
    before, _ = loo([0]); after, chosen_s = loo(range(S))
    acts = np.array([i["_activity"] for i in inputs]); new = np.array([i["id"] in NEWIDS for i in inputs])
    def sc(pred, m=None):
        ins = [i for j, i in enumerate(inputs) if m is None or m[j]]; pr = [int(p) for j, p in enumerate(pred) if m is None or m[j]]
        s = cal._score(ins, pr); return {"n": len(ins), "dir": s["directional_accuracy"], "cw": s["confident_wrong"], "cw_ids": s["confident_wrong_ids"], "band": s["band_accuracy"]}
    groups = {"all": None, "soccer": acts == "soccer", "pro_soccer(tolerant)": tol, "new21": new, "baseball": acts == "baseball",
              "softball": acts == "softball", "motocross": acts == "motocross"}
    res = {"settings_rule": [list(x) for x in SETTINGS], "base_points": K, "total_combinations": S * K,
           "before": {g: sc(before, m) for g, m in groups.items()}, "after": {g: sc(after, m) for g, m in groups.items()},
           "fold_choices": {str(SETTINGS[s]): int((chosen_s == s).sum()) for s in set(chosen_s.tolist())},
           "conf_play_before": [int((before >= 80).sum()), int(((before >= 80) & played).sum())],
           "conf_play_after": [int((after >= 80).sum()), int(((after >= 80) & played).sum())],
           "in_sample_best": {"setting": SETTINGS[int(np.argmin(flat)) // K], "point": points[int(np.argmin(flat)) % K]},
           "per_soccer": [{"id": inputs[j]["id"], "outcome": inputs[j]["outcome"], "before": int(before[j]), "after": int(after[j]),
                           "fold_setting": SETTINGS[chosen_s[j]]} for j in range(n) if acts[j] == "soccer"],
           "changed_non_soccer": [{"id": inputs[j]["id"], "before": int(before[j]), "after": int(after[j])} for j in range(n) if acts[j] != "soccer" and before[j] != after[j]]}
    json.dump(res, open("/workspace/soccer-bt/tune/results.json", "w"), indent=1, default=str)
    for k in ("base_points", "total_combinations", "fold_choices", "conf_play_before", "conf_play_after", "in_sample_best"): print(k, res[k])
    for g in groups: print(g, "before", res["before"][g], "| after", res["after"][g])
    print("changed non-soccer:", res["changed_non_soccer"])
    for r in res["per_soccer"]: print(r)

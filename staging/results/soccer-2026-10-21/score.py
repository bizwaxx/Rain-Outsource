"""Held-out (leave-one-out) score with the adopted default caps fixed (threat 79, unstable-air 2500, cancel floor 21;
no status ceiling, no cold-wet rule), every other axis re-chosen per fold. Same method as /workspace/retest/tune2.py."""
import sys, pickle, json
import numpy as np
from multiprocessing import Pool
sys.path.insert(0, "/tmp/bt")
from staging import calibrate as cal, backtest as bt
pairs = pickle.load(open("/workspace/soccer-bt/pairs175.pkl", "rb"))
NEWIDS = {f["id"] for f in json.load(open("/workspace/shared/rain-outsource/fixtures-staged/soccer-2026-10-21.json"))["fixtures"]}
points = [pt for pt in cal.grid_points() if (pt.get("threat_cap"), pt.get("mx_forecast_cap"), pt.get("unstable_cape"), pt.get("cancel_floor")) == (79, None, 2500, 21)
          and pt.get("status_ceiling") is None and pt.get("cold_wet_f") is None]
INPUTS = []
def chunk(r):
    return np.array([[cal.predict(i, cal.params_from(points[k])) for i in INPUTS] for k in range(*r)], dtype=np.int16)
def run(sel, label):
    inputs = [i for (fx, i), s in zip(pairs, sel) if s]
    acts = [fx["activity"] for (fx, i), s in zip(pairs, sel) if s]
    global INPUTS; INPUTS = inputs
    K = len(points)
    with Pool(8) as pool:
        P = np.vstack(pool.map(chunk, [(a, min(a + 2000, K)) for a in range(0, K, 2000)]))
    played = np.array([i["outcome"] == "played" for i in inputs])
    cw = (~played & (P >= bt.CONFIDENT_PLAY)) | (played & (P <= bt.CONFIDENT_CANCEL))
    L = (3 * cw + ((P >= 50) != played)).astype(np.int32); tot = L.sum(1)
    kin = int(np.argmin(tot)); kj = np.argmin(tot[:, None] - L, axis=0); lp = P[kj, np.arange(len(inputs))]
    def sc(pred, m=None):
        ins = [i for n, i in enumerate(inputs) if m is None or m[n]]; pr = [int(p) for n, p in enumerate(pred) if m is None or m[n]]
        s = cal._score(ins, pr); return {"n": len(ins), "dir": s["directional_accuracy"], "cw": s["confident_wrong"], "cw_ids": s["confident_wrong_ids"], "band": s["band_accuracy"]}
    soccer = np.array([a == "soccer" for a in acts]); new = np.array([i["id"] in NEWIDS for i in inputs])
    conf = lp >= 80; confright = int((conf & played).sum())
    out = {"label": label, "loo_all": sc(lp), "in_sample": sc(P[kin]), "loo_soccer": sc(lp, soccer), "loo_new21": sc(lp, new) if new.any() else None,
           "loo_old": sc(lp, ~new), "confident_play_calls": int(conf.sum()), "confident_play_right": confright,
           "per_new": [{"id": i["id"], "outcome": i["outcome"], "p_loo": int(lp[n]), "p_in": int(P[kin][n])} for n, i in enumerate(inputs) if soccer[n] or new[n]]}
    print(json.dumps({k: v for k, v in out.items() if k != "per_new"}), flush=True)
    return out
if __name__ == "__main__":
    base = run([fx["id"] not in NEWIDS for fx, _ in pairs], "154 baseline (reproduce)")
    full = run([True] * len(pairs), "175 with soccer")
    json.dump({"baseline": base, "full": full}, open("/workspace/soccer-bt/results.json", "w"), indent=1, default=str)

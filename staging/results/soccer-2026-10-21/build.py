import sys, pickle, time, json, os, hashlib
sys.path.insert(0, "/tmp/bt")
from staging import calibrate as cal, backtest as bt
from rainout_agent import motocross as mx
NEG="/workspace/soccer-bt/wxcache/_neg.json"
neg=set(json.load(open(NEG))) if os.path.exists(NEG) else set()
misses=[]
base=cal.caching_fetch("/workspace/soccer-bt/wxcache")
def fetch(url):
    p=os.path.join("/workspace/soccer-bt/wxcache", hashlib.sha1(url.encode()).hexdigest()+".json")
    if os.path.exists(p): return base(url)
    if url in neg: raise RuntimeError("known fetch failure (negative cache)")
    misses.append(url); print("MISS", url[:110], flush=True)
    try:
        return bt.http_json(url, timeout=20) and base(url)
    except Exception:
        neg.add(url); json.dump(sorted(neg), open(NEG,"w")); raise
t=time.time()
tracks={t_["short_id"]: t_ for t_ in mx.tracks()}
pairs=[]
for fx in bt.load_fixtures()["fixtures"]:
    i=cal.fixture_inputs(fx, fetch, tracks)
    if i: pairs.append((fx,i))
print(len(pairs), "inputs", round(time.time()-t,1), "s; misses", len(misses), "neg", len(neg))
pickle.dump(pairs, open("/workspace/soccer-bt/pairs175.pkl","wb"))

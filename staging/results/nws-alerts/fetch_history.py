"""Which NWS watches/warnings covered each backtest event, from the free Iowa Environmental Mesonet VTEC archive
(json/vtec_events_bypoint.py: storm-based polygons plus county/zone UGCs containing the point). Cached on disk."""
import sys, json, os, hashlib, time, urllib.request, pickle
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, "/tmp/bt")
from rainout_agent import motocross as mx
CACHE = "/workspace/soccer-bt/alerts/cache"
UA = "rainout-research/1.0 (+https://rainout-agent-source.vercel.app; backtest, cached)"
def get(url):
    p = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if os.path.exists(p): return json.load(open(p))
    for a in range(4):
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60)); break
        except Exception as e:
            if a == 3: raise
            time.sleep(3 * (a + 1))
    json.dump(d, open(p, "w")); time.sleep(0.3); return d
fixtures = json.load(open("/tmp/bt/staging/fixtures/known_outcomes.json"))["fixtures"]
tracks = {t["short_id"]: t for t in mx.tracks()}
out = {}
for fx in fixtures:
    if fx["field_type"] == "construction": continue
    if fx["field_type"] == "motocross":
        c = mx._field(tracks[fx["track"]])["coordinates"]; lat, lon, tz = c["lat"], c["lon"], ZoneInfo("America/Chicago")
        start = datetime.fromisoformat(fx["start"]).replace(tzinfo=tz); end = datetime.fromisoformat(fx["end"]).replace(tzinfo=tz)
    else:
        lat, lon, tz = fx["lat"], fx["lon"], ZoneInfo(fx["tz"])
        start = datetime.fromisoformat(fx["start"]).replace(tzinfo=tz); end = start + timedelta(hours=float(fx.get("duration_h", 3)))
    us = 18 <= lat <= 72 and -180 <= lon <= -64 and not (lat > 49.5 and lon > -123) and tz.key.startswith(("America/", "Pacific/Honolulu"))
    if not us:
        out[fx["id"]] = {"data": "no-data (non-US)"}; continue
    d0 = (start - timedelta(days=2)).date(); d1 = (end + timedelta(days=1)).date()
    url = f"https://mesonet.agron.iastate.edu/json/vtec_events_bypoint.py?lat={lat:.4f}&lon={lon:.4f}&sdate={d0}&edate={d1}"
    try:
        ev = get(url).get("events", [])
        out[fx["id"]] = {"data": "iem", "url": url, "start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(), "events": ev}
    except Exception as e:
        out[fx["id"]] = {"data": f"fetch failed: {e}"}
    print(fx["id"], out[fx["id"]]["data"], len(out[fx["id"]].get("events", [])), flush=True)
json.dump(out, open("/workspace/soccer-bt/alerts/history.json", "w"), indent=0)

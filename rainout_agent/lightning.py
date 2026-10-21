"""Advisory lightning near a venue: NOAA GOES-R GLM flashes + NWS active alerts.

Not a safety device. Follow the official call and your league's lightning policy. When thunder roars, go indoors.

How it works:
* GLM (Geostationary Lightning Mapper) flashes from NOAA's public AWS Open Data buckets (NODD):
  GOES-East (GOES-19, ``noaa-goes19``) or GOES-West (GOES-18, ``noaa-goes18``), whichever sees the venue best.
  One file covers about 20 s, so the last 30 minutes is about 90 files (about 15-25 MB). Each file is read once with the
  pure-Python reader in :mod:`rainout_agent.glm`, and the US flashes are kept in 5-minute buckets in the shared cache.
  A cold request downloads at most what's missing, within a time budget. ``/v1/lightning/ingest`` (pinger or cron)
  keeps the buckets warm so user requests read the cache only.
* NWS active alerts for the point (api.weather.gov): Severe Thunderstorm and Tornado Warnings, plus Special Weather
  Statements that mention lightning.

The 30-minute rule (NWS / NFHS): every flash within the radius (10 mi by default; 8 mi is a common league
choice) restarts a 30-minute clock. ``all_clear_at`` is the last such flash + 30 minutes.

lightning_status:
  active  - a flash within the radius in the last 30 min, or a Severe Thunderstorm / Tornado Warning covers the point
  nearby  - flashes within 30 mi (but not the radius), or a Special Weather Statement mentions lightning
  clear   - the full 30-minute GLM window was read, with no flashes within 30 mi and no warnings
  unknown - GLM data missing, late or partial, and nothing above was seen. Never reported as clear.
"""
from __future__ import annotations

import concurrent.futures as cf
import math
import re
import threading
import time as _time
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from rainout_agent import glm
from rainout_agent.cache import Cache, MemoryCache, get_cache

ADVISORY = "Not a safety device. Follow the official call and your league's lightning policy. When thunder roars, go indoors."
STATUSES = ("clear", "nearby", "active", "unknown")
WINDOW_MIN = 30
RADIUS_MI = 10.0          # NWS "if you can hear thunder" distance; restarts the 30-minute clock
NFHS_MI = 8.0             # reported too; many league policies use 8 mi
NEARBY_MI = 30.0
MIN_RADIUS_MI, MAX_RADIUS_MI = 3.0, 15.0
MAX_DATA_LAG_S = 240      # newest GLM file must end within 4 min of now (files land about 20-60 s after their end)
MAX_GAP_S = 45            # a gap between files longer than this means a missing file -> partial coverage
FETCH_BUDGET_S = 7.0
FETCH_WORKERS = 16
FILE_MAX_BYTES = 4_000_000
LIST_TTL_S = 20
BUCKET_S = 300
BUCKET_TTL_S = 45 * 60
# Flashes kept in the shared cache: North America and nearby waters only (keeps entries small).
KEEP_BOX = (10.0, 62.0, -170.0, -50.0)   # lat_min, lat_max, lon_min, lon_max
SAT_NAMES = {"east": "GOES-East (GOES-19)", "west": "GOES-West (GOES-18)"}
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_WARNINGS = {"Severe Thunderstorm Warning", "Tornado Warning"}
NWS_STATEMENTS = {"Special Weather Statement", "Significant Weather Advisory"}
_LIGHTNING_RE = re.compile(r"\blightning\b", re.I)
CAVEATS = [
    "GLM detects most but not all flashes (roughly 70-90%), and its locations are approximate (about 5-10 km).",
    "Satellite data arrives about 20-60 seconds late; a flash in the last minute may not be counted yet.",
    "If you hear thunder or see lightning, you are close enough to be struck, whatever this says.",
]

_LOCAL_STORE = MemoryCache(max_entries=256)
_MEMO: "OrderedDict[str, list]" = OrderedDict()
_MEMO_MAX = 400
_MEMO_LOCK = threading.Lock()
_LIST_MEMO: dict[tuple[str, str], tuple[float, list[str]]] = {}


class LightningError(ValueError):
    pass


def _store() -> Cache:
    cache = get_cache()
    return _LOCAL_STORE if cache.name in {"memory", "none"} else cache


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


# --- GLM source (public S3 over HTTPS, fixed hosts) --------------------------------------------------

class S3GLMSource:
    """Reads the public NOAA buckets anonymously. Hosts are fixed (no user-supplied URLs)."""

    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout

    def _url(self, sat: str) -> str:
        return f"https://{glm.BUCKETS[sat]}.s3.amazonaws.com/"

    def list_keys(self, sat: str, start: datetime, end: datetime) -> list[str]:
        keys: list[str] = []
        hours, h = [], start.replace(minute=0, second=0, microsecond=0)
        while h <= end:
            hours.append(h)
            h += timedelta(hours=1)
        for hour in hours:
            prefix = glm.hour_prefix(hour)
            memo = _LIST_MEMO.get((sat, prefix))
            if memo and _time.time() - memo[0] < LIST_TTL_S:
                keys += memo[1]
                continue
            found, token = [], None
            for _ in range(5):
                q = {"list-type": "2", "prefix": prefix}
                if token:
                    q["continuation-token"] = token
                with urllib.request.urlopen(self._url(sat) + "?" + urllib.parse.urlencode(q), timeout=self.timeout) as r:
                    xml = r.read(2_000_000).decode("utf-8", "replace")
                found += re.findall(r"<Key>([^<]+)</Key>", xml)
                m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
                if not m:
                    break
                token = m.group(1)
            _LIST_MEMO[(sat, prefix)] = (_time.time(), found)
            keys += found
        return keys

    def get(self, sat: str, key: str) -> bytes:
        if not key.startswith(glm.PRODUCT + "/") or ".." in key:
            raise LightningError("bad key")
        with urllib.request.urlopen(self._url(sat) + urllib.parse.quote(key), timeout=self.timeout) as r:
            data = r.read(FILE_MAX_BYTES + 1)
        if len(data) > FILE_MAX_BYTES:
            raise LightningError("GLM file too large")
        return data


def _bucket_key(sat: str, end: datetime) -> str:
    return f"glm:{sat}:{int(end.timestamp()) // BUCKET_S}"


def _keep(flashes: Iterable[tuple[float, float]]) -> list[list[float]]:
    a, b, c, d = KEEP_BOX
    return [[round(la, 3), round(lo, 3)] for la, lo in flashes if a <= la <= b and c <= lo <= d]


def glm_window(sat: str, now: datetime, *, source: Any = None, store: Cache | None = None,
               budget_s: float = FETCH_BUDGET_S, window_min: int = WINDOW_MIN,
               clock: Callable[[], float] = _time.monotonic) -> dict[str, Any]:
    """All kept flashes for the window ending at ``now`` from one satellite, plus honest coverage info."""
    source = source or S3GLMSource()
    store = store or _store()
    start = now - timedelta(minutes=window_min)
    t0 = clock()
    try:
        listed = source.list_keys(sat, start - timedelta(minutes=1), now)
    except Exception as exc:
        return {"satellite": sat, "status": "unavailable", "error": f"listing failed: {type(exc).__name__}", "files": {}, "listed": 0}
    keys: dict[str, tuple[datetime, datetime]] = {}
    for k in listed:
        try:
            s, e = glm.file_times(k)
        except glm.GLMFormatError:
            continue
        if e >= start and s <= now:
            keys[k] = (s, e)
    files: dict[str, list] = {}
    buckets: dict[str, dict] = {}
    for k, (_s, e) in keys.items():
        with _MEMO_LOCK:
            if k in _MEMO:
                files[k] = _MEMO[k]
                continue
        bk = _bucket_key(sat, e)
        if bk not in buckets:
            got = store.get(bk)
            buckets[bk] = dict(got) if isinstance(got, dict) else {}
        if k in buckets[bk]:
            files[k] = buckets[bk][k]
    missing = [k for k in sorted(keys, key=lambda x: keys[x][1], reverse=True) if k not in files]
    errors = 0
    fetched: dict[str, list] = {}
    if missing:
        left = budget_s - (clock() - t0)
        if left > 0.2:
            ex = cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS)
            futs = {ex.submit(lambda kk: _keep(glm.read_flashes(source.get(sat, kk))), k): k for k in missing}
            try:
                for f in cf.as_completed(futs, timeout=left):
                    try:
                        fetched[futs[f]] = f.result()
                    except Exception:
                        errors += 1
            except cf.TimeoutError:
                pass
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
    if fetched:
        changed: set[str] = set()
        for k, fl in fetched.items():
            files[k] = fl
            bk = _bucket_key(sat, keys[k][1])
            buckets.setdefault(bk, {})[k] = fl
            changed.add(bk)
        for bk in changed:
            store.set(bk, buckets[bk], BUCKET_TTL_S)
    with _MEMO_LOCK:
        for k, fl in files.items():
            _MEMO[k] = fl
            _MEMO.move_to_end(k)
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.popitem(last=False)
    # coverage: contiguous files from the window start to (nearly) now
    spans = sorted(keys[k] for k in files)
    newest = spans[-1][1] if spans else None
    full = bool(spans) and spans[0][0] <= start + timedelta(seconds=MAX_GAP_S) and \
        (now - newest).total_seconds() <= MAX_DATA_LAG_S and \
        all((b[0] - a[1]).total_seconds() <= MAX_GAP_S for a, b in zip(spans, spans[1:]))
    status = "ok" if full else ("partial" if files else "unavailable")
    return {"satellite": sat, "status": status, "listed": len(keys), "read": len(files), "fetched": len(fetched),
            "errors": errors, "newest_end": newest, "files": {k: (keys[k][1], files[k]) for k in files}}


# --- NWS alerts -----------------------------------------------------------------------------------

def _nws_get(lat: float, lon: float, timeout: float = 5.0) -> dict[str, Any]:
    from rainout_agent import weather

    return weather._http_get_json(f"{NWS_ALERTS_URL}?point={lat:.4f},{lon:.4f}", weather.NWS_HEADERS, timeout)


def nws_lightning_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in (payload or {}).get("features") or []:
        p = f.get("properties") or {}
        ev = p.get("event") or ""
        text = " ".join(str(p.get(k) or "") for k in ("headline", "description", "instruction"))
        if ev in NWS_WARNINGS:
            level = "active"
        elif ev in NWS_STATEMENTS and _LIGHTNING_RE.search(text):
            level = "nearby"
        else:
            continue
        out.append({"event": ev, "level": level, "headline": p.get("headline"), "severity": p.get("severity"),
                    "onset": p.get("onset") or p.get("effective"), "ends": p.get("ends") or p.get("expires"),
                    "sender": p.get("senderName"), "url": p.get("@id") or f.get("id")})
    return out


# --- the answer -----------------------------------------------------------------------------------

def _num(v: Any, name: str, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise LightningError(f"{name} must be a number.") from None
    if not (lo <= x <= hi) or math.isnan(x):
        raise LightningError(f"{name} must be between {lo:g} and {hi:g}.")
    return x


def check_lightning(lat: Any, lon: Any, *, radius_mi: Any = None, now: datetime | None = None,
                    glm_source: Any = None, nws_fetch: Callable[[float, float], dict] | None = None,
                    store: Cache | None = None, budget_s: float = FETCH_BUDGET_S, label: str | None = None) -> dict[str, Any]:
    lat_f = _num(lat, "lat", -90, 90)
    lon_f = _num(lon, "lon", -180, 180)
    radius = RADIUS_MI if radius_mi in (None, "") else _num(radius_mi, "radius_mi", MIN_RADIUS_MI, MAX_RADIUS_MI)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    sat = glm.satellite_for(lon_f)
    nws_fetch = nws_fetch or _nws_get
    a, b, c, d = KEEP_BOX
    in_box = a <= lat_f <= b and c <= lon_f <= d

    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        nws_future = ex.submit(nws_fetch, lat_f, lon_f)
        if in_box:
            win = glm_window(sat, now, source=glm_source, store=store, budget_s=budget_s)
        else:
            win = {"satellite": sat, "status": "unavailable", "error": "outside the covered area (North America)", "files": {}, "listed": 0}
        try:
            nws_alerts = nws_lightning_alerts(nws_future.result(timeout=max(0.5, budget_s)))
            nws_status = "ok"
        except Exception:
            nws_alerts, nws_status = [], "unavailable"

    start = now - timedelta(minutes=WINDOW_MIN)
    near: list[tuple[datetime, float]] = []
    for _k, (t, fl) in win["files"].items():
        if t < start:
            continue
        for la, lo in fl:
            if abs(la - lat_f) > 0.6 or abs(lo - lon_f) > 0.8:
                continue
            dist = miles(lat_f, lon_f, la, lo)
            if dist <= NEARBY_MI:
                near.append((t, dist))
    within = [x for x in near if x[1] <= radius]
    counts = {"within_radius": len(within), "within_8_mi": sum(1 for x in near if x[1] <= NFHS_MI),
              "within_10_mi": sum(1 for x in near if x[1] <= 10), "within_30_mi": len(near)}
    last = max(near, key=lambda x: (x[0], -x[1])) if near else None
    nearest = min(near, key=lambda x: (x[1], -x[0].timestamp())) if near else None
    last_in = max(within, key=lambda x: x[0]) if within else None

    warning = next((x for x in nws_alerts if x["level"] == "active"), None)
    statement = next((x for x in nws_alerts if x["level"] == "nearby"), None)
    all_clear = last_in[0] + timedelta(minutes=WINDOW_MIN) if last_in else None
    if within:
        status = "active"
        reason = (f"Lightning {last_in[1]:.0f} mi away at {_iso(last_in[0])}, within {radius:g} mi in the last 30 minutes. "
                  f"Stay off the field until at least {_iso(all_clear)}; any new flash within {radius:g} mi restarts the clock.")
    elif warning:
        status = "active"
        reason = f"NWS {warning['event']} in effect for this spot (until {warning['ends']}). Stay indoors."
    elif near:
        status = "nearby"
        reason = f"Lightning within {NEARBY_MI:g} mi (nearest {nearest[1]:.0f} mi) in the last 30 minutes, none within {radius:g} mi yet. Be ready to clear the field."
    elif statement:
        status = "nearby"
        reason = f"NWS {statement['event']} mentions lightning for this area."
    elif win["status"] == "ok":
        status = "clear"
        reason = f"No lightning detected within {NEARBY_MI:g} mi in the last 30 minutes."
    else:
        status = "unknown"
        reason = "Satellite lightning data is missing or late right now, so I can't say it's clear. Use your eyes and ears and the official call."
    if status == "active" and warning and within:
        reason += f" NWS {warning['event']} also in effect."
    if warning and all_clear is None:
        all_clear = None  # a warning has no flash clock; wait for it to end and then 30 minutes without lightning

    data_age = (now - win["newest_end"]).total_seconds() if win.get("newest_end") else None
    return {
        "lightning_status": status,
        "advisory": ADVISORY,
        "advisory_only": True,
        "reason": reason,
        "checked_at": _iso(now),
        "location": {"lat": lat_f, "lon": lon_f, **({"label": label} if label else {})},
        "radius_mi": radius,
        "nearby_mi": NEARBY_MI,
        "window_minutes": WINDOW_MIN,
        "flashes": counts,
        "last_flash": {"time": _iso(last[0]), "distance_mi": round(last[1], 1)} if last else None,
        "nearest_flash": {"time": _iso(nearest[0]), "distance_mi": round(nearest[1], 1)} if nearest else None,
        "all_clear_at": _iso(all_clear),
        "rule": "30-minute rule (NWS/NFHS): wait 30 minutes after the last flash within the radius; each new flash restarts the clock.",
        "glm": {"satellite": SAT_NAMES[sat], "status": win["status"], "files_in_window": win.get("listed", 0),
                "files_read": win.get("read", 0), "newest_data_at": _iso(win.get("newest_end")),
                "data_age_seconds": int(data_age) if data_age is not None else None,
                **({"error": win["error"]} if win.get("error") else {})},
        "nws": {"status": nws_status, "alerts": nws_alerts},
        "sources": [
            {"name": f"NOAA GOES-R Geostationary Lightning Mapper, {SAT_NAMES[sat]}", "url": f"https://{glm.BUCKETS[sat]}.s3.amazonaws.com/index.html#{glm.PRODUCT}/",
             "used_for": "lightning flashes (AWS Open Data / NOAA NODD)"},
            {"name": "National Weather Service active alerts", "url": f"{NWS_ALERTS_URL}?point={lat_f:.4f},{lon_f:.4f}",
             "used_for": "Severe Thunderstorm / Tornado Warnings, Special Weather Statements"},
        ],
        "caveats": CAVEATS,
    }


def ingest(now: datetime | None = None, sats: Iterable[str] = ("east", "west"), budget_s: float = 20.0,
           source: Any = None, store: Cache | None = None) -> dict[str, Any]:
    """Warm the shared GLM buckets (called by a pinger/cron); safe to call every minute."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out = {}
    per = budget_s / max(1, len(tuple(sats)))
    for sat in sats:
        w = glm_window(sat, now, source=source, store=store, budget_s=per)
        out[sat] = {k: w.get(k) for k in ("status", "listed", "read", "fetched", "errors")}
        out[sat]["newest_data_at"] = _iso(w.get("newest_end"))
    return {"ingested_at": _iso(now), "satellites": out}


# --- overrides for play answers -------------------------------------------------------------------

NEAR_GAME_BEFORE_H = 3.0   # check lightning only when the game starts within 3 h ...
NEAR_GAME_AFTER_H = 4.0    # ... or started less than 4 h ago


def relevant(start: datetime, now: datetime) -> bool:
    h = (start - now).total_seconds() / 3600
    return -NEAR_GAME_AFTER_H <= h <= NEAR_GAME_BEFORE_H


def summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("lightning_status", "reason", "last_flash", "all_clear_at", "radius_mi", "flashes", "advisory", "checked_at")
    out = {k: result.get(k) for k in keys}
    out["sources"] = [s["name"] for s in result.get("sources", [])]
    out["details_url"] = f"/v1/lightning?lat={result['location']['lat']}&lon={result['location']['lon']}"
    return out


def for_point(lat: float, lon: float, start: datetime | None, now: datetime | None = None, **kw: Any) -> dict[str, Any] | None:
    """Lightning summary for a venue when the event is close to now, else None. Never raises."""
    now = now or datetime.now(timezone.utc)
    if start is not None and not relevant(start, now):
        return None
    try:
        return summary(check_lightning(lat, lon, now=now, **kw))
    except Exception:
        return {"lightning_status": "unknown", "reason": "Lightning check failed.", "advisory": ADVISORY}


def _checker() -> Callable[..., dict[str, Any] | None]:
    return for_point  # monkeypatched in tests

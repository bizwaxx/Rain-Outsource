"""Live black-box checks against a deployment (production or a Vercel preview).

Every check returns a Check row: pass / fail / warn / skip / not_built, with an owner and a fix
for anything that is not a pass. Checks only read, except the player-report write test, which
runs on previews (or on production only with an explicit flag) and is clearly marked as a test.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
# "rainout-test" prefix: production usage metrics (PR #32) count this as internal traffic, not outside users.
UA = "rainout-test-staging-checklist/1.0 (+https://rainout-agent-source.vercel.app)"
PRODUCTION = "https://rainout-agent-source.vercel.app"

# ---------------------------------------------------------------- thresholds (documented in docs/staging-checklist.md)
THRESHOLDS = {
    "status_latency_p95_s": 10.0,          # /v1/status p95 (cold calls included)
    "status_latency_max_s": 25.0,          # any single call slower than this is a fail
    "official_check_age_min": 60,          # official_status_last_checked must be this fresh
    "weather_age_min": 180,                # last_checked (weather) must be this fresh
    "forecast_period_gap_min": 90,         # forecast period must start within this of game time
    "official_source_ok_rate": 0.90,       # share of venues with a source whose check matched
    "osm_status_ok_rate": 0.98,            # sampled map venues answering 200 with valid odds
    "backtest_directional_accuracy": 70,   # % (played vs not played, split at 50%)
    "backtest_confident_wrong": 0,         # p>=80 on a rained-out event or p<=20 on a played one
}
# Outcomes where the official page was read and understood (even if nothing is posted for this game).
OK_OUTCOMES = {"matched", "no_source", "not_polled", "no_alerts", "not_posted", "not_posted_yet", "no_game_listed", "no_status",
               "undated", "stale", "needs_game_time", "no_match"}
REQUIRED_STATUS_KEYS = ["field_id", "field", "official_status", "official_status_check", "official_status_last_checked",
                        "last_checked", "game_time_play_probability_percent", "rain_chance_percent", "reason",
                        "verification_level", "weather_source_id", "forecast_period_start", "community_reports"]
# Fields site/switch.js (PR #37) reads from /v1/status.
SWITCH_STATUS_FIELDS = ["official_status", "game_time_play_probability_percent", "official_status_check",
                        "official_status_last_checked", "official_status_source_url", "official_status_source_name",
                        "community_reports", "rain_chance_percent", "reason", "verification_level", "field", "field_id"]
# MCP tools every deployment must expose (main). Feature previews add more (checked when the feature is deployed).
CORE_MCP_TOOLS = ["find_team", "get_games", "check_game", "get_venue_status", "find_venue", "check_schedule", "find_track",
                  "get_track_events", "check_race", "get_track_history", "get_rain_chance", "report_cancellation"]
OFFICIAL_STATUSES = {"on", "cancelled", "delayed", "postponed", "closed", "field_closed", "unknown", "uncertain", "conflicting", "mixed", "playing", "open"}


@dataclass
class Check:
    id: str
    area: str
    target: str
    status: str                 # pass | fail | warn | skip | not_built
    detail: str
    owner: str = ""
    fix: str = ""
    severity: str = ""          # blocker | should_fix | known_gap | info
    data: dict = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Resp:
    status: int
    body: bytes
    headers: dict
    seconds: float
    error: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None, timeout: float = 40) -> Resp:
    h = {"User-Agent": UA, "Accept": "application/json,text/calendar,text/html,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Resp(r.status, r.read(), dict(r.headers), time.monotonic() - t0)
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read() or b"", dict(e.headers or {}), time.monotonic() - t0)
    except Exception as e:
        return Resp(0, b"", {}, time.monotonic() - t0, error=f"{type(e).__name__}: {e}"[:200])


class Target:
    """One deployment under test."""

    def __init__(self, base: str, label: str, kind: str, http: Callable[..., Resp] = request):
        self.base, self.label, self.kind, self.http = base.rstrip("/"), label, kind, http
        self.checks: list[Check] = []

    def get(self, path: str, **kw: Any) -> Resp:
        return self.http(self.base + path, **kw)

    def add(self, cid: str, area: str, ok: bool | None, detail: str, *, owner: str = "", fix: str = "",
            severity: str = "blocker", warn: bool = False, data: dict | None = None) -> Check:
        status = "pass" if ok else ("warn" if warn else "fail") if ok is not None else "skip"
        c = Check(cid, area, self.label, status, detail, owner if status != "pass" else "", fix if status != "pass" else "",
                  severity if status in ("fail", "warn") else "", data or {})
        self.checks.append(c)
        return c


def _age_min(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() / 60
    except ValueError:
        return None


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def game_time_tomorrow(tz: str = "America/Chicago", hour: int = 18) -> str:
    now = datetime.now(ZoneInfo(tz))
    return (now + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def valid_ics(text: str) -> tuple[bool, int]:
    ok = text.lstrip().startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in text
    events = text.count("BEGIN:VEVENT")
    if events and (text.count("END:VEVENT") != events or text.count("DTSTART") < events):
        ok = False
    return ok, events


# ---------------------------------------------------------------- core endpoints
def check_core(t: Target) -> None:
    r = t.get("/health")
    j = r.json() or {}
    t.add("health", "core", r.status == 200 and j.get("status") == "ok", f"/health -> {r.status} status={j.get('status')!r}",
          owner="API/backend", fix="Restore /health; check the last deploy logs in Vercel.")
    for path, needle in [("/openapi.yaml", "openapi:"), ("/llms.txt", "Rain"), ("/", ""), ("/v1/stats", "")]:
        r = t.get(path)
        ok = r.status == 200 and (needle in r.text if needle else True)
        t.add(f"endpoint:{path}", "core", ok, f"{path} -> {r.status or r.error} in {r.seconds:.1f}s",
              owner="API/backend", fix=f"{path} must return 200.", severity="blocker" if path != "/v1/stats" else "should_fix")


# ---------------------------------------------------------------- fields + status
def list_confirmed(t: Target) -> list[dict[str, Any]]:
    r = t.get("/v1/fields?verification=confirmed&limit=500")
    j = r.json() or {}
    fields = j.get("fields") or []
    t.add("fields:confirmed", "fields", r.status == 200 and len(fields) >= 60 and len(fields) == j.get("total"),
          f"/v1/fields confirmed -> {r.status}, {len(fields)} of total {j.get('total')} venues",
          owner="Data/catalog", fix="Confirmed venue list shrank or failed; check data/<city>/*.json loads.")
    r2 = t.get("/v1/fields?q=park&state=TX&limit=5")
    t.add("fields:search", "fields", r2.status == 200 and (r2.json() or {}).get("count", 0) > 0,
          f"/v1/fields?q=park&state=TX -> {r2.status}, {(r2.json() or {}).get('count')} hits", owner="API/backend",
          fix="Venue search returns nothing; check status_api.fields_payload.")
    return fields


def sample_confirmed(fields: list[dict[str, Any]], mode: str, seed: int = 7) -> list[dict[str, Any]]:
    """Sample mode: every distinct official source (host + source type) once, plus one per city.
    Full mode: everything."""
    if mode == "full":
        return list(fields)
    if mode == "preview":    # previews: a small spread (Krieg + one per city), keeps upstream traffic low
        out = {f["field_id"]: f for f in fields if "krieg" in f["field_id"]}
        for f in fields:
            if len(out) >= 12:
                break
            if f.get("city") not in {x.get("city") for x in out.values()}:
                out[f["field_id"]] = f
        return list(out.values())
    picked: dict[str, dict] = {}
    for f in fields:
        url = f.get("official_status_source_url") or ""
        host = urllib.parse.urlparse(url).netloc or "none"
        key = f"src:{host}:{f.get('status_source_type')}"
        picked.setdefault(key, f)
    for f in fields:
        picked.setdefault(f"city:{f.get('city')}", f)
    return list({f["field_id"]: f for f in picked.values()}.values())


def validate_status(j: dict[str, Any], game_time: str, now: datetime) -> list[str]:
    """Problems with one /v1/status payload (empty list = good)."""
    probs = []
    missing = [k for k in REQUIRED_STATUS_KEYS if k not in j]
    if missing:
        probs.append(f"missing keys {missing}")
    p = j.get("game_time_play_probability_percent")
    if not isinstance(p, int) or not 0 <= p <= 100:
        probs.append(f"bad probability {p!r}")
    st = str(j.get("official_status") or "").lower()
    if st not in OFFICIAL_STATUSES:
        probs.append(f"unexpected official_status {st!r}")
    if st in {"cancelled", "closed", "field_closed"} and p not in (0, None):
        probs.append(f"official {st} but odds {p}%")
    age = _age_min(j.get("official_status_last_checked"), now)
    chk = j.get("official_status_check") or {}
    if chk.get("outcome") not in ("no_source", "not_polled") and (age is None or age > THRESHOLDS["official_check_age_min"]):
        probs.append(f"stale official check ({None if age is None else round(age)} min)")
    wage = _age_min(j.get("last_checked"), now)
    if wage is None or wage > THRESHOLDS["weather_age_min"]:
        probs.append(f"stale weather ({None if wage is None else round(wage)} min)")
    fp, gt = _age_min(j.get("forecast_period_start"), now), _age_min(game_time, now)
    if fp is not None and gt is not None and abs(fp - gt) > THRESHOLDS["forecast_period_gap_min"]:
        probs.append(f"forecast period {j.get('forecast_period_start')} is far from game time")
    if not j.get("weather_source_id"):
        probs.append("no weather_source_id")
    return probs


def check_status_venues(t: Target, fields: list[dict[str, Any]], mode: str, workers: int = 6) -> list[dict[str, Any]]:
    game_time = game_time_tomorrow()
    targets = sample_confirmed(fields, mode)

    def one(f: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.urlencode({"field_id": f["field_id"], "game_time": game_time})
        r = t.get(f"/v1/status?{q}", timeout=45)
        j = r.json() if r.status == 200 else None
        return {"field": f, "resp": r, "json": j}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, targets))
    now = datetime.now(timezone.utc)
    lat = [x["resp"].seconds for x in rows]
    bad_http = [x for x in rows if x["resp"].status != 200]
    t.add("status:http", "status", not bad_http,
          f"/v1/status on {len(rows)} confirmed venues ({mode}): {len(rows) - len(bad_http)} x 200"
          + (f"; failing: {[(x['field']['field_id'], x['resp'].status or x['resp'].error) for x in bad_http][:6]}" if bad_http else ""),
          owner="API/backend", fix="Every venue must answer 200; see Vercel function logs for the failing field_ids.")
    p95, worst = _p95(lat), max(lat or [0])
    t.add("status:latency", "status", p95 <= THRESHOLDS["status_latency_p95_s"] and worst <= THRESHOLDS["status_latency_max_s"],
          f"/v1/status latency p95 {p95:.1f}s, max {worst:.1f}s (limits {THRESHOLDS['status_latency_p95_s']}s / {THRESHOLDS['status_latency_max_s']}s)",
          owner="API/backend", fix="Slow official pages block the response; cap the official fetch (e.g. 4 s) and serve the last cached result.",
          severity="should_fix", warn=worst <= THRESHOLDS["status_latency_max_s"] * 2)
    problems = {x["field"]["field_id"]: validate_status(x["json"], game_time, now) for x in rows if x["json"]}
    shape = {k: v for k, v in problems.items() if any(p.startswith(("missing", "bad prob", "unexpected", "official")) for p in v)}
    t.add("status:schema", "status", not shape, f"response shape valid on {len(problems) - len(shape)}/{len(problems)}"
          + (f"; problems: {dict(list(shape.items())[:4])}" if shape else ""), owner="API/backend", fix="Fix the response fields listed.")
    stale = {k: [p for p in v if p.startswith(("stale", "forecast period", "no weather"))] for k, v in problems.items()}
    stale = {k: v for k, v in stale.items() if v}
    t.add("status:freshness", "freshness", not stale,
          f"freshness OK on {len(problems) - len(stale)}/{len(problems)} (official <= {THRESHOLDS['official_check_age_min']} min, weather <= {THRESHOLDS['weather_age_min']} min)"
          + (f"; stale: {dict(list(stale.items())[:5])}" if stale else ""),
          owner="API/backend", fix="Check cache TTLs and the NWS/official fetch fallbacks for these venues.")
    # official sources, grouped by host
    by_src: dict[str, dict[str, Any]] = {}
    for x in rows:
        if not x["json"]:
            continue
        chk = x["json"].get("official_status_check") or {}
        url = x["json"].get("official_status_source_url") or ""
        host = urllib.parse.urlparse(url).netloc or "(no official page)"
        g = by_src.setdefault(host, {"venues": 0, "outcomes": {}, "parsers": set(), "examples": []})
        g["venues"] += 1
        g["outcomes"][chk.get("outcome")] = g["outcomes"].get(chk.get("outcome"), 0) + 1
        g["parsers"].add(chk.get("parser"))
        if chk.get("outcome") not in OK_OUTCOMES and len(g["examples"]) < 2:
            g["examples"].append({"field_id": x["field"]["field_id"], "outcome": chk.get("outcome"), "message": (chk.get("message") or "")[:160]})
    def outcome(x: dict[str, Any]) -> str | None:
        return (x["json"].get("official_status_check") or {}).get("outcome")

    answered = [x for x in rows if x["json"]]
    polled = [x for x in answered if outcome(x) not in ("no_source", "not_polled")]
    ok_src = [x for x in polled if outcome(x) in OK_OUTCOMES]
    rate = len(ok_src) / len(polled) if polled else 1.0
    t.add("official:coverage", "official_sources", True,
          f"of {len(answered)} venues checked: {len(polled)} have an official status feed we read automatically, "
          f"{sum(1 for x in answered if outcome(x) == 'not_polled')} link to an information page only (not read), "
          f"{sum(1 for x in answered if outcome(x) == 'no_source')} have no official page",
          data={"polled": [x["field"]["field_id"] for x in polled]})
    for host, g in sorted(by_src.items()):
        bad = {k: v for k, v in g["outcomes"].items() if k not in OK_OUTCOMES}
        t.add(f"official:{host}", "official_sources", not bad,
              f"{host}: {g['venues']} venue(s), parser(s) {sorted(str(p) for p in g['parsers'])}, outcomes {g['outcomes']}"
              + (f"; e.g. {g['examples'][0]}" if g["examples"] else ""),
              owner="Parsers (official_sources.py)",
              fix=("The site refuses automated requests (403): mark it link-only (poll_official_status: false) so agents get the "
                   "phone/URL fallback cleanly, or ask the operator for a feed." if "blocked" in bad else
                   "Page fetch timed out: lower the timeout, retry once, and keep serving the last good result."
                   if "unavailable" in bad else
                   "Page layout changed or isn't supported: add/adjust the adapter and a fixture test."),
              # The same upstream page is checked on every target; count it once (production), list it elsewhere.
              severity="blocker" if host != "(no official page)" and t.kind == "production" else "info", data={"outcomes": g["outcomes"]})
    t.add("official:rate", "official_sources", rate >= THRESHOLDS["official_source_ok_rate"], severity="blocker" if t.kind == "production" else "info", detail=
          f"official status feeds read and understood for {len(ok_src)}/{len(polled)} polled venues ({rate:.0%}; need {THRESHOLDS['official_source_ok_rate']:.0%})",
          owner="Parsers (official_sources.py)", fix="See the per-source rows above.")
    return rows


def state_files() -> list[Path]:
    return sorted(f for f in (ROOT / "data" / "osm").glob("*.json") if re.fullmatch(r"[A-Z]{2}\.json", f.name))


def _rows(f: Path) -> tuple[str, list[dict[str, Any]]]:
    d = json.loads(f.read_text())
    return d.get("state") or f.stem, [dict(zip(d["row_fields"], r)) for r in d["rows"]]


def check_osm_sample(t: Target, mode: str, per_state: int | None = None, seed: int = 11, workers: int = 6) -> None:
    """Random map venues from every state's catalog file, asked live for status."""
    per_state = per_state or (5 if mode == "full" else 1)
    rng = random.Random(seed)
    ids = []
    files = state_files()
    for f in files:
        _, rows = _rows(f)
        ids += [r["id"] for r in rng.sample(rows, min(per_state, len(rows)))]
    gt = game_time_tomorrow()

    def one(fid: str) -> tuple[str, Resp]:
        return fid, t.get("/v1/status?" + urllib.parse.urlencode({"field_id": fid, "game_time": gt}), timeout=45)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, ids))
    good = [fid for fid, r in res if r.status == 200 and isinstance((r.json() or {}).get("game_time_play_probability_percent"), int)
            and (r.json() or {}).get("verification_level") == "osm_only"]
    rate = len(good) / len(res) if res else 0
    t.add("osm:sample", "fields", rate >= THRESHOLDS["osm_status_ok_rate"] and len(res) > 0,
          f"{len(res)} random map (OSM) venues across {len(files)} state files: {len(good)} answered 200 with weather-only odds",
          owner="API/backend", fix="Map venues must answer with weather-only odds; check the failing ids.",
          data={"failing": [(fid, r.status, r.text[:120]) for fid, r in res if fid not in good][:10]})


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def osm_integrity(t: Target | None = None) -> Check:
    """Offline pass over every map venue row (full mode): ids, names, coordinates, NOAA station."""
    problems, rows_total, seen = [], 0, set()
    for f in state_files():
        state, rows = _rows(f)
        pts = sorted((r["lat"], r["lon"]) for r in rows if isinstance(r.get("lat"), (int, float)) and isinstance(r.get("lon"), (int, float)))
        med = (sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2]) if pts else None
        for r in rows:
            rows_total += 1
            rid = r.get("id")
            if rid in seen:
                problems.append((rid, "duplicate id"))
            seen.add(rid)
            if not r.get("name"):
                problems.append((rid, "no name"))
            if not isinstance(r.get("lat"), (int, float)) or not isinstance(r.get("lon"), (int, float)):
                problems.append((rid, "no coordinates"))
            elif med and state not in ("AK", "HI") and _km(med, (r["lat"], r["lon"])) > 1000:
                problems.append((rid, f"{round(_km(med, (r['lat'], r['lon'])))} km from the {state} median - wrong state?"))
            if "station" in r and not r.get("station"):
                problems.append((rid, "no NOAA station"))
            if not r.get("sports"):
                problems.append((rid, "no sport"))
    c = Check("osm:integrity", "fields", "Local catalog (offline)", "pass" if not problems else "warn",
              f"{rows_total} map venue rows in {len(state_files())} state files checked offline; {len(problems)} problems"
              + (f", e.g. {problems[:5]}" if problems else ""),
              owner="" if not problems else "Data/catalog",
              fix="" if not problems else "Fix or drop the listed rows; venues with no NOAA station just lose the history calibration.",
              severity="" if not problems else ("should_fix" if any(x[1] != "no NOAA station" for x in problems) else "info"),
              data={"problems": problems[:50], "rows": rows_total,
                    "counts": {k: sum(1 for x in problems if x[1].split(" ")[0] == k.split(" ")[0]) for k in ("no NOAA station", "duplicate id", "no name", "no coordinates", "no sport")}})
    if t:
        t.checks.append(c)
    return c


# ---------------------------------------------------------------- motocross, teams, ICS, rain
def check_tracks(t: Target) -> None:
    r = t.get("/v1/tracks")
    tracks = (r.json() or {}).get("tracks") or []
    t.add("tracks:list", "motocross", r.status == 200 and len(tracks) >= 10, f"/v1/tracks -> {r.status}, {len(tracks)} tracks",
          owner="API/backend", fix="Track list must load (data/motocross/tracks.json).")
    bad, ics_events, slow = [], 0, []
    for tr in tracks:
        tid = tr["track_id"]
        tomorrow = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).strftime("%Y-%m-%d")
        for sub in ("events", f"check?date={tomorrow}", "history"):
            rr = t.get(f"/v1/tracks/{tid}/{sub}", timeout=45)
            if rr.status != 200:
                bad.append((tid, sub, rr.status or rr.error))
            if rr.seconds > THRESHOLDS["status_latency_max_s"]:
                slow.append((tid, sub, round(rr.seconds, 1)))
        rr = t.get(f"/v1/tracks/{tid}/calendar.ics", timeout=45)
        ok, n = valid_ics(rr.text) if rr.status == 200 else (False, 0)
        ics_events += n
        if not ok:
            bad.append((tid, "calendar.ics", rr.status or rr.error))
    t.add("tracks:endpoints", "motocross", not bad, f"events/check/history/calendar.ics on {len(tracks)} tracks"
          + (f"; failing {bad[:6]}" if bad else f"; ICS valid ({ics_events} events)"),
          owner="API/backend + Parsers (mx_sources.py)", fix="Fix the failing track endpoints.")
    t.add("tracks:latency", "motocross", not slow, f"slow track calls: {slow[:6]}" if slow else "all track calls under the latency limit",
          owner="API/backend", fix="Cache the track event sources.", severity="should_fix", warn=True)


def check_teams_and_schedule(t: Target) -> None:
    r = t.get("/v1/teams/search?q=a")
    cands = (r.json() or {}).get("candidates") or []
    t.add("teams:search", "teams", r.status == 200 and len(cands) > 0, f"/v1/teams/search -> {r.status}, {len(cands)} candidates",
          owner="API/backend", fix="Team search must return candidates (league sources).", severity="should_fix")
    if cands:
        tid = cands[0]["team_id"]
        g = t.get(f"/v1/teams/{tid}/games", timeout=45)
        c = t.get(f"/v1/teams/{tid}/calendar.ics", timeout=45)
        ok, n = valid_ics(c.text) if c.status == 200 else (False, 0)
        t.add("teams:games+ics", "alerts:ics", g.status == 200 and ok, f"team {tid}: games {g.status}, calendar.ics {c.status} valid={ok} ({n} events)",
              owner="API/backend", fix="Team games/ICS feed must load.")
    tr = (t.get("/v1/tracks").json() or {}).get("tracks") or []
    if tr:
        feed = f"{PRODUCTION}/v1/tracks/{tr[0]['track_id']}/calendar.ics"
        s = t.get("/v1/schedule/check?" + urllib.parse.urlencode({"ics_url": feed, "format": "ics"}), timeout=60)
        ok, n = valid_ics(s.text) if s.status == 200 else (False, 0)
        t.add("schedule:ics", "alerts:ics", ok, f"/v1/schedule/check?ics_url=<track feed>&format=ics -> {s.status or s.error}, valid={ok} ({n} events)"
              + ("" if ok else f"; body: {s.text[:160]!r}"),
              owner="API/backend", fix="Schedule checker must turn a public ICS feed into an odds-annotated ICS.", severity="should_fix")


def check_rain(t: Target) -> None:
    for name, q in [("us", "lat=30.27&lon=-97.74"), ("intl", "lat=51.5&lon=-0.12")]:
        r = t.get(f"/v1/rain?{q}", timeout=40)
        j = r.json() or {}
        ok = r.status == 200 and not j.get("error")
        t.add(f"rain:{name}", "forecast", ok, f"/v1/rain ({name}) -> {r.status}, keys {sorted(j)[:8]}",
              owner="API/backend", fix="Rain endpoint must answer for US (NWS) and non-US (Open-Meteo) points.")


def check_providers(t: Target) -> None:
    """Upstream data providers, called directly (free, no key)."""
    nws = request("https://api.weather.gov/points/30.2672,-97.7431", headers={"Accept": "application/geo+json"})
    t.add("provider:nws", "forecast", nws.status == 200, f"NWS points API -> {nws.status or nws.error}",
          owner="Ops (upstream)", fix="NWS is down or blocking our User-Agent; Open-Meteo fallback must cover.", severity="should_fix")
    om = request("https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&hourly=precipitation_probability&forecast_days=1")
    t.add("provider:open_meteo", "forecast", om.status == 200, f"Open-Meteo forecast -> {om.status or om.error}",
          owner="Ops (upstream)", fix="Open-Meteo is the non-US and fallback source.", severity="should_fix")
    hf = request("https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&start_date=2025-05-01&end_date=2025-05-01&hourly=precipitation_probability")
    t.add("provider:open_meteo_history", "forecast", hf.status == 200, f"Open-Meteo historical forecast (backtest) -> {hf.status or hf.error}",
          owner="Ops (upstream)", fix="Backtest weather source unavailable.", severity="should_fix")
    noaa = request("https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations=USW00013904&startDate=2024-05-01&endDate=2024-05-03&dataTypes=PRCP&format=json")
    t.add("provider:noaa_ncei", "forecast", noaa.status == 200 and isinstance(noaa.json(), list),
          f"NOAA NCEI daily summaries (history refresh) -> {noaa.status or noaa.error}",
          owner="Ops (upstream)", fix="NOAA history refresh (scripts/fetch_noaa_history.py) can't run; production uses checked-in data.", severity="info", warn=True)


# ---------------------------------------------------------------- MCP
def mcp_call(t: Target, method: str, params: dict | None = None, rid: int = 1) -> tuple[Resp, dict]:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode()
    r = t.get("/mcp", method="POST", data=body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, timeout=60)
    return r, (r.json() or {})


def check_mcp(t: Target, expect_tools: list[str]) -> list[str]:
    r, j = mcp_call(t, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "staging-checklist", "version": "1"}})
    t.add("mcp:initialize", "mcp", r.status == 200 and "result" in j, f"MCP initialize -> {r.status}, protocol {((j.get('result') or {}).get('protocolVersion'))}",
          owner="API/backend", fix="MCP endpoint must answer initialize.")
    r, j = mcp_call(t, "tools/list", rid=2)
    names = [x["name"] for x in ((j.get("result") or {}).get("tools") or [])]
    missing = [n for n in expect_tools if n not in names]
    t.add("mcp:tools", "mcp", r.status == 200 and not missing, f"{len(names)} tools" + (f"; missing {missing}" if missing else f": {names}"),
          owner="API/backend", fix="Register the missing MCP tools.")
    r, j = mcp_call(t, "tools/call", {"name": "get_venue_status", "arguments": {"venue": "Krieg", "game_time": game_time_tomorrow()}}, rid=3)
    res = j.get("result") or {}
    t.add("mcp:get_venue_status", "mcp", r.status == 200 and not res.get("isError") and bool(res.get("content")),
          f"tools/call get_venue_status -> {r.status}, isError={res.get('isError')}", owner="API/backend", fix="MCP venue status tool failing.")
    r, j = mcp_call(t, "tools/call", {"name": "get_rain_chance", "arguments": {"query": "Austin, TX"}}, rid=4)
    res = j.get("result") or {}
    t.add("mcp:get_rain_chance", "mcp", r.status == 200 and not res.get("isError"), f"tools/call get_rain_chance -> {r.status}, isError={res.get('isError')}",
          owner="API/backend", fix="MCP rain tool failing.", severity="should_fix")
    return names


# ---------------------------------------------------------------- player reports
TEST_NOTE = "STAGING CHECKLIST TEST REPORT (automated). Not a real report; expires within 24 h."


def check_reports(t: Target, field_id: str, allow_write: bool) -> None:
    r = t.get("/v1/reports?" + urllib.parse.urlencode({"field_id": field_id}))
    j = r.json() or {}
    t.add("reports:read", "reports", r.status == 200 and "summary" in j, f"GET /v1/reports -> {r.status}", owner="API/backend",
          fix="Reports read endpoint must answer.")
    bad = t.get("/v1/reports", method="POST", data=b'{"field_id": "' + field_id.encode() + b'", "status": "nonsense"}',
                headers={"Content-Type": "application/json"})
    t.add("reports:validation", "reports", bad.status == 400, f"POST invalid status -> {bad.status} (want 400)", owner="API/backend",
          fix="Invalid reports must be rejected with 400.")
    if not allow_write:
        t.add("reports:write", "reports", None, "write test not run here (one marked test report per run, on the first preview; production only with --allow-prod-test-report)")
        return
    before = (j.get("summary") or {}).get("count", 0)
    game = (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat()
    body = json.dumps({"field_id": field_id, "status": "playing", "game_time": game, "note": TEST_NOTE}).encode()
    w = t.get("/v1/reports", method="POST", data=body, headers={"Content-Type": "application/json"})
    seen = []
    for _ in range(4):
        rr = t.get("/v1/reports?" + urllib.parse.urlencode({"field_id": field_id}))
        reps = (rr.json() or {}).get("reports") or []
        seen.append(any(TEST_NOTE in str(x.get("note", "")) for x in reps))
        time.sleep(0.5)
    if w.status == 429:
        t.add("reports:write", "reports", None, "POST test report -> 429 (rate limited by an earlier run this hour); read-back not tested")
        return
    t.add("reports:write", "reports", w.status in (200, 201, 202), f"POST test report -> {w.status}", owner="API/backend",
          fix="Report submission failing.")
    t.add("reports:read_after_write", "reports", all(seen),
          f"test report visible on {sum(seen)}/{len(seen)} follow-up reads" + ("" if all(seen) else
          " - reports live in per-instance memory, so other serverless instances don't see them"),
          owner="API/backend + Ops", fix="Configure the shared cache (Upstash/Vercel KV env vars) so reports and stats persist across instances.",
          severity="blocker")


# ---------------------------------------------------------------- preview features
def check_construction(t: Target) -> None:
    tomorrow = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).strftime("%Y-%m-%d")
    calls = [("nowcast", "/v1/nowcast?lat=30.27&lon=-97.74"),
             ("concrete", f"/v1/construction/concrete-pour?lat=30.27&lon=-97.74&pour_start={tomorrow}%2007:00"),
             ("materials", "/v1/construction/materials?lat=30.27&lon=-97.74&materials=drywall,lumber"),
             ("go_no_go:softball", f"/v1/go-no-go?activity=softball&venue=Krieg&time={tomorrow}%2018:00"),
             ("go_no_go:concrete", f"/v1/go-no-go?activity=concrete_pour&lat=30.27&lon=-97.74&time={tomorrow}%2007:00")]
    for name, path in calls:
        r = t.get(path, timeout=60)
        j = r.json() or {}
        ok = r.status == 200 and not j.get("error")
        if name.startswith("go_no_go"):
            ok = ok and j.get("decision") in ("go", "wait", "no_go", "no_event_found")
        t.add(f"construction:{name}", "go_no_go", ok, f"{path.split('?')[0]} -> {r.status}, decision={j.get('decision')!r}"
              + ("" if ok else f", body {r.text[:160]!r}"), owner="API/backend (PR #26-#31)", fix="Fix the failing construction endpoint.")
    # motocross with no race listed must not be a bare "go"
    far = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=2)).strftime("%Y-%m-%d")
    r = t.get(f"/v1/go-no-go?activity=motocross&track_id=freestone&time={far}%2003:00", timeout=60)
    j = r.json() or {}
    ok = r.status == 200 and (j.get("decision") != "go" or j.get("event_confirmed") is True)
    t.add("go_no_go:no_event", "go_no_go", ok, f"motocross, no race listed -> decision={j.get('decision')!r}, event_confirmed={j.get('event_confirmed')!r}",
          owner="API/backend (PR #31)", fix="No listed event must give no_event_found, never a bare go.")


def check_should_we_cancel(t: Target) -> None:
    tomorrow = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).strftime("%Y-%m-%d")
    for name, q in [("softball", f"activity=softball&venue=Krieg&time={tomorrow}%2018:00"),
                    ("concrete", f"activity=concrete_pour&lat=30.27&lon=-97.74&time={tomorrow}%2007:00"),
                    ("motocross_no_race", f"activity=motocross&track_id=freestone&time={tomorrow}%2003:00")]:
        r = t.get(f"/v1/should-we-cancel?{q}", timeout=60)
        j = r.json() or {}
        ok = r.status == 200 and j.get("recommendation") in ("play", "delay", "cancel", "wait_until", "no_event_found", None) and not j.get("error")
        if name == "motocross_no_race":
            ok = ok and j.get("recommendation") != "play"
        t.add(f"should_we_cancel:{name}", "go_no_go", ok, f"/v1/should-we-cancel ({name}) -> {r.status}, recommendation={j.get('recommendation')!r}"
              + ("" if ok else f", body {r.text[:160]!r}"), owner="API/backend (PR #35)", fix="Fix should_we_cancel for this case.")


def check_usage(t: Target) -> None:
    r = t.get("/v1/admin/usage")
    r2 = t.get("/v1/admin/usage", headers={"Authorization": "Bearer not-a-real-token"})
    closed = {401, 403}
    off = r.status == 503 and (r.json() or {}).get("error") == "admin_not_configured"
    ok = (r.status in closed and r2.status in closed) or (off and r2.status == 503)
    t.add("usage:auth", "admin", ok, f"/v1/admin/usage without token -> {r.status}, bogus token -> {r2.status} (want 401/403, or 503 when disabled)",
          owner="API/backend (PR #32)", fix="Admin usage must never be readable without RAINOUT_ADMIN_TOKEN.")
    if off:
        t.add("usage:configured", "admin", False, "usage dashboard is disabled here: RAINOUT_ADMIN_TOKEN is not set",
              owner="Ops (Vercel env)", fix="Set RAINOUT_ADMIN_TOKEN (16+ chars) in Vercel if you want usage numbers at launch.",
              severity="info", warn=True)


def check_landing(t: Target) -> None:
    # server.json / glama.json are registry files read from the repo, not served over HTTP.
    for path, kind in [("/", "html"), ("/.well-known/mcp/server-card", "json"), ("/robots.txt", "text"), ("/sitemap.xml", "text"),
                       ("/privacy", "html"), ("/terms", "html")]:
        r = t.get(path)
        ok = r.status == 200 and (r.json() is not None if kind == "json" else True)
        t.add(f"landing:{path}", "site", ok, f"{path} -> {r.status}" + ("" if ok else f" {r.text[:100]!r}"), owner="Frontend/growth (PR #33)",
              fix=f"{path} must be served{' as valid JSON' if kind == 'json' else ''}.", severity="should_fix")


def check_billing_webhook(t: Target) -> None:
    r = t.get("/v1/billing/webhook", method="POST", data=b'{"type":"checkout.session.completed"}', headers={"Content-Type": "application/json"})
    off = r.status == 503 and (r.json() or {}).get("error") == "billing_not_configured"
    ok = r.status in (400, 401, 403) or off
    t.add("webhook:stripe_unsigned", "alerts:webhooks", ok, f"unsigned POST /v1/billing/webhook -> {r.status} (want 400/401/403, or 503 when billing is off)",
          owner="API/backend (PR #27)", fix="Reject webhooks without a valid Stripe-Signature.")
    if off:
        t.add("webhook:stripe_signature", "alerts:webhooks", False,
              "billing is off on this preview (no Stripe test key), so signature verification could not be exercised live",
              owner="Ops (Vercel env, PR #27)", fix="Set a Stripe TEST key + webhook secret on the preview and re-run.", severity="info", warn=True)


def switch_contract(status_payload: dict[str, Any]) -> list[str]:
    return [k for k in SWITCH_STATUS_FIELDS if k not in status_payload]


def run_switch_harness(switch_js: str, node: str = "node") -> dict[str, Any]:
    harness = Path(__file__).resolve().parent / "switch_harness.js"
    p = subprocess.run([node, str(harness)], input=switch_js, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (p.stderr or p.stdout)[-400:]}


def check_switch(t: Target) -> None:
    for path in ("/switch", "/switch.js", "/switch/embed?field_id=Krieg", "/switch-embed.js"):
        r = t.get(path)
        t.add(f"switch:{path.split('?')[0]}", "alerts:switch", r.status == 200, f"{path} -> {r.status}", owner="Frontend (PR #37)",
              fix=f"{path} must be served.", severity="should_fix")
    js = t.get("/switch.js").text
    html = t.get("/switch").text
    st = t.get("/v1/status?" + urllib.parse.urlencode({"field_id": "Krieg", "game_time": game_time_tomorrow()}), timeout=45).json() or {}
    missing = switch_contract(st)
    t.add("switch:status_contract", "alerts:switch", not missing and bool(st),
          "every /v1/status field switch.js reads is present" if not missing else f"switch.js reads fields missing from /v1/status: {missing}",
          owner="Frontend (PR #37)", fix="Keep switch.js and /v1/status in sync.")
    if js:
        res = run_switch_harness(js)
        if "error" in res:
            t.add("switch:alert_logic", "alerts:switch", None, f"harness could not run: {res['error'][:200]}")
        else:
            bad = [s for s in res["scenarios"] if s["notified"] != s["expected"]]
            t.add("switch:alert_logic", "alerts:switch", not bad,
                  f"{len(res['scenarios']) - len(bad)}/{len(res['scenarios'])} browser re-check scenarios behaved"
                  + (f"; wrong: {bad}" if bad else ""), owner="Frontend (PR #37)", fix="Fix checkForAlert for the listed transitions.",
                  data=res)
    stub = "coming soon" in html.lower()
    for ch in ("Email alerts", "Push notifications", "Odds-change webhooks"):
        t.checks.append(Check(f"not_built:{ch}", "alerts", t.label, "not_built",
                              f"{ch}: not built" + (" (switch page shows 'Coming soon (stub)')" if stub and ch == "Email alerts" else ""),
                              owner="Jimmy (product call)", fix="Launch without it and say so, or build it before launch.", severity="known_gap"))

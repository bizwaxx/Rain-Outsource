"""Known-outcome backtest: replay real past games, races and pours through the production models.

Each fixture in ``staging/fixtures/known_outcomes.json`` is a real event with a public source URL
and a reported outcome (played / not_played). For each one we fetch that day's weather from free
Open-Meteo archives and run the same code production uses:

* sports (turf, grass soccer, dirt infield, football): ``calculate_play_probability`` with the
  official status unknown, at the forecast hour closest to the start (what /v1/status does);
* motocross: ``motocross.race_odds`` with the track's NOAA calibration, soil, soak and race-day rain;
* construction: ``concrete.evaluate`` (only when the construction modules from PR #28/#31 are
  importable; otherwise these fixtures are reported as skipped).

Weather grades (reported per fixture, never mixed up):
* ``archived_forecast``: archived hourly precipitation probability from the Open-Meteo
  historical-forecast API (available from about spring 2024). This is a fair forecast backtest.
* ``forecast_amount_proxy``: archived forecast rain amounts and weather codes but no archived
  probability, converted with PROXY_POP below.
* ``observed_proxy``: ERA5 reanalysis (what actually fell), converted with PROXY_POP. Hindsight,
  so it flatters the model; shown separately.

Also checked per fixture (offline, deterministic): an official cancellation always wins (0%),
player reports move the odds the right way only while the official status is unknown, and for
motocross the real cancellation notice is fed to the notice parser to see whether it would have
flagged the event.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "known_outcomes.json"
HIST_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "precipitation_probability,precipitation,weather_code,cape,temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m"
THUNDER = {95, 96, 99}
# Rain amount in the hour -> stand-in rain chance when no archived probability exists.
PROXY_POP = ((0.1, 5), (0.5, 35), (2.5, 60), (7.6, 80), (float("inf"), 95))
GO, WAIT = 70, 40               # go_no_go bands: >=70 go, 40-69 wait, <40 no_go
CONFIDENT_PLAY, CONFIDENT_CANCEL = 80, 20
UA = "rainout-test-staging-checklist/1.0 (+https://rainout-agent-source.vercel.app)"

Fetch = Callable[[str], dict]


def http_json(url: str, timeout: float = 30) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # pragma: no cover - network
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def http_text(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) " + UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def load_fixtures(path: Path = FIXTURES) -> dict[str, Any]:
    return json.loads(path.read_text())


def band(p: int | None) -> str | None:
    if p is None:
        return None
    return "go" if p >= GO else "wait" if p >= WAIT else "no_go"


def grade(outcome: str, p: int | None, decision: str | None = None) -> dict[str, Any]:
    """Score one call against the known outcome."""
    decision = decision or band(p)
    if decision is None:
        return {"decision": None, "result": "no_call", "directional_ok": None, "confident_wrong": False}
    want = "go" if outcome == "played" else "no_go"
    if decision == "wait":
        result = "hedged"
    else:
        result = "correct" if decision == want else "wrong"
    if p is not None:
        directional = (p >= 50) == (outcome == "played")
        confident_wrong = (outcome == "not_played" and p >= CONFIDENT_PLAY) or (outcome == "played" and p <= CONFIDENT_CANCEL)
    else:
        directional = decision == want if decision != "wait" else None
        confident_wrong = result == "wrong"
    return {"decision": decision, "result": result, "directional_ok": directional, "confident_wrong": confident_wrong}


def proxy_pop(mm: float | None) -> int:
    mm = mm or 0.0
    for limit, pop in PROXY_POP:
        if mm < limit:
            return pop
    return PROXY_POP[-1][1]


# ---------------------------------------------------------------- weather
def _om_url(base: str, lat: float, lon: float, d0: date, d1: date, hourly: str) -> str:
    q = {"latitude": lat, "longitude": lon, "start_date": d0.isoformat(), "end_date": d1.isoformat(), "hourly": hourly,
         "timezone": "UTC", "timeformat": "unixtime", "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}
    return base + "?" + urllib.parse.urlencode(q)


def _rows(payload: dict) -> dict[datetime, dict[str, Any]]:
    """{utc hour start: row}; Open-Meteo precipitation at T is the hour before T, so shift it."""
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    rows: dict[datetime, dict[str, Any]] = {}

    def col(name: str) -> list:
        return h.get(name) or [None] * len(times)

    for i, ts in enumerate(times):
        t = datetime.fromtimestamp(int(ts), timezone.utc)
        code = col("weather_code")[i]
        r = rows.setdefault(t, {})
        r.update({"pop": col("precipitation_probability")[i], "code": code, "thunder": code in THUNDER if code is not None else False,
                  "cape": col("cape")[i],
                  "temp_f": col("temperature_2m")[i], "rh": col("relative_humidity_2m")[i], "dew_f": col("dew_point_2m")[i],
                  "wind_mph": col("wind_speed_10m")[i], "gust_mph": col("wind_gusts_10m")[i]})
        rows.setdefault(t - timedelta(hours=1), {})["mm"] = col("precipitation")[i]
    return {t: r for t, r in rows.items() if "temp_f" in r}


def fetch_weather(lat: float, lon: float, d0: date, d1: date, fetch: Fetch = http_json) -> dict[str, Any]:
    """Archived forecast rows (may lack probability) plus ERA5 observed rows for the same range."""
    out: dict[str, Any] = {"forecast": {}, "observed": {}, "sources": []}
    try:
        url = _om_url(HIST_FORECAST, lat, lon, d0, d1, HOURLY)
        out["forecast"] = _rows(fetch(url))
        out["sources"].append(url)
    except Exception as exc:
        out["forecast_error"] = str(exc)[:200]
    try:
        url = _om_url(ARCHIVE, lat, lon, d0, d1, "precipitation,weather_code,temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m")
        out["observed"] = _rows(fetch(url))
        out["sources"].append(url)
    except Exception as exc:
        out["observed_error"] = str(exc)[:200]
    try:
        # Ground-wetness inputs: ERA5 rain and FAO-56 reference evapotranspiration for the week before.
        url = _om_url(ARCHIVE, lat, lon, d0 - timedelta(days=4), d1, "precipitation,et0_fao_evapotranspiration")
        out["wet"] = wet_rows(fetch(url))
        out["sources"].append(url)
    except Exception as exc:
        out["wet_error"] = str(exc)[:200]
    return out


def wet_rows(payload: dict) -> dict[datetime, dict[str, Any]]:
    """{utc hour start: {mm, et0}}; both Open-Meteo sums are for the hour before the timestamp."""
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    pr = h.get("precipitation") or [None] * len(times)
    et = h.get("et0_fao_evapotranspiration") or [None] * len(times)
    return {datetime.fromtimestamp(int(ts), timezone.utc) - timedelta(hours=1): {"mm": pr[i], "et0": et[i]} for i, ts in enumerate(times)}


def _hours(start: datetime, end: datetime) -> list[datetime]:
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def pick_rows(wx: dict[str, Any], start: datetime, end: datetime) -> tuple[dict[datetime, dict[str, Any]], str]:
    """Best available weather grade for [start, end) with a pop on every hour."""
    hrs = _hours(start, end)
    fc = wx.get("forecast") or {}
    if hrs and all(h in fc and fc[h].get("pop") is not None for h in hrs):
        return fc, "archived_forecast"
    if hrs and all(h in fc and fc[h].get("mm") is not None for h in hrs):
        return {t: {**r, "pop": proxy_pop(r.get("mm"))} for t, r in fc.items()}, "forecast_amount_proxy"
    ob = wx.get("observed") or {}
    if hrs and all(h in ob and ob[h].get("mm") is not None for h in hrs):
        return {t: {**r, "pop": proxy_pop(r.get("mm"))} for t, r in ob.items()}, "observed_proxy"
    return {}, "missing"


# ---------------------------------------------------------------- models
def _local(fx: dict[str, Any], key: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(fx[key]).replace(tzinfo=tz)


def score_sports(fx: dict[str, Any], wx: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.play_probability import calculate_play_probability

    tz = ZoneInfo(fx["tz"])
    start = _local(fx, "start", tz)
    rows, wgrade = pick_rows(wx, start, start + timedelta(hours=1))
    if not rows:
        return {"weather_grade": "missing", "p": None}
    hour = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    r = rows[hour]
    pop, thunder = int(r["pop"] or 0), bool(r.get("thunder"))
    res = calculate_play_probability("unknown", pop, thunder, 2.0, None)
    window = [rows[h] for h in _hours(start, start + timedelta(hours=float(fx.get("duration_h", 3)))) if h in rows]
    return {"weather_grade": wgrade, "p": res["play_probability_percent"], "rain_chance_percent": pop, "thunderstorm_likely": thunder,
            "window_max_pop": max((int(x["pop"] or 0) for x in window), default=None),
            "window_rain_mm": round(sum((x.get("mm") or 0) for x in window), 1),
            "model": "calculate_play_probability(unknown status, hour closest to start)"}


def score_motocross(fx: dict[str, Any], wx: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent import motocross as mx
    from rainout_agent.historical import calibration_for

    tz = ZoneInfo("America/Chicago")
    ws, we = _local(fx, "start", tz), _local(fx, "end", tz)
    rows, wgrade = pick_rows(wx, ws, we)
    if not rows:
        return {"weather_grade": "missing", "p": None}
    win = [rows[h] for h in _hours(ws, we)]
    pop = int(max(int(x["pop"] or 0) for x in win))
    storm_hours = sum(1 for x in win if x.get("thunder"))
    thunder = storm_hours >= 2 and pop >= 40
    race_in = round(sum((x.get("mm") or 0) for x in win) / 25.4, 2)
    ob = wx.get("observed") or {}

    def total(a: datetime, b: datetime) -> float | None:
        hs = _hours(a, b)
        if not all(h in ob and ob[h].get("mm") is not None for h in hs):
            return None
        return round(sum(ob[h]["mm"] for h in hs) / 25.4, 2)

    by_day = [total(ws - timedelta(hours=24 * (i + 1)), ws - timedelta(hours=24 * i)) for i in range(3)]
    soak = round(sum(w * v for w, v in zip(mx.SOAK_WEIGHTS, by_day)), 2) if all(v is not None for v in by_day) else None
    cal = calibration_for(track["field_id"], ws)
    odds = mx.race_odds("unknown", pop, thunder, 6.0, cal, soak, race_in, track.get("soil") or "unknown")
    return {"weather_grade": wgrade, "p": odds["play_probability"], "rain_chance_percent": pop, "thunderstorm_likely": thunder,
            "race_window_rain_in": race_in, "prior_rain_by_day_in": by_day, "soak_index_in": soak,
            "model": "motocross.race_odds(unknown status, NOAA calibration, soil, soak, race-day rain)",
            "soak_note": "prior-day rain from ERA5 observations (known at decision time)"}


def score_construction(fx: dict[str, Any], wx: dict[str, Any]) -> dict[str, Any]:
    try:
        from rainout_agent import concrete
        from rainout_agent import site_weather as sw
    except ImportError:
        return {"weather_grade": None, "p": None, "skipped": "construction modules not on this branch (PR #28/#31); run with --code-path pointing at the construction worktree"}
    tz = ZoneInfo(fx["tz"])
    start = _local(fx, "start", tz)
    cfg = sw.thresholds()
    mix = cfg["concrete"]["mixes"][cfg["concrete"]["defaults"]["mix"]]
    duration = float(fx.get("duration_h", 4))
    cure = float(cfg["concrete"]["defaults"]["cure_hours"])
    rows, wgrade = pick_rows(wx, start, start + timedelta(hours=duration + cure))
    if not rows:
        return {"weather_grade": "missing", "p": None}
    ev = concrete.evaluate(rows, start, cfg, duration_h=duration, cure_h=cure, critical_h=min(mix["critical_hours"], cure),
                           mix=mix, protection=False, concrete_f=None)
    if ev is None:
        return {"weather_grade": wgrade, "p": None, "skipped": "weather rows do not cover the cure window"}
    return {"weather_grade": wgrade, "p": ev["probability_percent"], "decision": ev["decision"],
            "rain_chance_percent": ev["rain_probability_percent"],
            "failing_checks": [c["check"] for c in ev["checks"] if c["status"] != "ok"],
            "model": "concrete.evaluate (default mix, 24 h cure, no protection)"}


def event_inputs(fx: dict[str, Any], wx: dict[str, Any], track: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Inputs for rainout_agent.event_risk (the v2 whole-event model), from the weather already fetched."""
    from rainout_agent import event_risk as er
    from rainout_agent import motocross as mx

    if fx["field_type"] == "construction":
        return None
    if fx["field_type"] == "motocross":
        tz = ZoneInfo("America/Chicago")
        start = _local(fx, "start", tz)
        duration = (_local(fx, "end", tz) - start).total_seconds() / 3600
        soil = mx.SOIL_FACTORS.get((track or {}).get("soil") or "unknown", 1.0)
    else:
        start = _local(fx, "start", ZoneInfo(fx["tz"]))
        duration, soil = float(fx.get("duration_h", 3)), 1.0
    rows, wgrade = pick_rows(wx, start - timedelta(hours=2), start + timedelta(hours=duration))
    if not rows:
        return None
    feat = er.features(wx.get("observed") or {}, rows, start, duration)
    for k in ("earlier_today_in", "prior_24h_in", "prior_48h_in"):
        if feat.get(k) is not None:
            feat[k] = round(feat[k] / soil, 3)   # sandy motocross soil drains faster
    drainage = er.drainage_class(field_type=fx["field_type"], maintained=bool(fx.get("maintained")))
    ball = fx["field_type"] != "motocross"
    series = er.wet_series(wx.get("wet") or {}, start, soil)
    er.add_wetness(feat, series, drainage, ball)
    return {"id": fx["id"], "field_type": fx["field_type"], "outcome": fx["outcome"], "grade": wgrade, "features": feat,
            "drainage": drainage, "ball_sport": ball, "series": series}


def score_event_risk(inp: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    from rainout_agent import event_risk as er

    feat = inp["features"]
    if params is not None and inp.get("series") is not None and params.get("soak_model") == "wetness":
        feat = {**feat, "wetness_in": er.wetness_in(inp["series"], inp["drainage"] if inp["ball_sport"] else "mx", params)}
    base = er.base_percent(feat.get("window_pop") or 0, inp["drainage"], params)
    out = er.adjust(base, feat, inp["drainage"], params, inp["ball_sport"])
    return {"p": out["play_probability_percent"], "base_percent": base, "features": feat, "drainage": inp["drainage"],
            "reasons": out["reasons"], "model": "event_risk v2 (window rain, soak by drainage, storms, wind, cold, humility cap)"}


# ---------------------------------------------------------------- behaviour checks
def behaviour_checks(fx: dict[str, Any], scored: dict[str, Any]) -> list[dict[str, Any]]:
    """Status precedence and player-report handling for this fixture's weather."""
    out: list[dict[str, Any]] = []
    if fx["field_type"] == "construction" or scored.get("p") is None:
        return out
    from rainout_agent.play_probability import calculate_play_probability
    from rainout_agent.reports import adjust_probability

    pop, thunder = scored.get("rain_chance_percent") or 0, bool(scored.get("thunderstorm_likely"))
    cancelled = calculate_play_probability("cancelled", pop, thunder, 2.0, None)["play_probability_percent"]
    out.append({"check": "official_cancel_wins", "ok": cancelled == 0, "detail": f"cancelled status -> {cancelled}%"})
    p = int(scored["p"])
    summary_cancel = {"count": 2, "by_status": {"cancelled": 2, "delayed": 0, "field_closed": 0, "playing": 0}}
    summary_play = {"count": 2, "by_status": {"cancelled": 0, "delayed": 0, "field_closed": 0, "playing": 2}}
    down, _ = adjust_probability(p, summary_cancel)
    up, _ = adjust_probability(p, summary_play)
    out.append({"check": "cancel_reports_lower_odds", "ok": down < p or p == 0, "detail": f"{p}% -> {down}% with 2 'cancelled' reports"})
    out.append({"check": "playing_reports_raise_odds", "ok": up > p or p == 100, "detail": f"{p}% -> {up}% with 2 'playing' reports"})
    return out


def report_precedence_check() -> dict[str, Any]:
    """apply_to_status must not touch the odds when an official status is posted."""
    from rainout_agent import reports
    from rainout_agent.cache import MemoryCache

    now = datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc)
    saved = reports._LOCAL_STORE
    reports._LOCAL_STORE = MemoryCache(max_entries=100)
    try:
        from rainout_agent.status_api import list_supported_fields, load_field

        fid = list_supported_fields()[0]["field_id"]
        field = load_field(fid)
        for i in range(2):
            reports.submit_report({"field_id": fid, "status": "cancelled", "game_time": (now + timedelta(hours=2)).isoformat()},
                                  client=f"staging-{i}", now=now)
        game = now + timedelta(hours=2)
        official = {"field_id": fid, "official_status": "on", "game_time_play_probability_percent": 90, "reason": ""}
        unknown = {"field_id": fid, "official_status": "unknown", "game_time_play_probability_percent": 90, "reason": ""}
        reports.apply_to_status(official, field, game, now=now)
        reports.apply_to_status(unknown, field, game, now=now)
        later = {"field_id": fid, "official_status": "unknown", "game_time_play_probability_percent": 90, "reason": ""}
        reports.apply_to_status(later, field, game, now=now + timedelta(hours=13))
        ok = (official["game_time_play_probability_percent"] == 90 and not official["community_reports"]["applied"]
              and unknown["game_time_play_probability_percent"] < 90 and unknown["community_reports"]["applied"]
              and later["community_reports"]["count"] == 0)
        return {"check": "reports_precedence_and_expiry", "ok": ok,
                "detail": (f"official 'on' kept {official['game_time_play_probability_percent']}%; unknown went 90% -> "
                           f"{unknown['game_time_play_probability_percent']}%; 13 h later relevant reports = {later['community_reports']['count']}")}
    finally:
        reports._LOCAL_STORE = saved


def wp_post_url(source_url: str) -> str | None:
    """WordPress REST URL for a freestonemx.com news post (the same feed production reads)."""
    m = re.match(r"https?://(?:www\.)?freestonemx\.com/latest-news/([^/]+)/?", source_url)
    if not m:
        return None
    return f"https://freestonemx.com/wp-json/wp/v2/posts?slug={m.group(1)}&_fields=date,link,title,content"


def notice_replay(fx: dict[str, Any], track: dict[str, Any], wp_payload: Any) -> dict[str, Any]:
    """Feed the real cancellation post, parsed exactly like production's wp_posts source, to
    motocross.official_status as if it were seen two hours before the start."""
    from rainout_agent import motocross as mx
    from rainout_agent import mx_sources

    notices = mx_sources.notices_from_wp(wp_payload) if wp_payload else []
    if not notices:
        return {"check": "mx_notice_parser", "ok": None, "detail": "notice post not readable through the WordPress feed"}
    tz = ZoneInfo("America/Chicago")
    ws, we = _local(fx, "start", tz), _local(fx, "end", tz)
    name = fx.get("event_name") or "Race"
    ev = {"name": name, "sources": [{"name": "staging", "url": fx["source_url"], "listed_as": name}],
          "start": ws, "end": we, "listing_cancelled": False, "day_labels": {}}
    notices = [{**n, "source_name": "Freestone Raceway news", "source_url": n.get("url")} for n in notices]
    posted = notices[0].get("posted")
    if posted and posted > we + timedelta(hours=12):
        st = mx.official_status(track, ev, notices, posted)
        return {"check": "mx_notice_parser", "ok": None,
                "detail": f"post is dated after the event ({posted.date()}), so it could not help on race day (parser: '{st['status']}')"}
    st = mx.official_status(track, ev, notices, max(ws - timedelta(hours=2), posted or ws))
    status = st["status"]
    if fx.get("outcome") == "played":
        # A race that ran: the parser must NOT turn its posts (results, live broadcasts) into a cancellation.
        ok = status not in ("cancelled", "postponed")
        return {"check": "mx_notice_parser", "ok": ok, "detail": f"race ran; parser says '{status}'"}
    ok = status in ("cancelled", "postponed", "uncertain")
    detail = f"parser says '{status}'" + (" (flagged as uncertain, odds capped at 50%)" if status == "uncertain" else "")
    if not ok:
        detail += f"; post title: {notices[0].get('title', '')[:80]!r}"
    return {"check": "mx_notice_parser", "ok": ok, "detail": detail}


# ---------------------------------------------------------------- runner
def run(fixtures: dict[str, Any] | None = None, fetch: Fetch = http_json, fetch_text: Callable[[str], str] = http_text,
        log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    from rainout_agent import motocross as mx

    fixtures = fixtures or load_fixtures()
    tracks = {t["short_id"]: t for t in mx.tracks()}
    results, inputs = [], []
    for fx in fixtures["fixtures"]:
        log(f"backtest {fx['id']}")
        if fx["field_type"] == "motocross":
            track = tracks[fx["track"]]
            loc = mx._field(track)["coordinates"]
            lat, lon = loc["lat"], loc["lon"]
        else:
            track, lat, lon = None, fx["lat"], fx["lon"]
        day = datetime.fromisoformat(fx["start"]).date()
        wx = fetch_weather(lat, lon, day - timedelta(days=4), day + timedelta(days=2), fetch)
        if fx["field_type"] == "motocross":
            scored = score_motocross(fx, wx, track)
        elif fx["field_type"] == "construction":
            scored = score_construction(fx, wx)
        else:
            scored = score_sports(fx, wx)
        inp = event_inputs(fx, wx, track)
        if inp is not None and scored.get("p") is not None:
            inputs.append(inp)
            v1 = {"p_v1": scored["p"], "model_v1": scored.get("model")}
            v2 = score_event_risk(inp)
            scored = {**scored, **v1, "p": v2["p"], "model": v2["model"], "event_risk": {k: v2[k] for k in ("base_percent", "features", "drainage", "reasons")}}
        if scored.get("skipped"):
            g = {"decision": None, "result": "skipped", "directional_ok": None, "confident_wrong": False}
        else:
            g = grade(fx["outcome"], scored.get("p"), scored.get("decision"))
            if scored.get("p_v1") is not None:
                g1 = grade(fx["outcome"], scored["p_v1"])
                g["v1"] = {"result": g1["result"], "directional_ok": g1["directional_ok"], "confident_wrong": g1["confident_wrong"]}
        behaviour = behaviour_checks(fx, scored)
        if fx["field_type"] == "motocross":
            wp = wp_post_url(fx["source_url"])
            try:
                payload = fetch(wp) if wp else None
            except Exception:
                payload = None
            behaviour.append(notice_replay(fx, track, payload))
        results.append({"id": fx["id"], "field_type": fx["field_type"], "activity": fx["activity"], "outcome": fx["outcome"],
                        "cause": fx["cause"], "source_url": fx["source_url"], "start": fx["start"], "assumptions": fx.get("assumptions", []),
                        **scored, **g, "behaviour": behaviour, "weather_sources": wx.get("sources", [])})
    summary = summarize(results)
    if inputs:
        log("backtest: leave-one-out calibration")
        from staging import calibrate
        cal = calibrate.calibrate(inputs)
        summary["held_out"] = {"leave_one_out": cal["leave_one_out"], "in_sample": cal["in_sample"], "chosen": cal["chosen"],
                               "grid_points": cal["grid_points"], "loo_distinct_choices": cal["loo_distinct_choices"],
                               "note": "event_risk scored on sports + motocross fixtures; each fixture predicted with parameters "
                                       "chosen on the others only (construction is scored by concrete.evaluate and not included)"}
    return {"results": results, "summary": summary, "precedence": report_precedence_check(),
            "excluded": fixtures.get("excluded", [])}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [r for r in rs if r["result"] not in ("skipped", "no_call")]
        dirs = [r for r in scored if r["directional_ok"] is not None]
        return {"fixtures": len(rs), "scored": len(scored),
                "played": sum(1 for r in rs if r["outcome"] == "played"), "not_played": sum(1 for r in rs if r["outcome"] == "not_played"),
                "correct": sum(1 for r in scored if r["result"] == "correct"), "hedged": sum(1 for r in scored if r["result"] == "hedged"),
                "wrong": sum(1 for r in scored if r["result"] == "wrong"),
                "directional_accuracy": round(100 * sum(1 for r in dirs if r["directional_ok"]) / len(dirs)) if dirs else None,
                "band_accuracy": round(100 * sum(1 for r in scored if r["result"] == "correct") / len(scored)) if scored else None,
                "confident_wrong": sum(1 for r in scored if r["confident_wrong"]),
                "skipped": sum(1 for r in rs if r["result"] == "skipped")}

    by_type: dict[str, list] = {}
    for r in results:
        by_type.setdefault(r["field_type"], []).append(r)
    by_grade: dict[str, list] = {}
    for r in results:
        by_grade.setdefault(r.get("weather_grade") or "none", []).append(r)
    def agg_v1(rs: list[dict[str, Any]]) -> dict[str, Any]:
        v = [r["v1"] for r in rs if r.get("v1")]
        d = [x for x in v if x["directional_ok"] is not None]
        return {"scored": len(v), "directional_accuracy": round(100 * sum(1 for x in d if x["directional_ok"]) / len(d)) if d else None,
                "confident_wrong": sum(1 for x in v if x["confident_wrong"])}

    return {"overall": agg(results), "by_field_type": {k: agg(v) for k, v in by_type.items()},
            "v1_overall": agg_v1(results), "v1_by_field_type": {k: agg_v1(v) for k, v in by_type.items()},
            "by_weather_grade": {k: agg(v) for k, v in by_grade.items()},
            "mismatches": [r["id"] for r in results if r["result"] in ("wrong", "hedged")]}

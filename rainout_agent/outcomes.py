"""'Was it on?' outcome reports, pre-event prediction snapshots, per-venue drainage memory, feed and scoreboard.

Storage: the shared cache backend when one is configured (Upstash / Vercel KV), else a dedicated in-memory
tier (same as player reports). Nothing personal is stored: no IPs, no names; the client address is only used,
hashed, as a rate-limit key and is never written into a record. **Test entries** (``test: true``, or the
staging-checklist note) are marked, kept in local memory only (never the shared store) and excluded from the
memory, feed and scoreboard.

* ``snapshot_prediction``: called by /v1/status before an event; keeps the latest odds made *before* the start
  (never after), so the scoreboard compares outcomes with what we actually said in advance.
* ``submit_outcome``: played / delayed / cancelled / unknown for a venue and date (up to 7 days back, not in the
  future), with the same rate limits as player reports. The matching snapshot is attached at report time.
* ``venue_memory``: per-venue drainage memory. On "wet days" (we took points off for rain that already fell) it
  compares how often the venue played with how often we expected, shrinks that toward our expectation
  (``PRIOR_STRENGTH`` pseudo-events) and turns it into a bounded multiplier on the soak penalty. No effect below
  ``MIN_WET_EVENTS`` reported wet days.
* ``feed`` / ``scoreboard``: public JSON + Atom feed and an accuracy page computed from reported outcomes vs the
  snapshotted odds.
"""
from __future__ import annotations

import html
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from rainout_agent import reports
from rainout_agent.cache import Cache

OUTCOMES = ("played", "delayed", "cancelled", "unknown")
ALIASES = {"played": "played", "on": "played", "yes": "played", "playing": "played", "it_was_on": "played",
           "delayed": "delayed", "delay": "delayed", "cancelled": "cancelled", "canceled": "cancelled", "no": "cancelled",
           "rained_out": "cancelled", "postponed": "cancelled", "off": "cancelled", "unknown": "unknown", "not_sure": "unknown"}
PLAYED = {"played", "delayed"}            # a delay still went ahead
TEST_MARK = re.compile(r"(?i)\b(test report|staging checklist|automated test)\b")
OUTCOME_TTL_S = 400 * 24 * 3600
SNAPSHOT_TTL_S = 30 * 24 * 3600
SNAPSHOT_EVERY_S = 1800                   # at most one snapshot write per game every 30 min
SNAPSHOT_AHEAD = timedelta(days=7)
REPORT_BACK = timedelta(days=7)
MAX_PER_VENUE, MAX_GLOBAL = 300, 1000
RATE_LIMIT_PER_HOUR, RATE_LIMIT_FIELD_PER_HOUR = 10, 3
MIN_WET_EVENTS = 3
PRIOR_STRENGTH = 4.0
MEMORY_BOUNDS = (0.4, 1.6)
_LOCK = threading.Lock()


class OutcomeError(reports.ReportError):
    pass


def _store(test: bool = False) -> Cache:
    return reports._LOCAL_STORE if test else reports._store()


def _venue_key(field_id: str) -> str:
    return f"outcomes:v:{field_id}"


def _snap_key(field_id: str, game_date: str) -> str:
    return f"outcomes:pred:{field_id}:{game_date}"


def is_test(data: dict[str, Any]) -> bool:
    flag = data.get("test")
    return flag in (True, "true", "1", 1, "yes") or bool(TEST_MARK.search(str(data.get("note") or "")))


# ------------------------------------------------------------------ prediction snapshots
def snapshot_prediction(response: dict[str, Any], field: dict[str, Any], game_time: datetime | None,
                        now: datetime | None = None, test: bool = False) -> bool:
    """Store the odds we gave before the event (latest pre-start call per game). Returns True if written."""
    now = now or reports._utcnow()
    if not response.get("field_id") or game_time is None or not (now < game_time <= now + SNAPSHOT_AHEAD):
        return False
    game_date = game_time.astimezone(reports._field_zone(field)).date().isoformat()
    store, key = _store(test), _snap_key(response["field_id"], game_date)
    model = response.get("model") or {}
    prof = response.get("venue_profile") or {}
    snap = {"game_time": game_time.isoformat(), "predicted_at": now.isoformat(timespec="seconds"),
            "play_probability_percent": response.get("game_time_play_probability_percent"),
            "model_version": model.get("version", "v1"), "official_status": response.get("official_status"),
            "soak_penalty": model.get("soak_penalty", 0) or 0, "soak_raw_in": model.get("soak_raw_in"),
            "drainage": model.get("drainage"), "surface": prof.get("surface"), "maintained": prof.get("maintained"),
            "test": bool(test)}
    with _LOCK:
        snaps = store.get(key) or {}
        prev = snaps.get(snap["game_time"])
        if prev and (now - datetime.fromisoformat(prev["predicted_at"])).total_seconds() < SNAPSHOT_EVERY_S:
            return False
        snaps[snap["game_time"]] = snap
        store.set(key, snaps, SNAPSHOT_TTL_S)
    return True


def _snapshot_for(field_id: str, game_date: str, game_time: datetime | None, test: bool) -> dict[str, Any] | None:
    snaps = _store(test).get(_snap_key(field_id, game_date)) or {}
    if not snaps:
        return None
    if game_time is not None:
        exact = snaps.get(game_time.isoformat())
        if exact:
            return exact
        return min(snaps.values(), key=lambda s: abs((datetime.fromisoformat(s["game_time"]) - game_time).total_seconds()))
    return max(snaps.values(), key=lambda s: s["predicted_at"])


# ------------------------------------------------------------------ outcome reports
def submit_outcome(data: dict[str, Any], client: str | None = None, now: datetime | None = None,
                   clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """Validate and store one 'Was it on?' report. Raises OutcomeError (400/404/429)."""
    from rainout_agent.status_api import load_field, resolve_field_id

    if not isinstance(data, dict):
        raise OutcomeError(400, "invalid_body", "Send a JSON object with field_id, date and outcome.")
    now = now or reports._utcnow()
    query = str(data.get("field_id") or data.get("field") or "").strip()
    if not query or len(query) > 200:
        raise OutcomeError(400, "missing_field_id", "field_id is required (see /v1/fields).")
    field_id = resolve_field_id(query)
    if not field_id:
        raise OutcomeError(404, "unknown_field", "Unknown field_id; search /v1/fields?q=<name>&state=<ST> first.")
    raw = re.sub(r"[\s-]+", "_", str(data.get("outcome") or data.get("status") or "").strip().lower())
    outcome = ALIASES.get(raw)
    if not outcome:
        raise OutcomeError(400, "invalid_outcome", f"outcome must be one of: {', '.join(OUTCOMES)}.", allowed=list(OUTCOMES))
    field = load_field(field_id)
    zone = reports._field_zone(field)
    game_time = None
    if data.get("game_time"):
        game_time = reports._parse_time(data["game_time"], now)
        game_date = game_time.astimezone(zone).isoformat()[:10]
    else:
        try:
            game_date = date.fromisoformat(str(data.get("date") or now.astimezone(zone).date().isoformat())[:10]).isoformat()
        except ValueError as exc:
            raise OutcomeError(400, "invalid_date", "date must be YYYY-MM-DD (the event's local date).") from exc
    today = now.astimezone(zone).date()
    if not (today - REPORT_BACK <= date.fromisoformat(game_date) <= today) or (game_time and game_time > now):
        raise OutcomeError(400, "date_out_of_range", "Outcomes are accepted after the event, for up to 7 days back.")
    note = data.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise OutcomeError(400, "invalid_note", "note must be a string.")
        note = reports._CTRL.sub(" ", note).strip()
        if len(note) > reports.MAX_NOTE_CHARS:
            raise OutcomeError(400, "note_too_long", f"note must be at most {reports.MAX_NOTE_CHARS} characters.")
    test = is_test(data)
    store = _store(test)
    with _LOCK:
        _check_rate(store, reports.client_key(client), field_id, (clock or time.time)())
        snap = _snapshot_for(field_id, game_date, game_time, test)
        rec = {"id": uuid.uuid4().hex[:12], "field_id": field_id, "field_name": field.get("field_name"), "date": game_date,
               "game_time": game_time.isoformat() if game_time else None, "outcome": outcome,
               "reported_at": now.isoformat(timespec="seconds"), "note": note or None, "test": test,
               "prediction": {k: snap[k] for k in ("play_probability_percent", "predicted_at", "model_version", "official_status",
                                                   "soak_penalty", "drainage", "surface", "maintained", "game_time")} if snap else None}
        venue = (store.get(_venue_key(field_id)) or []) + [rec]
        store.set(_venue_key(field_id), venue[-MAX_PER_VENUE:], OUTCOME_TTL_S)
        allr = (store.get("outcomes:all") or []) + [rec]
        store.set("outcomes:all", allr[-MAX_GLOBAL:], OUTCOME_TTL_S)
    return {"accepted": True, "outcome": rec, "test": test,
            "storage": {"backend": store.name, "kept_days": OUTCOME_TTL_S // 86400,
                        "note": ("Marked as a test: kept in this instance's memory only and excluded from the memory, feed "
                                 "and scoreboard." if test else
                                 "Anonymous and unverified; used for the venue's drainage memory, the public outcomes feed "
                                 "and the accuracy scoreboard.")}}


def _check_rate(store: Cache, client: str, field_id: str, now_s: float) -> None:
    hour, retry = int(now_s // 3600), 3600 - int(now_s % 3600)
    keys = ((f"outcomes:rl:{client}:{hour}", RATE_LIMIT_PER_HOUR), (f"outcomes:rl:{client}:{field_id}:{hour}", RATE_LIMIT_FIELD_PER_HOUR))
    for key, limit in keys:
        if int(store.get(key) or 0) >= limit:
            raise OutcomeError(429, "rate_limited", f"Too many outcome reports; at most {limit} per hour. Try again later.",
                               retry_after_seconds=retry)
    for key, _ in keys:
        store.set(key, int(store.get(key) or 0) + 1, 3600)


def venue_outcomes(field_id: str, include_test: bool = False) -> list[dict[str, Any]]:
    try:
        recs = list(_store().get(_venue_key(field_id)) or [])
        if include_test and _store(True) is not _store():
            recs += list(_store(True).get(_venue_key(field_id)) or [])
    except Exception:  # pragma: no cover - cache must never break a request
        return []
    return [r for r in recs if isinstance(r, dict) and (include_test or not r.get("test"))]


def resolve_events(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (venue, date, game): majority of played/delayed vs cancelled; unknown and ties drop out."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in recs:
        if r.get("test") or r.get("outcome") == "unknown":
            continue
        gt = (r.get("prediction") or {}).get("game_time") or r.get("game_time") or ""
        groups.setdefault((r["field_id"], r["date"], gt), []).append(r)
    out = []
    for (fid, d, gt), rs in groups.items():
        played = sum(r["outcome"] in PLAYED for r in rs)
        off = sum(r["outcome"] == "cancelled" for r in rs)
        if played == off:
            continue
        pred = next((r["prediction"] for r in sorted(rs, key=lambda r: r["reported_at"]) if r.get("prediction")), None)
        out.append({"field_id": fid, "field_name": rs[0].get("field_name"), "date": d, "game_time": gt or None,
                    "outcome": "played" if played > off else "not_played", "reports": len(rs), "prediction": pred,
                    "last_reported_at": max(r["reported_at"] for r in rs)})
    return sorted(out, key=lambda e: e["last_reported_at"], reverse=True)


# ------------------------------------------------------------------ drainage memory
def memory_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Bounded soak-penalty multiplier from wet-day outcomes vs our snapshotted odds (shrinkage toward 1.0)."""
    wet = [e for e in events if (e.get("prediction") or {}).get("soak_penalty", 0) > 0
           and (e["prediction"].get("play_probability_percent") is not None)
           and e["prediction"].get("official_status") in (None, "unknown")]
    n = len(wet)
    k = sum(e["outcome"] == "played" for e in wet)
    expected = sum(e["prediction"]["play_probability_percent"] / 100 for e in wet)
    out = {"wet_events": n, "played": k, "expected_played": round(expected, 1), "min_wet_events": MIN_WET_EVENTS,
           "soak_multiplier": 1.0, "active": False}
    if n == 0:
        out["explain"] = "No reported outcomes on wet days for this venue yet."
        return out
    prior = expected / n
    rate = (k + PRIOR_STRENGTH * prior) / (n + PRIOR_STRENGTH)             # shrunk play rate
    cancel_pred, cancel_obs = max(0.05, 1 - prior), max(0.0, 1 - rate)
    mult = min(MEMORY_BOUNDS[1], max(MEMORY_BOUNDS[0], cancel_obs / cancel_pred))
    out["explain"] = (f"This field has played through {k} of {n} reported wet days (we expected about {expected:.1f}).")
    if n >= MIN_WET_EVENTS:
        out.update(soak_multiplier=round(mult, 2), active=True)
        out["explain"] += f" Rain that already fell counts {round(mult, 2)}x here."
    else:
        out["explain"] += f" Needs {MIN_WET_EVENTS} before it changes the odds."
    return out


def venue_memory(field_id: str) -> dict[str, Any]:
    return memory_from_events(resolve_events(venue_outcomes(field_id)))


# ------------------------------------------------------------------ feed + scoreboard
def all_events(include_test: bool = False) -> list[dict[str, Any]]:
    try:
        recs = list(_store().get("outcomes:all") or [])
    except Exception:  # pragma: no cover
        recs = []
    return resolve_events([r for r in recs if include_test or not r.get("test")])


def scoreboard(events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = all_events() if events is None else events
    scored = [e for e in events if (e.get("prediction") or {}).get("play_probability_percent") is not None]

    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"events": 0}
        ps = [(r["prediction"]["play_probability_percent"], r["outcome"] == "played") for r in rows]
        directional = [(p >= 50) == played for p, played in ps if p != 50]
        cw = sum((p >= 80 and not played) or (p <= 20 and played) for p, played in ps)
        brier = sum((p / 100 - played) ** 2 for p, played in ps) / len(ps)
        return {"events": len(rows), "played": sum(pl for _, pl in ps),
                "directional_accuracy_percent": round(100 * sum(directional) / len(directional)) if directional else None,
                "confident_wrong": cw, "brier_score": round(brier, 3)}

    by_surface: dict[str, list] = {}
    for e in scored:
        by_surface.setdefault(e["prediction"].get("surface") or "unknown", []).append(e)
    model_only = [e for e in scored if e["prediction"].get("official_status") in (None, "unknown")]
    return {"as_of": reports._utcnow().isoformat(timespec="seconds"), "overall": agg(scored), "model_only": agg(model_only),
            "by_surface": {k: agg(v) for k, v in sorted(by_surface.items())},
            "reported_without_snapshot": len(events) - len(scored),
            "method": ("Outcomes are anonymous 'Was it on?' reports (majority per event; test entries excluded). Each is "
                       "compared with the odds we gave before the start (snapshotted at prediction time). Directional = "
                       ">= 50% and played, or < 50% and not played. Confident wrong = >= 80% and not played, or <= 20% "
                       "and played. Brier = mean squared error of the odds (lower is better).")}


def feed_json(limit: int = 50) -> dict[str, Any]:
    ev = all_events()[:limit]
    return {"title": "Rain-Outsource reported outcomes", "count": len(ev), "events": ev,
            "license": "Reports are anonymous and unverified.", "scoreboard_url": "/scoreboard"}


def feed_atom(base_url: str, limit: int = 50) -> str:
    ev = all_events()[:limit]
    esc = lambda s: html.escape(str(s if s is not None else ""), quote=True)
    updated = ev[0]["last_reported_at"] if ev else reports._utcnow().isoformat(timespec="seconds")
    items = []
    for e in ev:
        p = (e.get("prediction") or {}).get("play_probability_percent")
        said = f"we said {p}% before the start" if p is not None else "no pre-event odds on record"
        items.append(
            f"<entry><id>urn:rainout:outcome:{esc(e['field_id'])}:{esc(e['date'])}:{esc(e.get('game_time') or '')}</id>"
            f"<title>{esc(e.get('field_name') or e['field_id'])} {esc(e['date'])}: {esc(e['outcome'].replace('_', ' '))}</title>"
            f"<updated>{esc(e['last_reported_at'])}</updated>"
            f"<summary>{esc(e['outcome'].replace('_', ' '))} ({e['reports']} report(s)); {esc(said)}.</summary></entry>")
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<feed xmlns=\"http://www.w3.org/2005/Atom\">"
            f"<id>{esc(base_url)}/v1/outcomes/feed.atom</id><title>Rain-Outsource reported outcomes</title>"
            f"<link rel=\"self\" href=\"{esc(base_url)}/v1/outcomes/feed.atom\"/><link href=\"{esc(base_url)}/scoreboard\"/>"
            f"<updated>{esc(updated)}</updated>{''.join(items)}</feed>\n")


def scoreboard_html(board: dict[str, Any] | None = None) -> str:
    b = board or scoreboard()
    esc = lambda s: html.escape(str(s if s is not None else "-"))

    def row(name: str, a: dict[str, Any]) -> str:
        return (f"<tr><td>{esc(name)}</td><td>{a.get('events', 0)}</td><td>{esc(a.get('directional_accuracy_percent'))}"
                f"{'%' if a.get('directional_accuracy_percent') is not None else ''}</td><td>{esc(a.get('confident_wrong', 0))}</td>"
                f"<td>{esc(a.get('brier_score'))}</td></tr>")
    rows = row("All reported events", b["overall"]) + row("Model only (no official status)", b["model_only"])
    rows += "".join(row(f"Surface: {k}", v) for k, v in b["by_surface"].items())
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Rain-Outsource accuracy scoreboard</title><style>body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;"
            "padding:0 1rem;line-height:1.5}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.4rem;"
            "text-align:left}</style></head><body><h1>How accurate are our odds?</h1>"
            f"<p>Computed from 'Was it on?' reports against the odds we published <em>before</em> each event. As of {esc(b['as_of'])}.</p>"
            "<table><tr><th></th><th>Events</th><th>Directional</th><th>Confident wrong</th><th>Brier</th></tr>"
            f"{rows}</table><p>{esc(b['method'])}</p><p>{b['reported_without_snapshot']} reported event(s) had no pre-event odds on "
            "record and are not scored.</p><p><a href=\"/v1/outcomes/feed.atom\">Atom feed</a> · <a href=\"/v1/outcomes\">JSON</a> · "
            "Report: <code>POST /v1/outcomes {field_id, date, outcome}</code></p></body></html>\n")

"""Community "report a cancellation" reports.

Reports live in the existing cache layer (``rainout_agent.cache``): in-memory per
serverless instance by default, or shared when a backend such as Upstash/Vercel KV is
configured. Nothing is persisted elsewhere and no personal data is stored: the client
address is only used as a rate-limit key, after a keyed hash (HMAC-SHA256 under the daily
salt from ``RAINOUT_METRICS_SALT``, see ``client_key``). Rate-limit keys expire after an hour.

Recent reports nudge the play odds only while the official status is unknown; a
known official status always wins.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rainout_agent.cache import Cache, MemoryCache, get_cache

STATUSES = ("cancelled", "delayed", "field_closed", "playing")
STATUS_ALIASES = {
    "cancelled": "cancelled", "canceled": "cancelled", "rained_out": "cancelled", "rainout": "cancelled",
    "postponed": "cancelled", "delayed": "delayed", "delay": "delayed", "field_closed": "field_closed",
    "closed": "field_closed", "playing": "playing", "on": "playing", "open": "playing", "played": "playing",
}
NEGATIVE_WEIGHT = {"cancelled": 1.0, "field_closed": 1.0, "delayed": 0.5}

REPORT_TTL_S = 24 * 3600            # reports expire after a day
RELEVANT_HOURS = 12                 # only reports from the last 12 h affect the odds
MAX_REPORTS_PER_FIELD = 50
MAX_NOTE_CHARS = 280
GAME_PAST_LIMIT = timedelta(hours=12)
GAME_FUTURE_LIMIT = timedelta(hours=36)
RATE_LIMIT_PER_HOUR = 10            # per client, all fields
RATE_LIMIT_FIELD_PER_HOUR = 3       # per client and field

_LOCAL_STORE = MemoryCache(max_entries=4000)
_LOCK = threading.Lock()
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


class ReportError(ValueError):
    def __init__(self, status: int, error: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.error, self.extra = status, error, extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self), **self.extra}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _store() -> Cache:
    """The shared cache backend when one is configured, else a dedicated memory tier
    (same MemoryCache class) so weather lookups can't evict reports."""
    cache = get_cache()
    return _LOCAL_STORE if cache.name in {"memory", "none"} else cache


def _key(field_id: str) -> str:
    return f"reports:{field_id}"


def _parse_time(value: Any, now: datetime) -> datetime:
    if value in (None, ""):
        return now
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError(400, "invalid_game_time", "game_time must be ISO 8601, e.g. 2026-10-20T19:00:00-05:00.") from exc
    if parsed.tzinfo is None:
        raise ReportError(400, "invalid_game_time", "game_time needs a UTC offset, e.g. 2026-10-20T19:00:00-05:00.")
    return parsed


def _field_zone(field: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(field.get("timezone") or "America/Chicago")
    except Exception:  # pragma: no cover - bad data
        return ZoneInfo("America/Chicago")


def client_from_headers(headers: dict[str, str], fallback: str | None = None) -> str | None:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    forwarded = (h.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (h.get("x-real-ip") or "").strip() or fallback


def client_key(client: str | None, now_s: float | None = None) -> str:
    """Rate-limit key for a client address: HMAC-SHA256 under a salt that rotates every day.

    The salt is the same daily salt the usage counters use (``metrics._salt``:
    HMAC(RAINOUT_METRICS_SALT, America/Chicago date)), read from the environment only and
    never logged. A purpose prefix keeps these keys distinct from the metrics hashes, so the
    two can't be matched to each other. Without the secret, a key can't be turned back into
    an address by trying candidate IPs.

    Fallback: if RAINOUT_METRICS_SALT is unset (local runs, tests, a misconfigured deploy),
    ``metrics._salt`` uses a random per-instance salt. Nothing breaks, but each serverless
    instance then keeps its own limits, so the shared limit gets looser. The keys still can't
    be reversed. Around midnight CT the key changes, so a client's hourly count can restart
    early once a day; that's accepted for a spam limit.
    """
    from rainout_agent import metrics

    day = metrics.today(datetime.fromtimestamp(now_s if now_s is not None else time.time(), timezone.utc))
    msg = b"reports-rl:" + (client or "anonymous").encode()
    return hmac.new(metrics._salt(day), msg, hashlib.sha256).hexdigest()[:24]


def _check_rate(store: Cache, client: str, field_id: str, now_s: float) -> None:
    hour = int(now_s // 3600)
    retry = 3600 - int(now_s % 3600)
    for key, limit in ((f"reports:rl:{client}:{hour}", RATE_LIMIT_PER_HOUR),
                       (f"reports:rl:{client}:{field_id}:{hour}", RATE_LIMIT_FIELD_PER_HOUR)):
        if int(store.get(key) or 0) >= limit:
            raise ReportError(429, "rate_limited", f"Too many reports; at most {limit} per hour. Try again later.",
                              retry_after_seconds=retry)
    for key in (f"reports:rl:{client}:{hour}", f"reports:rl:{client}:{field_id}:{hour}"):
        store.set(key, int(store.get(key) or 0) + 1, 3600)


def submit_report(data: dict[str, Any], client: str | None = None, now: datetime | None = None,
                  clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """Validate and store one report. Raises ReportError (400/404/429)."""
    from rainout_agent.status_api import load_field, resolve_field_id

    if not isinstance(data, dict):
        raise ReportError(400, "invalid_body", "Send a JSON object with field_id and status.")
    now = now or _utcnow()
    query = str(data.get("field_id") or data.get("field") or "").strip()
    if not query or len(query) > 200:
        raise ReportError(400, "missing_field_id", "field_id is required (see /v1/fields).")
    field_id = resolve_field_id(query)
    if not field_id:
        raise ReportError(404, "unknown_field", "Unknown field_id; search /v1/fields?q=<name>&state=<ST> first.")
    raw_status = re.sub(r"[\s-]+", "_", str(data.get("status") or "").strip().lower())
    status = STATUS_ALIASES.get(raw_status)
    if not status:
        raise ReportError(400, "invalid_status", f"status must be one of: {', '.join(STATUSES)}.", allowed=list(STATUSES))
    game_time = _parse_time(data.get("game_time"), now)
    if not (now - GAME_PAST_LIMIT <= game_time <= now + GAME_FUTURE_LIMIT):
        raise ReportError(400, "game_time_out_of_range", "Reports are only accepted for games from 12 hours ago to 36 hours ahead.")
    note = data.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise ReportError(400, "invalid_note", "note must be a string.")
        note = _CTRL.sub(" ", note).strip()
        if len(note) > MAX_NOTE_CHARS:
            raise ReportError(400, "note_too_long", f"note must be at most {MAX_NOTE_CHARS} characters.")
    field = load_field(field_id)
    game_date = game_time.astimezone(_field_zone(field)).date().isoformat()

    store = _store()
    with _LOCK:
        now_s = (clock or time.time)()
        _check_rate(store, client_key(client, now_s), field_id, now_s)
        report = {
            "id": uuid.uuid4().hex[:12],
            "field_id": field_id,
            "status": status,
            "game_date": game_date,
            "game_time": game_time.isoformat(),
            "reported_at": now.isoformat(timespec="seconds"),
            "note": note or None,
        }
        reports = [r for r in (store.get(_key(field_id)) or []) if _is_fresh(r, now, REPORT_TTL_S / 3600)]
        reports = (reports + [report])[-MAX_REPORTS_PER_FIELD:]
        store.set(_key(field_id), reports, REPORT_TTL_S)
    return {
        "accepted": True,
        "report": report,
        "field_name": field.get("field_name"),
        "recent": summarize(field_id, game_date, now, reports),
        "storage": {"backend": store.name, "ttl_hours": REPORT_TTL_S // 3600,
                    "note": "Reports are unverified, expire after 24 hours and, without a shared cache "
                            "backend, are only visible to the server instance that received them."},
    }


def _is_fresh(report: dict[str, Any], now: datetime, hours: float) -> bool:
    try:
        return now - datetime.fromisoformat(report["reported_at"]) <= timedelta(hours=hours)
    except (KeyError, TypeError, ValueError):
        return False


def recent_reports(field_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _utcnow()
    try:
        reports = _store().get(_key(field_id)) or []
    except Exception:  # pragma: no cover - cache must never break a request
        return []
    return [r for r in reports if isinstance(r, dict) and _is_fresh(r, now, REPORT_TTL_S / 3600)]


def summarize(field_id: str, game_date: str | None, now: datetime | None = None,
              reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Counts of reports relevant to one game date (reported in the last 12 h)."""
    now = now or _utcnow()
    reports = recent_reports(field_id, now) if reports is None else reports
    relevant = [r for r in reports if _is_fresh(r, now, RELEVANT_HOURS) and (game_date is None or r.get("game_date") == game_date)]
    counts = {s: 0 for s in STATUSES}
    for r in relevant:
        if r.get("status") in counts:
            counts[r["status"]] += 1
    latest = max((r["reported_at"] for r in relevant), default=None)
    return {"count": len(relevant), "by_status": counts, "latest_reported_at": latest,
            "window_hours": RELEVANT_HOURS, "game_date": game_date}


def adjust_probability(probability: int, summary: dict[str, Any]) -> tuple[int, str | None]:
    """Shift play odds by recent reports. One cancellation report cuts the odds by a
    quarter (capped at an 80% cut); a "playing" report closes 15% of the gap to 100
    (capped at half). Opposite reports cancel each other out."""
    counts = summary.get("by_status") or {}
    negative = sum(NEGATIVE_WEIGHT[s] * counts.get(s, 0) for s in NEGATIVE_WEIGHT)
    net = negative - counts.get("playing", 0)
    if net > 0:
        adjusted = probability * max(0.2, 1 - 0.25 * net)
        reason = f"{summary['count']} recent user report(s) lean towards cancelled or delayed."
    elif net < 0:
        adjusted = probability + (100 - probability) * min(0.5, 0.15 * -net)
        reason = f"{summary['count']} recent user report(s) say the game is being played."
    else:
        return probability, None
    return max(0, min(100, int(round(adjusted)))), reason


def apply_to_status(response: dict[str, Any], field: dict[str, Any], game_time: datetime | None,
                    now: datetime | None = None) -> None:
    """Attach `community_reports` to a /v1/status response and adjust the odds when
    the official status is unknown."""
    if not response.get("field_id"):
        return
    now = now or _utcnow()
    game_date = game_time.astimezone(_field_zone(field)).date().isoformat() if game_time else None
    summary = summarize(response["field_id"], game_date, now)
    block: dict[str, Any] = {**summary, "applied": False, "adjustment_percent_points": 0,
                             "note": "Unverified user reports; the official status always takes precedence."}
    if summary["count"] and game_time and timedelta(hours=-6) <= game_time - now <= GAME_FUTURE_LIMIT \
            and response.get("official_status") == "unknown":
        key = "game_time_play_probability_percent"
        before = int(response.get(key) or 0)
        after, reason = adjust_probability(before, summary)
        if reason:
            response[key] = after
            block.update(applied=True, adjustment_percent_points=after - before, reason=reason,
                         play_probability_before_reports=before)
            response["reason"] = f"{response.get('reason', '')} {reason}".strip()
            if response.get("spoken_answer"):
                response["spoken_answer"] = re.sub(r"Estimated play probability is \d+%\.",
                                                   f"Estimated play probability is {after}%, counting {summary['count']} recent user report(s).",
                                                   response["spoken_answer"])
    response["community_reports"] = block

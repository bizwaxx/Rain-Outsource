"""Free alerts: subscriptions, triggers, channels (email stub, signed webhooks, calendar feed) and the dispatcher.

A league or player subscribes to one venue and game time, or to a team calendar (ICS link). The dispatcher
(``/v1/alerts/dispatch``, called by a cron or any pinger) checks every active subscription and sends:

* ``day_before``: around 6 PM local time the day before the start;
* ``three_hours``: 3 hours before the start;
* ``change``: the official status flips, the play odds cross 50 %, or a new player report / outcome arrives.

Channels: ``email`` (pluggable sender, a STUB by default: it renders, logs and stores the message, and never
sends), ``webhook`` (HMAC-signed POST, stored instead of sent unless ``RAINOUT_ALERTS_LIVE=1``) and
``calendar`` (a per-subscription ICS feed, refreshed on every fetch, built with the schedule ICS code).

Privacy: we keep only the email address or webhook URL, the venue + time or the ICS link, token hashes and which
alerts went out. Every message carries an unsubscribe link. Records expire a day after the event (venue) or after
120 days (calendar link). Unsubscribing deletes the record.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from rainout_agent import reports
from rainout_agent.cache import Cache

log = logging.getLogger("rainout.alerts")

BASE_URL = os.environ.get("RAINOUT_PUBLIC_URL", "https://rainout-agent-source.vercel.app").rstrip("/")
CHANNELS = ("email", "webhook", "calendar")
DAY_BEFORE_HOUR = 18           # local time, the day before
THREE_HOURS = timedelta(hours=3)
ODDS_LINE = 50
VENUE_TTL_AFTER_EVENT = timedelta(days=1)
ICS_SUB_TTL = timedelta(days=120)
PENDING_TTL_S = 2 * 86400      # unconfirmed email subscriptions vanish after 2 days
MAX_INDEX = 5000
OUTBOX_MAX = 200
SUBSCRIBE_PER_HOUR = 10        # per client (hashed address, kept 1 h)
WEBHOOK_TIMEOUT_S = 5.0
WEBHOOK_RETRIES = 3
WEBHOOK_BACKOFF_S = (0.5, 1.5)
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_LOCK = threading.Lock()


class AlertError(ValueError):
    def __init__(self, status: int, error: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.error, self.message, self.extra = status, error, message, extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.extra,
                "docs": "https://github.com/bizwaxx/Rain-Outsource/blob/main/docs/alerts.md"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def live_mode() -> bool:
    """Webhooks are only really POSTed when RAINOUT_ALERTS_LIVE=1. Email has no live sender at all yet."""
    return os.environ.get("RAINOUT_ALERTS_LIVE") == "1"


def _store() -> Cache:
    return reports._store()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sub_key(sub_id: str) -> str:
    return f"alerts:sub:{sub_id}"


# ------------------------------------------------------------------ storage
def load(sub_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-f0-9]{16}", sub_id or ""):
        return None
    rec = _store().get(_sub_key(sub_id))
    return rec if isinstance(rec, dict) else None


def _ttl_for(rec: dict[str, Any], now: datetime) -> float:
    if not rec.get("confirmed"):
        return PENDING_TTL_S
    return max(60.0, (datetime.fromisoformat(rec["expires_at"]) - now).total_seconds())


def save(rec: dict[str, Any], now: datetime | None = None) -> None:
    now = now or _now()
    store = _store()
    store.set(_sub_key(rec["id"]), rec, _ttl_for(rec, now))
    with _LOCK:
        index = [i for i in (store.get("alerts:index") or []) if i != rec["id"]] + [rec["id"]]
        store.set("alerts:index", index[-MAX_INDEX:], ICS_SUB_TTL.total_seconds())


def delete(sub_id: str) -> None:
    store = _store()
    store.set(_sub_key(sub_id), None, 1)
    with _LOCK:
        store.set("alerts:index", [i for i in (store.get("alerts:index") or []) if i != sub_id], ICS_SUB_TTL.total_seconds())


def all_ids() -> list[str]:
    return list(_store().get("alerts:index") or [])


def authorize(sub_id: str, token: str, allow_unsubscribe_token: bool = False) -> dict[str, Any]:
    """The manage token (shown once) opens everything; the unsubscribe token in messages can only unsubscribe."""
    rec = load(sub_id)
    ok = bool(rec and token) and (hmac.compare_digest(rec.get("token_hash", ""), _hash(token)) or (
        allow_unsubscribe_token and hmac.compare_digest(rec.get("unsub_token", ""), token)))
    if not ok:
        raise AlertError(404, "not_found", "Unknown subscription or wrong token.")
    return rec


# ------------------------------------------------------------------ links
def unsubscribe_url(rec: dict[str, Any], token: str | None = None) -> str:
    return f"{BASE_URL}/v1/alerts/unsubscribe?id={rec['id']}&token={quote(token or rec['unsub_token'])}"


def calendar_url(rec: dict[str, Any], token: str) -> str:
    return f"{BASE_URL}/v1/alerts/{rec['id']}/calendar.ics?token={quote(token)}"


def confirm_url(rec: dict[str, Any], confirm_token: str) -> str:
    return f"{BASE_URL}/v1/alerts/confirm?id={rec['id']}&code={quote(confirm_token)}"


# ------------------------------------------------------------------ webhook URL safety + signing
def validate_webhook_url(url: str) -> str:
    from rainout_agent.safe_fetch import UnsafeURLError, validate_url

    if not isinstance(url, str) or len(url) > 500:
        raise AlertError(400, "invalid_webhook_url", "webhook_url must be an https URL of at most 500 characters.")
    try:
        scheme, _host, _port, norm = validate_url(url)
    except UnsafeURLError as exc:
        raise AlertError(400, "unsafe_webhook_url", f"webhook_url not allowed: {exc}.") from None
    if scheme != "https" or url.lower().startswith("webcal:"):
        raise AlertError(400, "unsafe_webhook_url", "webhook_url must use https.")
    return norm


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Signature header value: ``t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>`` (Stripe-style)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300, now: float | None = None) -> bool:
    """Reference verifier for subscribers (also used in tests)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - ts) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, ts, body), header)


def post_webhook(url: str, body: bytes, secret: str, *, resolver: Callable[[str, int], str] | None = None,
                 validator: Callable[[str], str] | None = None, retries: int = WEBHOOK_RETRIES,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = WEBHOOK_TIMEOUT_S,
                 scheme_override: str | None = None) -> dict[str, Any]:
    """POST a signed body with retries. The host is re-validated and resolved to a public IP each attempt, the
    connection is pinned to that IP (no DNS rebinding) and redirects are not followed. 2xx = delivered;
    4xx (except 408/429) = give up; 5xx, 408, 429 and network errors are retried."""
    from rainout_agent.safe_fetch import (FetchError, UnsafeURLError, _PinnedHTTPConnection, _PinnedHTTPSConnection,
                                          resolve_public_ip)

    resolver = resolver or resolve_public_ip
    validator = validator or validate_webhook_url
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, retries + 1):
        try:
            norm = validator(url)
            parts = urlsplit(norm)
            scheme = scheme_override or parts.scheme
            port = parts.port or (443 if scheme == "https" else 80)
            ip = resolver(parts.hostname or "", port)
            conn_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
            conn = conn_cls(parts.hostname, ip, port, timeout=timeout)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            ts = int(time.time())
            try:
                conn.request("POST", path, body=body, headers={
                    "Content-Type": "application/json", "User-Agent": "Rain-Outsource alerts (+https://github.com/bizwaxx/Rain-Outsource)",
                    "X-Rainout-Signature": sign(secret, ts, body), "X-Rainout-Attempt": str(attempt)})
                resp = conn.getresponse()
                resp.read(4096)
                status = resp.status
            finally:
                conn.close()
            attempts.append({"attempt": attempt, "status": status})
            if 200 <= status < 300:
                return {"delivered": True, "attempts": attempts}
            if 400 <= status < 500 and status not in (408, 429):
                return {"delivered": False, "attempts": attempts, "gave_up": "client_error"}
        except (UnsafeURLError, AlertError) as exc:
            attempts.append({"attempt": attempt, "error": "unsafe_url", "message": str(exc)})
            return {"delivered": False, "attempts": attempts, "gave_up": "unsafe_url"}
        except (FetchError, OSError, http.client.HTTPException) as exc:
            attempts.append({"attempt": attempt, "error": exc.__class__.__name__})
        if attempt < retries:
            sleep(WEBHOOK_BACKOFF_S[min(attempt - 1, len(WEBHOOK_BACKOFF_S) - 1)])
    return {"delivered": False, "attempts": attempts, "gave_up": "retries_exhausted"}


# ------------------------------------------------------------------ senders (pluggable)
class StubEmailSender:
    """Default email sender: renders, logs and stores the message in the outbox. Never sends anything.

    A real sender (Resend, Brevo, Mailjet, Postmark, Amazon SES ...) only needs a ``send(message) -> dict`` method;
    it needs an account in Jimmy's name and is deliberately not included."""

    name = "stub"

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        log.info("alerts email (stub, not sent) to=%s subject=%s", message.get("to"), message.get("subject"))
        return {"sent": False, "stub": True, "sender": self.name}


_EMAIL_SENDER: Any = StubEmailSender()


def set_email_sender(sender: Any | None) -> None:
    global _EMAIL_SENDER
    _EMAIL_SENDER = sender or StubEmailSender()


def _outbox_add(entry: dict[str, Any]) -> None:
    store = _store()
    with _LOCK:
        box = (store.get("alerts:outbox") or []) + [entry]
        store.set("alerts:outbox", box[-OUTBOX_MAX:], 7 * 86400)


def outbox(limit: int = 50) -> list[dict[str, Any]]:
    return list(reversed((_store().get("alerts:outbox") or [])[-limit:]))


def deliver(rec: dict[str, Any], message: dict[str, Any], now: datetime | None = None,
            webhook_poster: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send one rendered message on the subscription's channel and record it in the outbox."""
    now = now or _now()
    entry = {"at": now.isoformat(timespec="seconds"), "subscription_id": rec["id"], "channel": rec["channel"],
             "trigger": message.get("trigger"), "subject": message.get("subject"), "test_mode": True}
    if rec["channel"] == "email":
        result = _EMAIL_SENDER.send({**message, "to": rec["email"]})
        entry.update({"to": rec["email"], "text": message["text"], "result": result,
                      "test_mode": not result.get("sent")})
    elif rec["channel"] == "webhook":
        body = json.dumps(message["webhook"], separators=(",", ":"), sort_keys=True).encode()
        if live_mode():
            result = (webhook_poster or post_webhook)(rec["webhook_url"], body, rec["webhook_secret"])
            entry["test_mode"] = False
        else:
            result = {"delivered": False, "test_mode": True,
                      "note": "Test mode: stored, not POSTed. Set RAINOUT_ALERTS_LIVE=1 to deliver."}
        entry.update({"to": rec["webhook_url"], "body": body.decode(), "signature": sign(rec["webhook_secret"], int(now.timestamp()), body),
                      "result": result})
    else:  # calendar subscriptions have nothing to push; the feed updates on fetch
        result = {"sent": False, "note": "calendar feed updates on fetch"}
        entry["result"] = result
    _outbox_add(entry)
    return result


# ------------------------------------------------------------------ subscribe / confirm / unsubscribe
def subscribe(data: dict[str, Any], client: str | None = None, now: datetime | None = None,
              clock: Callable[[], float] | None = None) -> dict[str, Any]:
    from rainout_agent.status_api import resolve_field_id

    if not isinstance(data, dict):
        raise AlertError(400, "invalid_body", "Send a JSON object.")
    now = now or _now()
    channel = str(data.get("channel") or "").strip().lower()
    if channel not in CHANNELS:
        raise AlertError(400, "invalid_channel", f"channel must be one of: {', '.join(CHANNELS)}.", allowed=list(CHANNELS))
    rec: dict[str, Any] = {"id": secrets.token_hex(8), "channel": channel, "created_at": now.isoformat(timespec="seconds"),
                           "sent": {}, "last": {}, "confirmed": channel != "email"}
    if data.get("ics_url"):
        from rainout_agent.safe_fetch import UnsafeURLError, validate_url

        try:
            _s, _h, _p, ics = validate_url(str(data["ics_url"])[:1000])
        except UnsafeURLError as exc:
            raise AlertError(400, "unsafe_ics_url", f"ics_url not allowed: {exc}.") from None
        rec.update({"kind": "team_ics", "ics_url": ics, "expires_at": (now + ICS_SUB_TTL).isoformat()})
    else:
        query = str(data.get("field_id") or data.get("venue") or "").strip()
        if not query or len(query) > 200:
            raise AlertError(400, "missing_target", "Give field_id (or venue) plus game_time, or ics_url.")
        field_id = resolve_field_id(query)
        if not field_id:
            raise AlertError(404, "unknown_field", "Unknown venue; search /v1/fields?q=<name>&state=<ST> first.")
        try:
            start = datetime.fromisoformat(str(data.get("game_time") or "").strip().replace("Z", "+00:00"))
        except ValueError:
            raise AlertError(400, "invalid_game_time", "game_time must be ISO 8601 with a UTC offset, e.g. 2026-10-23T19:00:00-05:00.") from None
        if start.tzinfo is None:
            raise AlertError(400, "invalid_game_time", "game_time needs a UTC offset, e.g. -05:00.")
        if not now - timedelta(hours=1) <= start <= now + timedelta(days=31):
            raise AlertError(400, "game_time_out_of_range", "game_time must be within the next 31 days.")
        rec.update({"kind": "venue", "field_id": field_id, "game_time": start.isoformat(),
                    "title": (str(data.get("title"))[:120] if data.get("title") else None),
                    "expires_at": (start + VENUE_TTL_AFTER_EVENT).isoformat()})
    if channel == "email":
        email = str(data.get("email") or "").strip()
        if not EMAIL_RE.match(email):
            raise AlertError(400, "invalid_email", "email must be a valid address.")
        rec["email"] = email.lower()
    elif channel == "webhook":
        rec["webhook_url"] = validate_webhook_url(str(data.get("webhook_url") or ""))
        rec["webhook_secret"] = "whsec_" + secrets.token_urlsafe(24)
    if data.get("test") in (True, "true", "1", 1):
        rec["test"] = True

    store = _store()
    hour = int((clock or time.time)() // 3600)
    rl_key = f"alerts:rl:{reports.client_key(client)}:{hour}"
    with _LOCK:
        count = int(store.get(rl_key) or 0)
        if count >= SUBSCRIBE_PER_HOUR:
            raise AlertError(429, "rate_limited", f"At most {SUBSCRIBE_PER_HOUR} new subscriptions per hour.")
        store.set(rl_key, count + 1, 3600)

    token = secrets.token_urlsafe(24)
    rec["token_hash"] = _hash(token)
    rec["unsub_token"] = secrets.token_urlsafe(12)  # can only unsubscribe; goes into every message
    out: dict[str, Any] = {"subscribed": True, "id": rec["id"], "channel": channel, "kind": rec["kind"],
                           "token": token, "unsubscribe_url": unsubscribe_url(rec),
                           "calendar_url": calendar_url(rec, token), "expires_at": rec["expires_at"],
                           "mode": "live" if (channel == "webhook" and live_mode()) else "test",
                           "triggers": ["day_before (about 6 PM local)", "three_hours", "change (official status, odds crossing 50%, new report or outcome)"],
                           "note": "Keep the token: it is shown once and is needed to unsubscribe or read the calendar feed."}
    if channel == "email":
        code = secrets.token_urlsafe(16)
        rec["confirm_hash"] = _hash(code)
        save(rec, now)
        deliver(rec, render_confirmation(rec, code), now)
        out.update({"status": "pending_confirmation",
                    "note": out["note"] + " We sent a confirmation link to the email address; no alerts go out until it is clicked. "
                            "Email sending is a stub right now: the message is stored, not sent."})
    else:
        save(rec, now)
        out["status"] = "active"
    if channel == "webhook":
        out["webhook_secret"] = rec["webhook_secret"]
        out["signature_header"] = "X-Rainout-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, '<t>.' + body)>"
    return out


def confirm(sub_id: str, code: str, now: datetime | None = None) -> dict[str, Any]:
    rec = load(sub_id)
    if not rec or rec.get("confirmed"):
        if rec:
            return {"confirmed": True, "id": sub_id}
        raise AlertError(404, "not_found", "Unknown or expired subscription.")
    if not code or not hmac.compare_digest(rec.get("confirm_hash", ""), _hash(code)):
        raise AlertError(404, "not_found", "Wrong confirmation code.")
    rec["confirmed"] = True
    rec.pop("confirm_hash", None)
    save(rec, now)
    return {"confirmed": True, "id": sub_id}


def unsubscribe(sub_id: str, token: str) -> dict[str, Any]:
    authorize(sub_id, token, allow_unsubscribe_token=True)
    delete(sub_id)
    return {"unsubscribed": True, "id": sub_id, "note": "Deleted: the address or URL and the venue are no longer stored."}


# ------------------------------------------------------------------ evaluation
def evaluate(rec: dict[str, Any], now: datetime | None = None, checker: Callable[..., dict[str, Any]] | None = None,
             fetcher: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Run the schedule engine for the subscription's games (one venue game or the team calendar)."""
    from rainout_agent import schedule

    now = now or _now()
    checker = checker or schedule.check_schedule
    if rec["kind"] == "team_ics":
        from rainout_agent.schedule_api import fetch_ics_text

        games = schedule.parse_ics(fetch_ics_text(rec["ics_url"], fetcher) if fetcher else fetch_ics_text(rec["ics_url"]))
    else:
        games = schedule.parse_json_games([{"start": rec["game_time"], "venue": rec["field_id"], "title": rec.get("title") or "Game"}])
    return checker(games, now=now, window_days=14)


def _signals(game: dict[str, Any], now: datetime) -> dict[str, Any]:
    from rainout_agent import outcomes

    fid = (game.get("venue") or {}).get("field_id")
    start = datetime.fromisoformat(game["start"])
    day = start.date().isoformat()
    rep = reports.summarize(fid, day, now) if fid else {"count": 0}
    outs = [o for o in outcomes.venue_outcomes(fid) if o.get("date") == day] if fid else []
    return {"official": game.get("official_status"), "pct": game.get("play_probability_percent"),
            "reports": rep.get("count", 0), "outcomes": len(outs)}


def due_triggers(game: dict[str, Any], sent: dict[str, Any], last: dict[str, Any] | None, sig: dict[str, Any],
                 now: datetime) -> list[str]:
    start = datetime.fromisoformat(game["start"])
    uid = str(game.get("uid"))
    out: list[str] = []
    if now > start + THREE_HOURS:
        due_change = bool(last) and sig["outcomes"] > last.get("outcomes", 0)   # outcomes come in after the game
        return ["change"] if due_change else []
    local = start  # start carries the venue's UTC offset from the schedule engine
    day_before_at = (local - timedelta(days=1)).replace(hour=DAY_BEFORE_HOUR, minute=0, second=0, microsecond=0)
    if now >= start - THREE_HOURS and now < start and f"three_hours:{uid}" not in sent:
        out.append("three_hours")   # supersedes a missed day-before alert
    elif day_before_at <= now < start - THREE_HOURS and f"day_before:{uid}" not in sent:
        out.append("day_before")
    if last and not out:
        flip = last.get("official") not in (None, sig["official"]) or (last.get("official") is None and sig["official"] not in (None, "unknown"))
        crossed = (last.get("pct") is not None and sig["pct"] is not None
                   and (last["pct"] >= ODDS_LINE) != (sig["pct"] >= ODDS_LINE))
        news = sig["reports"] > last.get("reports", 0) or sig["outcomes"] > last.get("outcomes", 0)
        if flip or crossed or news:
            out.append("change")
    return out


def _change_reason(last: dict[str, Any] | None, sig: dict[str, Any]) -> str | None:
    if not last:
        return None
    if last.get("official") != sig["official"] and sig["official"] not in (None, "unknown"):
        return f"Official status changed to {sig['official']}."
    if last.get("pct") is not None and sig["pct"] is not None and (last["pct"] >= ODDS_LINE) != (sig["pct"] >= ODDS_LINE):
        return f"Play odds moved from {last['pct']}% to {sig['pct']}%."
    if sig["reports"] > last.get("reports", 0):
        return "A player sent a new report from the field."
    if sig["outcomes"] > last.get("outcomes", 0):
        return "Someone reported whether it was played."
    return None


# ------------------------------------------------------------------ rendering
def _when(game: dict[str, Any]) -> str:
    start = datetime.fromisoformat(game["start"])
    return start.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def render_alert(rec: dict[str, Any], game: dict[str, Any], trigger: str, sig: dict[str, Any],
                 change: str | None, now: datetime) -> dict[str, Any]:
    venue = (game.get("venue") or {}).get("name") or game.get("location_text") or "your field"
    pct = sig["pct"]
    official = sig["official"] or "unknown"
    head = {"day_before": "Tomorrow", "three_hours": "In 3 hours", "change": "Update"}[trigger]
    subject = f"{head}: {game.get('title') or 'Game'} at {venue}, " + (f"{pct}% chance of play" if pct is not None else "odds not ready")
    lines = [f"{game.get('title') or 'Game'} at {venue}, {_when(game)}."]
    if change:
        lines.append(change)
    lines.append(f"Official status: {'not posted yet' if official == 'unknown' else official}. The league's call always wins.")
    if pct is not None:
        lines.append(f"Chance it's played: {pct}%." + (f" Rain chance {game['rain_chance_percent']}%." if game.get("rain_chance_percent") is not None else ""))
    if game.get("reason"):
        lines.append(game["reason"])
    if sig["reports"]:
        lines.append(f"Player reports today: {sig['reports']}.")
    phone = game.get("rainout_phone")
    if phone and phone != "not_published":
        lines.append(f"Rainout line: {phone}")
    if game.get("official_status_source_url"):
        lines.append(f"Official source: {game['official_status_source_url']}")
    unsub = unsubscribe_url(rec)
    lines += ["", "Odds are estimates, not safety advice.", f"Unsubscribe: {unsub}"]
    text = "\n".join(lines)
    return {"trigger": trigger, "subject": subject, "text": text,
            "webhook": {"type": "rainout.alert", "trigger": trigger, "subscription_id": rec["id"], "sent_at": now.isoformat(timespec="seconds"),
                        "change": change, "event": {"uid": game.get("uid"), "title": game.get("title"), "start": game.get("start"),
                                                     "field_id": (game.get("venue") or {}).get("field_id"), "venue": venue,
                                                     "official_status": official, "play_probability_percent": pct,
                                                     "rain_chance_percent": game.get("rain_chance_percent"), "reason": game.get("reason"),
                                                     "player_reports": sig["reports"], "outcomes": sig["outcomes"],
                                                     "status_url": game.get("status_url")},
                        "summary": subject, "unsubscribe_url": unsub, "test": bool(rec.get("test")) or not live_mode()}}


def render_confirmation(rec: dict[str, Any], code: str) -> dict[str, Any]:
    what = rec.get("field_id") or "your team calendar"
    text = "\n".join([
        "Someone (hopefully you) asked for free Rain-Outsource rain alerts" + (f" for {what}" if what else "") + ".",
        f"Confirm here: {confirm_url(rec, code)}",
        "If it wasn't you, ignore this email and nothing will be sent.",
        "", f"Unsubscribe any time: {unsubscribe_url(rec)}"])
    return {"trigger": "confirm", "subject": "Confirm your free Rain-Outsource alerts", "text": text, "webhook": {}}


def calendar_feed(rec: dict[str, Any], now: datetime | None = None, checker: Callable[..., dict[str, Any]] | None = None,
                  fetcher: Callable[[str], Any] | None = None) -> str:
    """Per-subscription ICS, recomputed on every fetch, with the odds in titles and descriptions."""
    from rainout_agent.schedule import build_risk_ics

    report = evaluate(rec, now, checker, fetcher)
    return build_risk_ics(report).replace("X-WR-CALNAME:Rainout Source risk", "X-WR-CALNAME:Rain-Outsource alerts", 1)


# ------------------------------------------------------------------ dispatcher
def dispatch(now: datetime | None = None, budget_s: float = 20.0, checker: Callable[..., dict[str, Any]] | None = None,
             fetcher: Callable[[str], Any] | None = None, webhook_poster: Callable[..., dict[str, Any]] | None = None,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Check every active subscription once and send what is due. Safe to call as often as every minute:
    each (trigger, game) pair goes out once; changes are compared with the last stored signals."""
    now = now or _now()
    started = clock()
    summary = {"checked": 0, "sent": 0, "skipped_unconfirmed": 0, "expired": 0, "errors": 0, "out_of_time": 0,
               "by_trigger": {"day_before": 0, "three_hours": 0, "change": 0}, "mode": "live" if live_mode() else "test",
               "email_sender": getattr(_EMAIL_SENDER, "name", "custom"), "at": now.isoformat(timespec="seconds")}
    for sub_id in all_ids():
        if clock() - started > budget_s:
            summary["out_of_time"] += 1
            continue
        rec = load(sub_id)
        if not rec:
            delete(sub_id)
            continue
        if datetime.fromisoformat(rec["expires_at"]) < now:
            delete(sub_id)
            summary["expired"] += 1
            continue
        if not rec.get("confirmed"):
            summary["skipped_unconfirmed"] += 1
            continue
        if rec["channel"] == "calendar":
            continue  # the feed is computed on fetch
        summary["checked"] += 1
        try:
            report = evaluate(rec, now, checker, fetcher)
            for game in report.get("games", []):
                if not game.get("start") or game.get("status") == "unmatched_venue":
                    continue
                uid = str(game.get("uid"))
                sig = _signals(game, now)
                last = rec["last"].get(uid)
                for trig in due_triggers(game, rec["sent"], last, sig, now):
                    change = _change_reason(last, sig) if trig == "change" else None
                    deliver(rec, render_alert(rec, game, trig, sig, change, now), now, webhook_poster)
                    if trig != "change":
                        rec["sent"][f"{trig}:{uid}"] = now.isoformat(timespec="seconds")
                    summary["sent"] += 1
                    summary["by_trigger"][trig] += 1
                rec["last"][uid] = sig
            save(rec, now)
        except Exception as exc:  # one bad subscription must not stop the run
            log.warning("alerts dispatch failed for %s: %s", sub_id, exc.__class__.__name__)
            summary["errors"] += 1
    return summary


def dispatch_authorized(headers: dict[str, str], query: dict[str, str]) -> tuple[bool, str]:
    """Internal token (X-Rainout-Internal), Vercel cron (Authorization: Bearer $CRON_SECRET) or a dispatch-only
    key (?key=$RAINOUT_DISPATCH_KEY, for pingers that can't set headers)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    candidates = []
    internal = os.environ.get("RAINOUT_INTERNAL_TOKEN") or ""
    cron = os.environ.get("CRON_SECRET") or ""
    key = os.environ.get("RAINOUT_DISPATCH_KEY") or ""
    if not (internal or cron or key):
        return False, "not_configured"
    if internal:
        candidates.append((h.get("x-rainout-internal", ""), internal))
    if cron:
        candidates.append((h.get("authorization", ""), f"Bearer {cron}"))
    if key:
        candidates.append((query.get("key", ""), key))
    ok = any(given and hmac.compare_digest(given, want) for given, want in candidates)
    return ok, "ok" if ok else "forbidden"

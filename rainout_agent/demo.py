"""ATX Boys staging demo: one Austin field, a one-tap rainout call, a test notification.

Served by the existing index function (no new Vercel function). Safety rules:

* Off in production: every /demo route returns 404 when VERCEL_ENV=production,
  so merging this by accident can't expose it on the live site.
* Demo writes never touch prod data. They go to keys under ``rainout:demo:``
  (separate from the ``rainout:v1:`` prefix the live service uses) with a 14-day
  expiry, and only when VERCEL_ENV=preview and a shared Redis is configured.
  Anywhere else they stay in this process's memory.
* The test notification is a stub. Nothing is emailed; the outbox shows exactly
  what would be sent, and the only recipient is Jimmy (RAINOUT_DEMO_NOTIFY_TO,
  never taken from the request).
* Odds are labelled as estimates and the official status is shown first.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
LEAGUE = "ATX Boys"
FIELD_ID = "austin-tx-krieg-field-softball-complex"
DEMO_PREFIX = "rainout:demo:"
CALLS_KEY = DEMO_PREFIX + "calls"
OUTBOX_KEY = DEMO_PREFIX + "outbox"
MAX_ITEMS = 50
TTL_S = 14 * 86400
CALLS = {"on": "Game ON", "rainout": "Rained out", "delay": "Delayed"}
ROUTES = ("/demo", "/demo/call", "/demo/notify", "/demo/outbox", "/demo/calls")

_lock = threading.Lock()
_local: dict[str, list[str]] = {CALLS_KEY: [], OUTBOX_KEY: []}


def enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("VERCEL_ENV") or "").strip().lower() != "production"


def _redis(env: dict[str, str] | None = None):
    """The shared Redis, but only on a Vercel preview; None means use process memory."""
    env = os.environ if env is None else env
    if (env.get("VERCEL_ENV") or "").strip().lower() != "preview":
        return None
    from rainout_agent.cache import get_cache

    cache = get_cache()
    return cache if "upstash" in cache.name else None


def store_name(env: dict[str, str] | None = None) -> str:
    return "redis, rainout:demo: keys, 14-day expiry" if _redis(env) is not None else "memory (this server only)"


def _push(key: str, item: dict[str, Any]) -> None:
    assert key.startswith(DEMO_PREFIX)
    raw = json.dumps(item)
    r = _redis()
    if r is not None:
        try:
            r.pipeline([["LPUSH", key, raw], ["LTRIM", key, 0, MAX_ITEMS - 1], ["EXPIRE", key, TTL_S]])
            return
        except Exception:
            pass  # fall through to memory so the demo still works
    with _lock:
        _local[key].insert(0, raw)
        del _local[key][MAX_ITEMS:]


def _list(key: str, n: int = 20) -> list[dict[str, Any]]:
    assert key.startswith(DEMO_PREFIX)
    raws: list[str] | None = None
    r = _redis()
    if r is not None:
        try:
            raws = (r.pipeline([["LRANGE", key, 0, n - 1]]) or [None])[0]
        except Exception:
            raws = None
    if raws is None:
        with _lock:
            raws = list(_local[key][:n])
    out = []
    for raw in raws or []:
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return out


def reset_local() -> None:
    with _lock:
        for v in _local.values():
            v.clear()


def ct(ts: str) -> str:
    """'2026-10-21T18:31:05+00:00' -> 'Wed Oct 21, 1:31:05 PM CT'."""
    d = datetime.fromisoformat(ts).astimezone(TZ)
    return d.strftime("%a %b %-d, %-I:%M:%S %p CT")


def default_game_time(now: datetime | None = None) -> str:
    """Next 7 PM Central (today if it's before 6 PM, else tomorrow)."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    game = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if now >= game - timedelta(hours=1):
        game += timedelta(days=1)
    return game.isoformat()


def _field() -> dict[str, Any]:
    from rainout_agent.status_api import load_field

    return load_field(FIELD_ID)


def make_call(call: str, game_time: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    if call not in CALLS:
        raise ValueError(f"call must be one of {', '.join(CALLS)}")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    field = _field()
    record = {
        "id": "call_" + secrets.token_hex(6),
        "demo": True,
        "league": LEAGUE,
        "field_id": FIELD_ID,
        "field": field["field_name"],
        "game_time": game_time or default_game_time(),
        "call": call,
        "call_label": CALLS[call],
        "made_by": f"{LEAGUE} organizer (demo)",
        "recorded_at": now.isoformat(timespec="seconds"),
        "recorded_at_ct": ct(now.isoformat()),
    }
    _push(CALLS_KEY, record)
    return record


def notify_to(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("RAINOUT_DEMO_NOTIFY_TO") or "").strip()


def _mask(addr: str) -> str:
    if "@" not in addr:
        return addr
    user, dom = addr.split("@", 1)
    return (user[:1] + "***@" + dom) if user else "***@" + dom


def test_notification(now: datetime | None = None) -> dict[str, Any]:
    """Build the test email for Jimmy and put it in the outbox. Never sends."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest = (_list(CALLS_KEY, 1) or [None])[0]
    field = _field()
    if latest:
        line = f"Latest call: {latest['call_label']} at {latest['recorded_at_ct']}."
    else:
        line = "No rainout call made yet."
    to = notify_to()
    msg = {
        "id": "msg_" + secrets.token_hex(6),
        "demo": True,
        "channel": "email",
        "to_label": "Jimmy (owner) only",
        "to": _mask(to) if to else "Jimmy's address (RAINOUT_DEMO_NOTIFY_TO not set)",
        "subject": f"[TEST] {LEAGUE}: {field['field_name']} rainout update",
        "body": (f"This is a test from the {LEAGUE} staging demo.\n{line}\n"
                 f"Field: {field['field_name']}, {field['address']}.\n"
                 "Odds are estimates. Official status comes from the league."),
        "status": "not sent (stub: no email provider is set up, so nothing leaves the server)",
        "queued_at": now.isoformat(timespec="seconds"),
        "queued_at_ct": ct(now.isoformat()),
    }
    _push(OUTBOX_KEY, msg)
    return msg


def weather_card(game_time: str) -> dict[str, Any]:
    try:
        from rainout_agent.status_api import build_status_result

        return build_status_result(FIELD_ID, game_time)
    except Exception as exc:  # the demo page must always render
        return {"error": "weather_unavailable", "message": f"Live weather couldn't load ({type(exc).__name__}). Try again in a minute."}


# ------------------------------------------------------------------- HTML

CSS = """
*{box-sizing:border-box}body{margin:0;font:17px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f8;color:#111}
.banner{background:#b00020;color:#fff;text-align:center;font-weight:700;padding:10px 12px;letter-spacing:.02em}
main{max-width:560px;margin:0 auto;padding:14px}
h1{font-size:1.45rem;margin:.4em 0 .1em}h2{font-size:1.1rem;margin:0 0 .5em}
.sub{color:#555;margin:0 0 12px}
.card{background:#fff;border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow-wrap:anywhere}
.big{font-size:1.6rem;font-weight:700}.est{display:inline-block;background:#fff3cd;color:#664d03;border-radius:6px;padding:1px 8px;font-size:.85rem;font-weight:700}
.official{border-left:6px solid #0b5ed7}
.btns{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button{width:100%;min-height:52px;border:0;border-radius:10px;font-size:1rem;font-weight:700;color:#fff;cursor:pointer}
.on{background:#198754}.out{background:#b02a37}.delay{background:#b35c00}.notify{background:#0b5ed7}
ul{padding-left:18px;margin:.3em 0}li{margin:.25em 0}
pre{white-space:pre-wrap;background:#f1f3f5;border-radius:8px;padding:10px;font-size:.9rem;margin:.4em 0}
.ok{background:#d1e7dd;color:#0f5132;border-radius:8px;padding:10px;font-weight:600}
small,.muted{color:#666}a{color:#0b5ed7}
"""

BANNER = "DEMO / staging &middot; estimates only &middot; not the live service"


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def page(game_time: str | None = None, flash: str | None = None) -> str:
    game_time = game_time or default_game_time()
    s = weather_card(game_time)
    calls = _list(CALLS_KEY, 10)
    outbox = _list(OUTBOX_KEY, 5)
    gt_ct = datetime.fromisoformat(game_time).astimezone(TZ).strftime("%a %b %-d, %-I:%M %p CT")

    if s.get("error"):
        official_html = f"<p>{_e(s.get('message'))}</p>"
        odds_html = "<p class='muted'>No estimate right now.</p>"
    else:
        status = (s.get("official_status") or "unknown").upper()
        src = s.get("official_status_source_url")
        phone = None
        try:
            phone = _field().get("rainout_phone")
        except Exception:
            pass
        said = "The league has not posted a call yet." if status == "UNKNOWN" else "Posted by the official source."
        line = f' Rainout line: <a href="tel:{_e(phone)}">{_e(phone)}</a>.' if phone else ""
        official_html = (
            f"<div class='big'>{_e(status)}</div>"
            f"<p>{said}{line}</p>"
            + (f"<small>Source: <a href='{_e(src)}' rel='noopener'>{_e(s.get('official_status_source_name') or src)}</a></small>" if src else "")
        )
        odds_html = (
            f"<div><span class='est'>ESTIMATE</span></div>"
            f"<div class='big'>{_e(s.get('game_time_play_probability_percent'))}% chance the game is played</div>"
            f"<ul><li>Rain chance at game time: {_e(s.get('rain_chance_percent'))}%</li>"
            f"<li>Storms likely: {'yes' if s.get('thunderstorm_likely') else 'no'}</li>"
            f"<li>Forecast: {_e(s.get('forecast'))}</li>"
            f"<li>Weather: {_e(s.get('weather_source'))} (live)</li></ul>"
            f"<small>This is an estimate from the forecast and NOAA history. The league's call always wins.</small>"
        )

    calls_html = "".join(
        f"<li><b>{_e(c.get('call_label'))}</b> &middot; {_e(c.get('recorded_at_ct'))}<br><small>record {_e(c.get('id'))} &middot; {_e(c.get('made_by'))}</small></li>"
        for c in calls) or "<li class='muted'>No calls yet.</li>"
    outbox_html = "".join(
        f"<li><b>To:</b> {_e(m.get('to_label'))} ({_e(m.get('to'))})<br><b>Subject:</b> {_e(m.get('subject'))}"
        f"<pre>{_e(m.get('body'))}</pre><small>{_e(m.get('status'))} &middot; {_e(m.get('queued_at_ct'))}</small></li>"
        for m in outbox) or "<li class='muted'>Nothing yet.</li>"
    flash_html = f"<div class='ok' role='status'>{_e(flash)}</div>" if flash else ""
    gt = _e(game_time)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{_e(LEAGUE)} rainout demo (staging)</title>
<style>{CSS}</style></head>
<body>
<div class="banner" role="note">{BANNER}</div>
<main>
<h1>{_e(LEAGUE)} &middot; game day</h1>
<p class="sub">{_e(s.get('field') or 'Krieg Field Softball Complex')}, Austin TX<br>Game: {_e(gt_ct)}</p>
{flash_html}
<section class="card official"><h2>1. Official status</h2>{official_html}</section>
<section class="card"><h2>2. Will it be played?</h2>{odds_html}</section>
<section class="card"><h2>3. Make the call</h2>
<p>One tap saves a time-stamped record.</p>
<form method="post" action="/demo/call" class="btns">
<input type="hidden" name="game_time" value="{gt}">
<button class="on" name="call" value="on">Game ON</button>
<button class="out" name="call" value="rainout">Rained out</button>
<button class="delay" name="call" value="delay">Delayed</button>
</form>
<h2 style="margin-top:14px">Call log</h2><ul>{calls_html}</ul>
<small><a href="/demo/calls">Records as JSON</a></small></section>
<section class="card"><h2>4. Test notification</h2>
<p>Goes to Jimmy only. Test mode: nothing is emailed. You see what would be sent.</p>
<form method="post" action="/demo/notify"><button class="notify">Send test notification</button></form>
<h2 style="margin-top:14px">Outbox</h2><ul>{outbox_html}</ul>
<small><a href="/demo/outbox">Outbox as JSON</a></small></section>
<p class="muted"><small>Staging demo. Records are demo data, kept apart from live data, and expire. Times are America/Chicago.</small></p>
</main></body></html>"""


def handle(method: str, path: str, query: str = "", body: bytes = b"", ctype: str = "",
           accept: str = "") -> tuple[int, dict[str, str], bytes]:
    """Returns (status, headers, body). Caller adds CORS/metrics."""
    path = path.rstrip("/") or "/"
    nocache = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
    if not enabled():
        return 404, {"Content-Type": "application/json", **nocache}, json.dumps({"error": "not_found"}).encode()

    def as_json(code: int, payload: Any) -> tuple[int, dict[str, str], bytes]:
        return code, {"Content-Type": "application/json", **nocache}, json.dumps(payload, indent=2).encode()

    def as_html(code: int, text: str) -> tuple[int, dict[str, str], bytes]:
        return code, {"Content-Type": "text/html; charset=utf-8", **nocache}, text.encode("utf-8")

    q = {k: v[-1] for k, v in parse_qs(query or "").items()}
    form: dict[str, str] = {}
    if method == "POST" and body:
        if "json" in (ctype or ""):
            try:
                data = json.loads(body.decode("utf-8"))
                form = {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except ValueError:
                return as_json(400, {"error": "bad_json"})
        else:
            form = {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
    wants_json = "application/json" in (accept or "") or "json" in (ctype or "")

    game_time = q.get("game_time") or form.get("game_time") or None
    if game_time:
        try:
            if datetime.fromisoformat(game_time).tzinfo is None:
                raise ValueError
        except ValueError:
            return as_json(400, {"error": "bad_game_time", "message": "game_time must be ISO 8601 with an offset"})

    if path == "/demo" and method == "GET":
        return as_html(200, page(game_time, flash=q.get("flash")))
    if path == "/demo/calls" and method == "GET":
        return as_json(200, {"demo": True, "league": LEAGUE, "store": store_name(), "calls": _list(CALLS_KEY)})
    if path == "/demo/outbox" and method == "GET":
        return as_json(200, {"demo": True, "sent": False, "note": "Stub outbox: nothing is emailed.",
                             "messages": _list(OUTBOX_KEY)})
    if path == "/demo/call" and method == "POST":
        try:
            rec = make_call(form.get("call", ""), game_time)
        except ValueError as exc:
            return as_json(400, {"error": "bad_call", "message": str(exc)})
        if wants_json:
            return as_json(201, {"demo": True, "record": rec})
        return 303, {"Location": "/demo?flash=" + _qs(f"Saved: {rec['call_label']} at {rec['recorded_at_ct']}"), **nocache}, b""
    if path == "/demo/notify" and method == "POST":
        msg = test_notification()
        if wants_json:
            return as_json(201, {"demo": True, "message": msg})
        return 303, {"Location": "/demo?flash=" + _qs(f"Test notification for Jimmy added to the outbox at {msg['queued_at_ct']} (not sent)"), **nocache}, b""
    if path in ROUTES:
        return as_json(405, {"error": "method_not_allowed"})
    return as_json(404, {"error": "not_found"})


def _qs(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")

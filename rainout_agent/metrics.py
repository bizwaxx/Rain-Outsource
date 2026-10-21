"""Aggregate usage counters (per America/Chicago day), stored in the shared Upstash Redis.

What is counted, per day:
* requests per endpoint and caller class,
* MCP tool calls per tool and caller class,
* unique clients per caller class (HyperLogLog, approximate, about 0.8% error),
* events such as free-key signups.

Caller classes:
* ``internal``: our own tests and tools. Header ``X-Rainout-Internal: <RAINOUT_INTERNAL_TOKEN>``,
  or a User-Agent starting with rainout-smoke/rainout-cli/rainout-monitor/rainout-test.
* ``monitor``: known uptime and synthetic monitors (UptimeRobot, Pingdom, Better Stack, ...).
* ``bot``: crawlers and indexers (anything identifying as bot, crawler or spider).
* ``external``: everyone else, i.e. real outside users and agents.

Privacy: no raw IP or User-Agent is stored. For unique counts the client IP is hashed
with HMAC-SHA256 under a salt that rotates every day
(HMAC(RAINOUT_METRICS_SALT, date)), so a client cannot be linked across days, and
only the hash is added to a Redis HyperLogLog, which keeps register maxima, not the
hashes themselves. Everything else is plain counters.

Storage (Upstash, through the cache layer's pipeline): one hash per day
``rainout:v1:metrics:<YYYY-MM-DD>`` with fields ``req|<endpoint>|<class>``,
``tool|<name>|<class>``, ``event|<name>|<class>``, plus one HyperLogLog per class
``rainout:v1:metrics:<day>:clients:<class>``. TTL about 400 days.

Cost: one HTTP round trip per request, in a background thread, never raising.
Typically 1 command per request (HINCRBY), plus PFADD only the first time an instance
sees a client that day, plus one HINCRBY per MCP tool call, plus EXPIRE once per key per
instance per day. Without Upstash (local, tests) counts go to an in-process store.
``RAINOUT_METRICS=off`` disables counting.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from rainout_agent.cache import KEY_PREFIX, get_cache

TZ = ZoneInfo("America/Chicago")
TTL_S = 400 * 86400
CLASSES = ("external", "internal", "monitor", "bot")
INTERNAL_HEADER = "x-rainout-internal"
INTERNAL_UA = ("rainout-smoke", "rainout-cli", "rainout-monitor", "rainout-test", "rainout-health")
MONITOR_UA = ("uptimerobot", "pingdom", "betteruptime", "better uptime", "better stack", "statuscake", "site24x7",
              "checkly", "datadog", "newrelicpinger", "freshping", "hetrixtools", "uptime-kuma", "updown.io",
              "cron-job.org", "googlestackdrivermonitoring", "vercel-screenshot", "vercel-favicon", "vercelbot",
              "uptime", "healthcheck", "monitor")
BOT_UA = ("bot", "crawler", "spider", "slurp", "facebookexternalhit", "bingpreview", "headlesschrome", "embedly")
_SEEN_MAX = 20_000
_LOCAL_SALT = secrets.token_bytes(32)  # only when RAINOUT_METRICS_SALT is unset (unique counts then per instance)


def enabled() -> bool:
    return (os.environ.get("RAINOUT_METRICS") or "on").strip().lower() not in {"off", "0", "false", "no"}


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).astimezone(TZ).date().isoformat()


def classify(headers: dict[str, str] | None) -> str:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    token = os.environ.get("RAINOUT_INTERNAL_TOKEN") or ""
    sent = (h.get(INTERNAL_HEADER) or "").strip()
    if token and sent and hmac.compare_digest(sent, token):
        return "internal"
    ua = (h.get("user-agent") or "").strip().lower()
    if ua.startswith(INTERNAL_UA):
        return "internal"
    if any(m in ua for m in MONITOR_UA):
        return "monitor"
    if any(b in ua for b in BOT_UA):
        return "bot"
    return "external"


def endpoint_for(path: str) -> str:
    """Bounded endpoint labels (ids never appear, so cardinality stays small)."""
    p = (path or "/").split("?", 1)[0].rstrip("/") or "/"
    fixed = {"/": "index", "/api": "index", "/health": "health", "/v1/status": "status", "/api/status": "status",
             "/v1/reports": "reports", "/v1/fields": "fields", "/openapi.yaml": "openapi", "/llms.txt": "llms",
             "/v1/stats": "stats", "/v1/schedule/check": "schedule", "/v1/teams/search": "teams.search",
             "/v1/tracks": "tracks.list", "/v1/rain": "rain", "/mcp": "mcp", "/v1/admin/usage": "admin.usage",
             "/v1/plans": "plans", "/v1/account": "account", "/v1/batch": "batch", "/v1/keys": "keys",
             "/v1/premium/batch": "premium.batch", "/privacy": "privacy", "/privacy.html": "privacy",
             "/terms": "terms", "/terms.html": "terms", "/icon.png": "icon",
             "/.well-known/mcp/server-card.json": "server_card", "/.well-known/mcp/server-card": "server_card",
             "/robots.txt": "robots", "/sitemap.xml": "sitemap", "/switch": "site.switch", "/switch/embed": "site.embed"}
    if p in fixed:
        return fixed[p]
    if p in {"/icon.svg", "/icon.png", "/icon-400.png", "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"}:
        return "site.icon"
    m = re.fullmatch(r"/v1/teams/[^/]+/(games|calendar\.ics)", p)
    if m:
        return "teams." + ("games" if m.group(1) == "games" else "calendar")
    m = re.fullmatch(r"/v1/tracks/[^/]+/(events|check|history|calendar\.ics)", p)
    if m:
        return "tracks." + m.group(1).replace(".ics", "")
    m = re.fullmatch(r"/v1/billing/(checkout|success|portal|webhook)", p)
    if m:
        return "billing." + m.group(1)
    return "other"


def _salt(day: str) -> bytes:
    base = (os.environ.get("RAINOUT_METRICS_SALT") or "").encode() or _LOCAL_SALT
    return hmac.new(base, day.encode(), hashlib.sha256).digest()


def client_hash(client: str | None, day: str) -> str:
    return hmac.new(_salt(day), (client or "unknown").encode(), hashlib.sha256).hexdigest()[:24]


def client_from(headers: dict[str, str] | None, fallback: str | None = None) -> str | None:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    fwd = (h.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (h.get("x-real-ip") or "").strip() or fallback


FIRST_KEY = f"{KEY_PREFIX}metrics:first_outside"
_FAMILIES = (
    (("claude", "anthropic"), "Claude"), (("chatgpt", "openai", "gptbot"), "OpenAI"), (("cursor",), "Cursor"),
    (("perplexity",), "Perplexity"), (("gemini", "google-extended"), "Gemini"), (("mcp",), "mcp-client"),
    (("python-requests",), "python-requests"), (("python-httpx", "httpx"), "python-httpx"), (("aiohttp",), "python-aiohttp"),
    (("python-urllib", "python/"), "python-urllib"), (("curl/",), "curl"), (("wget",), "wget"), (("postman",), "postman"),
    (("insomnia",), "insomnia"), (("node", "undici", "axios", "got/"), "node"), (("go-http-client",), "go"),
    (("okhttp", "java/", "apache-httpclient"), "java"), (("deno",), "deno"), (("bun/",), "bun"),
    (("mozilla/",), "browser"),
)


def client_family(user_agent: str | None) -> str:
    """Coarse client family from a User-Agent (never stores the UA itself)."""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return "none"
    for needles, family in _FAMILIES:
        if any(n in ua for n in needles):
            return family
    return "other"


def country(headers: dict[str, str] | None) -> str | None:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    c = (h.get("x-vercel-ip-country") or "").strip().upper()
    return c if re.fullmatch(r"[A-Z]{2}", c) else None


_first_known = False  # per-instance flag: once the marker exists, never send SET NX again


def first_outside_command(kind: str, where: str, headers: dict[str, str] | None, now: datetime | None = None) -> list[Any] | None:
    """SET NX (no TTL) for the very first outside event; None once this instance knows it exists."""
    global _first_known
    with _state_lock:
        if _first_known:
            return None
        _first_known = True  # optimistic; reset if the write fails
    h = {k.lower(): v for k, v in (headers or {}).items()}
    value = json.dumps({"at": (now or datetime.now(TZ)).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        "kind": kind, "where": where, "client_family": client_family(h.get("user-agent")),
                        "country": country(headers)}, separators=(",", ":"))
    return ["SET", FIRST_KEY, value, "NX"]


class _LocalStore:
    """In-process stand-in for Redis (no Upstash configured: local dev and tests)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hashes: dict[str, dict[str, int]] = {}
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}

    def run(self, commands: list[list[Any]]) -> list[Any]:
        out: list[Any] = []
        with self.lock:
            for cmd in commands:
                op, key = cmd[0], cmd[1]
                if op == "HINCRBY":
                    h = self.hashes.setdefault(key, {})
                    h[cmd[2]] = h.get(cmd[2], 0) + int(cmd[3])
                    out.append(h[cmd[2]])
                elif op == "PFADD":
                    s = self.sets.setdefault(key, set())
                    n = len(s)
                    s.update(cmd[2:])
                    out.append(int(len(s) > n))
                elif op == "HGETALL":
                    flat: list[Any] = []
                    for k, v in self.hashes.get(key, {}).items():
                        flat += [k, str(v)]
                    out.append(flat)
                elif op == "SET":
                    exists = key in self.strings
                    if "NX" in cmd[3:] and exists:
                        out.append(None)
                    else:
                        self.strings[key] = cmd[2]
                        out.append("OK")
                elif op == "GET":
                    out.append(self.strings.get(key))
                elif op == "PFCOUNT":
                    out.append(len(self.sets.get(key, set())))
                else:  # EXPIRE etc.
                    out.append(1)
        return out


LOCAL = _LocalStore()
_seen: OrderedDict[str, None] = OrderedDict()
_expired: set[str] = set()
_state_lock = threading.Lock()
LAST_COMMANDS: list[list[Any]] = []  # last pipeline built (tests and cost checks)


def _run(commands: list[list[Any]]) -> list[Any]:
    cache = get_cache()
    try:
        return cache.pipeline(commands)
    except NotImplementedError:
        return LOCAL.run(commands)


def build_commands(day: str, fields: Iterable[str], cls: str, client: str | None) -> list[list[Any]]:
    hkey = f"{KEY_PREFIX}metrics:{day}"
    cmds: list[list[Any]] = [["HINCRBY", hkey, f, 1] for f in fields]
    with _state_lock:
        if cls != "internal" and client is not None:
            ch = client_hash(client, day)
            seen_key = f"{day}|{cls}|{ch}"
            if seen_key not in _seen:
                _seen[seen_key] = None
                while len(_seen) > _SEEN_MAX:
                    _seen.popitem(last=False)
                hll = f"{hkey}:clients:{cls}"
                cmds.append(["PFADD", hll, ch])
                if hll not in _expired:
                    _expired.add(hll)
                    cmds.append(["EXPIRE", hll, TTL_S])
        if hkey not in _expired:
            _expired.add(hkey)
            cmds.append(["EXPIRE", hkey, TTL_S])
    return cmds


def _kind(endpoint: str, tools: Iterable[str], events: Iterable[str]) -> tuple[str, str]:
    events, tools = list(events), list(tools)
    if "free_key_signup" in events:
        return "signup", endpoint
    if "report" in events:
        return "report", "report_cancellation" if "report_cancellation" in tools else endpoint
    return "request", (f"{endpoint}:{tools[0]}" if tools else endpoint)


def record(endpoint: str, headers: dict[str, str] | None = None, client: str | None = None,
           tools: Iterable[str] = (), events: Iterable[str] = (), now: datetime | None = None,
           background: bool = True) -> threading.Thread | None:
    """Count one request. Never raises; by default runs the Redis call in a daemon thread."""
    global _first_known
    try:
        if not enabled():
            return None
        tools, events = list(tools), list(events)
        cls = classify(headers)
        day = today(now)
        fields = ([] if endpoint == "event" else [f"req|{endpoint}|{cls}"]) + [f"tool|{t}|{cls}" for t in tools] \
            + [f"event|{e}|{cls}" for e in events]
        cmds = build_commands(day, fields, cls, None if endpoint == "event" else client_from(headers, client))
        first = None
        if cls == "external":
            kind, where = _kind(endpoint, tools, events)
            first = first_outside_command(kind, where, headers, now)
            if first:
                cmds.append(first)
        LAST_COMMANDS[:] = cmds

        def send():
            global _first_known
            try:
                _run(cmds)
            except Exception:
                if first:
                    _first_known = False  # try again on a later outside request
                # counting must never break or slow a request

        if not background:
            send()
            return None
        t = threading.Thread(target=send, daemon=True)
        t.start()
        return t
    except Exception:
        return None


def event(name: str, headers: dict[str, str] | None = None, client: str | None = None) -> None:
    """Count an event (free_key_signup, report). Inside an instrumented request it rides on that
    request's pipeline (no extra round trip); otherwise it is written on its own."""
    try:
        if not enabled():
            return
        if getattr(_ctx, "events", None) is not None:
            note_event(name)
            return
        record("event", headers, client, events=[name])
    except Exception:
        pass


def _safe_run(cmds):
    try:
        _run(cmds)
    except Exception:
        pass


# ------------------------------------------------------------- tool capture
_ctx = threading.local()


def start_request() -> None:
    _ctx.tools = []
    _ctx.events = []


def note_event(name: str) -> None:
    events = getattr(_ctx, "events", None)
    if events is not None and len(events) < 20:
        events.append(re.sub(r"[^a-z0-9_]", "", str(name).lower())[:40] or "unknown")


def take_events() -> list[str]:
    events = getattr(_ctx, "events", None) or []
    _ctx.events = None
    return events


def note_tool(name: str) -> None:
    tools = getattr(_ctx, "tools", None)
    if tools is not None and len(tools) < 50:
        tools.append(re.sub(r"[^a-z0-9_]", "", str(name).lower())[:40] or "unknown")


def take_tools() -> list[str]:
    tools = getattr(_ctx, "tools", None) or []
    _ctx.tools = None
    return tools


def instrument(handler_cls: type) -> type:
    """Wrap a BaseHTTPRequestHandler's do_GET/do_POST/do_DELETE to count each request after it is handled."""
    for name in ("do_GET", "do_POST", "do_DELETE", "do_PUT"):
        original = handler_cls.__dict__.get(name)
        if original is None:
            continue

        def wrapped(self, _orig=original, name=name):
            start_request()
            self._rk_status = 0
            try:
                _send = self.send_response

                def capture(code, *a, **k):
                    self._rk_status = int(code)
                    return _send(code, *a, **k)

                self.send_response = capture
            except Exception:
                pass
            try:
                return _orig(self)
            finally:
                try:
                    headers = dict(self.headers.items()) if getattr(self, "headers", None) is not None else {}
                    endpoint = endpoint_for(getattr(self, "path", "/"))
                    events = take_events()
                    status = getattr(self, "_rk_status", 0)
                    if endpoint == "reports" and name == "do_POST" and 200 <= status < 300 and "report" not in events:
                        events.append("report")  # an accepted REST report
                    record(endpoint, headers, (getattr(self, "client_address", None) or [None])[0],
                           tools=take_tools(), events=events)
                except Exception:
                    pass

        wrapped.__name__ = name
        setattr(handler_cls, name, wrapped)
    return handler_cls


# ------------------------------------------------------------------- reading
def read_days(days: int, end: date | None = None) -> list[dict[str, Any]]:
    end = end or datetime.now(TZ).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
    cmds: list[list[Any]] = []
    for d in dates:
        cmds.append(["HGETALL", f"{KEY_PREFIX}metrics:{d}"])
        cmds += [["PFCOUNT", f"{KEY_PREFIX}metrics:{d}:clients:{c}"] for c in CLASSES if c != "internal"]
    res = _run(cmds)
    out, i = [], 0
    for d in dates:
        flat = res[i] or []
        i += 1
        uniques = {}
        for c in CLASSES:
            if c != "internal":
                uniques[c] = int(res[i] or 0)
                i += 1
        out.append(summarize_day(d, _pairs(flat), uniques))
    return out


def read_first_outside() -> dict[str, Any] | None:
    global _first_known
    raw = _run([["GET", FIRST_KEY]])[0]
    if not raw:
        return None
    _first_known = True
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)[:200]}


def _pairs(flat: Any) -> dict[str, int]:
    if isinstance(flat, dict):
        return {k: int(v) for k, v in flat.items()}
    return {flat[j]: int(flat[j + 1]) for j in range(0, len(flat) - 1, 2)}


def summarize_day(day: str, fields: dict[str, int], uniques: dict[str, int]) -> dict[str, Any]:
    req = {c: {} for c in CLASSES}
    tools = {c: {} for c in CLASSES}
    events = {c: {} for c in CLASSES}
    for field, n in fields.items():
        parts = field.split("|")
        if len(parts) != 3 or parts[2] not in CLASSES:
            continue
        kind, name, cls = parts
        target = {"req": req, "tool": tools, "event": events}.get(kind)
        if target is not None:
            target[cls][name] = target[cls].get(name, 0) + n
    totals = {c: sum(req[c].values()) for c in CLASSES}
    return {"date": day, "timezone": "America/Chicago", "requests_total": totals, "requests": req,
            "mcp_tools": tools, "events": events, "unique_clients": uniques,
            "free_keys_signed_up": sum(events[c].get("free_key_signup", 0) for c in CLASSES)}


def _top(d: dict[str, int], n: int = 6) -> str:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{k} {v:,}" for k, v in items) or "none"


def text_summary(day: dict[str, Any]) -> str:
    t = day["requests_total"]
    u = day["unique_clients"]
    lines = [
        f"Rainout Source usage {day['date']} (America/Chicago)",
        f"Requests: {t['external']:,} outside | {t['internal']:,} ours | {t['monitor']:,} monitors | {t['bot']:,} bots",
        f"Unique outside clients: {u.get('external', 0):,} (monitors {u.get('monitor', 0):,}, bots {u.get('bot', 0):,})",
        f"Top endpoints (outside): {_top(day['requests']['external'])}",
        f"MCP tools (outside): {_top(day['mcp_tools']['external'])}",
        f"Free keys signed up: {day['free_keys_signed_up']:,}",
    ]
    return "\n".join(lines)

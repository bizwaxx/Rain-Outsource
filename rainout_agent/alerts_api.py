"""HTTP glue for /v1/alerts/*, shared by the Vercel function (api/alerts.py) and tests."""
from __future__ import annotations

import html
import json
import os
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import alerts

MAX_BODY = 8_000
JSON = {"Content-Type": "application/json", "Cache-Control": "no-store"}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Rainout-Internal, Authorization"}


def _json(status: int, payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    return status, {**JSON, **CORS}, json.dumps(payload, indent=2).encode()


def _page(status: int, title: str, body_html: str) -> tuple[int, dict[str, str], bytes]:
    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta name=robots content=noindex><title>{html.escape(title)}</title>"
           "<style>body{font:17px/1.6 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#1a2433}"
           "button{padding:12px 18px;font-size:17px;font-weight:700;color:#fff;background:#0b63ce;border:0;border-radius:10px}</style>"
           f"</head><body><h1>{html.escape(title)}</h1>{body_html}<p><a href='/'>Rain-Outsource</a></p></body></html>")
    return status, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, doc.encode()


def _wants_html(headers: dict[str, str]) -> bool:
    accept = {k.lower(): v for k, v in headers.items()}.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "channel", "kind", "field_id", "game_time", "title", "ics_url", "email", "webhook_url", "created_at",
            "expires_at", "confirmed", "test")
    return {k: rec[k] for k in keep if k in rec} | {"alerts_sent": sorted(rec.get("sent", {}).keys())}


def _internal(headers: dict[str, str]) -> bool:
    import hmac

    token = os.environ.get("RAINOUT_INTERNAL_TOKEN") or ""
    given = {k.lower(): v for k, v in headers.items()}.get("x-rainout-internal", "")
    return bool(token and given and hmac.compare_digest(given, token))


def handle(method: str, path: str, query: str, body: bytes, content_type: str, headers: dict[str, str],
           client: str | None = None, **deps: Any) -> tuple[int, dict[str, str], bytes]:
    method = method.upper()
    params = {k: v[0] for k, v in parse_qs(query or "", keep_blank_values=True).items()}
    path = (path or "").rstrip("/")
    if method == "OPTIONS":
        return 204, CORS, b""
    form: dict[str, Any] = {}
    if method in {"POST", "DELETE"} and body:
        if len(body) > MAX_BODY:
            return _json(413, {"error": "payload_too_large", "message": f"Body is limited to {MAX_BODY} bytes."})
        text = body.decode("utf-8", errors="replace")
        if (content_type or "").startswith("application/x-www-form-urlencoded"):
            form = {k: v[0] for k, v in parse_qs(text).items()}
        else:
            try:
                form = json.loads(text or "{}")
            except ValueError:
                return _json(400, {"error": "invalid_json", "message": "Body must be JSON."})
    try:
        if path in {"/v1/alerts", "/v1/alerts/subscribe"}:
            if method != "POST":
                return _json(200, {"service": "rainout-source alerts", "subscribe": "POST /v1/alerts/subscribe",
                                   "channels": list(alerts.CHANNELS), "mode": "live" if alerts.live_mode() else "test",
                                   "example": {"channel": "webhook", "webhook_url": "https://example.org/hook",
                                               "field_id": "Krieg", "game_time": "2026-10-23T19:00:00-05:00"},
                                   "docs": "https://github.com/bizwaxx/Rain-Outsource/blob/main/docs/alerts.md"})
            return _json(201, alerts.subscribe(form, client=client))
        if path == "/v1/alerts/confirm":
            res = alerts.confirm(params.get("id", ""), params.get("code", ""))
            if _wants_html(headers):
                return _page(200, "You're subscribed", "<p>Free rain alerts are on. Every message has an unsubscribe link.</p>")
            return _json(200, res)
        if path == "/v1/alerts/unsubscribe":
            sub_id = params.get("id") or form.get("id", "")
            token = params.get("token") or form.get("token", "")
            if method == "GET":  # link previewers must not unsubscribe people: GET only shows a button
                alerts.authorize(sub_id, token, allow_unsubscribe_token=True)
                form_html = (f"<form method=post action='/v1/alerts/unsubscribe'><input type=hidden name=id value='{html.escape(sub_id)}'>"
                             f"<input type=hidden name=token value='{html.escape(token)}'><button>Unsubscribe</button></form>")
                if _wants_html(headers):
                    return _page(200, "Stop these alerts?", "<p>This deletes your subscription and the address or URL we kept.</p>" + form_html)
                return _json(200, {"id": sub_id, "next_step": "POST this URL (or DELETE /v1/alerts/{id}?token=) to unsubscribe"})
            res = alerts.unsubscribe(sub_id, token)
            if (content_type or "").startswith("application/x-www-form-urlencoded"):
                return _page(200, "Unsubscribed", "<p>Done. We deleted the subscription.</p>")
            return _json(200, res)
        if path == "/v1/alerts/dispatch":
            ok, why = alerts.dispatch_authorized(headers, params)
            if not ok:
                return _json(503 if why == "not_configured" else 403,
                             {"error": why, "message": "Dispatcher needs the internal token, the cron secret or the dispatch key."})
            return _json(200, alerts.dispatch(**{k: v for k, v in deps.items() if k in {"checker", "fetcher", "webhook_poster"}}))
        if path == "/v1/alerts/outbox":
            if not _internal(headers):
                return _json(403, {"error": "forbidden", "message": "Internal token required."})
            return _json(200, {"mode": "live" if alerts.live_mode() else "test", "messages": alerts.outbox(int(params.get("limit") or 50))})
        parts = path.split("/")
        if len(parts) >= 4 and parts[:3] == ["", "v1", "alerts"]:
            sub_id = parts[3]
            token = params.get("token") or form.get("token", "")
            if len(parts) == 5 and parts[4] == "calendar.ics" and method == "GET":
                rec = alerts.authorize(sub_id, token)
                ics = alerts.calendar_feed(rec, **{k: v for k, v in deps.items() if k in {"checker", "fetcher"}})
                return 200, {"Content-Type": "text/calendar; charset=utf-8", "Cache-Control": "private, max-age=600",
                             "Access-Control-Allow-Origin": "*"}, ics.encode()
            if len(parts) == 4:
                if method == "DELETE":
                    return _json(200, alerts.unsubscribe(sub_id, token))
                if method == "GET":
                    return _json(200, _public(alerts.authorize(sub_id, token)))
        return _json(404, {"error": "not_found", "message": "Unknown alerts path.", "subscribe": "POST /v1/alerts/subscribe"})
    except alerts.AlertError as exc:
        return _json(exc.status, exc.payload())
    except Exception as exc:  # FetchError from a team ICS etc.
        from rainout_agent.safe_fetch import FetchError, UnsafeURLError
        from rainout_agent.schedule import ScheduleInputError

        if isinstance(exc, (FetchError, UnsafeURLError, ScheduleInputError)):
            return _json(502, {"error": "calendar_unavailable", "message": str(exc)})
        raise

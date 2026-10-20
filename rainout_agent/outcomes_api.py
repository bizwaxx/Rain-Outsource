"""HTTP handler for 'Was it on?' outcomes: POST/GET /v1/outcomes, /v1/outcomes/feed.atom,
/v1/outcomes/scoreboard (JSON) and /scoreboard (HTML)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import outcomes, reports

MAX_BODY = 4096
CORS_METHODS = "GET, POST, OPTIONS"
BASE_URL = "https://rainout-agent-source.vercel.app"


def _json(status: int, payload: dict[str, Any], extra: dict[str, str] | None = None, cache: str = "no-store") -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json", "Cache-Control": cache, **(extra or {})}, json.dumps(payload).encode()


def handle_outcomes_request(method: str, path: str, query: str, body: bytes, content_type: str = "",
                            headers: dict[str, str] | None = None, client: str | None = None,
                            now: datetime | None = None) -> tuple[int, dict[str, str], bytes]:
    path = path.rstrip("/") or "/"
    if method == "OPTIONS":
        return 204, {"Access-Control-Allow-Methods": CORS_METHODS, "Access-Control-Allow-Headers": "Content-Type",
                     "Cache-Control": "no-store"}, b""
    params = {k: v[0] for k, v in parse_qs(query or "", keep_blank_values=True).items()}
    if path == "/scoreboard":
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300"}, \
            outcomes.scoreboard_html().encode()
    if path == "/v1/outcomes/scoreboard":
        return _json(200, outcomes.scoreboard(), cache="public, max-age=300")
    if path == "/v1/outcomes/feed.atom":
        return 200, {"Content-Type": "application/atom+xml; charset=utf-8", "Cache-Control": "public, max-age=300"}, \
            outcomes.feed_atom(BASE_URL).encode()
    if path != "/v1/outcomes":
        return _json(404, {"error": "not_found"})
    if method == "GET":
        fid = (params.get("field_id") or params.get("field") or "").strip()
        if fid:
            from rainout_agent.status_api import resolve_field_id

            field_id = resolve_field_id(fid)
            if not field_id:
                return _json(404, {"error": "unknown_field", "message": "Unknown field_id; search /v1/fields first."})
            events = outcomes.resolve_events(outcomes.venue_outcomes(field_id))
            return _json(200, {"field_id": field_id, "events": events[:50], "drainage_memory": outcomes.memory_from_events(events)})
        return _json(200, {**outcomes.feed_json(), "submit": "POST /v1/outcomes with {field_id, date (YYYY-MM-DD), outcome, note?}",
                           "outcomes": list(outcomes.OUTCOMES)}, cache="public, max-age=120")
    if method != "POST":
        return _json(405, {"error": "method_not_allowed", "allowed": CORS_METHODS}, {"Allow": CORS_METHODS})
    if len(body) > MAX_BODY:
        return _json(413, {"error": "body_too_large", "message": f"Body must be under {MAX_BODY} bytes."})
    ctype = (content_type or "").split(";")[0].strip().lower()
    try:
        if ctype == "application/x-www-form-urlencoded":
            data: Any = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        elif body.strip():
            data = json.loads(body.decode("utf-8"))
        else:
            data = params
    except (ValueError, UnicodeDecodeError):
        return _json(400, {"error": "invalid_json", "message": "Send JSON like {\"field_id\": \"...\", \"date\": \"2026-10-20\", \"outcome\": \"played\"}."})
    if isinstance(data, dict) and "rainout-test" in str((headers or {}).get("User-Agent") or (headers or {}).get("user-agent") or "").lower():
        data["test"] = True    # the staging checklist's writes are always test entries
    try:
        result = outcomes.submit_outcome(data, client=reports.client_from_headers(headers or {}, client), now=now)
    except reports.ReportError as exc:
        extra = {"Retry-After": str(exc.extra["retry_after_seconds"])} if exc.status == 429 else None
        return _json(exc.status, exc.payload(), extra)
    return _json(201, result)


def snapshot_mode(headers: dict[str, str] | None) -> str:
    """Live status requests snapshot their odds; the staging checklist's are marked as tests."""
    ua = str((headers or {}).get("User-Agent") or (headers or {}).get("user-agent") or "").lower()
    return "test" if "rainout-test" in ua else "real"

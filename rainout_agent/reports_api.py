"""HTTP handler for /v1/reports (community cancellation reports)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import reports

MAX_BODY = 4096
CORS_METHODS = "GET, POST, OPTIONS"


def _json(status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}
    return status, headers, json.dumps(payload).encode()


def handle_reports_request(method: str, query: str, body: bytes, content_type: str = "",
                           headers: dict[str, str] | None = None, client: str | None = None,
                           now: datetime | None = None) -> tuple[int, dict[str, str], bytes]:
    if method == "OPTIONS":
        return 204, {"Access-Control-Allow-Methods": CORS_METHODS, "Access-Control-Allow-Headers": "Content-Type",
                     "Cache-Control": "no-store"}, b""
    params = {k: v[0] for k, v in parse_qs(query or "", keep_blank_values=True).items()}
    if method == "GET":
        return _recent(params, now)
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
            data = params  # query-string only POST
    except (ValueError, UnicodeDecodeError):
        return _json(400, {"error": "invalid_json", "message": "Send JSON like {\"field_id\": \"...\", \"status\": \"cancelled\"}."})
    try:
        result = reports.submit_report(data, client=reports.client_from_headers(headers or {}, client), now=now)
    except reports.ReportError as exc:
        extra = {"Retry-After": str(exc.extra["retry_after_seconds"])} if exc.status == 429 else None
        return _json(exc.status, exc.payload(), extra)
    return _json(201, result)


def _recent(params: dict[str, str], now: datetime | None) -> tuple[int, dict[str, str], bytes]:
    from rainout_agent.status_api import load_field, resolve_field_id

    query = (params.get("field_id") or params.get("field") or "").strip()
    if not query:
        return _json(400, {"error": "missing_field_id", "message": "Pass field_id (see /v1/fields).",
                           "submit": "POST /v1/reports with {field_id, status, game_time?, note?}",
                           "statuses": list(reports.STATUSES)})
    field_id = resolve_field_id(query)
    if not field_id:
        return _json(404, {"error": "unknown_field", "message": "Unknown field_id; search /v1/fields first."})
    now = now or reports._utcnow()
    field = load_field(field_id)
    game_date = None
    if params.get("game_time"):
        try:
            game_time = reports._parse_time(params["game_time"], now)
        except reports.ReportError as exc:
            return _json(exc.status, exc.payload())
        game_date = game_time.astimezone(reports._field_zone(field)).date().isoformat()
    items = sorted(reports.recent_reports(field_id, now), key=lambda r: r["reported_at"], reverse=True)
    if game_date:
        items = [r for r in items if r.get("game_date") == game_date]
    return _json(200, {"field_id": field_id, "field_name": field.get("field_name"),
                       "summary": reports.summarize(field_id, game_date, now), "reports": items[:20],
                       "note": "Unverified user reports from the last 24 hours; the official status always wins."})

"""HTTP glue for /v1/schedule/check, shared by the Vercel function and the local app."""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qs

from rainout_agent import schedule
from rainout_agent.safe_fetch import FetchError, UnsafeURLError, safe_get

MAX_BODY_BYTES = schedule.MAX_INPUT_BYTES


def _json(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(payload).encode("utf-8")


def _error(status: int, code: str, message: str) -> tuple[int, str, bytes]:
    return _json(status, {
        "error": code,
        "message": message,
        "docs": "https://github.com/bizwaxx/Rain-Outsource/blob/main/docs/schedule.md",
    })


def _int(value: Any, default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise schedule.ScheduleInputError(f"{name} must be an integer") from None


def fetch_ics_text(url: str, fetcher: Callable[[str], Any] = safe_get) -> str:
    result = fetcher(url)
    text = result.body.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        raise schedule.ScheduleInputError("the URL did not return an iCalendar (.ics) document")
    return text


def handle_schedule_request(
    method: str,
    query_string: str,
    body: bytes = b"",
    content_type: str = "",
    *,
    fetcher: Callable[[str], Any] = safe_get,
    checker: Callable[..., dict[str, Any]] | None = None,
) -> tuple[int, str, bytes]:
    """Return (status, content_type, body) for a schedule check request."""
    checker = checker or schedule.check_schedule
    params = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    payload: dict[str, Any] = {}
    content_type = (content_type or "").split(";")[0].strip().lower()

    if method == "POST":
        if len(body) > MAX_BODY_BYTES:
            return _error(413, "payload_too_large", f"Request body is limited to {MAX_BODY_BYTES} bytes.")
        text = body.decode("utf-8", errors="replace")
        if content_type in {"text/calendar", "application/ics"} or text.lstrip().upper().startswith("BEGIN:VCALENDAR"):
            payload = {"ics": text}
        elif content_type == "text/csv":
            payload = {"csv": text}
        else:
            try:
                payload = json.loads(text or "{}")
            except ValueError:
                return _error(400, "invalid_json", "Body must be JSON, text/calendar or text/csv.")
            if isinstance(payload, list):
                payload = {"games": payload}
            if not isinstance(payload, dict):
                return _error(400, "invalid_json", "JSON body must be an object or a list of games.")
    elif method != "GET":
        return _error(405, "method_not_allowed", "Use GET with ics_url or POST a schedule.")

    options = {**params, **{k: v for k, v in payload.items() if k not in {"games", "ics", "csv"}}}
    output = str(options.get("format") or "json").lower()
    if output not in {"json", "ics"}:
        return _error(400, "invalid_format", "format must be json or ics.")

    try:
        if payload.get("games") is not None:
            games = schedule.parse_json_games(payload["games"])
        elif payload.get("ics"):
            games = schedule.parse_ics(str(payload["ics"]))
        elif payload.get("csv"):
            games = schedule.parse_csv_games(str(payload["csv"]))
        elif options.get("ics_url"):
            games = schedule.parse_ics(fetch_ics_text(str(options["ics_url"]), fetcher))
        else:
            return _error(400, "missing_schedule", "Provide ics_url, or POST games (JSON), ics or csv.")
        report = checker(
            games,
            risk_threshold=_int(options.get("risk_threshold"), schedule.DEFAULT_RISK_THRESHOLD, "risk_threshold"),
            window_days=_int(options.get("days"), schedule.DEFAULT_WINDOW_DAYS, "days"),
            default_venue=options.get("venue") or None,
            default_timezone=options.get("timezone") or None,
        )
    except UnsafeURLError as exc:
        return _error(400, "unsafe_url", str(exc))
    except FetchError as exc:
        return _error(502, "ics_fetch_failed", str(exc))
    except schedule.ScheduleInputError as exc:
        return _error(400, "invalid_schedule", str(exc))

    if output == "ics":
        return 200, "text/calendar; charset=utf-8", schedule.build_risk_ics(report).encode("utf-8")
    return _json(200, report)

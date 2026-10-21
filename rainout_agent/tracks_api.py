"""HTTP layer for /v1/tracks* (shared by the Vercel handler and the WSGI app)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from rainout_agent import http_cache, motocross
from rainout_agent.safe_fetch import safe_get

LIST_CACHE = "public, max-age=300, s-maxage=600"
DATA_CACHE = "public, max-age=120, s-maxage=300"
HISTORY_CACHE = "public, max-age=3600, s-maxage=3600"
ICS_CACHE = "public, max-age=900, s-maxage=900"
TRACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")
TRUE = {"1", "true", "yes", "on"}


def _json(status: int, payload: dict[str, Any], cache: str = http_cache.NO_STORE) -> tuple[int, str, bytes, str]:
    from rainout_agent import odds_label
    odds_label.apply(payload)
    return status, "application/json", json.dumps(payload, default=str).encode("utf-8"), cache


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, str, bytes, str]:
    return _json(status, {"error": code, "message": message, "docs": "https://rainout-agent-source.vercel.app/llms.txt", **extra})


def route(path: str) -> tuple[str, str | None] | None:
    """Return (action, track_id) for a /v1/tracks path."""
    path = path.rstrip("/")
    if path == "/v1/tracks":
        return "list", None
    m = re.fullmatch(r"/v1/tracks/([^/]+)/(events|check|history|calendar\.ics)", path)
    if m:
        return m.group(2).replace(".ics", ""), unquote(m.group(1))
    return None


def handle_tracks_request(method: str, path: str, query_string: str, *,
                          fetcher: Callable[[str], Any] = safe_get) -> tuple[int, str, bytes, str]:
    """Return (status, content_type, body, cache_control)."""
    params = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    routed = route(path)
    if routed is None:
        return _error(404, "not_found", "Use /v1/tracks, /v1/tracks/{track_id}/events, /check, /history or /calendar.ics.")
    if method != "GET":
        return _error(405, "method_not_allowed", "Use GET.")
    action, track_id = routed
    events_url = params.get("events_url") or None
    try:
        if action == "list":
            return _json(200, motocross.list_tracks(params.get("q") or None, params.get("state") or None,
                                                          verification=params.get("verification") or None,
                                                          limit=params.get("limit") or None, offset=params.get("offset") or None), LIST_CACHE)
        if not track_id or not TRACK_ID_RE.match(track_id.lower()):
            raise motocross.UnknownTrack(track_id or "")
        if action == "events":
            result = motocross.track_events(
                track_id, date_param=params.get("date") or None, from_param=params.get("from") or None,
                to_param=params.get("to") or None, include_practice=(params.get("include_practice", "").lower() in TRUE),
                events_url=events_url, fetcher=fetcher)
            return _json(200, result, DATA_CACHE)
        if action == "check":
            result = motocross.check_race(track_id, date_param=params.get("date") or None, event_id=params.get("event_id") or None,
                                          events_url=events_url, fetcher=fetcher)
            return _json(200, result, DATA_CACHE)
        if action == "history":
            return _json(200, motocross.track_history(track_id), HISTORY_CACHE)
        if action == "calendar":
            text = motocross.track_calendar(track_id)
            return 200, "text/calendar; charset=utf-8", text.encode("utf-8"), ICS_CACHE
    except motocross.TracksError as exc:
        return _error(400, "invalid_request", str(exc))
    except motocross.UnknownTrack:
        return _error(404, "unknown_track", "That track_id is not one of the supported motocross tracks. Call /v1/tracks?q=... first.",
                      next_step="find_track")
    except Exception as exc:  # weather or source outage
        return _error(502, "track_source_unavailable", f"Track data could not be assembled right now ({exc.__class__.__name__}).",
                      next_step="ask_user_for_events_url",
                      spoken_answer="I couldn't reach the track's calendars or the forecast right now. Check the track's own page, "
                                    "or send me a link to the event and I'll try that.")
    return _error(404, "not_found", "Unknown tracks action.")

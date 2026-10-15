"""HTTP layer for /v1/teams/* (shared by the Vercel handler and the WSGI app)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from rainout_agent import http_cache, team_sources, teams
from rainout_agent.safe_fetch import safe_get

TEAM_ID_RE = re.compile(r"^(tsl-\d+-\d+-\d+|ambl-\d{4}(?:spring|fall)-\d+|url-[A-Za-z0-9_-]{8,1600})$")
SEARCH_CACHE = "public, max-age=300, s-maxage=600"
GAMES_CACHE = "public, max-age=120, s-maxage=300"
ICS_CACHE = "public, max-age=900, s-maxage=900"


def _json(status: int, payload: dict[str, Any], cache: str = http_cache.NO_STORE) -> tuple[int, str, bytes, str]:
    return status, "application/json", json.dumps(payload).encode("utf-8"), cache


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, str, bytes, str]:
    return _json(status, {"error": code, "message": message, "docs": "https://rainout-agent-source.vercel.app/llms.txt", **extra})


def route(path: str) -> tuple[str, str | None] | None:
    """Return (action, team_id) for a /v1/teams path."""
    path = path.rstrip("/")
    if path == "/v1/teams/search":
        return "search", None
    m = re.fullmatch(r"/v1/teams/([^/]+)/(games|calendar\.ics)", path)
    if m:
        return ("games" if m.group(2) == "games" else "calendar"), unquote(m.group(1))
    return None


def handle_teams_request(
    method: str,
    path: str,
    query_string: str,
    *,
    fetcher: Callable[[str], Any] = safe_get,
    checker: Callable[..., dict[str, Any]] | None = None,
) -> tuple[int, str, bytes, str]:
    """Return (status, content_type, body, cache_control)."""
    params = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    routed = route(path)
    if routed is None and params.get("action"):
        routed = (params["action"], params.get("team_id"))
    if routed is None:
        return _error(404, "not_found", "Use /v1/teams/search or /v1/teams/{team_id}/games.")
    if method != "GET":
        return _error(405, "method_not_allowed", "Use GET.")
    action, team_id = routed
    try:
        if action == "search":
            result = teams.search_teams(
                params.get("q"), venue=params.get("venue") or None, sport=params.get("sport") or None,
                day=params.get("day") or None, schedule_url=params.get("schedule_url") or None, fetcher=fetcher,
            )
            return _json(200, result, SEARCH_CACHE if not params.get("schedule_url") else GAMES_CACHE)
        if not team_id or not TEAM_ID_RE.match(team_id):
            return _error(404, "unknown_team", "That team_id is not valid. Call /v1/teams/search first.",
                          next_step="search_teams")
        if action == "games":
            result = teams.team_games(team_id, date_param=params.get("date") or None, from_param=params.get("from") or None,
                                      to_param=params.get("to") or None, fetcher=fetcher, checker=checker)
            return _json(200, result, GAMES_CACHE)
        if action == "calendar":
            text = teams.team_calendar(team_id, fetcher=fetcher, checker=checker)
            return 200, "text/calendar; charset=utf-8", text.encode("utf-8"), ICS_CACHE
    except teams.TeamsError as exc:
        return _error(400, "invalid_request", str(exc))
    except team_sources.UnknownTeam:
        return _error(404, "unknown_team", "That team was not found in its league schedule. Call /v1/teams/search again.",
                      next_step="search_teams")
    except team_sources.SourceError as exc:
        return _error(502, "schedule_source_unavailable", f"The league schedule could not be read right now ({exc}).",
                      next_step="ask_user_for_schedule_url",
                      spoken_answer="I couldn't reach the league schedule right now. If you have a link to your schedule, send it and I'll check that.")
    return _error(404, "not_found", "Unknown teams action.")

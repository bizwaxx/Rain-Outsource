"""MCP server (Streamable HTTP transport, stateless JSON responses) for Rainout Source.

Endpoint: ``POST /mcp`` takes JSON-RPC 2.0 messages and returns ``application/json``.
``GET /mcp`` answers 405 because the server never opens SSE streams. Every tool is
read-only. No tool writes to calendars: the tools return a ready-made Google
Calendar event, and their descriptions tell the agent to ask the user first.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.parse import urlsplit

from rainout_agent import team_sources, teams
from rainout_agent.safe_fetch import FetchError, UnsafeURLError

SERVER_NAME = "rainout-source"
SERVER_VERSION = "0.9.0"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256_000

INSTRUCTIONS = (
    "Rainout Source tells people whether their outdoor game (softball, baseball, soccer, motocross) will be played. "
    "Typical flow: find_team with the team name and what the user said (venue, sport, weekday), then confirm the "
    "match with the user. Next, get_games or check_game, and read the one-sentence `reason` aloud with the play odds. "
    "If find_team returns next_step=ask_user_for_schedule_url, ask the user for their league schedule link and "
    "call find_team again with schedule_url. Never add anything to a calendar without asking the user first; "
    "Rainout Source never writes to calendars. If they agree, create the game's `calendar_event` (Google Calendar "
    "event body with a stable iCalUID) using your calendar tool. Official league or venue status always wins over odds."
)

CONFIRM = " Confirm the team match with the user before relying on it."
ASK_CAL = (" Each game includes calendar_event (Google Calendar API event body) and ics. ALWAYS ask the user before adding "
           "a game to their calendar; this server never writes to calendars.")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_team",
        "title": "Find a team",
        "description": (
            "Find the user's team in public league schedules, e.g. 'I play on ATX Boys, Friday nights at Craig Fields' -> "
            "query='ATX Boys', venue='Craig Fields', day='friday', sport='softball'. Returns candidates with team_id and confidence."
            + CONFIRM + " If next_step is ask_user_for_schedule_url, ask the user for their league's schedule link "
            "(calendar/ICS link or schedule page) and call again with schedule_url."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Team name as the user said it."},
                "venue": {"type": "string", "description": "Where they play (name or alias), e.g. Craig Fields."},
                "sport": {"type": "string", "description": "softball, baseball, soccer, ..."},
                "day": {"type": "string", "description": "Weekday they usually play, e.g. friday."},
                "schedule_url": {"type": "string", "description": "League schedule link from the user (ICS/webcal feed or schedule page)."},
            },
        },
    },
    {
        "name": "get_games",
        "title": "Get a team's games with play odds",
        "description": (
            "List a team's games (team_id from find_team) for date='today'|'tomorrow'|'YYYY-MM-DD' or from/to (max 62 days; "
            "default next 14 days). Each game has start/end with timezone, opponent, venue/field, play_probability (0-100), a "
            "one-sentence reason to read aloud, status sources and as_of. When the range has no game, next_game is the next one."
            + ASK_CAL
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "date": {"type": "string", "description": "today, tomorrow or YYYY-MM-DD"},
                "from": {"type": "string", "description": "YYYY-MM-DD"},
                "to": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "check_game",
        "title": "Is my game on?",
        "description": (
            "Answer 'do I have a game today?' / 'is my game on?'. Pass team_id (and optional date, default today) to get that "
            "day's game, or the next game if there is none, with play odds and a spoken answer. Or pass venue + game_time "
            "(ISO-8601) for a game that isn't in a known league." + ASK_CAL
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "date": {"type": "string", "description": "today (default), tomorrow or YYYY-MM-DD"},
                "venue": {"type": "string"},
                "game_time": {"type": "string", "description": "ISO-8601 with offset, e.g. 2026-10-16T20:20:00-05:00"},
            },
        },
    },
    {
        "name": "get_venue_status",
        "title": "Venue status and play odds",
        "description": (
            "Official rainout status (when published), rain chance and play probability for a venue at a game time. Use "
            "when the user names a field but not a team. venue accepts names/aliases like 'Craig Fields', 'Downs Field'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "game_time": {"type": "string", "description": "ISO-8601 with offset"},
            },
            "required": ["venue", "game_time"],
        },
    },
    {
        "name": "check_schedule",
        "title": "Check a whole schedule",
        "description": (
            "Score every game in a schedule and flag likely rainouts. Pass ics_url (public calendar link) or games "
            "[{start, venue, title?}]. Returns per-game play probability and risk reasons. Ask before adding anything to a calendar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ics_url": {"type": "string"},
                "games": {"type": "array", "items": {"type": "object"}},
                "days": {"type": "integer", "minimum": 1, "maximum": 31},
                "risk_threshold": {"type": "integer", "minimum": 0, "maximum": 100},
                "venue": {"type": "string", "description": "Default venue for games without a recognisable location."},
            },
        },
    },
]
for _tool in TOOLS:
    _tool["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


class ToolError(ValueError):
    """Bad tool arguments; reported to the model as isError=true."""


# --------------------------------------------------------------- tools
def _find_team(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return teams.search_teams(args.get("query"), venue=args.get("venue"), sport=args.get("sport"),
                                  day=args.get("day"), schedule_url=args.get("schedule_url"))
    except teams.TeamsError as exc:
        raise ToolError(str(exc)) from exc


def _get_games(args: dict[str, Any]) -> dict[str, Any]:
    team_id = _require(args, "team_id")
    try:
        return teams.team_games(team_id, date_param=args.get("date"), from_param=args.get("from"), to_param=args.get("to"))
    except teams.TeamsError as exc:
        raise ToolError(str(exc)) from exc
    except team_sources.UnknownTeam as exc:
        raise ToolError("Unknown team_id. Call find_team first.") from exc


def _check_game(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("team_id"):
        data = _get_games({"team_id": args["team_id"], "date": args.get("date") or "today"})
        game = data["games"][0] if data["games"] else None
        if game:
            answer = f"Yes: {game['when_spoken']} vs {game.get('opponent') or 'TBD'}. {game['reason']}"
        elif data.get("next_game"):
            ng = data["next_game"]
            answer = f"{data['message']} {ng['reason']}"
        else:
            answer = data["message"]
        return {"answer": answer, "game": game, "next_game": None if game else data.get("next_game"),
                "team": data["team"], "calendar_policy": data["calendar_policy"], "as_of": data["as_of"]}
    if args.get("venue") and args.get("game_time"):
        return _venue_status(args)
    raise ToolError("Pass team_id (from find_team), or venue and game_time.")


def _venue_status(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.status_api import build_status_result

    venue, game_time = _require(args, "venue"), _require(args, "game_time")
    result = build_status_result(field_query=venue, game_time=game_time)
    if result.get("error") == "unknown_field":
        raise ToolError(result.get("spoken_answer") or "Unknown venue.")
    return result


def _check_schedule(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.schedule_api import handle_schedule_request

    payload = {k: v for k, v in args.items() if k in {"games", "ics_url", "days", "risk_threshold", "venue"} and v is not None}
    if not payload.get("games") and not payload.get("ics_url"):
        raise ToolError("Pass ics_url or games.")
    status, _, body = handle_schedule_request("POST", "", json.dumps(payload).encode(), "application/json")
    data = json.loads(body)
    if status != 200:
        raise ToolError(data.get("message") or data.get("error") or "schedule check failed")
    return data


def _require(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} is required.")
    return value.strip()


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "find_team": _find_team,
    "get_games": _get_games,
    "check_game": _check_game,
    "get_venue_status": _venue_status,
    "check_schedule": _check_schedule,
}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    try:
        data = handler(args or {})
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except team_sources.SourceError as exc:
        msg = (f"The league schedule could not be read right now ({exc}). Ask the user for their schedule link and "
               "call find_team with schedule_url.")
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    except (FetchError, UnsafeURLError) as exc:
        return {"content": [{"type": "text", "text": f"Could not use that link: {exc}"}], "isError": True}
    except Exception as exc:  # pragma: no cover - live safety net (e.g. weather outage)
        return {"content": [{"type": "text", "text": f"Rainout Source error: {exc.__class__.__name__}. Tell the user to check the official source."}],
                "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}], "structuredContent": data, "isError": False}


# ------------------------------------------------------------ JSON-RPC
def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Invalid Request")
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    is_notification = "id" not in msg
    if method is None:  # a response from the client (we never send requests)
        return None
    if is_notification:
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Rainout Source", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(msg_id, -32602, "arguments must be an object")
        try:
            return _result(msg_id, call_tool(name, args))
        except KeyError:
            return _err(msg_id, -32602, f"Unknown tool: {name}")
    if method in {"resources/list", "prompts/list"}:
        return _result(msg_id, {method.split("/")[0]: []})
    return _err(msg_id, -32601, f"Method not found: {method}")


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    allowed = [o.strip() for o in os.environ.get("RAINOUT_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if allowed:
        return origin in allowed
    host = (urlsplit(origin).hostname or "").lower()
    # Public read-only server: any web origin is fine except local/private hosts (DNS-rebinding guard).
    from rainout_agent.safe_fetch import is_public_ip

    if host in {"localhost"} or host.endswith(".localhost") or host.endswith(".local"):
        return os.environ.get("RAINOUT_MCP_ALLOW_LOCAL") == "1"
    if host.replace(".", "").isdigit() and not is_public_ip(host):
        return os.environ.get("RAINOUT_MCP_ALLOW_LOCAL") == "1"
    return True


CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS, DELETE",
    "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, Authorization",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, MCP-Protocol-Version",
    "Cache-Control": "no-store",
}


def handle_http(method: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    """Streamable HTTP endpoint. Returns (status, headers, body)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    out = dict(CORS)
    if not _origin_allowed(h.get("origin")):
        return 403, {**out, "Content-Type": "application/json"}, json.dumps(_err(None, -32000, "Origin not allowed")).encode()
    if method == "OPTIONS":
        return 204, out, b""
    if method != "POST":
        info = {"name": SERVER_NAME, "transport": "streamable-http", "endpoint": "/mcp", "usage": "POST JSON-RPC 2.0 messages.",
                "docs": "https://rainout-agent-source.vercel.app/llms.txt"}
        return 405, {**out, "Allow": "POST, OPTIONS", "Content-Type": "application/json"}, json.dumps(info).encode()
    if len(body) > MAX_BODY:
        return 413, {**out, "Content-Type": "application/json"}, json.dumps(_err(None, -32600, "Request too large")).encode()
    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return 400, {**out, "Content-Type": "application/json"}, json.dumps(_err(None, -32700, "Parse error")).encode()
    if isinstance(payload, list):
        if not payload or len(payload) > 20:
            return 400, {**out, "Content-Type": "application/json"}, json.dumps(_err(None, -32600, "Invalid batch")).encode()
        responses = [r for r in (handle_message(m) for m in payload) if r is not None]
    else:
        single = handle_message(payload)
        responses = [single] if single is not None else []
    if not responses:
        return 202, out, b""
    if isinstance(payload, dict) and payload.get("method") == "initialize":
        out["MCP-Protocol-Version"] = responses[0].get("result", {}).get("protocolVersion", SUPPORTED_VERSIONS[0])
    data = responses if isinstance(payload, list) else responses[0]
    return 200, {**out, "Content-Type": "application/json"}, json.dumps(data, default=str).encode()

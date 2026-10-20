"""MCP server (Streamable HTTP transport, stateless JSON responses) for Rainout Source.

Endpoint: ``POST /mcp`` takes JSON-RPC 2.0 messages and returns ``application/json``.
``GET /mcp`` answers 405 because the server never opens SSE streams. Every tool is
read-only except report_cancellation, which stores an anonymous report in the cache. No tool writes to calendars: the tools return a ready-made Google
Calendar event, and their descriptions tell the agent to ask the user first.
"""
from __future__ import annotations

import contextvars
import json
import os
from typing import Any, Callable
from urllib.parse import urlsplit

from rainout_agent import motocross, rain, team_sources, teams
from rainout_agent.safe_fetch import FetchError, UnsafeURLError

SERVER_NAME = "rainout-source"
SERVER_VERSION = "0.16.0"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256_000

INSTRUCTIONS = (
    "Rainout Source tells people whether their outdoor game (softball, baseball, soccer, motocross) will be played. "
    "Typical flow: find_team with the team name and what the user said (venue, sport, weekday), then confirm the "
    "match with the user. Next, get_games or check_game, and read the one-sentence `reason` aloud with the play odds. "
    "If find_team returns next_step=ask_user_for_schedule_url, ask the user for their league schedule link and "
    "call find_team again with schedule_url. Never add anything to a calendar without asking the user first; "
    "Rainout Source never writes to calendars. If they agree, create the game's `calendar_event` (Google Calendar "
    "event body with a stable iCalUID) using your calendar tool. Official league or venue status always wins over odds. "
    "Motocross: find_track (confirm the track with the user), then get_track_events or check_race for race-day odds, "
    "and get_track_history for monthly rain stats and documented rain cancellations. "
    "Venues not in the hand-verified list: find_venue searches tens of thousands of OpenStreetMap fields, soccer complexes "
    "and motocross tracks nationwide (verification_level osm_only: community map data, no official status source); pass its "
    "field_id as venue to get_venue_status, or as track_id to check_race. "
    "If the user says their game was rained out, delayed, the field is closed or they are playing, offer to share it with "
    "report_cancellation (unverified, expires in 24 h, nudges the odds others see while the official status is unknown). "
    "Anywhere in the world: get_rain_chance for hourly or daily rain probability and amounts, and "
    "get_precipitation_nowcast for the next 2 hours in 15-minute steps. "
    "Construction (v2 beta, weather screening, not engineering advice): check_concrete_pour, check_materials_risk."
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
            "when the user names a field but not a team. venue accepts names/aliases like 'Craig Fields', 'Downs Field', "
            "or a field_id from find_venue (OpenStreetMap venues are labeled verification_level=osm_only)."
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
        "name": "find_venue",
        "title": "Find a field or venue nationwide",
        "description": (
            "Search ball fields, soccer complexes and motocross tracks across the US by name and/or location, e.g. "
            "query='Zilker', state='TX' or near='30.27,-97.74'. Returns field_id, address, sport and verification_level "
            "('confirmed' = hand-verified with official sources; 'osm_only' = OpenStreetMap data, no official status). "
            "Confirm the venue with the user, then call get_venue_status with venue=field_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Venue name, e.g. 'Zilker Park'."},
                "state": {"type": "string", "description": "Two-letter state, e.g. TX."},
                "sport": {"type": "string", "description": "softball, baseball, soccer or motocross."},
                "near": {"type": "string", "description": "lat,lon to search around."},
                "radius_km": {"type": "number", "minimum": 1, "maximum": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
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
    {
        "name": "find_track",
        "title": "Find a motocross track",
        "description": (
            "Find a motocross track by name, alias or town, e.g. 'Swan MX', 'Freestone', 'Dade City'. Returns "
            "candidates with track_id, confidence and verification_level: 'confirmed' tracks have event calendars and "
            "researched history; 'osm_only' tracks come from OpenStreetMap (nationwide) and only get weather-based odds. "
            "Confirm the track with the user before relying on it. If next_step is ask_user_for_events_url, the track is "
            "not covered: ask for an event link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Track name or town as the user said it."},
                "state": {"type": "string", "description": "Two-letter state filter, e.g. TX."},
                "verification": {"type": "string", "enum": ["confirmed", "osm_only", "all"],
                                 "description": "Default: all when query or state is given, otherwise confirmed."},
            },
        },
    },
    {
        "name": "get_track_events",
        "title": "Upcoming motocross events with riding odds",
        "description": (
            "List races (optionally practice days) at a track from its own calendar and series calendars, each with "
            "play_probability, a one-sentence reason, official_status and source URLs. Never invents events: if none are "
            "found, ask the user for an event or series link and pass it as events_url." + ASK_CAL.replace("Each game", "Each event").replace("a game", "an event")
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "track_id from find_track (short ids like 'swan' work too)."},
                "date": {"type": "string", "description": "today, tomorrow, saturday, sunday, this weekend or YYYY-MM-DD."},
                "from": {"type": "string", "description": "Range start YYYY-MM-DD (default today)."},
                "to": {"type": "string", "description": "Range end YYYY-MM-DD (default 90 days out)."},
                "include_practice": {"type": "boolean", "description": "Include open practice days."},
                "events_url": {"type": "string", "description": "Optional user-supplied event page or ICS link."},
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "check_race",
        "title": "Will the race happen?",
        "description": (
            "Answer 'what are the chances the race at <track> on <date> happens'. Combines official cancel/postpone notices, "
            "the race-window forecast, the prior 24-48h of rain (soaked dirt cancels races) and the track's NOAA rain history. "
            "Read `answer` aloud. Official status always wins over odds." + ASK_CAL.replace("Each game", "The event").replace("a game", "the race")
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "track_id from find_track."},
                "date": {"type": "string", "description": "today, tomorrow, saturday, sunday, this weekend or YYYY-MM-DD."},
                "event_id": {"type": "string", "description": "event_id from get_track_events (instead of date)."},
                "events_url": {"type": "string", "description": "Optional user-supplied event page or ICS link."},
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "get_track_history",
        "title": "Track rain history and past rainouts",
        "description": (
            "Monthly NOAA rain stats for a track (rainy-day and inch-plus-day frequency, climatology race odds) plus publicly "
            "documented past cancelled or postponed events with source links and coverage notes. An empty list means nothing "
            "was publicly documented, not that rainouts never happened."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"track_id": {"type": "string", "description": "track_id from find_track."}},
            "required": ["track_id"],
        },
    },
    {
        "name": "get_rain_chance",
        "title": "Rain chance anywhere",
        "description": (
            "Worldwide hourly or daily chance of rain and expected amount for a place name or lat/lon, e.g. "
            "query='London' or query='Austin, TX' or lat/lon. Uses NWS in the US (Open-Meteo elsewhere and as fallback). "
            "Read `summary` aloud. If ambiguous=true, ask the user which of `matches` they mean and call again with "
            "a more specific query (add state/country), pick, or lat/lon."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Place name ('Tokyo', 'Austin, TX') or 'lat,lon'."},
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
                "hours": {"type": "integer", "minimum": 1, "maximum": 384, "description": "Hourly rows from now (default 24)."},
                "days": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Daily rows from today."},
                "at": {"type": "string", "description": "Local time at the place, e.g. '2026-10-18 19:00', or ISO with offset."},
                "pick": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Choose match N from an ambiguous result."},
            },
        },
    },
]
TOOLS.append({
    "name": "report_cancellation",
    "title": "Report a cancellation",
    "description": (
        "Share what the user sees at the field: status='cancelled' (rained out/postponed), 'delayed', 'field_closed' or "
        "'playing'. Only report what the user told you, after they agree to share it. field_id from find_venue/find_team "
        "venues (or a venue alias); game_time ISO 8601 with offset, from 12 hours ago to 36 hours ahead (default now). "
        "Reports are anonymous and unverified, expire after 24 hours and adjust the play odds in get_venue_status only "
        "while the official status is unknown. Limited to 3 reports per venue and 10 in total per hour."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "field_id": {"type": "string", "description": "Venue field_id or alias."},
            "status": {"type": "string", "enum": ["cancelled", "delayed", "field_closed", "playing"]},
            "game_time": {"type": "string", "description": "Game start, ISO 8601 with UTC offset (default now)."},
            "note": {"type": "string", "maxLength": 280, "description": "Optional short note, e.g. 'umpire called it at 7:10'."},
        },
        "required": ["field_id", "status"],
    },
})
TOOLS.append({
    "name": "get_precipitation_nowcast",
    "title": "Rain in the next 2 hours",
    "description": (
        "Two-hour precipitation nowcast in 15-minute steps for a place or job site: amount, intensity (light/moderate/"
        "heavy) and rain chance per step, when rain starts and stops, and (US) what the nearest station measured in the "
        "last hour. Use it for 'is it about to rain here?' and right before a pour, roofing or a game. Pass lat/lon for a "
        "job site, or query with a town name. Read `summary` aloud. Model nowcast (NOAA HRRR via Open-Meteo, NWS), not radar."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Town name ('Austin, TX') or 'lat,lon'."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "pick": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Choose match N from an ambiguous result."},
        },
    },
})
TOOLS.append({
    "name": "check_concrete_pour",
    "title": "Concrete pour go / wait / no-go",
    "description": (
        "Weather go/wait/no-go for a concrete pour at a job site: rain and thunderstorms during the pour and the early-set "
        "window (2-4 h after, by mix), heavy rain later in the cure window, cold (<40F, ACI 306), freeze, heat (>90F, ACI 305), "
        "wind gusts and the ACI 305 evaporation rate. Returns decision, probability_percent, a plain `reason` to read aloud, "
        "per-check details, best_alternative start in the next few days and recheck_at. Pass lat/lon for the site (or "
        "location with a town name) and pour_start in local time. Weather screening only, not engineering advice; say so."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Town name ('Austin, TX') or 'lat,lon' when lat/lon aren't given."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "pour_start": {"type": "string", "description": "Local time at the site, e.g. '2026-10-21 07:00', or ISO 8601 with offset."},
            "duration_hours": {"type": "number", "minimum": 0.5, "maximum": 16, "description": "Pour length (default 4)."},
            "cure_hours": {"type": "number", "minimum": 2, "maximum": 72, "description": "Cure window to protect after the pour (default 24)."},
            "critical_hours": {"type": "number", "minimum": 1, "maximum": 8, "description": "Early-set window most sensitive to rain (default by mix, 2-6)."},
            "mix": {"type": "string", "enum": ["standard", "high_early", "accelerated", "retarded", "slag_or_fly_ash", "low_wc_or_silica_fume"]},
            "protection": {"type": "boolean", "description": "Covers/blankets ready on site (softens later-cure rain and freeze to wait)."},
            "concrete_temp_f": {"type": "number", "description": "Concrete temperature at placement if known (default: air temperature)."},
            "search_days": {"type": "integer", "minimum": 0, "maximum": 5, "description": "Days to scan for a better start (default 3)."},
            "pick": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["pour_start"],
    },
})
TOOLS.append({
    "name": "check_materials_risk",
    "title": "Rain damage risk for job-site materials",
    "description": (
        "Rain damage risk per material left on a job site over a time window (default next 24 h): drywall, insulation, "
        "lumber, osb (or plywood), cement_bags (mortar, grout), open_roof_deck, electrical, flooring, masonry_units, "
        "windows_doors, rebar, open_excavation. Returns overall_risk and per material risk (low/moderate/high/severe), "
        "reason and plain protective actions, plus expected rain total, peak intensity and gusts (covers can blow off "
        "at 25+ mph). Covered materials drop one step. Read `summary` aloud. Weather screening only, not engineering advice."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Town name or 'lat,lon' when lat/lon aren't given."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "materials": {"type": "array", "minItems": 1, "maxItems": 20,
                          "items": {"type": "string"}, "description": "Material names, e.g. ['drywall', 'lumber', 'cement_bags']."},
            "covered": {"type": "boolean", "description": "Whether the materials are tarped or under cover (default false)."},
            "covered_materials": {"type": "array", "items": {"type": "string"}, "description": "Only these materials are covered."},
            "start": {"type": "string", "description": "Window start, local time at the site (default now)."},
            "end": {"type": "string", "description": "Window end, local time (default start + hours)."},
            "hours": {"type": "number", "minimum": 1, "maximum": 168, "description": "Window length when end is not given (default 24)."},
            "pick": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["materials"],
    },
})
for _tool in TOOLS:
    _tool["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
_REPORT = next(t for t in TOOLS if t["name"] == "report_cancellation")
_REPORT["annotations"] = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
_CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("rainout_mcp_client", default=None)


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


def _track_call(fn: Callable[..., dict[str, Any]], *a: Any, **kw: Any) -> dict[str, Any]:
    try:
        return fn(*a, **kw)
    except motocross.TracksError as exc:
        raise ToolError(str(exc)) from exc
    except motocross.UnknownTrack as exc:
        raise ToolError("Unknown track_id. Call find_track first.") from exc


def _find_track(args: dict[str, Any]) -> dict[str, Any]:
    return _track_call(motocross.list_tracks, args.get("query") or None, args.get("state") or None,
                       verification=args.get("verification") or None)


def _find_venue(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.status_api import fields_payload

    if not (args.get("query") or args.get("state") or args.get("near")):
        raise ToolError("Pass query, state or near.")
    try:
        return fields_payload(sport=args.get("sport"), state=args.get("state"), q=args.get("query"), near=args.get("near"),
                              radius_km=args.get("radius_km"), verification="all", limit=min(int(args.get("limit") or 10), 50))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _get_track_events(args: dict[str, Any]) -> dict[str, Any]:
    return _track_call(motocross.track_events, _require(args, "track_id"), date_param=args.get("date"),
                       from_param=args.get("from"), to_param=args.get("to"),
                       include_practice=bool(args.get("include_practice")), events_url=args.get("events_url"))


def _check_race(args: dict[str, Any]) -> dict[str, Any]:
    return _track_call(motocross.check_race, _require(args, "track_id"), date_param=args.get("date"),
                       event_id=args.get("event_id"), events_url=args.get("events_url"))


def _get_track_history(args: dict[str, Any]) -> dict[str, Any]:
    return _track_call(motocross.track_history, _require(args, "track_id"))


def _get_rain_chance(args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("query") and (args.get("lat") is None or args.get("lon") is None):
        raise ToolError("Pass query (place name or 'lat,lon') or lat and lon.")
    try:
        return rain.rain_chance(args.get("query"), lat=args.get("lat"), lon=args.get("lon"), hours=args.get("hours"),
                                days=args.get("days"), at=args.get("at"), pick=args.get("pick"))
    except rain.RainError as exc:
        raise ToolError(str(exc)) from exc
    except rain.PlaceNotFound as exc:
        names = "; ".join(s["label"] for s in exc.suggestions) or "none"
        raise ToolError(f"No place matched '{exc.query}'. Similar names: {names}. Ask the user for a country or lat,lon.") from exc


def _get_precipitation_nowcast(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.nowcast import get_precipitation_nowcast

    if not args.get("query") and (args.get("lat") is None or args.get("lon") is None):
        raise ToolError("Pass lat and lon, or query (town name or 'lat,lon').")
    return _place_call(get_precipitation_nowcast, args.get("query"), lat=args.get("lat"), lon=args.get("lon"), pick=args.get("pick"))


def _check_concrete_pour(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.concrete import check_concrete_pour

    if not args.get("location") and (args.get("lat") is None or args.get("lon") is None):
        raise ToolError("Pass lat and lon for the job site, or location (town name or 'lat,lon').")
    kw = {k: args.get(k) for k in ("pour_start", "duration_hours", "cure_hours", "critical_hours", "mix", "protection",
                                   "concrete_temp_f", "search_days", "pick")}
    return _place_call(check_concrete_pour, args.get("location"), lat=args.get("lat"), lon=args.get("lon"), **kw)


def _check_materials_risk(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.materials import check_materials_risk

    if not args.get("location") and (args.get("lat") is None or args.get("lon") is None):
        raise ToolError("Pass lat and lon for the job site, or location (town name or 'lat,lon').")
    mats = args.get("materials")
    if not mats:
        raise ToolError("materials is required, e.g. ['drywall', 'lumber'].")
    covered_only = {str(x).strip().lower() for x in args.get("covered_materials") or []}
    if covered_only and isinstance(mats, list):
        mats = [{"material": x, "covered": str(x).strip().lower() in covered_only} for x in mats]
    return _place_call(check_materials_risk, args.get("location"), lat=args.get("lat"), lon=args.get("lon"), pick=args.get("pick"),
                       materials=mats, covered=args.get("covered"), start=args.get("start"), end=args.get("end"), hours=args.get("hours"))


def _place_call(fn: Callable[..., dict[str, Any]], *a: Any, **kw: Any) -> dict[str, Any]:
    try:
        return fn(*a, **kw)
    except rain.RainError as exc:
        raise ToolError(str(exc)) from exc
    except rain.PlaceNotFound as exc:
        names = "; ".join(s["label"] for s in exc.suggestions) or "none"
        raise ToolError(f"No place matched '{exc.query}'. Similar names: {names}. Ask the user for a state or lat/lon.") from exc


def _report_cancellation(args: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent import reports

    try:
        return reports.submit_report(dict(args), client=_CLIENT.get())
    except reports.ReportError as exc:
        extra = f" Retry after {exc.extra['retry_after_seconds']} s." if exc.status == 429 else ""
        raise ToolError(str(exc) + extra) from exc


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
    "find_track": _find_track,
    "find_venue": _find_venue,
    "get_track_events": _get_track_events,
    "check_race": _check_race,
    "get_track_history": _get_track_history,
    "get_rain_chance": _get_rain_chance,
    "report_cancellation": _report_cancellation,
    "get_precipitation_nowcast": _get_precipitation_nowcast,
    "check_concrete_pour": _check_concrete_pour,
    "check_materials_risk": _check_materials_risk,
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
    from rainout_agent.reports import client_from_headers

    _CLIENT.set(client_from_headers(h))
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

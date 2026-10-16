"""Team lookup and per-team game odds for agents.

``search_teams`` finds candidate teams in known league sources (or in a
user-supplied schedule URL). ``team_games`` returns a team's games with play
odds, a one-sentence spoken reason, a Google Calendar API event body and ICS.

The API never writes to anyone's calendar. ``calendar_event`` is a ready-made
body that an agent may send to Google Calendar *after the user agrees*.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rainout_agent import team_sources as ts
from rainout_agent.safe_fetch import FetchError, UnsafeURLError, safe_get

BASE_URL = "https://rainout-agent-source.vercel.app"
DEFAULT_TZ = "America/Chicago"
MAX_RANGE_DAYS = 62
ODDS_WINDOW_DAYS = 31
MIN_CONFIDENCE = 0.6
NOT_PLAYED = {"cancelled", "postponed", "forfeit"}

ASK_FOR_LINK = (
    "I couldn't find that team in the public league schedules I know about. "
    "Ask the user for their league's schedule link (a calendar/ICS link or the schedule page URL) "
    "and call search_teams again with schedule_url."
)


class TeamsError(ValueError):
    """Bad request input (shown to the caller as 400)."""


# ---------------------------------------------------------------- search
def search_teams(
    q: str | None = None,
    *,
    venue: str | None = None,
    sport: str | None = None,
    day: str | None = None,
    schedule_url: str | None = None,
    fetcher: Callable[[str], Any] = safe_get,
    limit: int = 10,
) -> dict[str, Any]:
    from rainout_agent.status_api import load_field, resolve_field_id

    q = (q or "").strip()
    if not q and not schedule_url:
        raise TeamsError("Provide q (team name) or schedule_url.")
    if len(q) > 120:
        raise TeamsError("q is too long.")
    sport = (sport or "").strip().lower() or None
    day_norm = ts.parse_day(day) if day else None
    if day and not day_norm:
        raise TeamsError("day must be a weekday name, e.g. friday.")
    venue_id = resolve_field_id(venue) if venue else None
    venue_info = None
    if venue_id:
        f = load_field(venue_id)
        venue_info = {"field_id": venue_id, "name": f["field_name"]}

    searched: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    extraction = None

    if schedule_url:
        try:
            found, extraction = _teams_from_url(schedule_url, fetcher, day_norm, venue_id)
            teams.extend(found)
            searched.append({"source": schedule_url, "status": "ok", "teams": len(found), "extraction": extraction})
        except UnsafeURLError as exc:
            raise TeamsError(f"schedule_url is not allowed: {exc}") from exc
        except (FetchError, ts.SourceError) as exc:
            searched.append({"source": schedule_url, "status": "error", "error": str(exc)})
    else:
        for league in ts.load_leagues():
            if sport and league.get("sport") and league["sport"] != sport:
                continue
            if venue_id and venue_id not in (league.get("location_venues") or {}).values():
                continue
            try:
                if league["platform"] == "teamsideline":
                    found = ts.tsl_list_teams(league, day=day_norm, venue_field_id=venue_id)
                elif league["platform"] == "ambl":
                    found = ts.ambl_list_teams(league)
                else:
                    continue
                teams.extend(found)
                searched.append({"source": league["league_id"], "name": league["name"], "status": "ok", "teams": len(found)})
            except ts.SourceError as exc:
                searched.append({"source": league["league_id"], "name": league["name"], "status": "error", "error": str(exc)})

    candidates = []
    for team in teams:
        sim = ts.name_similarity(q, team["name"]) if q else 0.7
        if q and sim < 0.7:
            continue
        conf = 0.75 * sim
        for wanted, have in ((sport, team.get("sport")), (day_norm, team.get("day")), (venue_id, team.get("venue_field_id"))):
            if wanted and have:
                conf += 0.08 if wanted == have else -0.25
            elif wanted:
                conf += 0.02
        if not (sport or day_norm or venue_id):
            conf += 0.15 * sim
        conf = round(max(0.0, min(0.99, conf)), 2)
        if conf < MIN_CONFIDENCE and q:
            continue
        candidates.append(_public_team(team, conf))
    candidates.sort(key=lambda c: (-c["confidence"], c["name"]))
    candidates = candidates[: max(1, min(25, limit))]

    if candidates:
        top = candidates[0]
        next_step = "confirm_with_user"
        message = (
            f"Best match: {top['name']} ({top['league']}"
            + (f", {top['division']}" if top.get("division") else "")
            + "). Confirm with the user that this is their team before showing games."
        )
    else:
        next_step = "ask_user_for_schedule_url"
        message = ASK_FOR_LINK if not schedule_url else (
            "No matching team was found in that schedule. Ask the user to check the link or the exact team name."
        )
    return {
        "query": {"q": q or None, "venue": venue, "venue_resolved": venue_info, "sport": sport, "day": day_norm, "schedule_url": schedule_url},
        "count": len(candidates),
        "candidates": candidates,
        "next_step": next_step,
        "message": message,
        "searched_sources": searched,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def _teams_from_url(url: str, fetcher, day, venue_id) -> tuple[list[dict[str, Any]], str | None]:
    platform = ts.detect_platform(url)
    if platform is None:
        data = ts.generic_events(url, fetcher)
        if data.get("platform") == "generic":
            names = ts.generic_team_names(data.get("events") or [])
            return [
                {"team_id": ts.encode_url_team(url, n), "name": n, "league_id": None, "league": _host(url), "division": None,
                 "sport": None, "day": None, "venue_field_id": None, "schedule_url": url, "platform": "generic"}
                for n in names
            ], data.get("method")
        platform = {"platform": data["platform"], "league": ts.league_by_id(data["league_id"]) if data.get("league_id") else None,
                    "division_id": data.get("division_id")}
        if platform["platform"] == "teamsideline" and not platform["league"]:
            platform["league"] = ts._tsl_league(data["external_uid"])
    if platform["platform"] == "ambl":
        return ts.ambl_list_teams(platform["league"]), "ambl"
    return ts.tsl_list_teams(platform["league"], day=day, venue_field_id=venue_id, division_id=platform.get("division_id")), "teamsideline"


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc


def _public_team(team: dict[str, Any], confidence: float) -> dict[str, Any]:
    from rainout_agent.status_api import load_field

    venue = None
    if team.get("venue_field_id"):
        f = load_field(team["venue_field_id"])
        venue = {"field_id": f["id"], "name": f["field_name"], "city": f["city"], "state": f["state"]}
    return {
        "team_id": team["team_id"],
        "name": team["name"],
        "league": team.get("league"),
        "league_id": team.get("league_id"),
        "division": team.get("division"),
        "season": team.get("season"),
        "sport": team.get("sport"),
        "day": team.get("day"),
        "venue": venue,
        "schedule_source_url": team.get("schedule_url"),
        "platform": team.get("platform"),
        "confidence": confidence,
        "games_url": f"{BASE_URL}/v1/teams/{team['team_id']}/games",
    }


# ----------------------------------------------------------------- games
def load_team(team_id: str, fetcher: Callable[[str], Any] = safe_get, start: date | None = None, end: date | None = None):
    if team_id.startswith("tsl-"):
        return ts.tsl_games(team_id)
    if team_id.startswith("ambl-"):
        return ts.ambl_games(team_id, start, end)
    if team_id.startswith("url-"):
        return ts.generic_games(team_id, fetcher)
    raise ts.UnknownTeam(team_id)


def resolve_range(date_param: str | None, from_param: str | None, to_param: str | None, tz: ZoneInfo, now: datetime) -> tuple[date, date]:
    today = now.astimezone(tz).date()

    def parse(value: str, name: str) -> date:
        v = value.strip().lower()
        if v == "today":
            return today
        if v == "tomorrow":
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(v[:10])
        except ValueError as exc:
            raise TeamsError(f"{name} must be today, tomorrow or YYYY-MM-DD.") from exc

    if date_param:
        d = parse(date_param, "date")
        return d, d
    start = parse(from_param, "from") if from_param else today
    end = parse(to_param, "to") if to_param else start + timedelta(days=13)
    if end < start:
        raise TeamsError("to must be on or after from.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise TeamsError(f"Date range is limited to {MAX_RANGE_DAYS} days.")
    return start, end


def team_games(
    team_id: str,
    *,
    date_param: str | None = None,
    from_param: str | None = None,
    to_param: str | None = None,
    now: datetime | None = None,
    fetcher: Callable[[str], Any] = safe_get,
    checker: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(_team_tz(team_id))
    start_d, end_d = resolve_range(date_param, from_param, to_param, tz, now)
    lookahead_end = max(end_d, now.astimezone(tz).date() + timedelta(days=45))
    team, raw_games = load_team(team_id, fetcher, start_d - timedelta(days=1), lookahead_end)
    in_range = [g for g in raw_games if start_d <= g["start"].astimezone(tz).date() <= end_d]
    upcoming = sorted((g for g in raw_games if g["start"] >= now - timedelta(hours=3) and g["league_status"] not in NOT_PLAYED | {"final"}),
                      key=lambda g: g["start"])
    next_raw = upcoming[0] if upcoming else None
    to_score = list({g["source_game_id"]: g for g in in_range + ([next_raw] if next_raw else [])}.values())
    scored = score_games(team, to_score, now=now, checker=checker)
    games = [scored[g["source_game_id"]] for g in sorted(in_range, key=lambda g: g["start"])]
    next_game = scored.get(next_raw["source_game_id"]) if next_raw else None

    label = _range_label(start_d, end_d, now.astimezone(tz).date())
    if games:
        message = f"{team['name']} has {len(games)} game{'s' if len(games) != 1 else ''} {label}."
    else:
        message = f"{team['name']} has no games {label}."
        if next_game:
            message += f" Next game: {next_game['when_spoken']} vs {next_game['opponent']}."
    return {
        "team": _public_team(team, 1.0) | {"confidence": None},
        "range": {"from": start_d.isoformat(), "to": end_d.isoformat(), "timezone": str(tz)},
        "count": len(games),
        "games": games,
        "next_game": next_game,
        "message": message,
        "calendar_ics_url": f"{BASE_URL}/v1/teams/{team_id}/calendar.ics",
        "calendar_policy": "Rainout Source never writes to calendars. Ask the user before adding any event; then create it with calendar_event.",
        "as_of": now.isoformat(),
    }


def _team_tz(team_id: str) -> str:
    if team_id.startswith("tsl-"):
        uid = team_id.split("-")[1]
        return ts._tsl_league(uid).get("timezone") or DEFAULT_TZ
    if team_id.startswith("ambl-"):
        return (ts.league_by_id("ambl") or {}).get("timezone") or DEFAULT_TZ
    return DEFAULT_TZ


def _range_label(start: date, end: date, today: date) -> str:
    if start == end:
        if start == today:
            return "today"
        if start == today + timedelta(days=1):
            return "tomorrow"
        return f"on {start.strftime('%a %b')} {start.day}"
    return f"between {start.strftime('%b')} {start.day} and {end.strftime('%b')} {end.day}"


def score_games(team: dict[str, Any], raw_games: list[dict[str, Any]], *, now: datetime,
                checker: Callable[..., dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    from rainout_agent.schedule import check_schedule

    checker = checker or check_schedule
    pending = [g for g in raw_games if g["start"] >= now - timedelta(hours=3) and g["league_status"] not in NOT_PLAYED | {"final"}]
    report: dict[str, Any] = {"games": []}
    if pending:
        horizon = max(g["start"] for g in pending)
        window = max(1, min(ODDS_WINDOW_DAYS, (horizon - now).days + 1))
        report = checker(
            [{"id": g["source_game_id"], "title": f"{team['name']} vs {g.get('opponent') or 'TBD'}",
              "venue_query": g.get("venue_field_id") or g.get("field_label"), "location": g.get("field_label"),
              "start": g["start"], "end": g["end"]} for g in pending],
            now=now, window_days=window,
        )
    by_uid = {r.get("uid"): r for r in report.get("games", [])}
    return {g["source_game_id"]: build_game(team, g, by_uid.get(g["source_game_id"]), now) for g in raw_games}


def build_game(team: dict[str, Any], g: dict[str, Any], check: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    from rainout_agent.status_api import load_field

    tz = g["start"].tzinfo
    field = load_field(g["venue_field_id"]) if g.get("venue_field_id") else None
    venue = None
    if field:
        venue = {"field_id": field["id"], "name": field["field_name"], "field": g.get("field_label"),
                 "address": field.get("address"), "rainout_phone": field.get("rainout_phone")}
    elif g.get("field_label"):
        venue = {"field_id": None, "name": g["field_label"], "field": g.get("field_label"), "address": g.get("address"), "rainout_phone": None}

    status = g["league_status"]
    play, risk_level, weather, official, at_risk = None, None, None, None, None
    if status in NOT_PLAYED:
        play, risk_level, at_risk = 0, "cancelled_by_league", True
    elif status == "final":
        play, risk_level = 100, "played"
    elif check and check.get("status") == "evaluated":
        play = check.get("play_probability_percent")
        risk_level = check.get("risk_level")
        at_risk = check.get("at_risk")
        if check.get("rain_chance_percent") is not None:
            weather = {"rain_chance_percent": check.get("rain_chance_percent"), "thunderstorm_likely": check.get("thunderstorm_likely"),
                       "forecast": check.get("forecast"), "source": check.get("weather_source"), "source_id": check.get("weather_source_id")}
        official = check.get("official_status")
    when = _when_spoken(g["start"], now)
    reason = spoken_reason(team, g, play, check, venue, when)
    game_id = g["source_game_id"]
    out = {
        "game_id": game_id,
        "team": team["name"],
        "opponent": g.get("opponent"),
        "home_away": g.get("home_away"),
        "start": g["start"].isoformat(),
        "end": g["end"].isoformat() if g.get("end") else None,
        "end_estimated": bool(g.get("end_estimated")),
        "timezone": str(tz),
        "when_spoken": when,
        "venue": venue,
        "league_status": status,
        "play_probability": play,
        "risk_level": risk_level,
        "at_risk": at_risk,
        "reason": reason,
        "reason_detail": (check or {}).get("reason"),
        "weather": weather,
        "status_source": {
            "league_schedule": {"name": team.get("league"), "url": team.get("schedule_url"), "status": g.get("league_status_text")},
            "official": {"name": field.get("official_status_source_name") if field else None,
                         "url": field.get("official_status_source_url") if field else None,
                         "status": official, "checked": bool((check or {}).get("official_status_checked"))},
            "weather": weather.get("source") if weather else None,
        },
        "as_of": now.isoformat(),
    }
    out["calendar_event"] = calendar_event(team, out)
    out["ics"] = game_ics([out], team)
    return out


def _when_spoken(start: datetime, now: datetime) -> str:
    local_now = now.astimezone(start.tzinfo)
    days = (start.date() - local_now.date()).days
    clock = start.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif 1 < days < 7:
        day = start.strftime("%A")
    else:
        day = f"{start.strftime('%a %b')} {start.day}"
    return f"{day} at {clock}"


def spoken_reason(team, g, play, check, venue, when) -> str:
    place = venue["name"] if venue else "the field"
    opp = f" vs {g['opponent']}" if g.get("opponent") else ""
    status = g["league_status"]
    if status in {"cancelled", "postponed"}:
        return f"The league lists {when}{opp} as {g.get('league_status_text', status).lower()}, so it won't be played as scheduled."
    if status == "forfeit":
        return f"The league lists {when}{opp} as a forfeit, so there's no game to play."
    if status == "final":
        return f"That game{opp} was already played ({g.get('league_status_text')})."
    if play is None:
        if not venue or not (venue.get("field_id")):
            return f"I found the game {when}{opp}, but I don't know the venue's weather, so check with the league before leaving."
        return f"The game {when}{opp} is too far out for a forecast, so check again closer to game day."
    rain = (check or {}).get("rain_chance_percent")
    storms = (check or {}).get("thunderstorm_likely")
    official = (check or {}).get("official_status") or "unknown"
    if (check or {}).get("basis") == "history_only":
        return f"It's {play}% likely the game {when}{opp} at {place} is played, based on rain history because it's beyond the forecast."
    if official in {"cancelled", "field_closed", "closed"}:
        return f"The official status for {place} says {official.replace('_', ' ')}, so the game {when}{opp} is off."
    if storms:
        weather = f"thunderstorms are likely ({rain}% chance of rain)"
    elif rain is not None and rain <= 10:
        weather = "no meaningful rain in the forecast"
    elif rain is not None and rain < 35:
        weather = f"only a {rain}% chance of rain"
    else:
        weather = f"a {rain}% chance of rain"
    if play >= 85:
        lead = "should be on"
    elif play >= 60:
        lead = "will probably be played"
    else:
        lead = "is at real risk of a rainout"
    tail = " and the official status is on" if official in {"on", "open", "playing"} else " and no cancellation has been posted"
    return f"Your game {when}{opp} at {place} {lead} ({play}% likely): {weather}{tail}."


# --------------------------------------------------------------- calendar
def _uid(game_id: str) -> str:
    return f"{game_id}@rainout-agent-source.vercel.app"


def calendar_event(team: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    """Google Calendar API event resource (events.insert / events.import body)."""
    venue = game.get("venue") or {}
    loc_parts = [venue.get("field"), venue.get("name") if venue.get("name") != venue.get("field") else None, venue.get("address")]
    location = ", ".join(p for p in loc_parts if p) or None
    odds = game.get("play_probability")
    lines = [
        f"Play odds: {odds}%" if odds is not None else "Play odds: not available yet",
        game.get("reason") or "",
        f"Checked {game['as_of'][:16].replace('T', ' ')} UTC by Rainout Source; odds change with the forecast.",
    ]
    if venue.get("rainout_phone") and venue["rainout_phone"] not in ("not_published", "unknown"):
        lines.append(f"Rainout line: {venue['rainout_phone']}")
    if team.get("schedule_url"):
        lines.append(f"League schedule: {team['schedule_url']}")
    tzname = game["timezone"]
    event = {
        "summary": f"{game['team']} vs {game['opponent']}" if game.get("opponent") else f"{game['team']} game",
        "location": location,
        "description": "\n".join(l for l in lines if l),
        "start": {"dateTime": game["start"], "timeZone": tzname},
        "end": {"dateTime": game["end"] or game["start"], "timeZone": tzname},
        "iCalUID": _uid(game["game_id"]),
        "source": {"title": "Rainout Source", "url": f"{BASE_URL}/v1/teams/{team['team_id']}/games"},
        "reminders": {"useDefault": True},
    }
    if game.get("league_status") in NOT_PLAYED:
        event["status"] = "cancelled"
    return event


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    from rainout_agent.schedule import _fold as fold

    return fold(line)


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def game_ics(games: list[dict[str, Any]], team: dict[str, Any], name: str | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JEEZ Labs//Rainout Source//EN", "CALSCALE:GREGORIAN"]
    if name:
        lines += [f"X-WR-CALNAME:{_esc(name)}", "X-WR-TIMEZONE:" + (games[0]["timezone"] if games else DEFAULT_TZ)]
    for g in games:
        ev = g["calendar_event"]
        lines += ["BEGIN:VEVENT", f"UID:{ev['iCalUID']}", f"DTSTAMP:{stamp}", f"DTSTART:{_utc(ev['start']['dateTime'])}",
                  f"DTEND:{_utc(ev['end']['dateTime'])}", f"SUMMARY:{_esc(ev['summary'])}"]
        if ev.get("location"):
            lines.append(f"LOCATION:{_esc(ev['location'])}")
        lines.append(f"DESCRIPTION:{_esc(ev['description'])}")
        lines.append("STATUS:CANCELLED" if ev.get("status") == "cancelled" else "STATUS:CONFIRMED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def team_calendar(team_id: str, *, now: datetime | None = None, fetcher=safe_get, checker=None) -> str:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(_team_tz(team_id))
    today = now.astimezone(tz).date()
    team, raw = load_team(team_id, fetcher, today - timedelta(days=7), today + timedelta(days=120))
    raw = [g for g in raw if g["start"].astimezone(tz).date() >= today - timedelta(days=7)]
    scored = score_games(team, raw, now=now, checker=checker)
    games = [scored[g["source_game_id"]] for g in sorted(raw, key=lambda g: g["start"])]
    return game_ics(games, team, name=f"{team['name']} ({team.get('league') or 'schedule'}) - Rainout Source")

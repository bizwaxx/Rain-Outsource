"""TeamSnap prototype: read a team's schedule and attach Rain-Outsource play odds.

PROTOTYPE, not wired into any Vercel function (no new function, nothing live).

* Reads TeamSnap API v3 (https://api.teamsnap.com/v3), which speaks
  Collection+JSON (application/vnd.collection+json): every record is an item
  whose ``data`` is a list of ``{"name": ..., "value": ...}`` pairs.
* Without a ``TEAMSNAP_TOKEN`` env var it reads recorded/mock fixtures shaped
  like the documented responses (tests/fixtures/teamsnap). With the token it
  calls the real API read-only. The token is only sent as a Bearer header to
  api.teamsnap.com and is never printed, logged or stored.
* Official status comes first: a game the team manager cancelled in TeamSnap
  is "cancelled", then the venue's official rainout source, and only then our
  estimate. Odds are always labelled as an estimate.

Docs used (shape of the responses):
  https://github.com/teamsnap/teamsnap-javascript-sdk  (APIv3 SDK, collections, OAuth quick start)
  https://github.com/teamsnap/teamsnap_rb              (APIv3 Ruby client, root + search calls)
  https://github.com/teamsnap/omniauth-teamsnap        (OAuth2 app id/secret, "read" scope)
  http://amundsen.com/media-types/collection/          (Collection+JSON format)
  Events search filters (team_id, is_game, started_after, sort_start_date,
  page_size) and the event template (is_game, is_canceled, start_date,
  location_id, opponent_id, time_zone ...) as shown in TeamSnap's own APIv3
  talk: https://slides.com/dmccraw/teamsnap_and_graphql
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from rainout_agent.schedule import check_schedule

API_ROOT = "https://api.teamsnap.com/v3"
API_HOST = "api.teamsnap.com"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "teamsnap"
TOKEN_ENV = "TEAMSNAP_TOKEN"
ESTIMATE_LABEL = "Estimate, not official. Official status comes from the league or venue."
MAX_PAGE = 100


class TeamSnapError(RuntimeError):
    pass


# ------------------------------------------------------------ Collection+JSON
def parse_collection(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a Collection+JSON document into plain dicts (one per item)."""
    coll = (doc or {}).get("collection")
    if not isinstance(coll, dict):
        raise TeamSnapError("not a Collection+JSON document")
    if coll.get("error"):
        err = coll["error"]
        raise TeamSnapError(f"TeamSnap error: {err.get('title') or err.get('message') or 'unknown'}")
    rows = []
    for item in coll.get("items") or []:
        row = {d.get("name"): d.get("value") for d in item.get("data") or [] if isinstance(d, dict) and d.get("name")}
        if item.get("href"):
            row["_href"] = item["href"]
        rows.append(row)
    return rows


# ------------------------------------------------------------------ sources
class FixtureSource:
    """Recorded/mock responses on disk: events.json, locations.json, opponents.json, teams.json."""

    name = "fixtures"

    def __init__(self, directory: Path | str = FIXTURE_DIR):
        self.dir = Path(directory)

    def _load(self, kind: str, team_id: str) -> list[dict[str, Any]]:
        path = self.dir / f"{kind}.json"
        if not path.exists():
            return []
        rows = parse_collection(json.loads(path.read_text()))
        return [r for r in rows if str(r.get("team_id", team_id) if kind != "teams" else r.get("id")) == str(team_id)]

    def team(self, team_id: str) -> dict[str, Any] | None:
        return (self._load("teams", team_id) or [None])[0]

    def events(self, team_id: str, started_after: datetime | None = None) -> list[dict[str, Any]]:
        return self._load("events", team_id)

    def locations(self, team_id: str) -> list[dict[str, Any]]:
        return self._load("locations", team_id)

    def opponents(self, team_id: str) -> list[dict[str, Any]]:
        return self._load("opponents", team_id)


class TokenSource:
    """Read-only client for the real API. Needs an OAuth access token (scope "read")."""

    name = "teamsnap_api"

    def __init__(self, token: str, opener: Callable[..., Any] | None = None, timeout: float = 10.0):
        if not token:
            raise TeamSnapError("missing token")
        self._token = token
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return "TokenSource(token=***)"

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{API_ROOT}/{path}?{urllib.parse.urlencode(params)}"
        if urllib.parse.urlparse(url).hostname != API_HOST:
            raise TeamSnapError("refusing to send the token to another host")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.collection+json",
            "User-Agent": "rainout-source-teamsnap-prototype",
        })
        try:
            with self._open(req, timeout=self.timeout) as resp:
                body = resp.read(5_000_000)
        except Exception as exc:  # do not echo the request (it carries the token)
            raise TeamSnapError(f"TeamSnap request to /{path} failed ({type(exc).__name__})") from None
        return parse_collection(json.loads(body))

    def team(self, team_id: str) -> dict[str, Any] | None:
        return (self._get("teams/search", {"id": team_id}) or [None])[0]

    def events(self, team_id: str, started_after: datetime | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"team_id": team_id, "sort_start_date": "asc", "page_size": MAX_PAGE}
        if started_after:
            params["started_after"] = started_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get("events/search", params)

    def locations(self, team_id: str) -> list[dict[str, Any]]:
        return self._get("locations/search", {"team_id": team_id})

    def opponents(self, team_id: str) -> list[dict[str, Any]]:
        return self._get("opponents/search", {"team_id": team_id})


def default_source(env: dict[str, str] | None = None, fixture_dir: Path | str = FIXTURE_DIR):
    """Real API only when TEAMSNAP_TOKEN is set; otherwise fixtures."""
    env = os.environ if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    return TokenSource(token) if token else FixtureSource(fixture_dir)


# ------------------------------------------------------------------ mapping
def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def events_to_games(events, locations, opponents, team_name: str | None = None,
                    shift: timedelta | None = None) -> list[dict[str, Any]]:
    """Turn TeamSnap events into Rain-Outsource schedule games (games only, not practices)."""
    locs = {str(l.get("id")): l for l in locations}
    opps = {str(o.get("id")): o for o in opponents}
    games = []
    for ev in events:
        if not ev.get("is_game"):
            continue
        start = _parse_ts(ev.get("start_date"))
        if start is None or ev.get("is_tbd"):
            continue
        if shift:
            start = start + shift
        loc = locs.get(str(ev.get("location_id"))) or {}
        opp = (opps.get(str(ev.get("opponent_id"))) or {}).get("name")
        us = team_name or "Team"
        home_away = str(ev.get("game_type") or "").lower()
        home, away = (opp, us) if home_away == "away" else (us, opp)
        title = ev.get("name") or (f"{home} vs {away}" if opp else f"{us} game")
        game: dict[str, Any] = {
            "source": "teamsnap",
            "id": f"teamsnap-event-{ev.get('id')}",
            "title": title,
            "home": home,
            "away": away,
            "venue_query": loc.get("name"),
            "location": ", ".join(x for x in (loc.get("name"), loc.get("address")) if x) or None,
            "start": start,
            "teamsnap_event_id": ev.get("id"),
        }
        if ev.get("duration_in_minutes"):
            game["end"] = start + timedelta(minutes=int(ev["duration_in_minutes"]))
        if ev.get("time_zone_iana_name"):
            game["timezone"] = ev["time_zone_iana_name"]
        if ev.get("is_canceled"):
            game["calendar_cancelled"] = True
            game["cancelled_in_teamsnap"] = True
        games.append(game)
    return games


def _official_block(entry: dict[str, Any], cancelled_in_teamsnap: bool) -> dict[str, Any]:
    if cancelled_in_teamsnap:
        return {"status": "cancelled", "source": "TeamSnap (marked cancelled by the team)"}
    status = entry.get("official_status") or "unknown"
    if entry.get("official_status_checked") and status != "unknown":
        return {"status": status, "source": entry.get("official_status_source_url") or "venue official source"}
    return {"status": "not posted", "source": entry.get("official_status_source_url"),
            "note": "No official call yet. Check the rainout line or official page before leaving."}


def team_odds(team_id: str, *, source=None, now: datetime | None = None, shift: timedelta | None = None,
              forecast_provider=None, official_provider=None, window_days: int = 14) -> dict[str, Any]:
    """Read the team's schedule and attach play odds. Official status first, odds as an estimate."""
    source = source or default_source()
    now = now or datetime.now(timezone.utc)
    team = source.team(team_id) or {}
    events = source.events(team_id, started_after=now - timedelta(hours=6))
    games = events_to_games(events, source.locations(team_id), source.opponents(team_id),
                            team.get("name"), shift=shift)
    by_id = {g["id"]: g for g in games}
    report = check_schedule(games, now=now, window_days=window_days,
                            forecast_provider=forecast_provider, official_provider=official_provider)
    out = []
    for entry in report["games"]:
        g = by_id.get(entry.get("uid")) or {}
        play = entry.get("play_probability_percent")
        out.append({
            "official_status": _official_block(entry, bool(g.get("cancelled_in_teamsnap"))),
            "play_odds": {
                "percent": play,
                "label": "estimate",
                "note": ESTIMATE_LABEL,
                "risk_level": entry.get("risk_level"),
                "basis": entry.get("basis"),
                "reason": entry.get("reason"),
            } if play is not None else {"percent": None, "label": "estimate", "note": "No estimate: " + (
                entry.get("recommendation") or "no forecast or history for this venue.")},
            "teamsnap_event_id": g.get("teamsnap_event_id"),
            "title": entry.get("title"),
            "start": entry.get("start"),
            "location_text": entry.get("location_text"),
            "venue": entry.get("venue"),
            "status_url": entry.get("status_url"),
            "rainout_phone": entry.get("rainout_phone"),
        })
    return {
        "service": "rainout-source",
        "prototype": "teamsnap",
        "data_source": source.name,
        "team": {"id": str(team_id), "name": team.get("name")},
        "generated_at": now.isoformat(),
        "games": out,
        "summary": report["summary"],
        "warnings": report["warnings"],
        "labels": [
            "Official status is shown first and always wins.",
            "Play odds are an estimate, not an official call.",
        ],
    }


def format_text(result: dict[str, Any]) -> str:
    lines = [f"{result['team'].get('name') or 'Team'} (TeamSnap {result['team']['id']}, source: {result['data_source']})"]
    for g in result["games"]:
        off, odds = g["official_status"], g["play_odds"]
        if off["status"] in ("cancelled", "canceled"):
            est = "none needed, the game is officially cancelled."
        elif odds.get("percent") is not None:
            est = f"{odds['percent']}% chance it's played. {odds['note']}"
        else:
            est = odds["note"]
        lines.append(f"- {g['title']} | {g.get('start')} | {g.get('location_text') or 'no location'}")
        lines.append(f"    Official: {off['status']}" + (f" ({off['source']})" if off.get("source") else ""))
        lines.append(f"    Estimate: {est}")
    lines += [f"Note: {w}" for w in result.get("warnings") or []]
    return "\n".join(lines)

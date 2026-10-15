"""League schedule source adapters used for team lookup.

Each adapter turns a public league schedule into two things:

* teams: ``{team_id, name, league_id, league, division, sport, day, venue_field_id,
  schedule_url, platform}``
* raw games: ``{source_game_id, team, opponent, home_away, start, end, end_estimated,
  field_label, venue_field_id, league_status, league_status_text}``

Adapters:

* ``teamsideline``: TeamSideline league sites (for example City of Austin adult
  softball). Reads the same public JSON API that the league's own schedule pages
  use (``cms-api.teamsideline.com``).
* ``ambl``: Austin Metro Baseball League ``schedule.php`` pages.
* ``generic``: a user-supplied ``schedule_url`` (ICS feed, or an HTML page with a
  linked ICS, schema.org SportsEvent JSON-LD or a schedule table). It is fetched
  with the SSRF-safe fetcher. If deterministic parsing finds nothing and
  ``RAINOUT_LLM_API_KEY`` is set, an optional LLM extractor is tried. Any LLM
  failure means "no games found", never an error.

Team ids are self-describing, so the API stays stateless on serverless hosts:
``tsl-<org>-<division>-<team>``, ``ambl-<year><season>-<team>`` and
``url-<base64url(json{u,t})>``.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rainout_agent.cache import get_cache
from rainout_agent.safe_fetch import FetchError, UnsafeURLError, safe_get, validate_url

log = logging.getLogger("rainout.teams")

LEAGUES_DIR = Path(__file__).resolve().parents[1] / "data" / "leagues"
USER_AGENT = "Mozilla/5.0 (compatible; RainoutSource/1.0; +https://rainout-agent-source.vercel.app)"
TSL_API = "https://cms-api.teamsideline.com/api/v1"
TEAMS_TTL_S = 6 * 3600
GAMES_TTL_S = 600
FETCH_TIMEOUT_S = 6.0
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class SourceError(RuntimeError):
    """A league schedule source could not be read."""


class UnknownTeam(ValueError):
    """The team id is malformed or refers to an unsupported source."""


# ------------------------------------------------------------------ utilities
def _http_get(url: str, timeout: float = FETCH_TIMEOUT_S, accept: str = "application/json") -> bytes:
    """GET from a fixed, trusted league host (monkeypatched in tests)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(4_000_000)
    except Exception as exc:  # network, HTTP errors, timeouts
        raise SourceError(f"{urllib.parse.urlsplit(url).netloc}: {exc.__class__.__name__}") from exc


def _get_json(url: str, timeout: float = FETCH_TIMEOUT_S) -> Any:
    raw = _http_get(url, timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SourceError(f"{urllib.parse.urlsplit(url).netloc}: not JSON") from exc


def _cached(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    cache = get_cache()
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = producer()
    cache.set(key, value, ttl)
    return value


def normalize_name(value: str | None) -> str:
    text = html_lib.unescape(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [w for w in text.split() if w not in {"the"}]
    return " ".join(words)


def name_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(len(ta), len(tb))
    contained = 0.85 if (f" {na} " in f" {nb} " or f" {nb} " in f" {na} ") else 0.0
    return round(max(ratio, overlap * 0.95, contained), 3)


def parse_day(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    for day in DAYS:
        if text and (text == day or text.startswith(day[:3]) or day in text):
            return day
    return None


@lru_cache(maxsize=1)
def load_leagues() -> tuple[dict[str, Any], ...]:
    leagues: list[dict[str, Any]] = []
    for path in sorted(LEAGUES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        leagues.extend(data.get("leagues", []))
    return tuple(leagues)


def league_by_id(league_id: str) -> dict[str, Any] | None:
    return next((lg for lg in load_leagues() if lg["league_id"] == league_id), None)


def _venue_for_text(league: dict[str, Any], text: str | None) -> str | None:
    lowered = (text or "").lower()
    for key, field_id in (league.get("location_venues") or {}).items():
        if key in lowered:
            return field_id
    from rainout_agent.schedule import match_venue  # local import: schedule imports status_api

    field_id, _ = match_venue(text)
    return field_id


# --------------------------------------------------------------- TeamSideline
def _tsl_league(uid: str) -> dict[str, Any]:
    for lg in load_leagues():
        if lg.get("platform") == "teamsideline" and str(lg.get("external_uid")) == str(uid):
            return lg
    return {
        "league_id": f"tsl-{uid}",
        "name": f"TeamSideline league {uid}",
        "platform": "teamsideline",
        "external_uid": str(uid),
        "site_url": None,
        "sport": None,
        "timezone": "America/Chicago",
        "location_venues": {},
        "area_venues": {},
    }


def tsl_divisions(league: dict[str, Any]) -> list[dict[str, Any]]:
    uid = league["external_uid"]

    def produce() -> list[dict[str, Any]]:
        data = _get_json(f"{TSL_API}/Schedules?externalUID={urllib.parse.quote(str(uid))}")
        out = []
        for item in (data or {}).get("items", []):
            desc = item.get("leagueDescription") or ""
            for div in item.get("divisionMenuItems") or []:
                out.append({
                    "division_id": div.get("recordId"),
                    "division": (div.get("displayValue") or "").strip(),
                    "league_desc": desc,
                    "season": item.get("seasonDescription"),
                })
        return out

    divisions = _cached(f"tsl:divisions:{uid}", TEAMS_TTL_S, produce)
    flt = (league.get("league_filter") or "").lower()
    return [d for d in divisions if not flt or flt in d["league_desc"].lower()]


def _tsl_division_meta(league: dict[str, Any], div: dict[str, Any]) -> dict[str, Any]:
    day = parse_day(div["league_desc"])
    venue = None
    words = div["division"].lower().split()
    for area, field_id in (league.get("area_venues") or {}).items():
        if area in words:
            venue = field_id
    sport = league.get("sport")
    if not sport:
        lowered = div["league_desc"].lower()
        sport = next((s for s in ("softball", "baseball", "soccer", "kickball", "volleyball", "basketball") if s in lowered), None)
    return {"day": day, "venue_field_id": venue, "sport": sport}


def tsl_teams(league: dict[str, Any], div: dict[str, Any]) -> list[dict[str, Any]]:
    uid, div_id = league["external_uid"], div["division_id"]
    url = f"{TSL_API}/Teams?externalUId={uid}&divisionId={div_id}&scheduleTypeId=1&teamId=0"
    raw = _cached(f"tsl:teams:{uid}:{div_id}", TEAMS_TTL_S, lambda: [
        {"id": t.get("id"), "name": (t.get("longTeamName") or t.get("teamName") or "").strip()}
        for t in (_get_json(url) or []) if isinstance(t, dict) and t.get("id")
    ])
    meta = _tsl_division_meta(league, div)
    schedule_url = f"{league['site_url']}/schedule?divisionid={div_id}" if league.get("site_url") else url
    return [
        {
            "team_id": f"tsl-{uid}-{div_id}-{t['id']}",
            "name": t["name"],
            "league_id": league["league_id"],
            "league": league["name"],
            "division": f"{div['league_desc']} {div['division']}".strip(),
            "season": div.get("season"),
            "sport": meta["sport"],
            "day": meta["day"],
            "venue_field_id": meta["venue_field_id"],
            "schedule_url": schedule_url,
            "platform": "teamsideline",
        }
        for t in raw
    ]


def tsl_list_teams(league: dict[str, Any], day: str | None = None, venue_field_id: str | None = None,
                   division_id: int | None = None) -> list[dict[str, Any]]:
    divisions = tsl_divisions(league)
    if division_id:
        divisions = [d for d in divisions if str(d["division_id"]) == str(division_id)]
    if day:
        divisions = [d for d in divisions if parse_day(d["league_desc"]) in (None, day)]
    if venue_field_id:
        divisions = [d for d in divisions if _tsl_division_meta(league, d)["venue_field_id"] in (None, venue_field_id)]
    teams: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for chunk in pool.map(lambda d: _safe(lambda: tsl_teams(league, d)), divisions[:60]):
            teams.extend(chunk or [])
    return teams


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except SourceError as exc:
        log.warning("team source error: %s", exc)
        return None


def _tsl_status(description: str) -> str:
    d = (description or "").strip().lower()
    if not d:
        return "scheduled"
    if "forfeit" in d:
        return "forfeit"
    if any(k in d for k in ("rain", "weather", "cancel", "lightning", "field closed")):
        return "cancelled"
    if "postpon" in d or "resched" in d:
        return "postponed"
    if d in {"played", "final", "complete", "completed"}:
        return "final"
    return d.replace(" ", "_")


def tsl_games(team_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    m = re.fullmatch(r"tsl-(\d+)-(\d+)-(\d+)", team_id)
    if not m:
        raise UnknownTeam(team_id)
    uid, div_id, tid = m.groups()
    league = _tsl_league(uid)
    tz = ZoneInfo(league.get("timezone") or "America/Chicago")
    div = next((d for d in tsl_divisions({**league, "league_filter": ""}) if str(d["division_id"]) == div_id), None)
    if div is None:
        raise UnknownTeam(team_id)
    team = next((t for t in tsl_teams(league, div) if t["team_id"] == team_id), None)
    if team is None:
        raise UnknownTeam(team_id)
    url = f"{TSL_API}/Schedules/Games?externalUId={uid}&divisionId={div_id}&scheduleTypeId=1"
    items = _cached(f"tsl:games:{uid}:{div_id}", GAMES_TTL_S, lambda: _get_json(url) or [])
    games = []
    for row in items:
        s = (row or {}).get("scheduleItem") or {}
        home_id, away_id = str(s.get("homeTeamId")), str(s.get("awayTeamId"))
        if tid not in (home_id, away_id) or not s.get("itemDateTime"):
            continue
        start = datetime.fromisoformat(s["itemDateTime"]).replace(tzinfo=tz)
        end, estimated = start + timedelta(minutes=55), True
        times = re.findall(r"(\d{1,2}:\d{2}\s*[AP]M)", s.get("itemDateTimeShortTime") or "", re.I)
        if len(times) == 2:
            end_t = datetime.strptime(times[1].upper().replace(" ", ""), "%I:%M%p").time()
            end = datetime.combine(start.date(), end_t, tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)
            estimated = False
        is_home = tid == home_id
        location = s.get("location") or {}
        label = (location.get("locationShortName") or "").strip()
        games.append({
            "source_game_id": f"tsl-{uid}-{s.get('id')}",
            "team": team["name"],
            "opponent": (s.get("longAwayTeamName") or s.get("awayTeamName")) if is_home else (s.get("longHomeTeamName") or s.get("homeTeamName")),
            "home_away": "home" if is_home else "away",
            "start": start,
            "end": end,
            "end_estimated": estimated,
            "field_label": label or None,
            "address": ", ".join(x for x in (location.get("streetAddress"), location.get("city"), location.get("state")) if x) or None,
            "venue_field_id": _venue_for_text(league, label) or team.get("venue_field_id"),
            "league_status": _tsl_status(s.get("dispositionDescription")),
            "league_status_text": (s.get("dispositionDescription") or "").strip() or "Scheduled",
            "comments": (s.get("comments") or "").strip() or None,
        })
    return team, games


# ---------------------------------------------------------------------- AMBL
AMBL_SEASON_MONTHS = {"spring": list(range(3, 9)), "fall": [9, 10, 11]}


def _strip_tags(value: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def ambl_index(league: dict[str, Any]) -> dict[str, Any]:
    def produce() -> dict[str, Any]:
        page = _http_get(f"{league['site_url']}/schedule.php", accept="text/html").decode("utf-8", "replace")
        season = re.search(r'<option value="(\d{4})\|(spring|fall)"\s+selected', page)
        if not season:
            season = re.search(r'<option value="(\d{4})\|(spring|fall)"', page)
        if not season:
            raise SourceError("AMBL: could not find the current season")
        select = re.search(r'<select name="team".*?</select>', page, re.S)
        teams = []
        for value, label in re.findall(r"<option value=['\"](\d+)['\"][^>]*>([^<]+)", select.group(0) if select else ""):
            label = label.strip()
            if int(value) >= 100 or int(value) == 0 or label.upper() in {"OPEND"} or label.lower().startswith(("only", "full")):
                continue
            teams.append({"id": value, "name": label})
        return {"year": season.group(1), "season": season.group(2), "teams": teams}

    return _cached("ambl:index", TEAMS_TTL_S, produce)


def ambl_list_teams(league: dict[str, Any], **_: Any) -> list[dict[str, Any]]:
    idx = ambl_index(league)
    key = f"{idx['year']}{idx['season']}"
    return [
        {
            "team_id": f"ambl-{key}-{t['id']}",
            "name": t["name"],
            "league_id": league["league_id"],
            "league": league["name"],
            "division": f"{idx['season'].title()} {idx['year']}",
            "season": f"{idx['season'].title()} {idx['year']}",
            "sport": "baseball",
            "day": None,  # AMBL plays mostly weekends, mixed days
            "venue_field_id": None,
            "schedule_url": f"{league['site_url']}/schedule.php?season_key={idx['year']}%7C{idx['season']}&team={t['id']}",
            "platform": "ambl",
        }
        for t in idx["teams"]
    ]


def parse_ambl_schedule(page: str, year: int, month: int) -> list[dict[str, Any]]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cells) != 11 or not cells[1].isdigit() or not cells[2].isdigit():
            continue
        tm = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap]m)?", cells[4].strip().lower())
        if not tm:
            continue
        hour, minute = int(tm.group(1)), int(tm.group(2))
        meridiem = tm.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem is None and hour < 8:  # 1:00, 4:00, 7:00 are afternoon/evening; 8-11 morning
            hour += 12
        rows.append({
            "game_no": cells[1], "year": year, "month": month, "day": int(cells[2]),
            "hour": hour, "minute": minute,
            "visitor": cells[5], "visitor_score": cells[6], "home": cells[7], "home_score": cells[8],
            "field": cells[9], "division": cells[10],
        })
    return rows


def _ambl_status(row: dict[str, Any]) -> tuple[str, str]:
    scores = f"{row['visitor_score']} {row['home_score']}".lower()
    if "rain" in scores:
        return "cancelled", "Rained out (postponed)"
    if "ppd" in scores or "post" in scores:
        return "postponed", "Postponed"
    if re.search(r"\d", row["visitor_score"]) and re.search(r"\d", row["home_score"]):
        return "final", f"Final {row['visitor_score']}-{row['home_score']}"
    return "scheduled", "Scheduled"


def ambl_games(team_id: str, start: date | None = None, end: date | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    m = re.fullmatch(r"ambl-(\d{4})(spring|fall)-(\d+)", team_id)
    league = league_by_id("ambl")
    if not m or not league:
        raise UnknownTeam(team_id)
    year, season, tnum = int(m.group(1)), m.group(2), m.group(3)
    try:
        idx = ambl_index(league)
        name = next((t["name"] for t in idx["teams"] if t["id"] == tnum), None)
    except SourceError:
        name = None
    team = {
        "team_id": team_id, "name": name or f"AMBL team {tnum}", "league_id": "ambl", "league": league["name"],
        "division": f"{season.title()} {year}", "sport": "baseball", "day": None, "venue_field_id": None,
        "schedule_url": f"{league['site_url']}/schedule.php?season_key={year}%7C{season}&team={tnum}", "platform": "ambl",
    }
    months = AMBL_SEASON_MONTHS[season]
    if start and end:
        wanted = {(d.year, d.month) for d in (start + timedelta(days=i) for i in range((end - start).days + 1))}
        months = [mo for mo in months if (year, mo) in wanted]
    tz = ZoneInfo(league.get("timezone") or "America/Chicago")
    length = timedelta(minutes=int(league.get("game_length_minutes") or 180))
    games = []
    for month in months:
        url = f"{league['site_url']}/schedule.php?season_key={year}%7C{season}&month={month}&team={tnum}"
        page = _cached(f"ambl:sched:{year}{season}:{month}:{tnum}", GAMES_TTL_S,
                       lambda url=url: _http_get(url, accept="text/html").decode("utf-8", "replace"))
        for row in parse_ambl_schedule(page, year, month):
            is_home = normalize_name(row["home"]) == normalize_name(team["name"])
            if name and not is_home and normalize_name(row["visitor"]) != normalize_name(name):
                continue
            begin = datetime(row["year"], row["month"], row["day"], row["hour"], row["minute"], tzinfo=tz)
            status, text = _ambl_status(row)
            games.append({
                "source_game_id": f"ambl-{year}{season}-{row['game_no']}",
                "team": team["name"],
                "opponent": row["visitor"] if is_home else row["home"],
                "home_away": "home" if is_home else "away",
                "start": begin,
                "end": begin + length,
                "end_estimated": True,
                "field_label": row["field"],
                "address": None,
                "venue_field_id": _venue_for_text(league, row["field"]),
                "league_status": status,
                "league_status_text": text,
                "comments": None,
            })
    return team, games


# ------------------------------------------------------------------- generic
def encode_url_team(url: str, team: str) -> str:
    raw = json.dumps({"u": url, "t": team}, separators=(",", ":")).encode("utf-8")
    return "url-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_url_team(team_id: str) -> tuple[str, str]:
    if not team_id.startswith("url-") or len(team_id) > 1600:
        raise UnknownTeam(team_id)
    token = team_id[4:]
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8"))
        url, name = str(data["u"]), str(data["t"])
        validate_url(url)
    except (ValueError, KeyError, TypeError, UnsafeURLError) as exc:
        raise UnknownTeam(team_id) from exc
    return url, name


_VS = re.compile(r"\s+(?:vs\.?|v\.?|versus|@|at)\s+", re.I)


def _split_matchup(title: str | None) -> tuple[str | None, str | None]:
    text = re.sub(r"^\s*(game|match|softball|baseball)\s*[:\-]\s*", "", title or "", flags=re.I)
    parts = _VS.split(text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(" -:") or None, re.split(r"\s+[\-(\[]", parts[1].strip())[0].strip() or None
    return None, None


def _events_from_ics(text: str) -> list[dict[str, Any]]:
    from rainout_agent.schedule import parse_ics

    events = []
    for ev in parse_ics(text):
        home, away = ev.get("home"), ev.get("away")
        if not (home and away):
            a, b = _split_matchup(ev.get("title"))
            # "A at B" means A is visiting B; "A vs B" lists the home team first by convention.
            if a and b:
                if re.search(r"\s(@|at)\s", ev.get("title") or "", re.I):
                    away, home = a, b
                else:
                    home, away = a, b
        events.append({**ev, "home": home, "away": away})
    return events


def _events_from_jsonld(page: str) -> list[dict[str, Any]]:
    from rainout_agent.schedule import _parse_iso

    events = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item:
                    stack.extend(item["@graph"] if isinstance(item["@graph"], list) else [item["@graph"]])
                kind = item.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if any(k in ("SportsEvent", "Event") for k in kinds) and item.get("startDate"):
                    start = _parse_iso(str(item["startDate"]))
                    if not start:
                        continue
                    loc = item.get("location")
                    loc_text = loc.get("name") if isinstance(loc, dict) else (str(loc) if loc else None)
                    if isinstance(loc, dict) and isinstance(loc.get("address"), dict):
                        addr = loc["address"]
                        loc_text = ", ".join(x for x in (loc_text, addr.get("streetAddress"), addr.get("addressLocality")) if x)
                    home = (item.get("homeTeam") or {}).get("name") if isinstance(item.get("homeTeam"), dict) else item.get("homeTeam")
                    away = (item.get("awayTeam") or {}).get("name") if isinstance(item.get("awayTeam"), dict) else item.get("awayTeam")
                    if not (home and away):
                        home, away = _split_matchup(item.get("name"))
                    end = _parse_iso(str(item["endDate"])) if item.get("endDate") else None
                    events.append({"title": item.get("name"), "home": home, "away": away, "start": start, "end": end,
                                   "location": loc_text, "status": str(item.get("eventStatus") or "")})
            elif isinstance(item, list):
                stack.extend(item)
    return events


_HEADER_KEYS = {
    "date": ("date",), "time": ("time", "start"), "home": ("home",), "away": ("away", "visitor", "visiting"),
    "opponent": ("opponent", "vs"), "location": ("field", "location", "venue", "park", "diamond", "site"),
    "matchup": ("game", "matchup", "teams", "event"), "status": ("status", "result", "notes"),
}


def _events_from_tables(page: str, default_year: int) -> list[dict[str, Any]]:
    from rainout_agent.schedule import _parse_iso

    events = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S | re.I):
        rows = [[_strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
                for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)]
        header_idx, cols = None, {}
        for i, row in enumerate(rows[:5]):
            found = {}
            for j, cell in enumerate(row):
                low = cell.strip().lower()
                for key, words in _HEADER_KEYS.items():
                    if key not in found and any(low == w or low.startswith(w) for w in words):
                        found[key] = j
            if "date" in found and ({"home", "away"} <= found.keys() or "opponent" in found or "matchup" in found):
                header_idx, cols = i, found
                break
        if header_idx is None:
            continue
        for row in rows[header_idx + 1:]:
            def cell(key: str) -> str:
                j = cols.get(key)
                return row[j].strip() if j is not None and j < len(row) else ""
            date_text, time_text = cell("date"), cell("time")
            if not date_text:
                continue
            if not re.search(r"\d{4}", date_text):
                date_text = f"{date_text} {default_year}"
            start = _parse_iso(f"{date_text} {time_text}".strip()) or _parse_loose_datetime(date_text, time_text)
            if not start:
                continue
            home, away = cell("home") or None, cell("away") or None
            if not (home and away) and cell("matchup"):
                home, away = _split_matchup(cell("matchup"))
            events.append({"title": cell("matchup") or (f"{away} at {home}" if home and away else None), "home": home,
                           "away": away, "opponent": cell("opponent") or None, "start": start,
                           "location": cell("location") or None, "status": cell("status")})
    return events


def _parse_loose_datetime(date_text: str, time_text: str) -> datetime | None:
    date_text = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s*", "", date_text.strip(), flags=re.I)
    time_text = (time_text or "12:00 PM").strip().upper().replace(".", "")
    if re.fullmatch(r"\d{1,2}(:\d{2})?\s*[AP]M?", time_text):
        time_text = re.sub(r"\s*([AP])M?$", r" \1M", time_text)
        if ":" not in time_text:
            time_text = time_text.replace(" ", ":00 ", 1)
    for dfmt in ("%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        for tfmt in ("%I:%M %p", "%H:%M"):
            try:
                return datetime.strptime(f"{date_text} {time_text}", f"{dfmt} {tfmt}")
            except ValueError:
                continue
    return None


def _llm_extract(text: str, url: str) -> list[dict[str, Any]]:
    """Optional: only when RAINOUT_LLM_API_KEY is set; returns [] on any problem."""
    key = os.environ.get("RAINOUT_LLM_API_KEY")
    if not key:
        return []
    from rainout_agent.schedule import _parse_iso

    base = os.environ.get("RAINOUT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("RAINOUT_LLM_MODEL", "gpt-4o-mini")
    prompt = (
        "Extract every scheduled game from this sports schedule page. Reply with JSON only: "
        '{"games":[{"date":"YYYY-MM-DD","time":"HH:MM (24h)","home":"","away":"","location":""}]}. '
        "Use only facts present in the text; omit anything you are unsure of.\n\nURL: " + url + "\n\n" + text[:20000]
    )
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object"}, "temperature": 0}).encode("utf-8")
    try:
        req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        raw = _llm_transport(req)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        rows = json.loads(content).get("games") or []
    except Exception as exc:
        log.warning("LLM schedule extraction skipped: %s", exc.__class__.__name__)
        return []
    events = []
    for row in rows[:500]:
        if not isinstance(row, dict):
            continue
        start = _parse_iso(f"{row.get('date', '')}T{row.get('time') or '12:00'}")
        if start:
            events.append({"title": f"{row.get('away')} at {row.get('home')}", "home": row.get("home"), "away": row.get("away"),
                           "start": start, "location": row.get("location"), "extraction": "llm"})
    return events


def _llm_transport(req: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read(2_000_000)


def detect_platform(url: str, page: str | None = None) -> dict[str, Any] | None:
    """Recognise known league platforms behind a user-supplied schedule URL."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    query = urllib.parse.parse_qs(parts.query)
    if host.endswith("austinmetrobaseball.com"):
        team = (query.get("team") or [None])[0]
        return {"platform": "ambl", "league": league_by_id("ambl"), "team": team}
    if host == "cms-api.teamsideline.com":
        uid = (query.get("externalUID") or query.get("externalUId") or [None])[0]
        if uid:
            return {"platform": "teamsideline", "league": _tsl_league(uid), "division_id": (query.get("divisionId") or [None])[0]}
    for lg in load_leagues():
        site = (lg.get("site_url") or "").lower()
        if site and urllib.parse.urlsplit(site).netloc.replace("www.", "") == host.replace("www.", ""):
            if lg["platform"] == "teamsideline":
                return {"platform": "teamsideline", "league": lg, "division_id": (query.get("divisionid") or query.get("divisionId") or [None])[0]}
    if page:
        m = re.search(r"ExternalUid:\s*'(\d+)'", page)
        if m and "teamsideline" in page.lower():
            return {"platform": "teamsideline", "league": _tsl_league(m.group(1)), "division_id": (query.get("divisionid") or [None])[0]}
    return None


def generic_events(url: str, fetcher: Callable[[str], Any] = safe_get) -> dict[str, Any]:
    """Fetch a user-supplied schedule URL and extract events deterministically."""
    def produce() -> dict[str, Any]:
        result = fetcher(url)
        text = result.body.decode("utf-8", "replace")
        method = None
        events: list[dict[str, Any]] = []
        if "BEGIN:VCALENDAR" in text[:2000].upper():
            events, method = _events_from_ics(text), "ics"
        else:
            platform = detect_platform(url, text)
            if platform:
                return {"platform": platform["platform"], "league_id": platform["league"]["league_id"] if platform.get("league") else None,
                        "external_uid": (platform.get("league") or {}).get("external_uid"),
                        "division_id": platform.get("division_id"), "events": []}
            links = re.findall(r'href=["\']((?:webcal|https?)://[^"\']+\.ics[^"\']*|[^"\']+\.ics(?:\?[^"\']*)?)["\']', text, re.I)
            for link in links[:2]:
                link_url = urllib.parse.urljoin(url, html_lib.unescape(link))
                try:
                    linked = fetcher(link_url).body.decode("utf-8", "replace")
                    if "BEGIN:VCALENDAR" in linked[:2000].upper():
                        events, method = _events_from_ics(linked), "linked_ics"
                        break
                except (FetchError, UnsafeURLError, Exception):  # noqa: BLE001 - a bad link must not fail the page
                    continue
            if not events:
                events, method = _events_from_jsonld(text), "json_ld"
            if not events:
                events, method = _events_from_tables(text, datetime.now(timezone.utc).year), "html_table"
            if not events:
                plain = _strip_tags(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I))
                events, method = _llm_extract(plain, url), "llm"
        return {"platform": "generic", "method": method if events else None, "events": [_serialise(e) for e in events]}

    return _cached(f"generic:{url}", GAMES_TTL_S, produce)


def _serialise(event: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k in ("title", "home", "away", "opponent", "location", "status", "id", "description", "extraction"):
        if event.get(k):
            out[k] = str(event[k])
    for k in ("start", "end"):
        v = event.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, date):
            out[k] = v.isoformat()
    if event.get("calendar_cancelled"):
        out["cancelled"] = True
    return out


def generic_team_names(events: list[dict[str, Any]]) -> list[str]:
    names: dict[str, str] = {}
    for ev in events:
        for k in ("home", "away", "opponent"):
            if ev.get(k):
                names.setdefault(normalize_name(ev[k]), ev[k].strip())
    return [n for key, n in names.items() if key]


def generic_games(team_id: str, fetcher: Callable[[str], Any] = safe_get) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from rainout_agent.schedule import _parse_iso, match_venue

    url, name = decode_url_team(team_id)
    try:
        data = generic_events(url, fetcher)
    except (FetchError, UnsafeURLError) as exc:
        raise SourceError(str(exc)) from exc
    team = {"team_id": team_id, "name": name, "league_id": None, "league": urllib.parse.urlsplit(url).netloc,
            "division": None, "sport": None, "day": None, "venue_field_id": None, "schedule_url": url, "platform": "generic"}
    events = data.get("events") or []
    named = [e for e in events if any(name_similarity(name, e.get(k)) >= 0.85 for k in ("home", "away"))
             or normalize_name(name) in normalize_name(e.get("title"))]
    single_team_feed = not named and not generic_team_names(events)
    games = []
    for ev in (named if not single_team_feed else events):
        start = _parse_iso(ev["start"]) if ev.get("start") else None
        if not start:
            continue
        end = _parse_iso(ev["end"]) if ev.get("end") else None
        home, away = ev.get("home"), ev.get("away")
        is_home = name_similarity(name, home) >= name_similarity(name, away) if home or away else None
        opponent = ev.get("opponent") or ((away if is_home else home) if (home or away) else None)
        field_id, _ = match_venue(ev.get("location"), ev.get("title"))
        status = "cancelled" if ev.get("cancelled") or re.search(r"cancel|rain(ed)?\s*out|postpon", ev.get("status", ""), re.I) else "scheduled"
        games.append({
            "source_game_id": ev.get("id") or "url-" + hashlib.sha1(f"{url}|{ev.get('start')}|{ev.get('title')}".encode()).hexdigest()[:16],
            "team": name, "opponent": opponent, "home_away": ("home" if is_home else "away") if is_home is not None else None,
            "start": start, "end": end or (start + timedelta(hours=1, minutes=30)), "end_estimated": end is None,
            "field_label": ev.get("location"), "address": None, "venue_field_id": field_id,
            "league_status": status, "league_status_text": ev.get("status") or ("Cancelled" if status == "cancelled" else "Scheduled"),
            "comments": None,
        })
    return team, games

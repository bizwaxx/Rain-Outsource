"""Schedule checking: parse ICS / JSON / CSV schedules, match games to venues,
estimate game-time play probability and flag at-risk games.

Stateless apart from the shared weather cache: forecasts are fetched once per
venue per request (in parallel, with a time budget, through the cached
NWS -> Open-Meteo weather layer) and reused for every game at that venue.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rainout_agent.historical import calibration_for
from rainout_agent.play_probability import UNKNOWN_BASELINE_PERCENT, calculate_play_probability
from rainout_agent.status_api import (
    _load_all_fields,
    _normalize,
    fetch_hourly_forecast,
    fetch_official_rainout_status,
    resolve_field_id,
    weather_from_periods,
)

MAX_INPUT_BYTES = 1_000_000
MAX_INPUT_GAMES = 2000
MAX_EVALUATED_GAMES = 300
MAX_FORECAST_VENUES = 20
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 31
DEFAULT_RISK_THRESHOLD = 60
DEFAULT_BUDGET_S = 8.0
FORECAST_FETCH_TIMEOUT_S = 6.0
OFFICIAL_FETCH_TIMEOUT_S = 4.0
OFFICIAL_POLL_HOURS = 12
PAST_GRACE = timedelta(hours=3)
DEFAULT_GAME_LENGTH = timedelta(hours=2)
CANCELLED_STATUSES = {"cancelled", "canceled", "field closed", "field_closed", "closed"}

STATE_TIMEZONES = {
    "TX": "America/Chicago", "OK": "America/Chicago", "LA": "America/Chicago", "AR": "America/Chicago",
    "FL": "America/New_York", "GA": "America/New_York", "CA": "America/Los_Angeles", "AZ": "America/Phoenix",
}
WINDOWS_TZ = {
    "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "us mountain standard time": "America/Phoenix",
}


class ScheduleInputError(ValueError):
    """The schedule input could not be parsed."""


# --------------------------------------------------------------------- helpers
def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    name = name.strip().strip('"')
    name = WINDOWS_TZ.get(name.lower(), name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def venue_timezone(field: dict[str, Any] | None, default: str | None = None) -> ZoneInfo:
    if field and field.get("timezone"):
        zone = _zone(field["timezone"])
        if zone:
            return zone
    if field and field.get("state") in STATE_TIMEZONES:
        return ZoneInfo(STATE_TIMEZONES[field["state"]])
    return _zone(default) or ZoneInfo("America/Chicago")


def _parse_iso(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------------- ICS
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    # NAME;PARAM=val;PARAM2="v:al":VALUE  (colon inside quotes belongs to params)
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return line.upper(), {}, ""
    parts = head.split(";")
    params = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.upper()] = val.strip('"')
    return parts[0].upper(), params, value


def _parse_ics_datetime(value: str, params: dict[str, str]) -> tuple[datetime | date | None, bool]:
    """Returns (datetime-or-date, is_floating)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value[:8], "%Y%m%d").date(), False
        except ValueError:
            return None, False
    try:
        if value.endswith("Z"):
            return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
        naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    zone = _zone(params.get("TZID"))
    if zone:
        return naive.replace(tzinfo=zone), False
    return naive, True


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Parse VEVENTs from an iCalendar document into game dicts."""
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        raise ScheduleInputError("not an iCalendar document (missing BEGIN:VCALENDAR)")
    games: list[dict[str, Any]] = []
    event: dict[str, Any] | None = None
    depth = 0
    for line in _unfold(text):
        if not line:
            continue
        name, params, value = _split_property(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            event, depth = {"source": "ics"}, 0
            continue
        if event is None:
            continue
        if name == "BEGIN":
            depth += 1  # nested VALARM etc.
            continue
        if name == "END" and value.upper() != "VEVENT":
            depth = max(0, depth - 1)
            continue
        if name == "END":
            if event.get("status", "").upper() != "CANCELLED":
                games.append(event)
            else:
                event["calendar_cancelled"] = True
                games.append(event)
            event = None
            if len(games) > MAX_INPUT_GAMES:
                raise ScheduleInputError(f"too many events (limit {MAX_INPUT_GAMES})")
            continue
        if depth:
            continue
        if name == "UID":
            event["id"] = value.strip()
        elif name == "SUMMARY":
            event["title"] = _unescape(value).strip()
        elif name == "LOCATION":
            event["location"] = _unescape(value).strip()
        elif name == "DESCRIPTION":
            event["description"] = _unescape(value).strip()[:2000]
        elif name == "STATUS":
            event["status"] = value.strip()
        elif name == "RRULE":
            event["recurring"] = True
            event["rrule"] = value.strip()
        elif name == "EXDATE":
            for part in value.split(","):
                parsed, _ = _parse_ics_datetime(part, params)
                if parsed is not None:
                    event.setdefault("exdates", []).append(parsed)
        elif name == "RECURRENCE-ID":
            event["recurrence_id"], _ = _parse_ics_datetime(value, params)
        elif name in {"DTSTART", "DTEND"}:
            parsed, floating = _parse_ics_datetime(value, params)
            key = "start" if name == "DTSTART" else "end"
            event[key] = parsed
            if floating:
                event[f"{key}_floating"] = True
    return games


# ------------------------------------------------------------------- JSON / CSV
VENUE_KEYS = ("venue_id", "field_id", "venue", "field", "location")
START_KEYS = ("start", "game_time", "datetime", "start_time")


def _game_from_mapping(row: dict[str, Any], index: int) -> dict[str, Any]:
    row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    venue = next((str(row[k]).strip() for k in VENUE_KEYS if row.get(k)), "")
    start_raw = next((str(row[k]).strip() for k in START_KEYS if row.get(k)), "")
    if not start_raw and row.get("date"):
        start_raw = f"{str(row['date']).strip()} {str(row.get('time') or '12:00').strip()}"
    start = _parse_iso(start_raw)
    if start is None:
        raise ScheduleInputError(f"game {index}: could not read the date/time {start_raw!r}")
    teams = row.get("teams")
    if isinstance(teams, str):
        teams = [t.strip() for t in re.split(r"\s+(?:vs\.?|v\.?|@|at)\s+|,", teams) if t.strip()]
    home = row.get("home") or row.get("home_team") or (teams[0] if teams else None)
    away = row.get("away") or row.get("away_team") or (teams[1] if teams and len(teams) > 1 else None)
    title = row.get("title") or row.get("name") or (f"{home} vs {away}" if home and away else None)
    end = _parse_iso(str(row.get("end") or "")) if row.get("end") else None
    game = {
        "source": "json",
        "id": str(row.get("id") or row.get("game_id") or "") or None,
        "title": title,
        "home": home,
        "away": away,
        "venue_query": venue or None,
        "start": start,
        "end": end,
    }
    if start.tzinfo is None:
        game["start_floating"] = True
    if row.get("timezone"):
        game["timezone"] = str(row["timezone"])
    return game


def parse_json_games(games: Any) -> list[dict[str, Any]]:
    if not isinstance(games, list):
        raise ScheduleInputError("games must be a list")
    if len(games) > MAX_INPUT_GAMES:
        raise ScheduleInputError(f"too many games (limit {MAX_INPUT_GAMES})")
    out = []
    for i, row in enumerate(games):
        if not isinstance(row, dict):
            raise ScheduleInputError(f"game {i}: must be an object")
        out.append(_game_from_mapping(row, i))
    return out


def parse_csv_games(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ScheduleInputError("CSV needs a header row")
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_INPUT_GAMES:
            raise ScheduleInputError(f"too many games (limit {MAX_INPUT_GAMES})")
        if any((v or "").strip() for v in row.values() if isinstance(v, str)):
            rows.append(_game_from_mapping(row, i))
    for row in rows:
        row["source"] = "csv"
    return rows


# --------------------------------------------------------------- venue matching
_ALIAS_INDEX: list[tuple[str, str]] | None = None
_EXACT: dict[str, str] | None = None


def _exact_map() -> dict[str, str]:
    """Cached exact lookup (same rules as status_api.resolve_field_id)."""
    global _EXACT
    if _EXACT is None:
        exact: dict[str, str] = {}
        for field in _load_all_fields():
            for key in [field["id"], field["field_name"], *field.get("aliases", [])]:
                exact[_normalize(key)] = field["id"]
            exact[field["id"]] = field["id"]
        _EXACT = exact
    return _EXACT


def _alias_index() -> list[tuple[str, str]]:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        pairs = set()
        for field in _load_all_fields():
            for alias in [field["field_name"], *field.get("aliases", [])]:
                norm = _normalize(alias)
                if len(norm) >= 4:
                    pairs.add((norm, field["id"]))
        _ALIAS_INDEX = sorted(pairs, key=lambda p: -len(p[0]))
    return _ALIAS_INDEX


def match_venue(*texts: str | None) -> tuple[str | None, str | None]:
    """Match free text (venue id, name, ICS LOCATION/SUMMARY) to a venue.

    Returns (field_id, method) where method is "exact" or "alias_in_text".
    """
    exact = _exact_map()
    for text in texts:
        if text:
            hit = exact.get(text.strip()) or exact.get(_normalize(text))
            if hit:
                return hit, "exact"
    for text in texts:
        if not text:
            continue
        haystack = " " + re.sub(r"[^a-z0-9&']+", " ", _normalize(text)) + " "
        for alias, field_id in _alias_index():
            needle = " " + re.sub(r"[^a-z0-9&']+", " ", alias).strip() + " "
            if needle.strip() and needle in haystack:
                return field_id, "alias_in_text"
    return None, None


# ------------------------------------------------------------------ evaluation
def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _period_bounds(periods: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    starts = [_parse_iso(p.get("startTime", "")) for p in periods]
    ends = [_parse_iso(p.get("endTime", "")) for p in periods]
    starts = [s for s in starts if s]
    ends = [e for e in ends if e]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _game_uid(game: dict[str, Any], index: int) -> str:
    if game.get("id"):
        return str(game["id"])
    seed = f"{index}|{game.get('title')}|{game.get('start')}|{game.get('venue_query') or game.get('location')}"
    return hashlib.sha1(seed.encode()).hexdigest()[:16] + "@rainout-source"


def _normalise_forecast(result: Any) -> dict[str, Any]:
    """Accept a bare NWS period list or the weather layer's dict."""
    if isinstance(result, dict):
        return {
            "periods": result.get("periods") or [],
            "source": result.get("source") or "National Weather Service API",
            "source_id": result.get("source_id") or "nws",
        }
    return {"periods": list(result or []), "source": "National Weather Service API", "source_id": "nws"}


def check_schedule(
    games: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    risk_threshold: int = DEFAULT_RISK_THRESHOLD,
    window_days: int = DEFAULT_WINDOW_DAYS,
    default_venue: str | None = None,
    default_timezone: str | None = None,
    forecast_provider: Callable[[dict[str, Any], float], list[dict[str, Any]] | dict[str, Any]] | None = None,
    official_provider: Callable[[dict[str, Any], float], dict[str, Any]] | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
) -> dict[str, Any]:
    """Evaluate every game and return the agent-readable schedule report."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    risk_threshold = max(0, min(100, int(risk_threshold)))
    window_days = max(1, min(MAX_WINDOW_DAYS, int(window_days)))
    window_end = now + timedelta(days=window_days)
    forecast_provider = forecast_provider or (lambda field, timeout: fetch_hourly_forecast(field, timeout=timeout))
    official_provider = official_provider or (lambda field, timeout: fetch_official_rainout_status(field, timeout=timeout))

    default_field_id = resolve_field_id(default_venue) if default_venue else None
    if default_venue and not default_field_id:
        raise ScheduleInputError(f"unknown default venue {default_venue!r}")
    fields = {f["id"]: f for f in _load_all_fields()}
    warnings: list[str] = []
    input_count = len(games)
    if any(g.get("rrule") for g in games):
        from rainout_agent.rrule import expand_events

        games, rrule_warnings = expand_events(games, now - PAST_GRACE, window_end)
        warnings += rrule_warnings

    # 1) normalise, match, window
    prepared = []
    skipped_past = skipped_future = 0
    for index, game in enumerate(games):
        field_id, method = match_venue(game.get("venue_query"), game.get("location"), game.get("title"))
        if not field_id and default_field_id:
            field_id, method = default_field_id, "default_venue"
        field = fields.get(field_id) if field_id else None
        zone = _zone(game.get("timezone")) or venue_timezone(field, default_timezone)
        start = game.get("start")
        all_day = isinstance(start, date) and not isinstance(start, datetime)
        if all_day:
            start = datetime.combine(start, datetime.min.time().replace(hour=12), tzinfo=zone)
        elif isinstance(start, datetime) and start.tzinfo is None:
            start = start.replace(tzinfo=zone)
        if not isinstance(start, datetime):
            prepared.append({"index": index, "game": game, "error": "missing_start_time"})
            continue
        if start < now - PAST_GRACE:
            skipped_past += 1
            continue
        if start > window_end:
            skipped_future += 1
            continue
        prepared.append({"index": index, "game": game, "field": field, "match_method": method, "start": start.astimezone(zone), "all_day": all_day})

    if len(prepared) > MAX_EVALUATED_GAMES:
        warnings.append(f"Only the first {MAX_EVALUATED_GAMES} games in the window were evaluated.")
        prepared.sort(key=lambda p: p.get("start") or now)
        prepared = prepared[:MAX_EVALUATED_GAMES]

    # 2) fetch forecasts (once per venue) and near-term official status, in parallel
    forecast_horizon = now + timedelta(days=7, hours=12)
    need_forecast: list[str] = []
    need_official: list[str] = []
    for p in prepared:
        field = p.get("field")
        if not field:
            continue
        if p["start"] <= forecast_horizon and field["id"] not in need_forecast:
            need_forecast.append(field["id"])
        if (
            p["start"] <= now + timedelta(hours=OFFICIAL_POLL_HOURS)
            and field.get("poll_official_status") is not False
            and field.get("official_status_source_url")
            and field["id"] not in need_official
        ):
            need_official.append(field["id"])
    if len(need_forecast) > MAX_FORECAST_VENUES:
        warnings.append(f"Forecasts were fetched for the first {MAX_FORECAST_VENUES} venues only; other games use history only.")
        need_forecast = need_forecast[:MAX_FORECAST_VENUES]

    forecasts: dict[str, dict[str, Any]] = {}
    officials: dict[str, dict[str, Any]] = {}
    remaining = budget_s - (time.monotonic() - started)
    if (need_forecast or need_official) and remaining > 0.5:
        pool = ThreadPoolExecutor(max_workers=8)
        jobs = {}
        for fid in need_forecast:
            jobs[pool.submit(forecast_provider, fields[fid], min(FORECAST_FETCH_TIMEOUT_S, remaining))] = ("forecast", fid)
        for fid in need_official:
            jobs[pool.submit(official_provider, fields[fid], min(OFFICIAL_FETCH_TIMEOUT_S, remaining))] = ("official", fid)
        done, not_done = wait(jobs, timeout=remaining)
        for future in done:
            kind, fid = jobs[future]
            try:
                result = future.result()
            except Exception:
                warnings.append(f"Could not fetch the {'forecast' if kind == 'forecast' else 'official status'} for {fid}.")
                continue
            if kind == "forecast":
                forecasts[fid] = _normalise_forecast(result)
            else:
                officials[fid] = result
        for future in not_done:
            kind, fid = jobs[future]
            warnings.append(f"Timed out fetching the {'forecast' if kind == 'forecast' else 'official status'} for {fid}.")
        pool.shutdown(wait=False, cancel_futures=True)

    # 3) evaluate each game
    results = []
    for p in prepared:
        game = p["game"]
        entry: dict[str, Any] = {
            "index": p["index"],
            "uid": _game_uid(game, p["index"]),
            "title": game.get("title"),
            "home": game.get("home"),
            "away": game.get("away"),
            "location_text": game.get("location") or game.get("venue_query"),
        }
        if p.get("error"):
            entry.update({"status": "invalid", "error": p["error"], "at_risk": False})
            results.append(entry)
            continue
        start, field = p["start"], p.get("field")
        entry["start"] = _iso(start)
        if game.get("end") and isinstance(game["end"], datetime):
            end = game["end"] if game["end"].tzinfo else game["end"].replace(tzinfo=start.tzinfo)
            entry["end"] = _iso(end.astimezone(start.tzinfo))
        if p.get("all_day"):
            entry["all_day"] = True
        if game.get("recurrence"):
            entry["recurrence"] = game["recurrence"]
            entry["note"] = "Occurrence of a repeating calendar event."
        elif game.get("recurring"):
            entry["note"] = "Repeating calendar event with a rule Rainout Source can't expand: only the first occurrence is evaluated."
        if not field:
            entry.update({"status": "unmatched_venue", "venue": None, "at_risk": False,
                          "recommendation": "Venue not recognised. Pass venue=<field id or alias> or check /v1/fields."})
            results.append(entry)
            continue
        entry["venue"] = {
            "field_id": field["id"], "name": field["field_name"], "city": field["city"], "state": field["state"],
            "match_method": p["match_method"],
        }
        entry["rainout_phone"] = field.get("rainout_phone")
        entry["official_status_source_url"] = field.get("official_status_source_url")
        entry["status_url"] = (
            f"https://rainout-agent-source.vercel.app/v1/status?field_id={field['id']}&game_time={start.isoformat()}"
        )
        if game.get("calendar_cancelled"):
            entry.update({"status": "evaluated", "basis": "calendar", "official_status": "cancelled",
                          "play_probability_percent": 0, "risk_level": "officially cancelled", "at_risk": True,
                          "risk_reasons": ["calendar event is marked CANCELLED"], "recommendation": "do not drive"})
            results.append(entry)
            continue

        calibration = calibration_for(field["id"], start)
        official = officials.get(field["id"])
        official_status = (official or {}).get("official_status") or "unknown"
        forecast_info = forecasts.get(field["id"]) or {}
        periods = forecast_info.get("periods")
        in_range = False
        if periods:
            first, last = _period_bounds(periods)
            in_range = bool(first and last and first - timedelta(hours=1) <= start <= last)
        hours_until = max(0.0, (start - now).total_seconds() / 3600)
        reasons: list[str] = []

        if in_range:
            weather = weather_from_periods(periods, start, source=forecast_info["source"])
            fallback = forecast_info["source_id"] == "open_meteo"
            # Same model as /v1/status: unknown status starts from the 96% baseline
            # and rain/storms bring it down; delayed/conflicting is capped at 50%.
            probability = calculate_play_probability(
                official_status=official_status,
                precipitation_chance_percent=weather["rain_chance_percent"],
                thunderstorm_likely=weather["thunderstorm_likely"],
                hours_until_game=hours_until,
                historical_calibration=calibration,
            )
            entry.update({
                "status": "evaluated",
                "basis": ("open_meteo_forecast" if fallback else "nws_forecast") + ("+history" if calibration else ""),
                "weather_source": forecast_info["source"],
                "weather_source_id": forecast_info["source_id"],
                "official_status": official_status,
                "official_status_checked": bool(official and official.get("checked")),
                "assumes_game_scheduled": official_status not in CANCELLED_STATUSES | {"on", "playing", "open"},
                "rain_chance_percent": weather["rain_chance_percent"],
                "thunderstorm_likely": weather["thunderstorm_likely"],
                "forecast": weather.get("forecast"),
                "forecast_period_start": weather.get("forecast_period_start"),
                "play_probability_percent": probability["play_probability_percent"],
                "risk_level": probability["risk_level"],
                "recommendation": probability["recommendation"],
                "reason": probability["reason"],
            })
            if official_status in CANCELLED_STATUSES:
                reasons.append(f"official status is {official_status}")
            if weather["thunderstorm_likely"]:
                reasons.append("thunderstorms likely")
            if weather["rain_chance_percent"] >= 60:
                reasons.append(f"rain chance {weather['rain_chance_percent']}%")
        else:
            frac = (calibration or {}).get("rainout_day_frac")
            play = None if frac is None else int(round(UNKNOWN_BASELINE_PERCENT * (1 - float(frac))))
            entry.update({
                "status": "evaluated" if play is not None else "no_forecast",
                "basis": "history_only" if play is not None else "none",
                "official_status": official_status,
                "official_status_checked": bool(official and official.get("checked")),
                "rain_chance_percent": None,
                "thunderstorm_likely": None,
                "play_probability_percent": play,
                "risk_level": "outlook",
                "recommendation": "Forecast not available yet (NWS covers about 7 days). Check again closer to game day.",
                "reason": (
                    f"Historically about {round(float(frac) * 100)}% of days in this month reach rainout-level rain near this venue."
                    if frac is not None else "No forecast or history available for this game."
                ),
            })
        play = entry.get("play_probability_percent")
        if play is not None and play < risk_threshold:
            reasons.append(f"play probability {play}% is below {risk_threshold}%")
        entry["at_risk"] = bool(reasons)
        entry["risk_reasons"] = reasons
        if calibration:
            entry["historical_calibration"] = {
                "station_id": calibration.get("station_id"),
                "station_name": calibration.get("station_name"),
                "month": calibration.get("month"),
                "rainy_day_share_reaching_threshold": calibration.get("cancel_given_wet"),
                "rainout_level_day_frequency": calibration.get("rainout_day_frac"),
            }
        results.append(entry)

    far_future = datetime.max.replace(tzinfo=timezone.utc)
    results.sort(key=lambda r: (datetime.fromisoformat(r["start"]) if r.get("start") else far_future, r["index"]))
    evaluated = [r for r in results if r.get("status") == "evaluated"]
    return {
        "service": "rainout-source",
        "creator": "JEEZ Labs",
        "generated_at": now.isoformat(),
        "risk_threshold_percent": risk_threshold,
        "window": {"start": (now - PAST_GRACE).isoformat(), "end": window_end.isoformat(), "days": window_days},
        "summary": {
            "games_in_input": input_count,
            "repeating_occurrences": sum(1 for g in games if g.get("recurrence")),
            "games_in_window": len(prepared),
            "evaluated": len(evaluated),
            "at_risk": sum(1 for r in results if r.get("at_risk")),
            "unmatched_venue": sum(1 for r in results if r.get("status") == "unmatched_venue"),
            "with_nws_forecast": sum(1 for r in evaluated if str(r.get("basis", "")).startswith("nws_forecast")),
            "with_fallback_forecast": sum(1 for r in evaluated if str(r.get("basis", "")).startswith("open_meteo_forecast")),
            "history_only": sum(1 for r in evaluated if r.get("basis") == "history_only"),
            "skipped_past": skipped_past,
            "skipped_beyond_window": skipped_future,
        },
        "games": results,
        "warnings": warnings,
        "assumptions": [
            "Play probability uses the same model as /v1/status. When no official status is posted, it starts from a 96% baseline (normal no-shows and venue issues) and rain and storms lower it. Cancelled is 0%; delayed or conflicting signals are capped at 50%. Official status is polled only for games in the next 12 hours.",
            "Games beyond the NWS forecast range (about 7 days) get a history-only outlook from NOAA records.",
            "Official sources win. Check the rainout line or official page before leaving.",
        ],
    }


# ------------------------------------------------------------------ ICS output
def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def _ics_utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_risk_ics(report: dict[str, Any]) -> str:
    """Render the checked games as an iCalendar feed with risk annotations."""
    stamp = datetime.fromisoformat(report["generated_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//JEEZ Labs//Rainout Source//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Rainout Source risk",
    ]
    for game in report["games"]:
        if not game.get("start"):
            continue
        play = game.get("play_probability_percent")
        title = game.get("title") or "Game"
        if game.get("status") == "unmatched_venue":
            tag = "[venue unknown]"
        elif game.get("at_risk"):
            tag = f"[RAIN RISK: {play}% play]" if play is not None else "[RAIN RISK]"
        elif play is not None:
            tag = f"[{play}% play]" + (" (outlook)" if game.get("basis") == "history_only" else "")
        else:
            tag = "[no forecast yet]"
        end = game.get("end") or (datetime.fromisoformat(game["start"]) + DEFAULT_GAME_LENGTH).isoformat()
        desc = [f"Rainout Source: {game.get('recommendation') or ''}".strip()]
        if game.get("reason"):
            desc.append(game["reason"])
        if game.get("risk_reasons"):
            desc.append("Risk: " + "; ".join(game["risk_reasons"]))
        if game.get("rain_chance_percent") is not None:
            desc.append(f"Rain chance {game['rain_chance_percent']}%" + (", storms likely" if game.get("thunderstorm_likely") else ""))
        phone = game.get("rainout_phone")
        if phone and phone != "not_published":
            desc.append(f"Rainout line: {phone}")
        if game.get("official_status_source_url"):
            desc.append(f"Official source: {game['official_status_source_url']}")
        desc.append(f"Checked {report['generated_at']}. Official sources win.")
        venue = game.get("venue") or {}
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_ics_escape(str(game['uid']))}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_utc(game['start'])}",
            f"DTEND:{_ics_utc(end)}",
            f"SUMMARY:{_ics_escape(tag + ' ' + title)}",
            f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}",
        ]
        location = venue.get("name") or game.get("location_text")
        if location:
            lines.append(f"LOCATION:{_ics_escape(location)}")
        if game.get("at_risk"):
            lines.append("CATEGORIES:RAIN RISK")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"

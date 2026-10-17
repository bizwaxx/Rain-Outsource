from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from rainout_agent import weather as _weather
from rainout_agent.agent_response import build_agent_status_response
from rainout_agent.historical import calibration_for
from rainout_agent.play_probability import calculate_play_probability
from rainout_agent.venue_schema import SPORTS, STATES, sports_for

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _normalize(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("-", " ").split())


def _load_all_fields() -> list[dict[str, Any]]:
    fields = []
    for path in sorted(DATA_DIR.glob("**/*.json")):
        # Historical cancellation records use a different schema (field_id +
        # historical_cancellations) and are not field definitions. Skip them so
        # the field loader only sees genuine field-definition documents.
        parts = path.relative_to(DATA_DIR).parts
        if "historical" in parts or parts[0] in ("osm", "noaa", "motocross", "leagues"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        # Defensive: only include objects that are actual field definitions.
        if isinstance(data, dict) and "id" in data:
            fields.append(data)
    return fields


def _alias_map() -> dict[str, str]:
    aliases = {}
    for field in _load_all_fields():
        field_id = field["id"]
        aliases[_normalize(field_id)] = field_id
        aliases[field_id] = field_id
        aliases[_normalize(field["field_name"])] = field_id
        for alias in field.get("aliases", []):
            aliases[_normalize(alias)] = field_id
    return aliases


def resolve_field_id(field_query: str) -> str | None:
    """Resolve a field name or alias (or an OSM venue id) to a canonical Rainout Source field ID."""
    from rainout_agent import osm_venues

    if osm_venues.is_osm_id(field_query):
        v = osm_venues.get(field_query)
        return v["id"] if v else None
    return _alias_map().get(field_query) or _alias_map().get(_normalize(field_query))


def load_field(field_id: str) -> dict[str, Any]:
    from rainout_agent import osm_venues

    if osm_venues.is_osm_id(field_id):
        v = osm_venues.get(field_id)
        if v:
            return osm_venues.to_field(v)
        raise ValueError(f"Unsupported field_id: {field_id}")
    for field in _load_all_fields():
        if field["id"] == field_id:
            return field
    raise ValueError(f"Unsupported field_id: {field_id}")


SPORT_FILTER_ALIASES = {"baseball_softball": ["baseball", "softball"], "mx": ["motocross"], "football": ["soccer"]}


def normalize_sport_filter(value: str | None) -> list[str] | None:
    """Turn a ?sport= value into canonical sports. Returns None when no filter, [] when invalid."""
    if value is None or not str(value).strip():
        return None
    wanted: list[str] = []
    for part in str(value).lower().replace("/", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part in SPORTS:
            wanted.append(part)
        elif part in SPORT_FILTER_ALIASES:
            wanted.extend(SPORT_FILTER_ALIASES[part])
        else:
            return []
    return wanted or None


def normalize_state_filter(value: str | None) -> list[str] | None:
    """Turn a ?state= value (e.g. "TX" or "tx,ok") into codes. None when no filter, [] when invalid."""
    if value is None or not str(value).strip():
        return None
    states = [part.strip().upper() for part in str(value).split(",") if part.strip()]
    if not states or any(state not in STATES for state in states):
        return []
    return states


def list_supported_fields(sport: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    """Return agent-readable metadata for every supported field.

    ``sport`` and ``state`` optionally filter the list (comma-separated values
    allowed). Invalid filter values raise ``ValueError``.
    """
    sport_filter = normalize_sport_filter(sport)
    state_filter = normalize_state_filter(state)
    if sport_filter == []:
        raise ValueError(f"invalid sport filter: {sport!r}")
    if state_filter == []:
        raise ValueError(f"invalid state filter: {state!r}")
    supported = []
    for field in _load_all_fields():
        field_id = field["id"]
        field_sports = sports_for(field)
        if sport_filter and not set(sport_filter) & set(field_sports):
            continue
        if state_filter and field.get("state") not in state_filter:
            continue
        aliases = field.get("aliases", [field["field_name"], field_id])
        supported.append(
            {
                "field_id": field_id,
                "name": field["field_name"],
                "city": field["city"],
                "state": field["state"],
                "address": field["address"],
                "sport": field["sport"],
                "sports": field_sports,
                "venue_type": field.get("venue_type"),
                "coordinates": field.get("coordinates"),
                "operator": field.get("operator"),
                "surface": field.get("surface", "unknown"),
                "drainage": field.get("drainage", "unknown"),
                "rainout_phone": field["rainout_phone"],
                "ownership_type": field.get("ownership_type", "unknown"),
                "status_source_type": field.get("status_source_type", "official_source"),
                "aliases": aliases,
                "weather_source": field["weather_source"],
                "official_status_source_url": field.get("official_status_source_url"),
                "official_status_source_name": field.get("official_status_source_name"),
                "source_reliability": field.get("source_reliability", "medium"),
                "verification_level": "confirmed",
                "status_url": f"https://rainout-agent-source.vercel.app/v1/status?field_id={aliases[0]}&game_time={{ISO-8601-game-time}}",
            }
        )
    return supported


MAX_FIELDS_LIMIT = 500
VERIFICATION_LEVELS = ("confirmed", "osm_only", "all")


def _parse_near(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    try:
        lat, lon = (float(x) for x in str(value).split(","))
    except ValueError as exc:
        raise ValueError("near must be lat,lon (e.g. 30.27,-97.74)") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("near is out of range")
    return lat, lon


def fields_payload(sport: str | None = None, state: str | None = None, q: str | None = None, near: str | None = None,
                   radius_km: str | float | None = None, verification: str | None = None,
                   limit: str | int | None = None, offset: str | int | None = None) -> dict[str, Any]:
    """/v1/fields: hand-verified venues ("confirmed") plus OpenStreetMap venues ("osm_only").

    Without q/near the default is confirmed only (the original list). OSM venues need a state,
    q or near so responses stay small; results are paginated with limit/offset.
    """
    from rainout_agent import osm_venues

    sport_filter = normalize_sport_filter(sport)
    state_filter = normalize_state_filter(state)
    if sport_filter == []:
        raise ValueError(f"invalid sport filter: {sport!r}")
    if state_filter == []:
        raise ValueError(f"invalid state filter: {state!r}")
    q = (q or "").strip() or None
    if q and len(q) > 120:
        raise ValueError("q is too long")
    point = _parse_near(near)
    try:
        radius = float(radius_km) if radius_km not in (None, "") else 25.0
        lim = int(limit) if limit not in (None, "") else (100 if not (q or point) else 25)
        off = int(offset) if offset not in (None, "") else 0
    except ValueError as exc:
        raise ValueError("radius_km, limit and offset must be numbers") from exc
    if not 0 < radius <= 200:
        raise ValueError("radius_km must be 1-200")
    if not 1 <= lim <= MAX_FIELDS_LIMIT or off < 0:
        raise ValueError(f"limit must be 1-{MAX_FIELDS_LIMIT} and offset >= 0")
    level = (verification or ("all" if (q or point) else "confirmed")).strip().lower()
    if level not in VERIFICATION_LEVELS:
        raise ValueError("verification must be confirmed, osm_only or all")
    if level != "confirmed" and not (q or point or state_filter):
        raise ValueError("OSM venues need state, q or near (there are tens of thousands)")

    items: list[tuple[float, float, dict[str, Any]]] = []
    if level in ("confirmed", "all"):
        for f in list_supported_fields(sport=sport, state=state):
            score, dist = 1.0, None
            if q:
                score = max(osm_venues.name_score({"name": n, "city": f["city"]}, q) for n in [f["name"], *f.get("aliases", [])])
                if score < 0.6:
                    continue
            if point and f.get("coordinates"):
                dist = osm_venues._dist_km(point[0], point[1], float(f["coordinates"]["lat"]), float(f["coordinates"]["lon"]))
                if dist > radius:
                    continue
                f = {**f, "distance_km": round(dist, 1)}
            elif point:
                continue
            items.append((score + 0.05, dist or 0, f))  # confirmed wins ties
    osm_total = 0
    if level in ("osm_only", "all"):
        for score, v in osm_venues.search(q, states=state_filter, sports=sport_filter, near=point, radius_km=radius):
            dist = osm_venues._dist_km(point[0], point[1], v["lat"], v["lon"]) if point else None
            items.append((score, dist or 0, osm_venues.public(v, score if q else None, dist)))
            osm_total += 1
    if q or point:
        items.sort(key=lambda x: (-x[0], x[1]))
    page = [f for _, _, f in items[off:off + lim]]
    payload: dict[str, Any] = {
        "service": "rainout-source", "creator": "JEEZ Labs", "count": len(page), "total": len(items),
        "offset": off, "limit": lim, "verification": level,
        "counts": {"confirmed": len(items) - osm_total, "osm_only": osm_total},
        "fields": page,
    }
    if off + lim < len(items):
        payload["next_offset"] = off + lim
    if level != "confirmed":
        payload["attribution"] = osm_venues.ATTRIBUTION
        payload["verification_levels"] = {
            "confirmed": "Hand-verified venue with an official rainout source or phone where one exists.",
            "osm_only": "From OpenStreetMap only; name and location are community data and no official status source is configured.",
        }
    filters = {k: v for k, v in (("sport", sport), ("state", state), ("q", q), ("near", near)) if v}
    if filters:
        payload["filters"] = filters
    return payload


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _display_game_time(value: str) -> str:
    parsed = _parse_time(value)
    if not parsed:
        return value
    hour = parsed.hour % 12 or 12
    minute = f":{parsed.minute:02d}" if parsed.minute else ""
    am_pm = "AM" if parsed.hour < 12 else "PM"
    return f"{hour}{minute} {am_pm}"


def fetch_official_rainout_status(field: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
    """Safely check the official status page without guessing.

    The first public Austin source is a general athletics page, not a structured
    rainout feed. This poller only promotes clear closure/cancellation/delay
    language; otherwise it returns unknown with provenance so agents know the
    source was checked.
    """
    source_url = field.get("official_status_source_url")
    source_name = field.get("official_status_source_name")
    result = {
        "official_status": "unknown",
        "source_url": source_url,
        "source_name": source_name,
        "checked": False,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    if not source_url:
        return result
    if field.get("poll_official_status") is False:
        result["polling_skipped"] = "not_a_status_feed"
        return result

    is_ambl = "austinmetrobaseball.com/rainout.php" in (source_url or "")

    headers = {"User-Agent": "Rainout Source by JEEZ Labs (public pilot)"}
    request = urllib.request.Request(source_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except Exception:
        result["error"] = "official_source_unavailable"
        return result

    if is_ambl:
        result["official_status"] = _parse_ambl_rainout_status(raw_text)
    else:
        result["official_status"] = parse_official_rainout_status(raw_text)
    result["checked"] = True
    return result


def _parse_ambl_rainout_status(raw_text: str) -> str:
    """Conservative parser for AMBL rainout.php - only explicit signals."""
    text = (raw_text or "").lower()
    if re.search(r"\b(rainout|postponed)\b", text) or re.search(r"\b(777|999)\b", raw_text or ""):
        return "cancelled"
    return "unknown"


def parse_official_rainout_status(source_text: str) -> str:
    """Parse clear official rainout signals from source text conservatively."""
    raw_text = source_text or ""
    raw_lower = raw_text.lower()

    # SportsConnect/Blue Sombrero field-status widgets use CSS classes such as
    # skOpen and skClose instead of plain text labels. Promote those only when
    # the page has a clear one-sided signal.
    open_count = len(re.findall(r"\bskopen\b", raw_lower))
    close_count = len(re.findall(r"\bskclose\b", raw_lower))
    if close_count and close_count > open_count:
        return "field_closed"
    if open_count and not close_count:
        return "on"

    text = unescape(re.sub(r"<[^>]+>", " ", raw_text)).lower()
    text = " ".join(text.split())
    if not text:
        return "unknown"

    # Some private complexes publish sport-specific lines like
    # "Baseball: Field 1 Open". Prefer those over generic page words like
    # "closure" that may only explain weather policy.
    sport_status = re.search(r"\b(baseball|softball)\b.{0,200}\b(open|closed|cancelled|canceled|delayed|postponed)\b", text)
    if sport_status:
        status_word = sport_status.group(2)
        if status_word == "open":
            return "on"
        if status_word in {"closed", "cancelled", "canceled"}:
            return "field_closed" if status_word == "closed" else "cancelled"
        return "delayed"

    if re.search(r"\b(cancelled|canceled|cancellations?|rain(?:ed)? out|rainedout)\b", text):
        return "cancelled"
    if re.search(r"\b(fields?|diamonds?)\b.{0,40}\b(closed|closure)\b|\b(closed|closure)\b.{0,40}\b(fields?|diamonds?)\b", text):
        return "field_closed"
    if re.search(r"\b(delayed?|postponed|lightning delay)\b", text):
        return "delayed"
    return "unknown"


def _has_published_rainout_phone(field: dict[str, Any]) -> bool:
    phone = (field.get("rainout_phone") or "").strip().lower()
    return bool(phone and phone not in {"not_published", "unknown", "none", "n/a"})


def _unknown_status_instruction(field: dict[str, Any]) -> str:
    if field.get("verification_level") == "osm_only":
        return ("check with your league or the venue before leaving; this venue comes from OpenStreetMap "
                "and has no official rainout source on file")
    if _has_published_rainout_phone(field):
        return f"call {field['rainout_phone']} before leaving"
    return "check the official source before leaving"


def _answer_requirements(field: dict[str, Any]) -> dict[str, Any]:
    return {
        "must_include": [
            "field name",
            "official status",
            "rain chance",
            "play probability",
            "rainout phone or official source",
        ],
        "must_not_do": [
            "do not guess official status",
            "do not omit the rainout phone when one is published",
            "do not use the word rainfall in Dad-facing answers",
        ],
        "if_official_status_unknown": f"Say official status is unknown and tell the user to {_unknown_status_instruction(field)}.",
    }


NWS_HEADERS = _weather.NWS_HEADERS


def fetch_nws_hourly_periods(field: dict[str, Any], timeout: float = 20) -> list[dict[str, Any]]:
    """Hourly forecast periods (about 7 days) for a field.

    Goes through the cached weather layer, so this may return cached NWS data or
    Open-Meteo periods when NWS is down. Use ``fetch_hourly_forecast`` to learn
    which source answered.
    """
    return fetch_hourly_forecast(field, timeout=timeout)["periods"]


def fetch_hourly_forecast(field: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    """Cached forecast with source marking: {periods, source, source_id, cache, fetched_at}."""
    return _weather.get_hourly_forecast(field, timeout=timeout)


def weather_from_periods(
    periods: list[dict[str, Any]],
    game_time: str | datetime | None,
    source: str = "National Weather Service API",
) -> dict[str, Any]:
    """Pick the forecast period closest to the game time and summarise it."""
    target = game_time if isinstance(game_time, datetime) else _parse_time(game_time)
    selected = periods[0]
    if target:
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        selected = min(
            periods,
            key=lambda period: abs(
                (_parse_time(period.get("startTime")) or target) - target
            ).total_seconds(),
        )

    precip = (selected.get("probabilityOfPrecipitation") or {}).get("value")
    if precip is None:
        precip = 0
    short_forecast = selected.get("shortForecast", "") or ""
    detailed_forecast = selected.get("detailedForecast", "") or ""
    storm_text = f"{short_forecast} {detailed_forecast}".lower()

    return {
        "rain_chance_percent": int(max(0, min(100, round(float(precip))))),
        "thunderstorm_likely": "thunder" in storm_text,
        "forecast": short_forecast,
        "source": source,
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "forecast_period_start": selected.get("startTime"),
    }


def fetch_nws_weather(field: dict[str, Any], game_time: str) -> dict[str, Any]:
    """Forecast for the game hour: NWS first (cached), Open-Meteo if NWS fails.

    We choose the hourly period closest to the requested game time. The result
    names the source that answered (``source`` / ``source_id``) and the cache state.
    """
    forecast = fetch_hourly_forecast(field)
    weather = weather_from_periods(forecast["periods"], game_time, source=forecast["source"])
    weather["source_id"] = forecast["source_id"]
    weather["cache"] = forecast["cache"]
    weather["last_checked"] = forecast["fetched_at"]
    return weather


def build_status_result(
    field_query: str,
    game_time: str,
    weather: dict[str, Any] | None = None,
    official_status: str | None = None,
) -> dict[str, Any]:
    """Build the live API JSON result for one field and game time."""
    field_id = resolve_field_id(field_query)
    if not field_id:
        supported_names = ", ".join(field["name"] for field in list_supported_fields())
        return {
            "creator": "JEEZ Labs",
            "error": "unknown_field",
            "supported_fields_url": "https://rainout-agent-source.vercel.app/v1/fields",
            "search_url": "https://rainout-agent-source.vercel.app/v1/fields?q={venue name}&state={ST}",
            "next_step": "Search /v1/fields?q=<name>&state=<ST> (includes OpenStreetMap venues) and retry with its field_id.",
            "spoken_answer": f"I do not know that field yet. Right now Rain-Outsource supports: {supported_names}.",
        }

    field = load_field(field_id)
    official_data = {
        "official_status": official_status or "unknown",
        "source_url": field.get("official_status_source_url"),
        "source_name": field.get("official_status_source_name"),
        "checked": False,
        "last_checked": None,
    }
    if official_status is None:
        official_data = fetch_official_rainout_status(field)
    official_status_value = official_data.get("official_status") or "unknown"

    weather_data = weather or fetch_nws_weather(field, game_time)
    calibration = calibration_for(field_id, _parse_time(game_time))
    probability = calculate_play_probability(
        official_status=official_status_value,
        precipitation_chance_percent=weather_data["rain_chance_percent"],
        thunderstorm_likely=bool(weather_data["thunderstorm_likely"]),
        hours_until_game=_hours_until_game(game_time),
        historical_calibration=calibration,
    )

    response = build_agent_status_response(
        field_name=field["field_name"],
        game_time=_display_game_time(game_time),
        official_status=official_status_value,
        rain_chance_percent=weather_data["rain_chance_percent"],
        thunderstorm_likely=bool(weather_data["thunderstorm_likely"]),
        play_probability_percent=probability["play_probability_percent"],
        rainout_phone=field["rainout_phone"],
        weather_source=_weather_label(weather_data),
    )
    response.update(
        {
            "field_id": field_id,
            "address": field["address"],
            "official_status_source_url": official_data.get("source_url"),
            "official_status_source_name": official_data.get("source_name"),
            "official_status_checked": bool(official_data.get("checked")),
            "official_status_last_checked": official_data.get("last_checked"),
            "source_reliability": _calculate_source_reliability(official_data),
            "recommendation": probability["recommendation"],
            "risk_level": probability["risk_level"],
            "reason": probability["reason"],
            "weather_source": weather_data["source"],
            "weather_source_id": weather_data.get("source_id", "nws"),
            "weather_cache": weather_data.get("cache"),
            "answer_requirements": _answer_requirements(field),
            "last_checked": weather_data.get("last_checked"),
            "forecast": weather_data.get("forecast"),
            "forecast_period_start": weather_data.get("forecast_period_start"),
            "historical_calibration": _public_calibration(calibration, probability),
            "verification_level": field.get("verification_level", "confirmed"),
        }
    )
    if field.get("verification_level") == "osm_only":
        response["venue"] = {"name": field["field_name"], "city": field.get("city"), "state": field.get("state"),
                             "sports": field.get("sports"), "venue_type": field.get("venue_type"),
                             "coordinates": field.get("coordinates"), "osm_elements": field["osm"]["elements"]}
        response["verification_note"] = ("OpenStreetMap venue: name and location are community map data and no official "
                                         "rainout source is configured, so the odds are weather and NOAA history only.")
        response["attribution"] = field["osm"]["attribution"]

    if response["official_status"] == "unknown":
        storm_phrase = " with storms possible" if response["thunderstorm_likely"] else ""
        source_instruction = _unknown_status_instruction(field)
        response["spoken_answer"] = (
            f"Official rainout status is unknown for {field['field_name']}. "
            f"At game time, {_display_game_time(game_time)}, rain chance is {response['rain_chance_percent']}%{storm_phrase}. "
            f"Estimated play probability is {response['game_time_play_probability_percent']}%. "
            f"Please {source_instruction}."
        )
    return response


def _weather_label(weather_data: dict[str, Any]) -> str:
    if weather_data.get("source_id") == "open_meteo":
        return "Open-Meteo (National Weather Service unavailable)"
    return "National Weather Service"


def _public_calibration(calibration: dict[str, Any] | None, probability: dict[str, Any]) -> dict[str, Any] | None:
    """Additive response block describing the NOAA history behind the estimate."""
    if not calibration:
        return None
    return {
        "source": calibration.get("source"),
        "station_id": calibration.get("station_id"),
        "station_name": calibration.get("station_name"),
        "station_distance_km": calibration.get("station_distance_km"),
        "period": calibration.get("period"),
        "month": calibration.get("month"),
        "rain_to_cancel_threshold_in": calibration.get("rain_to_cancel_threshold_in"),
        "rainy_day_share_reaching_threshold": calibration.get("cancel_given_wet"),
        "rainout_level_day_frequency": calibration.get("rainout_day_frac"),
        "rain_severity_applied": probability.get("rain_severity"),
    }


def _hours_until_game(game_time: str) -> float:
    parsed = _parse_time(game_time)
    if not parsed:
        return 0.0
    now = datetime.now(parsed.tzinfo or timezone.utc)
    return max(0.0, (parsed - now).total_seconds() / 3600)

def _calculate_source_reliability(official_data: dict[str, Any]) -> str:
    if not official_data.get("source_url"):
        return "low"
    if official_data.get("polling_skipped"):
        return "low"
    if official_data.get("checked") and official_data.get("official_status") != "unknown":
        return "high"
    if official_data.get("checked"):
        return "medium"
    return "unknown"

def search_fields(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search fields by name, city, or alias. Returns lightweight results for agents."""
    if not query or not query.strip():
        return []

    q = _normalize(query)
    results = []
    seen = set()

    for field in _load_all_fields():
        field_id = field["id"]
        if field_id in seen:
            continue

        name = field.get("field_name", "")
        city = field.get("city", "")
        aliases = field.get("aliases", [])

        haystack = " ".join([name, city] + aliases + [field_id]).lower()

        if q in haystack or any(q in _normalize(a) for a in aliases):
            results.append(
                {
                    "field_id": field_id,
                    "name": name,
                    "city": city,
                    "state": field.get("state"),
                    "sport": field.get("sport"),
                    "official_status_source_url": field.get("official_status_source_url"),
                    "status_url": f"https://rainout-agent-source.vercel.app/v1/status?field_id={field_id}&game_time={{ISO-8601-game-time}}",
                }
            )
            seen.add(field_id)

        if len(results) >= limit:
            break

    return results

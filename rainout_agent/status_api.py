from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rainout_agent.agent_response import build_agent_status_response
from rainout_agent.play_probability import calculate_play_probability

FIELD_ID = "austin-tx-krieg-field-softball-complex"
DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "austin" / "krieg-field-softball-complex.json"

ALIASES = {
    FIELD_ID: FIELD_ID,
    "krieg": FIELD_ID,
    "krieg field": FIELD_ID,
    "krieg fields": FIELD_ID,
    "krieg field softball complex": FIELD_ID,
    "krieg softball": FIELD_ID,
    "austin krieg": FIELD_ID,
}


def _normalize(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("-", " ").split())


def resolve_field_id(field_query: str) -> str | None:
    """Resolve a field name or alias to a canonical Rainout Source field ID."""
    if field_query == FIELD_ID:
        return FIELD_ID
    return ALIASES.get(_normalize(field_query))


def load_field(field_id: str) -> dict[str, Any]:
    if field_id != FIELD_ID:
        raise ValueError(f"Unsupported field_id: {field_id}")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


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


def fetch_nws_weather(field: dict[str, Any], game_time: str) -> dict[str, Any]:
    """Fetch real forecast data from the National Weather Service API.

    The NWS hourly forecast contains precipitation probability and short forecast
    text. We choose the forecast period closest to the requested game time.
    """
    lat = field["coordinates"]["lat"]
    lon = field["coordinates"]["lon"]
    headers = {"User-Agent": "Rainout Source by JEEZ Labs (public pilot)"}

    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    points_req = urllib.request.Request(points_url, headers=headers)
    with urllib.request.urlopen(points_req, timeout=20) as response:
        points = json.loads(response.read().decode("utf-8"))

    hourly_url = points["properties"]["forecastHourly"]
    hourly_req = urllib.request.Request(hourly_url, headers=headers)
    with urllib.request.urlopen(hourly_req, timeout=20) as response:
        forecast = json.loads(response.read().decode("utf-8"))

    periods = forecast["properties"]["periods"]
    target = _parse_time(game_time)
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

    precip = selected.get("probabilityOfPrecipitation", {}).get("value")
    if precip is None:
        precip = 0
    short_forecast = selected.get("shortForecast", "") or ""
    detailed_forecast = selected.get("detailedForecast", "") or ""
    storm_text = f"{short_forecast} {detailed_forecast}".lower()

    return {
        "rain_chance_percent": int(max(0, min(100, round(float(precip))))),
        "thunderstorm_likely": "thunder" in storm_text,
        "forecast": short_forecast,
        "source": "National Weather Service API",
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "forecast_period_start": selected.get("startTime"),
    }


def build_status_result(
    field_query: str,
    game_time: str,
    weather: dict[str, Any] | None = None,
    official_status: str = "unknown",
) -> dict[str, Any]:
    """Build the live API JSON result for one field and game time."""
    field_id = resolve_field_id(field_query)
    if not field_id:
        return {
            "creator": "JEEZ Labs",
            "error": "unknown_field",
            "spoken_answer": "I do not know that field yet. Right now Rain-Outsource supports Krieg Field in Austin.",
        }

    field = load_field(field_id)
    weather_data = weather or fetch_nws_weather(field, game_time)
    probability = calculate_play_probability(
        official_status=official_status,
        precipitation_chance_percent=weather_data["rain_chance_percent"],
        thunderstorm_likely=bool(weather_data["thunderstorm_likely"]),
        hours_until_game=_hours_until_game(game_time),
    )

    response = build_agent_status_response(
        field_name=field["field_name"],
        game_time=_display_game_time(game_time),
        official_status=official_status,
        rain_chance_percent=weather_data["rain_chance_percent"],
        thunderstorm_likely=bool(weather_data["thunderstorm_likely"]),
        play_probability_percent=probability["play_probability_percent"],
        rainout_phone=field["rainout_phone"],
    )
    response.update(
        {
            "field_id": field_id,
            "address": field["address"],
            "recommendation": probability["recommendation"],
            "risk_level": probability["risk_level"],
            "reason": probability["reason"],
            "weather_source": weather_data["source"],
            "last_checked": weather_data.get("last_checked"),
            "forecast": weather_data.get("forecast"),
            "forecast_period_start": weather_data.get("forecast_period_start"),
        }
    )

    if response["official_status"] == "unknown":
        storm_phrase = " with storms possible" if response["thunderstorm_likely"] else ""
        response["spoken_answer"] = (
            f"Official rainout status is unknown for {field['field_name']}. "
            f"At game time, {_display_game_time(game_time)}, rain chance is {response['rain_chance_percent']}%{storm_phrase}. "
            f"Estimated play probability is {response['game_time_play_probability_percent']}%. "
            f"Call the rainout line, {field['rainout_phone']}, before leaving."
        )
    return response


def _hours_until_game(game_time: str) -> float:
    parsed = _parse_time(game_time)
    if not parsed:
        return 0.0
    now = datetime.now(parsed.tzinfo or timezone.utc)
    return max(0.0, (parsed - now).total_seconds() / 3600)

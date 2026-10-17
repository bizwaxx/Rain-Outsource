"""HTTP layer for GET /v1/rain (shared by the Vercel handler and the WSGI app)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import http_cache, rain, weather

DATA_CACHE = "public, max-age=300, s-maxage=600"


def _json(status: int, payload: dict[str, Any], cache: str = http_cache.NO_STORE) -> tuple[int, str, bytes, str]:
    return status, "application/json", json.dumps(payload, default=str).encode("utf-8"), cache


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, str, bytes, str]:
    return _json(status, {"error": code, "message": message, "docs": "https://rainout-agent-source.vercel.app/llms.txt", **extra})


def _int(params: dict[str, str], key: str) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise rain.RainError(f"{key} must be a whole number.") from exc


def handle_rain_request(method: str, path: str, query_string: str) -> tuple[int, str, bytes, str]:
    """Return (status, content_type, body, cache_control)."""
    if path.rstrip("/") != "/v1/rain":
        return _error(404, "not_found", "Use /v1/rain?q=place or /v1/rain?lat=&lon=.")
    if method != "GET":
        return _error(405, "method_not_allowed", "Use GET.")
    params = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    q = params.get("q") or params.get("place")
    try:
        if not q and not (params.get("lat") and params.get("lon")):
            raise rain.RainError("Pass q=<place name or lat,lon> or lat= and lon=.")
        result = rain.rain_chance(q, lat=params.get("lat"), lon=params.get("lon"), hours=_int(params, "hours"),
                                  days=_int(params, "days"), at=params.get("at") or None, pick=_int(params, "pick"))
        return _json(200, result, DATA_CACHE)
    except rain.RainError as exc:
        return _error(400, "invalid_request", str(exc))
    except rain.PlaceNotFound as exc:
        return _error(404, "place_not_found", f"No place matched \"{exc.query}\". Try adding a country, or use lat,lon.",
                      suggestions=exc.suggestions, next_step="ask_user_for_place")
    except Exception as exc:  # weather or geocoder outage
        kind = "weather_unavailable" if isinstance(exc, weather.WeatherUnavailable) else "upstream_unavailable"
        return _error(502, kind, f"Rain data could not be fetched right now ({exc.__class__.__name__}).",
                      next_step="retry_later",
                      spoken_answer="I couldn't reach the weather service right now. Try again in a minute.")

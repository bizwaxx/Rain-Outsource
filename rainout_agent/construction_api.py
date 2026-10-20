"""HTTP layer for the v2 construction endpoints (shared by the Vercel handler and the WSGI app).

GET /v1/nowcast?lat=&lon=  (or q=place)
"""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qs

from rainout_agent import http_cache, rain, weather

DATA_CACHE = "public, max-age=120, s-maxage=300"
MAX_BODY = 64_000
DOCS = "https://rainout-agent-source.vercel.app/llms.txt"


class BadRequest(ValueError):
    pass


def _json(status: int, payload: dict[str, Any], cache: str = http_cache.NO_STORE) -> tuple[int, str, bytes, str]:
    return status, "application/json", json.dumps(payload, default=str).encode("utf-8"), cache


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, str, bytes, str]:
    return _json(status, {"error": code, "message": message, "docs": DOCS, **extra})


def _params(method: str, query_string: str, body: bytes, content_type: str) -> dict[str, Any]:
    params: dict[str, Any] = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    if method == "POST" and body:
        if len(body) > MAX_BODY:
            raise BadRequest("Body too large.")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("Body must be JSON.") from exc
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object.")
        params.update(data)
    return params


def _location(params: dict[str, Any]) -> str | None:
    return params.get("location") or params.get("q") or params.get("place") or params.get("address")


def _nowcast(params: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.nowcast import get_precipitation_nowcast

    if not _location(params) and not (params.get("lat") not in (None, "") and params.get("lon") not in (None, "")):
        raise rain.RainError("Pass lat and lon, or q=<town name or 'lat,lon'>.")
    return get_precipitation_nowcast(_location(params), lat=params.get("lat"), lon=params.get("lon"), pick=params.get("pick"))


ROUTES: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], tuple[str, ...]]] = {
    "/v1/nowcast": (_nowcast, ("GET",)),
}


def handle_construction_request(method: str, path: str, query_string: str, body: bytes = b"",
                                 content_type: str = "") -> tuple[int, str, bytes, str]:
    """Return (status, content_type, body, cache_control)."""
    route = ROUTES.get(path.rstrip("/"))
    if route is None:
        return _error(404, "not_found", "Unknown endpoint. See /openapi.yaml.")
    fn, methods = route
    if method not in methods:
        return _error(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
    try:
        params = _params(method, query_string, body, content_type)
        result = fn(params)
        if result.get("ambiguous"):
            return _json(200, result, http_cache.NO_STORE)
        return _json(200, result, DATA_CACHE if method == "GET" else http_cache.NO_STORE)
    except (BadRequest, rain.RainError) as exc:
        return _error(400, "invalid_request", str(exc))
    except rain.PlaceNotFound as exc:
        return _error(404, "place_not_found", f"No place matched \"{exc.query}\". Add a state or country, or pass lat and lon.",
                      suggestions=exc.suggestions, next_step="ask_user_for_place")
    except Exception as exc:  # weather or geocoder outage
        kind = "weather_unavailable" if isinstance(exc, weather.WeatherUnavailable) else "upstream_unavailable"
        return _error(502, kind, f"Weather data could not be fetched right now ({exc.__class__.__name__}).",
                      next_step="retry_later", spoken_answer="I couldn't reach the weather service right now. Try again in a minute.")

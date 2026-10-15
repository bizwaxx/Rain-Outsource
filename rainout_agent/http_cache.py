"""Cache-Control policy shared by the Vercel handlers and the local WSGI app.

``max-age`` is for browsers and agents, ``s-maxage`` for the Vercel edge (which
keys on the full URL, query string included). Errors, POSTs, health checks and
stats are never cached.
"""
from __future__ import annotations

NO_STORE = "no-store"
FIELDS = "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400"
OPENAPI = "public, max-age=3600, s-maxage=86400"
INDEX = "public, max-age=300, s-maxage=3600"
# Status mixes a forecast (cached ~10 min upstream) with official rainout status,
# which can flip to "cancelled" at any time, so keep the edge copy short.
STATUS = "public, max-age=60, s-maxage=120, stale-while-revalidate=60"
# Fallback / stale weather: cache even more briefly so NWS data returns quickly.
STATUS_DEGRADED = "public, max-age=30, s-maxage=30"
SCHEDULE_GET = "public, max-age=300, s-maxage=600"


def status_cache_control(status_code: int, payload: dict | None) -> str:
    if status_code != 200 or not isinstance(payload, dict) or payload.get("error"):
        return NO_STORE
    if payload.get("weather_source_id") in {"open_meteo", "nws_stale"}:
        return STATUS_DEGRADED
    return STATUS


def schedule_cache_control(method: str, status_code: int) -> str:
    if method.upper() == "GET" and status_code == 200:
        return SCHEDULE_GET
    return NO_STORE


def fields_cache_control(status_code: int) -> str:
    return FIELDS if status_code == 200 else NO_STORE

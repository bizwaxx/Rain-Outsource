"""One place to read a game_time and pin it to the venue's clock.

A game_time with no UTC offset (e.g. ``2026-10-28T19:00``) means the local time at
the field. We use the field's ``timezone``; if it has none, derive one from its
state and coordinates (same rule as the OpenStreetMap venues); UTC only as a last
resort.
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo


def field_timezone(field: dict[str, Any] | None) -> tzinfo:
    field = field or {}
    name = field.get("timezone")
    if name:
        try:
            return ZoneInfo(str(name).strip())
        except Exception:
            pass
    state = (field.get("state") or "").strip().upper()
    coords = field.get("coordinates") or {}
    lat = coords.get("lat", field.get("lat", field.get("latitude")))
    lon = coords.get("lon", field.get("lon", field.get("longitude")))
    if state:
        from rainout_agent.osm_venues import STATE_TZ, timezone_for

        if state in STATE_TZ:
            try:
                if lat is not None and lon is not None:
                    return ZoneInfo(timezone_for(state, float(lat), float(lon)))
                return ZoneInfo(STATE_TZ[state])
            except Exception:
                pass
    return timezone.utc


def parse_game_time(value: Any, field: dict[str, Any] | None = None) -> datetime | None:
    """Parse ISO 8601; a naive time is taken as the field's local time. None if unreadable."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=field_timezone(field))
    return parsed


def normalize_game_time(value: Any, field: dict[str, Any] | None = None) -> Any:
    """Pin a naive game_time to the field's zone (ISO string in, ISO string out); leave the rest as given."""
    if isinstance(value, str):
        try:
            raw = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return value
        return parse_game_time(raw, field).isoformat() if raw.tzinfo is None else value
    if isinstance(value, datetime) and value.tzinfo is None:
        return parse_game_time(value, field)
    return value

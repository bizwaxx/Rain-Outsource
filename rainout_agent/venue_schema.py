"""Venue schema for Rainout Source.

Every venue (field definition) in ``data/`` is one JSON document. The formal
JSON Schema lives in ``schema/venue.schema.json``; this module mirrors it with a
dependency-free validator used by the tests, the NOAA script and the API.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

SPORTS = ("baseball", "softball", "soccer", "motocross", "multipurpose")
VENUE_TYPES = ("ball_field", "soccer_complex", "motocross_track", "multipurpose_field")
SURFACES = ("grass", "artificial_turf", "dirt", "grass_with_dirt_infield", "mixed", "unknown")
DRAINAGE = ("poor", "average", "good", "unknown")
COORDINATE_METHODS = (
    "official_venue_source",
    "openstreetmap_cross_checked",
    "place_listing_cross_checked",
    "legacy",
)
STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR"
).split()

# Which sports each venue type must include.
VENUE_TYPE_SPORTS = {
    "ball_field": {"baseball", "softball"},
    "soccer_complex": {"soccer"},
    "motocross_track": {"motocross"},
    "multipurpose_field": {"multipurpose", "soccer", "baseball", "softball"},
}

# Legacy free-text "sport" values mapped to canonical sports.
LEGACY_SPORT_MAP = {
    "baseball": ["baseball"],
    "softball": ["softball"],
    "baseball/softball": ["baseball", "softball"],
    "baseball_softball": ["baseball", "softball"],
    "multipurpose fields": ["multipurpose"],
    "soccer": ["soccer"],
    "motocross": ["motocross"],
}

REQUIRED_STRINGS = ("id", "field_name", "city", "state", "address", "sport", "rainout_phone", "weather_source")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sports_for(field: dict[str, Any]) -> list[str]:
    """Canonical sports for a venue (uses ``sports`` or maps the legacy ``sport``)."""
    sports = field.get("sports")
    if isinstance(sports, list) and sports:
        return [str(s).lower() for s in sports]
    return LEGACY_SPORT_MAP.get(str(field.get("sport") or "").strip().lower(), [])


def _is_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_venue(field: Any) -> list[str]:
    """Return a list of human-readable schema errors (empty when valid)."""
    if not isinstance(field, dict):
        return ["venue must be a JSON object"]
    errors: list[str] = []

    for key in REQUIRED_STRINGS:
        if not isinstance(field.get(key), str) or not field[key].strip():
            errors.append(f"{key}: required non-empty string")

    if isinstance(field.get("id"), str) and not ID_RE.match(field["id"]):
        errors.append("id: must be a lowercase slug")
    if field.get("state") not in STATES:
        errors.append(f"state: must be a 2-letter US state code, got {field.get('state')!r}")

    coords = field.get("coordinates")
    if not isinstance(coords, dict):
        errors.append("coordinates: required object with lat/lon")
    else:
        lat, lon = coords.get("lat"), coords.get("lon")
        if not isinstance(lat, (int, float)) or not 17.5 <= lat <= 72:
            errors.append("coordinates.lat: must be a number inside the US (17.5..72)")
        if not isinstance(lon, (int, float)) or not -180 <= lon <= -64:
            errors.append("coordinates.lon: must be a number inside the US (-180..-64)")

    source = field.get("coordinates_source")
    if not isinstance(source, dict):
        errors.append("coordinates_source: required object")
    else:
        method = source.get("method")
        if method not in COORDINATE_METHODS:
            errors.append(f"coordinates_source.method: must be one of {COORDINATE_METHODS}")
        elif method != "legacy":
            urls = source.get("sources")
            if not isinstance(urls, list) or not urls or not all(_is_url(u) for u in urls):
                errors.append("coordinates_source.sources: verified venues need at least one public source URL")
            if not isinstance(source.get("verified_on"), str) or not DATE_RE.match(source["verified_on"]):
                errors.append("coordinates_source.verified_on: required YYYY-MM-DD date")

    venue_type = field.get("venue_type")
    if venue_type not in VENUE_TYPES:
        errors.append(f"venue_type: must be one of {VENUE_TYPES}")

    sports = field.get("sports")
    if not isinstance(sports, list) or not sports:
        errors.append("sports: required non-empty list")
    else:
        bad = [s for s in sports if s not in SPORTS]
        if bad:
            errors.append(f"sports: unknown values {bad}")
        if venue_type in VENUE_TYPE_SPORTS and not set(sports) & VENUE_TYPE_SPORTS[venue_type]:
            errors.append(f"sports: {sports} do not fit venue_type {venue_type}")

    if "operator" not in field or not (field["operator"] is None or (isinstance(field["operator"], str) and field["operator"].strip())):
        errors.append("operator: required, string or null")
    if field.get("surface") not in SURFACES:
        errors.append(f"surface: must be one of {SURFACES}")
    if field.get("drainage") not in DRAINAGE:
        errors.append(f"drainage: must be one of {DRAINAGE}")

    url = field.get("official_status_source_url", None)
    if "official_status_source_url" not in field:
        errors.append("official_status_source_url: required (use null when none)")
    elif url is not None and not _is_url(url):
        errors.append("official_status_source_url: must be an http(s) URL or null")

    match = field.get("official_status_match")
    if match is not None and (not isinstance(match, list) or not match or not all(isinstance(m, str) and len(m.strip()) >= 3 for m in match)):
        errors.append("official_status_match: must be a non-empty list of names (3+ characters) as written on the status page")
    if field.get("official_status_scope") not in (None, "all", "page"):
        errors.append("official_status_scope: must be 'all' (every entry on the page is this venue) or 'page' (page-wide notices apply)")

    aliases = field.get("aliases")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) and a.strip() for a in aliases):
        errors.append("aliases: required non-empty list of strings")

    threshold = field.get("rain_to_cancel_threshold_in")
    if threshold is not None and (not isinstance(threshold, (int, float)) or threshold <= 0):
        errors.append("rain_to_cancel_threshold_in: must be a positive number")
    return errors

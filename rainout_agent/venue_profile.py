"""Venue maintenance profile: tarp/grounds crew, surface and level, each with its source.

Rules (never guess a venue as maintained):

* ``maintenance``: "yes" | "no" | "unknown" (tarp or grounds crew). Default "unknown".
* ``level``: "college_pro" | "rec". Default "rec" (community/school/rec), unless a source says college or pro.
* ``surface``: "turf" | "grass" | "dirt_infield" | "motocross_dirt" | "unknown".
* ``maintained`` = maintenance == "yes" or level == "college_pro" (college and pro venues have grounds crews and
  tarps; that is the evidence the level source carries). Everything else is treated as a rec venue.

Sources, in order: a league override (``/v1/status?venue_maintenance=yes&venue_level=...&venue_surface=...``,
marked as unverified), a ``venue_profile`` block in the field catalog JSON (with a source per item), the catalog
``surface`` field, then the OpenStreetMap ``surface`` tag of the venue's pitch (data/osm/surfaces.json, built by
scripts/osm_surfaces.py). Without evidence an item stays "unknown"/"rec" and says so.
"""
from __future__ import annotations

from typing import Any

MAINTENANCE = ("yes", "no", "unknown")
LEVELS = ("college_pro", "rec")
SURFACES = ("turf", "grass", "dirt_infield", "motocross_dirt", "unknown")
# Field-catalog / fixture surface words -> profile surface.
SURFACE_WORDS = {"turf": "turf", "artificial_turf": "turf", "artificial_grass": "turf", "grass": "grass", "grass_soccer": "grass",
                 "football": "grass", "dirt_infield": "dirt_infield", "grass_with_dirt_infield": "dirt_infield", "dirt": "dirt_infield",
                 "clay": "dirt_infield", "motocross": "motocross_dirt", "motocross_dirt": "motocross_dirt"}
# Profile surface -> event_risk.FIELD_TYPE_DRAINAGE key.
SURFACE_TO_FIELD_TYPE = {"turf": "turf", "grass": "grass", "dirt_infield": "dirt_infield", "motocross_dirt": "motocross"}
NO_EVIDENCE = "no evidence (default)"


def _norm(value: Any, allowed: tuple[str, ...]) -> str | None:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    v = {"college": "college_pro", "pro": "college_pro", "professional": "college_pro", "community": "rec", "recreational": "rec",
         "true": "yes", "false": "no"}.get(v, v)
    if allowed is SURFACES:
        v = SURFACE_WORDS.get(v, v)
    return v if v in allowed else None


def parse_override(params: dict[str, Any]) -> dict[str, str] | None:
    """League override from query/tool params; invalid values are ignored (reported in ``override_errors``)."""
    out: dict[str, str] = {}
    errors = []
    for key, allowed in (("venue_maintenance", MAINTENANCE), ("venue_level", LEVELS), ("venue_surface", SURFACES)):
        raw = params.get(key)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw in (None, ""):
            continue
        v = _norm(raw, allowed)
        if v is None:
            errors.append(f"{key}={raw!r} is not one of {', '.join(allowed)}")
        else:
            out[key.removeprefix("venue_")] = v
    if errors:
        out["_errors"] = "; ".join(errors)
    return out or None


def build(field: dict[str, Any], override: dict[str, str] | None = None) -> dict[str, Any]:
    block = field.get("venue_profile") or {}
    prof: dict[str, Any] = {"maintenance": "unknown", "level": "rec", "surface": "unknown",
                            "sources": {"maintenance": NO_EVIDENCE, "level": NO_EVIDENCE, "surface": NO_EVIDENCE}}
    for key, allowed in (("maintenance", MAINTENANCE), ("level", LEVELS), ("surface", SURFACES)):
        v = _norm(block.get(key), allowed)
        if v is not None and v != "unknown" and block.get(f"{key}_source"):
            prof[key], prof["sources"][key] = v, block[f"{key}_source"]
    if prof["surface"] == "unknown":
        v = _norm(field.get("surface"), SURFACES)
        if v and v != "unknown":
            prof["surface"], prof["sources"]["surface"] = v, field.get("surface_source") or "field catalog"
    if prof["surface"] == "unknown" and (field.get("osm") or {}).get("surface"):
        s = field["osm"]["surface"]
        v = _norm(s.get("value"), SURFACES)
        if v:
            tag = str(s.get("tag") or "")
            what = tag[1:-1] if tag.startswith("(") else f"surface={tag}"
            prof["surface"], prof["sources"]["surface"] = v, f"OpenStreetMap {what} on {s.get('element')}"
    if override:
        applied = {k: v for k, v in override.items() if not k.startswith("_")}
        for k, v in applied.items():
            prof[k], prof["sources"][k] = v, "league override (unverified, supplied with the request)"
        prof["override_applied"] = bool(applied)
        if override.get("_errors"):
            prof["override_errors"] = override["_errors"]
    prof["maintained"] = prof["maintenance"] == "yes" or prof["level"] == "college_pro"
    prof["treated_as"] = "maintained (tarp or crew, college/pro)" if prof["maintained"] else "rec venue"
    return prof


def field_type(prof: dict[str, Any]) -> str | None:
    return SURFACE_TO_FIELD_TYPE.get(prof.get("surface"))

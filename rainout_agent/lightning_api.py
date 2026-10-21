"""HTTP layer for GET /v1/lightning and /v1/lightning/ingest (served by api/rain.py: no extra Vercel function)."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import http_cache, lightning

LIVE_CACHE = "public, max-age=30, s-maxage=30"


def _json(status: int, payload: dict[str, Any], cache: str = http_cache.NO_STORE) -> tuple[int, str, bytes, str]:
    return status, "application/json", json.dumps(payload, default=str).encode("utf-8"), cache


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, str, bytes, str]:
    return _json(status, {"error": code, "message": message, "advisory": lightning.ADVISORY,
                          "docs": "https://rainout-agent-source.vercel.app/llms.txt", **extra})


def is_lightning_path(path: str) -> bool:
    return (path or "").rstrip("/") in ("/v1/lightning", "/v1/lightning/ingest")


def resolve_point(params: dict[str, str]) -> tuple[float | str, float | str, str | None]:
    """lat/lon, or a venue (field_id / venue / field) or motocross track_id -> (lat, lon, label)."""
    venue = params.get("field_id") or params.get("venue") or params.get("field")
    track = params.get("track_id")
    if venue or track:
        from rainout_agent.status_api import load_field, resolve_field_id

        if track:
            from rainout_agent import motocross

            try:
                fid = motocross.resolve_track(track)["field_id"]
            except motocross.UnknownTrack:
                raise lightning.LightningError("Unknown track_id. Call find_track first.") from None
        else:
            fid = resolve_field_id(venue)
            if not fid:
                raise lightning.LightningError(f"Unknown venue '{venue}'. Use /v1/fields?q= and pass its field_id.")
        field = load_field(fid)
        c = field.get("coordinates") or {}
        if c.get("lat") is None:
            raise lightning.LightningError("That venue has no coordinates on file; pass lat and lon.")
        return c["lat"], c["lon"], field.get("field_name") or fid
    if params.get("lat") in (None, "") or params.get("lon") in (None, ""):
        raise lightning.LightningError("Pass lat and lon, or field_id (find_venue), or track_id (find_track).")
    return params["lat"], params["lon"], None


def ingest_authorized(headers: dict[str, str], params: dict[str, str]) -> bool | None:
    """True/False, or None when no secret is configured at all (endpoint disabled)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    pairs = [(os.environ.get("RAINOUT_INTERNAL_TOKEN") or "", h.get("x-rainout-internal") or ""),
             (os.environ.get("CRON_SECRET") or "", (h.get("authorization") or "").removeprefix("Bearer ").strip()),
             (os.environ.get("RAINOUT_DISPATCH_KEY") or "", params.get("key") or "")]
    if not any(secret for secret, _ in pairs):
        return None
    return any(secret and given and hmac.compare_digest(secret, given) for secret, given in pairs)


def handle_lightning_request(method: str, path: str, query_string: str, headers: dict[str, str] | None = None,
                             **deps: Any) -> tuple[int, str, bytes, str]:
    path = (path or "").rstrip("/")
    params = {k: v[0] for k, v in parse_qs(query_string or "", keep_blank_values=True).items()}
    if method not in ("GET", "POST"):
        return _error(405, "method_not_allowed", "Use GET.")
    if path == "/v1/lightning/ingest":
        ok = ingest_authorized(headers or {}, params)
        if ok is None:
            return _error(503, "not_configured", "Ingest needs RAINOUT_INTERNAL_TOKEN, CRON_SECRET or RAINOUT_DISPATCH_KEY.")
        if not ok:
            return _error(403, "forbidden", "Wrong or missing token.")
        return _json(200, lightning.ingest(source=deps.get("glm_source"), store=deps.get("store")))
    if path != "/v1/lightning":
        return _error(404, "not_found", "Use /v1/lightning?lat=&lon= or ?field_id=.")
    if method != "GET":
        return _error(405, "method_not_allowed", "Use GET.")
    try:
        lat, lon, label = resolve_point(params)
        result = lightning.check_lightning(lat, lon, radius_mi=params.get("radius_mi"), label=label,
                                           glm_source=deps.get("glm_source"), nws_fetch=deps.get("nws_fetch"),
                                           store=deps.get("store"), now=deps.get("now"))
    except lightning.LightningError as exc:
        return _error(400, "invalid_request", str(exc))
    if params.get("field_id") or params.get("venue") or params.get("field"):
        result["location"]["field_id"] = params.get("field_id") or params.get("venue") or params.get("field")
    return _json(200, result, LIVE_CACHE if result["lightning_status"] != "unknown" else http_cache.NO_STORE)

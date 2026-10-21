"""NWS severe and tropical watches/warnings as an input to the event model. OFF by default (RAINOUT_NWS_ALERTS=on).

Live: free api.weather.gov ``/alerts/active?point=lat,lon`` (US only), with the NWS User-Agent and a short shared
cache. History (backtest only): the free Iowa Environmental Mesonet VTEC archive, see staging/results/nws-alerts.md.

Covered events (VTEC phenomena.significance):
  warnings: Severe Thunderstorm (SV.W), Tornado (TO.W), Flash Flood (FF.W), Tropical Storm (TR.W), Hurricane (HU.W)
  watches:  Severe Thunderstorm (SV.A), Tornado (TO.A), Flash Flood (FF.A), Flood (FA.A), Tropical Storm (TR.A),
            Hurricane (HU.A)

Features (as known at the start; event_risk turns them into caps, all off unless PARAMS set them):
  alert_warn      a warning in effect at the start (or in the hour before)
  alert_watch     a watch or warning in effect at the start (or in the hour before)
  alert_tropical  a tropical storm or hurricane watch/warning in effect in the 24 h before the start

Alert items use the same shape as rainout_agent.lightning.nws_lightning_alerts (#49):
  {"event", "level", "headline", "severity", "onset", "ends", "sender", "url"} plus "kind" ("warning" | "watch").
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from rainout_agent.cache import Cache, get_cache

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
CACHE_TTL_S = 180
WARNINGS = {"Severe Thunderstorm Warning", "Tornado Warning", "Flash Flood Warning", "Tropical Storm Warning",
            "Hurricane Warning"}
WATCHES = {"Severe Thunderstorm Watch", "Tornado Watch", "Flash Flood Watch", "Flood Watch", "Tropical Storm Watch",
           "Hurricane Watch"}
TROPICAL = {"Tropical Storm Warning", "Hurricane Warning", "Tropical Storm Watch", "Hurricane Watch"}
# Live alerts only describe the next few hours; older or later events get no alert features.
LIVE_BEFORE_H, LIVE_AFTER_H = 12, 3


def enabled() -> bool:
    return (os.environ.get("RAINOUT_NWS_ALERTS") or "off").strip().lower() in {"1", "on", "true", "yes"}


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def relevant_alerts(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for f in (payload or {}).get("features") or []:
        p = f.get("properties") or {}
        ev = p.get("event") or ""
        if ev not in WARNINGS and ev not in WATCHES:
            continue
        kind = "warning" if ev in WARNINGS else "watch"
        out.append({"event": ev, "level": "active" if kind == "warning" else "nearby", "kind": kind,
                    "headline": p.get("headline"), "severity": p.get("severity"),
                    "onset": p.get("onset") or p.get("effective"), "ends": p.get("ends") or p.get("expires"),
                    "sender": p.get("senderName"), "url": p.get("@id") or f.get("id")})
    return out


def features_from(alerts: list[dict[str, Any]], start: datetime) -> dict[str, Any]:
    """alert_* features for an event starting at ``start`` from alert items (live shape)."""
    warn = watch = trop = False
    names = set()
    for a in alerts:
        on, ends = _parse(a.get("onset")) or start, _parse(a.get("ends"))
        in_effect = on <= start and (ends is None or ends > start - timedelta(hours=1))
        if in_effect:
            watch = True
            warn = warn or a.get("kind") == "warning"
            names.add(a["event"])
        if a["event"] in TROPICAL and on <= start and (ends is None or ends > start - timedelta(hours=24)):
            trop = True
            names.add(a["event"])
    return {"alert_warn": warn, "alert_watch": watch, "alert_tropical": trop, "alert_names": sorted(names)}


def fetch(lat: float, lon: float, *, timeout: float = 4.0, cache: Cache | None = None) -> list[dict[str, Any]]:
    """Relevant active alerts for a US point (cached). Raises on fetch failure (callers catch)."""
    from rainout_agent import weather

    cache = get_cache() if cache is None else cache
    key = f"rainout:v1:nws_alerts:{lat:.3f},{lon:.3f}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    payload = weather._http_get_json(f"{NWS_ALERTS_URL}?point={lat:.4f},{lon:.4f}", weather.NWS_HEADERS, timeout)
    alerts = relevant_alerts(payload)
    cache.set(key, alerts, CACHE_TTL_S)
    return alerts


def live_features(lat: float, lon: float, start: datetime, now: datetime | None = None, *, timeout: float = 3.0,
                  cache: Cache | None = None) -> dict[str, Any] | None:
    """alert_* features for an event near now, or None (switch off, too far out, or fetch failed). Never raises."""
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    hours = (start - now).total_seconds() / 3600
    if not -LIVE_AFTER_H <= hours <= LIVE_BEFORE_H:
        return None
    try:
        alerts = fetch(lat, lon, timeout=timeout, cache=cache)
    except Exception:
        return None
    # Live: an alert in effect now that lasts into the event counts as known at the start.
    feats = features_from([{**a, "onset": min(_parse(a.get("onset")) or now, start).isoformat()} for a in alerts], start)
    return {**feats, "alerts": alerts}

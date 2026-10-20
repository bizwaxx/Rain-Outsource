"""Resolve a job site or place (lat/lon, "lat,lon" or a place name) for the construction tools.

Construction job sites have no venue database. Callers pass lat/lon (best) or a place
name, which goes through the same free Open-Meteo geocoder as /v1/rain. That geocoder
knows cities and towns, not street addresses, so a street address resolves only as far
as its town (``precision: "place"``) and the response says so.
"""
from __future__ import annotations

from typing import Any

from rainout_agent import rain
from rainout_agent.cache import Cache

STREET_HINT = ("street", " st ", " st,", " ave", " rd", " blvd", " dr ", " dr,", " ln", " hwy", " pkwy", " way")


def _num(v: Any) -> bool:
    return v not in (None, "")


def resolve_site(query: str | None = None, *, lat: Any = None, lon: Any = None, pick: Any = None,
                 cache: Cache | None = None) -> dict[str, Any]:
    """Return {lat, lon, label, place, precision} or {ambiguous: True, matches, summary}.

    Raises rain.RainError (bad input) or rain.PlaceNotFound.
    """
    if _num(lat) or _num(lon):
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise rain.RainError("lat and lon must both be numbers.") from exc
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            raise rain.RainError("Latitude must be -90..90 and longitude -180..180.")
        return {"lat": round(la, 4), "lon": round(lo, 4), "label": f"{la:.4f},{lo:.4f}", "place": None, "precision": "exact"}
    ll = rain.parse_latlon(query)
    if ll:
        return {"lat": ll[0], "lon": ll[1], "label": f"{ll[0]:.4f},{ll[1]:.4f}", "place": None, "precision": "exact"}
    text = (query or "").strip()
    if not text:
        raise rain.RainError("Pass lat and lon, or location (a town name or 'lat,lon').")
    looks_like_street = any(ch.isdigit() for ch in text.split(",")[0]) or any(h in f" {text.lower()} " for h in STREET_HINT)
    geo_query = text
    if looks_like_street and "," in text:
        # "123 Main St, Austin, TX" -> geocode "Austin, TX" (the free geocoder has no street addresses).
        geo_query = ",".join(text.split(",")[1:]).strip()
    geo = rain.geocode(geo_query, cache=cache)
    if geo["ambiguous"] and not pick:
        return {"ambiguous": True, "query": geo["query"], "matches": geo["matches"], "next_step": "ask_user_which_place",
                "summary": (f"\"{geo['query']}\" matches several places: "
                            + "; ".join(f"{i}. {m['label']}" for i, m in enumerate(geo["matches"], 1))
                            + ". Add a state or country, or pass lat and lon for the job site.")}
    if pick:
        try:
            idx = int(pick)
        except (TypeError, ValueError) as exc:
            raise rain.RainError("pick must be a whole number.") from exc
        if not 1 <= idx <= len(geo["matches"]):
            raise rain.RainError(f"pick must be 1-{len(geo['matches'])}.")
        place = geo["matches"][idx - 1]
    else:
        place = geo["place"]
    out = {"lat": round(float(place["latitude"]), 4), "lon": round(float(place["longitude"]), 4), "label": place["label"],
           "place": place, "precision": "place"}
    out["precision_note"] = ("Resolved to the town center, not the street address. Pass lat and lon for the exact job site."
                             if looks_like_street else
                             "Resolved to the town center. Pass lat and lon for the exact job site.")
    return out

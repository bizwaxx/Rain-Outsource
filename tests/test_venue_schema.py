import copy
import io
import json
from pathlib import Path

import pytest

from api.fields import handler as FieldsHandler
from rainout_agent.historical import HISTORICAL_DIR
from rainout_agent.http_app import application
from rainout_agent.status_api import _load_all_fields, _normalize, list_supported_fields, resolve_field_id
from rainout_agent.venue_schema import (
    COORDINATE_METHODS,
    DRAINAGE,
    SPORTS,
    SURFACES,
    VENUE_TYPES,
    sports_for,
    validate_venue,
)

ROOT = Path(__file__).resolve().parents[1]
FIELDS = _load_all_fields()
BY_ID = {f["id"]: f for f in FIELDS}

# Generous state bounding boxes (lat_min, lat_max, lon_min, lon_max).
STATE_BOXES = {
    "TX": (25.8, 36.6, -106.7, -93.5),
    "OK": (33.6, 37.1, -103.1, -94.4),
    "LA": (28.9, 33.1, -94.1, -88.8),
    "FL": (24.4, 31.1, -87.7, -79.9),
}

MX_TRACKS = {
    "floresville-tx-cycle-ranch-motocross-park": (29.142134, -98.1114),
    "red-rock-tx-murphys-mx": (29.940622, -97.475),
    "wortham-tx-freestone-raceway": (31.805522, -96.400399),
}


@pytest.mark.parametrize("field_id", sorted(BY_ID))
def test_every_venue_matches_schema(field_id):
    assert validate_venue(BY_ID[field_id]) == []


# Pre-existing ambiguity (Austin and San Antonio both have a "Northwest Little
# League"); left as-is so existing lookups keep resolving the same way.
KNOWN_SHARED_ALIASES = {"northwest little league"}


def test_venue_ids_and_aliases_are_unique():
    assert len(BY_ID) == len(FIELDS)
    seen = {}
    for field in FIELDS:
        for alias in set(_normalize(a) for a in field["aliases"]):
            if alias in KNOWN_SHARED_ALIASES:
                continue
            assert seen.setdefault(alias, field["id"]) == field["id"], f"alias {alias!r} is shared"


def test_verified_venues_lie_inside_their_state():
    for field in FIELDS:
        if field["coordinates_source"]["method"] == "legacy":
            continue
        box = STATE_BOXES[field["state"]]
        lat, lon = field["coordinates"]["lat"], field["coordinates"]["lon"]
        assert box[0] <= lat <= box[1] and box[2] <= lon <= box[3], field["id"]
        assert field["address"].rstrip().split(",")[-1].strip().startswith(field["state"]), field["id"]


def test_three_motocross_tracks_have_verified_definitions():
    for field_id, (lat, lon) in MX_TRACKS.items():
        field = BY_ID[field_id]
        assert field["venue_type"] == "motocross_track"
        assert field["sports"] == ["motocross"]
        assert field["coordinates"] == {"lat": lat, "lon": lon}
        assert field["coordinates_source"]["method"] != "legacy"
    assert BY_ID["wortham-tx-freestone-raceway"]["coordinates_source"]["method"] == "official_venue_source"
    assert resolve_field_id("Freestone") == "wortham-tx-freestone-raceway"
    assert resolve_field_id("Murphys MX") == "red-rock-tx-murphys-mx"
    assert resolve_field_id("Cycle Ranch") == "floresville-tx-cycle-ranch-motocross-park"


def test_coverage_spans_soccer_motocross_and_several_states():
    sports = {s for f in FIELDS for s in sports_for(f)}
    states = {f["state"] for f in FIELDS}
    assert {"baseball", "softball", "soccer", "motocross"} <= sports
    assert {"TX", "OK", "LA", "FL"} <= states


def test_every_historical_doc_belongs_to_a_venue():
    for path in HISTORICAL_DIR.glob("*.json"):
        assert json.loads(path.read_text())["field_id"] in BY_ID, path.name


def test_json_schema_file_matches_python_constants():
    schema = json.loads((ROOT / "schema" / "venue.schema.json").read_text())
    props = schema["properties"]
    assert props["venue_type"]["enum"] == list(VENUE_TYPES)
    assert props["sports"]["items"]["enum"] == list(SPORTS)
    assert props["surface"]["enum"] == list(SURFACES)
    assert props["drainage"]["enum"] == list(DRAINAGE)
    assert props["coordinates_source"]["properties"]["method"]["enum"] == list(COORDINATE_METHODS)


# ------------------------------------------------------------------ validator
VALID = BY_ID["wortham-tx-freestone-raceway"]


def _broken(**changes):
    doc = copy.deepcopy(VALID)
    for key, value in changes.items():
        if value is KeyError:
            doc.pop(key)
        else:
            doc[key] = value
    return validate_venue(doc)


def test_validator_rejects_bad_documents():
    assert validate_venue("nope") == ["venue must be a JSON object"]
    assert any("state" in e for e in _broken(state="Texas"))
    assert any("coordinates.lat" in e for e in _broken(coordinates={"lat": 51.5, "lon": -0.1}) + _broken(coordinates={"lat": "x", "lon": -97}))
    assert any("venue_type" in e for e in _broken(venue_type="stadium"))
    assert any("do not fit venue_type" in e for e in _broken(sports=["soccer"]))
    assert any("sports: unknown" in e for e in _broken(sports=["curling"]))
    assert any("surface" in e for e in _broken(surface="mud"))
    assert any("drainage" in e for e in _broken(drainage="great"))
    assert any("operator" in e for e in _broken(operator=KeyError))
    assert any("official_status_source_url" in e for e in _broken(official_status_source_url="ftp://x"))
    assert any("official_status_source_url" in e for e in _broken(official_status_source_url=KeyError))
    assert any("id" in e for e in _broken(id="Bad ID"))
    assert any("rain_to_cancel" in e for e in _broken(rain_to_cancel_threshold_in=-1))
    assert any("sources" in e for e in _broken(coordinates_source={"method": "openstreetmap_cross_checked", "verified_on": "2026-10-14"}))
    assert any("verified_on" in e for e in _broken(coordinates_source={"method": "official_venue_source", "sources": ["https://x.org"]}))
    assert _broken(coordinates_source={"method": "legacy"}) == []
    assert _broken(operator=None, official_status_source_url=None) == []


# -------------------------------------------------------------------- filters
def test_list_supported_fields_filters_by_sport_and_state():
    everything = list_supported_fields()
    assert len(everything) == len(FIELDS)
    soccer = list_supported_fields(sport="soccer")
    assert soccer and all("soccer" in f["sports"] for f in soccer)
    tx_mx = list_supported_fields(sport="motocross", state="tx")
    assert {f["field_id"] for f in tx_mx} >= set(MX_TRACKS)
    assert all(f["state"] == "TX" and f["venue_type"] == "motocross_track" for f in tx_mx)
    ok_fl = list_supported_fields(state="OK,FL")
    assert {f["state"] for f in ok_fl} == {"OK", "FL"}
    ball = list_supported_fields(sport="baseball_softball")
    assert ball and all({"baseball", "softball"} & set(f["sports"]) for f in ball)
    assert list_supported_fields(sport="MX") == list_supported_fields(sport="motocross")
    with pytest.raises(ValueError):
        list_supported_fields(sport="curling")
    with pytest.raises(ValueError):
        list_supported_fields(state="Texas")


def test_fields_listing_exposes_schema_attributes():
    entry = next(f for f in list_supported_fields(state="LA") if f["field_id"] == "baton-rouge-la-brec-burbank-soccer-complex")
    assert entry["venue_type"] == "soccer_complex"
    assert entry["sports"] == ["soccer"]
    assert entry["coordinates"]["lat"] == pytest.approx(30.34599)
    assert entry["surface"] == "unknown" and entry["drainage"] == "unknown"
    assert entry["operator"].startswith("BREC")


def _vercel_fields(query):
    instance = FieldsHandler.__new__(FieldsHandler)
    instance.path = "/v1/fields" + query
    instance.wfile = io.BytesIO()
    status = {}
    instance.send_response = lambda code, message=None: status.setdefault("code", code)
    instance.send_header = lambda *a: None
    instance.end_headers = lambda: None
    instance.do_GET()
    return status["code"], json.loads(instance.wfile.getvalue())


def test_vercel_fields_endpoint_filters_and_rejects_bad_filters():
    code, payload = _vercel_fields("?sport=motocross&state=OK")
    assert code == 200
    assert payload["filters"] == {"sport": "motocross", "state": "OK"}
    assert payload["count"] == len(payload["fields"]) >= 2
    assert all(f["state"] == "OK" for f in payload["fields"])
    code, payload = _vercel_fields("?sport=curling")
    assert code == 400 and payload["error"] == "invalid_filter"
    code, payload = _vercel_fields("")
    assert code == 200 and "filters" not in payload and payload["count"] == len(FIELDS)


def test_local_http_app_fields_filter():
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/fields", "QUERY_STRING": "sport=soccer&state=FL"}, start_response))
    payload = json.loads(body)
    assert captured["status"] == "200 OK"
    assert payload["count"] >= 3 and all(f["state"] == "FL" and "soccer" in f["sports"] for f in payload["fields"])
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/fields", "QUERY_STRING": "state=XX"}, start_response))
    assert captured["status"] == "400 Bad Request"

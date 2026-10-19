"""OpenStreetMap venue pipeline (scripts/osm_venues.py) and runtime (rainout_agent/osm_venues.py)."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("osm_pipeline", ROOT / "scripts" / "osm_venues.py")
pipe = importlib.util.module_from_spec(spec)
sys.modules["osm_pipeline"] = pipe
spec.loader.exec_module(pipe)

from rainout_agent import motocross, osm_venues, status_api  # noqa: E402
from rainout_agent.historical import calibration_for  # noqa: E402

# ------------------------------------------------------------------ fixtures
D = 0.0009  # ~100 m of latitude


def _poly(lat, lon, h=0.0004):
    return {"type": "Polygon", "coordinates": [[[lon - h, lat - h], [lon + h, lat - h], [lon + h, lat + h], [lon - h, lat + h], [lon - h, lat - h]]]}


def _pt(lat, lon):
    return {"type": "Point", "coordinates": [lon, lat]}


def _feat(kind, oid, geom, **tags):
    return {"type": "Feature", "geometry": geom, "properties": {"@type": kind, "@id": oid, **tags}}


def _gj(tmp_path, feats):
    p = tmp_path / "XX.geojsonseq"
    p.write_text("".join("\x1e" + json.dumps(f) + "\n" for f in feats))
    return p


FEATURES = [
    _feat("node", 1, _pt(30.0, -97.0), place="town", name="Testville"),
    _feat("node", 2, _pt(30.02, -97.0), place="hamlet", name="Tinyburg"),
    # park polygon with two softball pitches (and a batting cage complex elsewhere)
    _feat("way", 10, _poly(30.01, -97.01, 0.003), leisure="park", name="Lamar Park", **{"addr:street": "Lamar Blvd", "addr:housenumber": "100", "addr:city": "Testville"}),
    _feat("way", 11, _poly(30.0105, -97.0105), leisure="pitch", sport="softball"),
    _feat("way", 12, _poly(30.0095, -97.0095), leisure="pitch", sport="softball;baseball"),
    # loose unnamed soccer pitches 120 m apart, near (not inside) a named school
    _feat("way", 20, _poly(30.05, -97.05), leisure="pitch", sport="soccer"),
    _feat("way", 21, _poly(30.05 + 1.2 * D, -97.05), leisure="pitch", sport="soccer"),
    _feat("way", 22, _poly(30.05 + 2.4 * D, -97.05), leisure="pitch", sport="american_football"),
    _feat("node", 23, _pt(30.0515, -97.0515), amenity="school", name="Riverside Middle School"),
    # isolated unnamed pitch: dropped
    _feat("way", 30, _poly(30.2, -97.3), leisure="pitch", sport="baseball"),
    # named batting cage: skipped
    _feat("way", 31, _poly(30.3, -97.3), leisure="pitch", sport="baseball", name="Smith Batting Cages"),
    # motocross track pieces 500 m apart
    _feat("way", 40, _poly(30.1, -97.2), leisure="track", sport="motocross", name="Red Dirt MX"),
    _feat("way", 41, _poly(30.1 + 5 * D, -97.2), leisure="track", sport="motocross"),
    # pitch inside the confirmed venue (dedupe)
    _feat("way", 50, _poly(30.4, -97.4), leisure="pitch", sport="softball", name="Confirmed Fields North"),
]
CONFIRMED = [{"id": "testville-xx-confirmed", "name": "Confirmed Fields", "state": "XX", "lat": 30.4005, "lon": -97.4005, "sports": ["softball"]}]


def test_build_state_clusters_names_and_dedupes(tmp_path):
    venues, stats = pipe.build_state("XX", _gj(tmp_path, FEATURES), [dict(c) for c in CONFIRMED])
    by_name = {v["name"]: v for v in venues}
    assert set(by_name) == {"Lamar Park", "Riverside Middle School", "Red Dirt MX"}
    lamar = by_name["Lamar Park"]
    assert lamar["kind"] == "ball_field" and lamar["sports"] == ["baseball", "softball"] and lamar["pitches"] == 2
    assert lamar["address"] == "100 Lamar Blvd, Testville, XX" and lamar["city"] == "Testville"
    assert lamar["id"] == "osm-xx-lamar-park-w10" and lamar["osm"][0] == "w10"
    school = by_name["Riverside Middle School"]
    assert school["kind"] == "soccer_complex" and school["pitches"] == 2  # football pitch not a target sport
    assert school["city"] == "Testville"  # prefers the town over the nearer hamlet
    mx = by_name["Red Dirt MX"]
    assert mx["kind"] == "motocross_track" and mx["sports"] == ["motocross"] and len(mx["osm"]) == 2
    assert stats["unnamed_pitch_clusters_dropped"] == 2  # the lone pitch and the batting cage
    assert stats["duplicates_of_confirmed"] == 1


def test_slug_poly_and_rows():
    assert pipe.slug("Zilker Park – Field #3") == "zilker-park-field-3"
    rings = pipe.parse_poly("x\n1\n  0 0\n  10 0\n  10 10\n  0 10\nEND\nEND\n")
    assert pipe.in_poly(5, 5, rings) and not pipe.in_poly(15, 5, rings)
    v = {"id": "osm-tx-a-w1", "name": "A", "sports": ["soccer"], "kind": "soccer_complex", "lat": 1, "lon": 2, "city": "C",
         "address": "C, TX", "pitches": 3, "osm": ["w1", "w2"], "station": "USC1"}
    row = dict(zip(pipe.ROW_FIELDS, pipe._row(v)))
    assert row["kind"] == "s" and row["osm"] == "w1;w2" and row["sports"] == "soccer"


def test_station_stats_monthly_shape():
    from datetime import date, timedelta

    start, end = date(2016, 1, 1), date(2016, 12, 31)
    vals = {start + timedelta(days=i): (1.2 if i % 10 == 0 else 0.0) for i in range(366)}
    cov, annual, monthly = pipe.station_stats(vals, start, end)
    assert cov == 100.0 and len(monthly) == 12 and len(monthly["01"]) == 3 + 5
    assert monthly["01"][0] == 31 and monthly["01"][2] == pytest.approx(monthly["01"][-1])  # every wet day is >= 1 in


# ------------------------------------------------------------------ runtime with a fixture data dir
@pytest.fixture()
def osm_data(tmp_path, monkeypatch):
    osm, noaa = tmp_path / "osm", tmp_path / "noaa"
    osm.mkdir(), noaa.mkdir()
    fields = pipe.ROW_FIELDS
    rows = [
        ["osm-tx-lamar-park-w10", "Lamar Park", "baseball,softball", "b", 30.01, -97.01, "Testville", "100 Lamar Blvd, Testville, TX", 2, "w10;w11", "USC00000001"],
        ["osm-tx-riverside-fields-w20", "Riverside Fields", "soccer", "s", 30.05, -97.05, "Testville", "near Testville, TX", 4, "w20", None],
        ["osm-tx-el-paso-park-w30", "El Paso Park", "baseball", "b", 31.76, -106.44, "El Paso", "El Paso, TX", 1, "w30", None],
        ["osm-tx-red-dirt-mx-w40", "Red Dirt MX", "motocross", "x", 30.1, -97.2, "Testville", "near Testville, TX", 2, "w40;w41", "USC00000001"],
    ]
    (osm / "TX.json").write_text(json.dumps({"state": "TX", "row_fields": fields, "rows": rows}))
    (osm / "motocross.json").write_text(json.dumps({"row_fields": ["state"] + fields, "rows": [["TX"] + rows[3]]}))
    (osm / "index.json").write_text(json.dumps({"states": {"TX": {"venues": 4}}, "totals": {"venues": 4}}))
    mf = ["days_observed", "mean_precip_in", "wet_day_frac", "p_ge_0_10", "p_ge_0_25", "p_ge_0_50", "p_ge_0_75", "p_ge_1_00"]
    monthly = {f"{m:02d}": [300, 3.0, 0.3, 0.2, 0.15, 0.1, 0.06, 0.04] for m in range(1, 13)}
    (noaa / "stations.json").write_text(json.dumps({
        "source": "NOAA test", "period": {"start": "2016-01-01", "end": "2025-12-31"}, "monthly_fields": mf,
        "stations": {"USC00000001": {"name": "TESTVILLE 1 N", "lat": 30.02, "lon": -97.01, "coverage_percent": 98.0,
                                     "annual_mean_precip_in": 36.0, "monthly": monthly}}}))
    monkeypatch.setattr(osm_venues, "OSM_DIR", osm)
    monkeypatch.setattr(osm_venues, "STATIONS_FILE", noaa / "stations.json")
    for fn in (osm_venues.index, osm_venues.load_state, osm_venues.motocross_tracks, osm_venues.stations):
        fn.cache_clear()
    yield
    for fn in (osm_venues.index, osm_venues.load_state, osm_venues.motocross_tracks, osm_venues.stations):
        fn.cache_clear()


def test_get_and_to_field_label_osm_only(osm_data):
    assert osm_venues.is_osm_id("osm-tx-lamar-park-w10") and not osm_venues.is_osm_id("austin-tx-zilker")
    f = status_api.load_field("osm-tx-lamar-park-w10")
    assert f["verification_level"] == "osm_only" and f["status_source_type"] == "none"
    assert f["rainout_phone"] == "not_published" and f["timezone"] == "America/Chicago"
    assert "OpenStreetMap" in f["osm"]["attribution"] and f["osm"]["elements"][0].endswith("/way/10")
    assert status_api.resolve_field_id("osm-tx-lamar-park-w10") == "osm-tx-lamar-park-w10"
    assert status_api.resolve_field_id("osm-tx-nope-w999") is None
    assert osm_venues.to_field(osm_venues.get("osm-tx-el-paso-park-w30"))["timezone"] == "America/Denver"


def test_timezone_splits():
    assert osm_venues.timezone_for("FL", 30.4, -87.2) == "America/Chicago"  # Pensacola
    assert osm_venues.timezone_for("FL", 25.8, -80.2) == "America/New_York"
    assert osm_venues.timezone_for("TN", 36.0, -83.9) == "America/New_York"  # Knoxville
    assert osm_venues.timezone_for("TN", 36.2, -86.8) == "America/Chicago"
    assert osm_venues.timezone_for("AZ", 33.4, -112.0) == "America/Phoenix"


def test_station_profile_feeds_calibration(osm_data):
    cal = calibration_for("osm-tx-lamar-park-w10", datetime(2026, 5, 9, 19))
    assert cal["station_id"] == "USC00000001" and cal["rain_to_cancel_threshold_in"] == 0.5
    assert cal["rainout_day_frac"] == 0.1 and cal["cancel_given_wet"] == pytest.approx(0.333, abs=0.001)
    assert calibration_for("osm-tx-riverside-fields-w20", datetime(2026, 5, 9, 19)) is None  # no station


def test_fields_payload_search_and_pagination(osm_data):
    default = status_api.fields_payload()
    assert default["verification"] == "confirmed" and default["counts"]["osm_only"] == 0
    assert all(f["verification_level"] == "confirmed" for f in default["fields"])
    tx = status_api.fields_payload(state="TX", verification="osm_only", limit=2)
    assert tx["total"] == 4 and tx["count"] == 2 and tx["next_offset"] == 2 and "OpenStreetMap" in tx["attribution"]
    assert tx["osm_coverage"]["full_states"] == ["TX"]
    q = status_api.fields_payload(q="Lamar Park")
    assert q["fields"][0]["field_id"] == "osm-tx-lamar-park-w10" and q["fields"][0]["verification_level"] == "osm_only"
    near = status_api.fields_payload(near="30.05,-97.05", radius_km=2, sport="soccer")
    assert [f["name"] for f in near["fields"]] == ["Riverside Fields"] and near["fields"][0]["distance_km"] == 0.0
    with pytest.raises(ValueError):
        status_api.fields_payload(verification="osm_only")  # needs state, q or near
    with pytest.raises(ValueError):
        status_api.fields_payload(near="abc")


def test_status_result_for_osm_venue(osm_data):
    weather = {"rain_chance_percent": 20, "thunderstorm_likely": False, "source": "test", "source_id": "nws"}
    r = status_api.build_status_result("osm-tx-lamar-park-w10", "2026-10-20T19:00:00-05:00", weather=weather)
    assert r["verification_level"] == "osm_only" and r["official_status"] == "unknown"
    assert "OpenStreetMap" in r["spoken_answer"] and "OpenStreetMap" in r["attribution"]
    assert r["historical_calibration"]["station_id"] == "USC00000001"


def test_tracks_include_osm_motocross(osm_data, monkeypatch):
    listed = motocross.list_tracks(None, "TX")
    names = {t["name"]: t for t in listed["tracks"]}
    assert names["Red Dirt MX"]["verification_level"] == "osm_only" and listed["counts"]["osm_only"] == 1
    assert names["Freestone Raceway"]["verification_level"] == "confirmed"
    assert motocross.list_tracks()["counts"]["osm_only"] == 0  # default stays confirmed-only
    found = motocross.list_tracks("Red Dirt MX")
    assert found["tracks"][0]["track_id"] == "osm-tx-red-dirt-mx-w40" and found["next_step"] == "confirm_with_user"
    t = motocross.resolve_track("osm-tx-red-dirt-mx-w40")
    assert t["verification_level"] == "osm_only" and motocross.resolve_track("Red Dirt MX")["field_id"] == t["field_id"]
    with pytest.raises(motocross.UnknownTrack):
        motocross.resolve_track("osm-tx-lamar-park-w10")  # not a motocross venue
    hist = motocross.track_history("osm-tx-red-dirt-mx-w40")
    assert hist["noaa_rain_history"]["station"]["id"] == "USC00000001" and hist["documented_events"] == []

    monkeypatch.setattr(motocross, "get_hourly", lambda f: None)
    monkeypatch.setattr(motocross, "get_amounts", lambda f: None)
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    res = motocross.check_race("osm-tx-red-dirt-mx-w40", date_param="2026-12-12", now=now)
    assert res["found"] is False and "OpenStreetMap" in res["answer"]
    cond = res["conditions_if_racing"]
    assert cond["assumed"] and cond["basis"] == "climatology" and cond["play_probability"] > 50


def test_http_fields_endpoint_params(osm_data):
    from rainout_agent.http_app import application

    seen = {}
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/fields", "QUERY_STRING": "q=Lamar+Park&state=TX"},
                                lambda s, h: seen.update(status=s)))
    data = json.loads(body)
    assert seen["status"].startswith("200") and data["fields"][0]["verification_level"] == "osm_only"
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/fields", "QUERY_STRING": "limit=0"},
                                lambda s, h: seen.update(status=s)))
    assert seen["status"].startswith("400")


def test_mcp_find_venue(osm_data):
    from rainout_agent import mcp_server

    out = mcp_server.call_tool("find_venue", {"query": "Riverside", "state": "TX"})
    text = json.dumps(out)
    assert "osm-tx-riverside-fields-w20" in text and "osm_only" in text


def test_shipped_data_is_consistent():
    """The committed data/osm files parse, ids match their state, and stations exist."""
    idx = json.loads((ROOT / "data" / "osm" / "index.json").read_text())
    assert "ODbL" in idx["license"]
    stations = json.loads((ROOT / "data" / "noaa" / "stations.json").read_text())["stations"]
    for st, meta in idx["states"].items():
        doc = json.loads((ROOT / "data" / "osm" / f"{st}.json").read_text())
        assert len(doc["rows"]) == meta["venues"]
        ids = set()
        for r in doc["rows"]:
            v = dict(zip(doc["row_fields"], r))
            assert v["id"].startswith(f"osm-{st.lower()}-") and osm_venues.ID_RE.match(v["id"]) and v["id"] not in ids
            assert len(v["id"]) <= 81  # fits the tracks route pattern
            ids.add(v["id"])
            assert v["station"] is None or v["station"] in stations


def test_multi_site_league_does_not_swallow_nearby_park(tmp_path):
    league = [{"id": "x-league", "name": "Some Adult League", "state": "XX", "lat": 30.4005, "lon": -97.4005,
               "sports": ["softball"], "multi_site": True}]
    venues, stats = pipe.build_state("XX", _gj(tmp_path, FEATURES), league)
    assert "Confirmed Fields North" in {v["name"] for v in venues} and not stats.get("duplicates_of_confirmed")


FIXED_TX = {  # 2026-10-17: legacy coordinates were 3-29 km off; now on the OSM feature
    "austin-tx-western-hills-little-league": (30.27051, -97.76122),
    "san-antonio-tx-northwest-little-league": (29.4962, -98.61763),
    "fort-worth-tx-fort-worth-university-little-league": (32.66214, -97.32392),
    "fort-worth-tx-senior-softball-dfw": (32.78489, -97.02715),
    "dallas-tx-dfw-adult-baseball-association": (32.81784, -97.10673),
}


@pytest.mark.parametrize("field_id", sorted(FIXED_TX))
def test_fixed_texas_coordinates(field_id):
    f = status_api.load_field(field_id)
    lat, lon = FIXED_TX[field_id]
    assert osm_venues._dist_km(float(f["coordinates"]["lat"]), float(f["coordinates"]["lon"]), lat, lon) < 0.3
    cs = f["coordinates_source"]
    assert cs["method"] == "openstreetmap_cross_checked" and cs["sources"][0].startswith("https://www.openstreetmap.org/")
    from rainout_agent.historical import load_historical_profile

    station = load_historical_profile(field_id)["noaa_history"]["station"]
    assert station["distance_km"] < 15


def test_documented_venue_search_example_returns_results():
    """The /v1/fields?q=... example in README, llms.txt and OpenAPI must find something."""
    import re
    from pathlib import Path

    from rainout_agent.status_api import fields_payload

    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "llms.txt", "docs/venues-osm.md"):
        for q, state in re.findall(r"/v1/fields\?q=([^&\s)]+)&state=([A-Z]{2})", (root / doc).read_text()):
            payload = fields_payload(q=q.replace("%20", " "), state=state, verification="all")
            assert payload["count"] >= 1, f"{doc}: example q={q}&state={state} finds nothing"
    example = re.search(r"description: Venue name search\., example: ([^}]+)}", (root / "openapi.yaml").read_text()).group(1)
    assert fields_payload(q=example, state="TX", verification="all")["count"] >= 1

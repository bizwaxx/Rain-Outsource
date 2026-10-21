"""Advisory lightning: GLM reader, 30-minute rule, NWS alerts, coverage honesty, HTTP/MCP, play overrides.

Fixtures are recorded from the real services (no network in tests):
* tests/fixtures/glm/OR_GLM-L2-LCFA_G18_...nc: one real GOES-18 GLM LCFA file (2026-10-21 14:00 UTC).
* tests/fixtures/glm/g18-az-storm-window.json: real GOES-18 flashes within 60 mi of a severe storm near
  34.302, -112.694 (Yavapai County, AZ) for every file in the 31 minutes before 14:16:05 UTC, 2026-10-21.
* tests/fixtures/nws-alerts-point-svr-yavapai.json: api.weather.gov/alerts/active?point= at the same spot and time
  (a real Severe Thunderstorm Warning).
* tests/fixtures/nws-alerts-point-austin.json / nws-alerts-sws.json: real responses with no lightning alerts.
* synthetic-*.nc: written with the netCDF4 library (multi-chunk and empty datasets) to exercise the reader.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rainout_agent import glm, lightning
from rainout_agent.cache import MemoryCache

FIX = Path(__file__).parent / "fixtures"
GLMF = FIX / "glm"
REAL_NC = GLMF / "OR_GLM-L2-LCFA_G18_s20262941400000_e20262941400200_c20262941400216.nc"
AZ = json.loads((GLMF / "g18-az-storm-window.json").read_text())
AZ_NOW = datetime.fromisoformat(AZ["recorded_at"])
AZ_LAT, AZ_LON = AZ["point"]
SVR = json.loads((FIX / "nws-alerts-point-svr-yavapai.json").read_text())
AUSTIN = json.loads((FIX / "nws-alerts-point-austin.json").read_text())
SWS = json.loads((FIX / "nws-alerts-sws.json").read_text())
EMPTY_NWS = {"features": []}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    lightning._MEMO.clear()
    lightning._LIST_MEMO.clear()
    lightning._LOCAL_STORE.clear()
    monkeypatch.delenv("RAINOUT_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.delenv("RAINOUT_DISPATCH_KEY", raising=False)
    yield


class FixtureSource:
    """Serves the recorded AZ window. get() returns the key; read_flashes is patched to look it up."""

    def __init__(self, files=None, fail_list=False, delay=0.0):
        self.files = dict(AZ["files"] if files is None else files)
        self.fail_list, self.delay, self.gets = fail_list, delay, 0

    def list_keys(self, sat, start, end):
        if self.fail_list:
            raise OSError("S3 down")
        return list(self.files)

    def get(self, sat, key):
        self.gets += 1
        if self.delay:
            time.sleep(self.delay)
        return key.encode()


@pytest.fixture
def recorded(monkeypatch):
    real = glm.read_flashes

    def fake(data):
        key = data.decode() if len(data) < 300 else None
        if key and key in AZ["files"]:
            return [tuple(x) for x in AZ["files"][key]]
        return real(data)
    monkeypatch.setattr(glm, "read_flashes", fake)
    return FixtureSource


def run(source, lat=AZ_LAT, lon=AZ_LON, now=AZ_NOW, nws=SVR, **kw):
    return lightning.check_lightning(lat, lon, now=now, glm_source=source, nws_fetch=lambda la, lo: nws,
                                     store=kw.pop("store", MemoryCache()), **kw)


# --- GLM reader -----------------------------------------------------------------------------------

def test_reader_matches_h5py_on_a_real_goes18_file():
    expected = json.loads((GLMF / "g18-expected.json").read_text())
    flashes = glm.read_flashes(REAL_NC.read_bytes())
    assert len(flashes) == len(expected["flash_lat"]) == 28
    for (la, lo), ela, elo in zip(flashes, expected["flash_lat"], expected["flash_lon"]):
        assert abs(la - ela) < 1e-5 and abs(lo - elo) < 1e-5


def test_reader_multi_chunk_and_empty_files():
    exp = json.loads((GLMF / "synthetic-5000-expected.json").read_text())
    fl = glm.read_flashes((GLMF / "synthetic-5000-flashes.nc").read_bytes())
    assert len(fl) == exp["count"] == 5000  # three 2048-flash chunks
    assert abs(sum(x[0] for x in fl) - exp["lat_sum"]) < 0.5 and abs(sum(x[1] for x in fl) - exp["lon_sum"]) < 0.5
    for i, key in ((0, "first"), (-1, "last"), (2048, "at_2048")):
        assert fl[i] == pytest.approx(tuple(exp[key]), abs=1e-4)
    assert glm.read_flashes((GLMF / "synthetic-empty.nc").read_bytes()) == []


def test_reader_rejects_garbage_and_truncation():
    with pytest.raises(glm.GLMFormatError):
        glm.read_flashes(b"not hdf5 at all")
    data = REAL_NC.read_bytes()
    with pytest.raises((glm.GLMFormatError, Exception)):
        glm.read_flashes(data[:4000])


def test_file_names_satellite_and_prefix():
    s, e = glm.file_times(REAL_NC.name)
    assert s == datetime(2026, 10, 21, 14, 0, 0, tzinfo=timezone.utc) and e == s + timedelta(seconds=20)
    assert glm.satellite_for(-97.7) == "east" and glm.satellite_for(-112.7) == "west"
    assert glm.hour_prefix(s) == "GLM-L2-LCFA/2026/294/14/"
    with pytest.raises(glm.GLMFormatError):
        glm.file_times("whatever.nc")


# --- the answer -----------------------------------------------------------------------------------

def test_recorded_az_storm_is_active_with_30_minute_clock(recorded):
    r = run(recorded())
    assert r["lightning_status"] == "active"
    assert r["advisory"] == lightning.ADVISORY and r["advisory_only"] is True
    assert r["glm"]["status"] == "ok" and r["glm"]["satellite"] == "GOES-West (GOES-18)"
    assert r["flashes"]["within_radius"] > 0 and r["flashes"]["within_8_mi"] <= r["flashes"]["within_10_mi"] <= r["flashes"]["within_30_mi"]
    assert r["last_flash"]["distance_mi"] <= 30
    # all_clear = latest file with a flash within 10 mi, + 30 min
    latest = max(glm.file_times(k)[1] for k, fl in AZ["files"].items()
                 if any(lightning.miles(AZ_LAT, AZ_LON, a, b) <= 10 for a, b in fl) and glm.file_times(k)[1] >= AZ_NOW - timedelta(minutes=30))
    assert r["all_clear_at"] == lightning._iso(latest + timedelta(minutes=30))
    assert [a["event"] for a in r["nws"]["alerts"]] == ["Severe Thunderstorm Warning"]
    assert {s["used_for"].split(" (")[0] for s in r["sources"]} >= {"lightning flashes"}
    assert "Severe Thunderstorm Warning also in effect" in r["reason"]


def test_radius_8_counts_fewer_and_validates(recorded):
    r10, r8 = run(recorded()), run(recorded(), radius_mi=8)
    assert r8["radius_mi"] == 8 and r8["flashes"]["within_radius"] == r10["flashes"]["within_8_mi"]
    with pytest.raises(lightning.LightningError):
        run(recorded(), radius_mi=40)
    with pytest.raises(lightning.LightningError):
        run(recorded(), lat="abc")


def test_far_point_with_full_coverage_is_clear(recorded):
    r = run(recorded(), lat=AZ_LAT + 2.0, lon=AZ_LON, nws=EMPTY_NWS)
    assert r["lightning_status"] == "clear" and r["last_flash"] is None and r["all_clear_at"] is None


def test_nearby_ring_between_radius_and_30_mi(recorded):
    # move the point so flashes fall 10-30 mi away only
    pts = [(lightning.miles(AZ_LAT + 0.3, AZ_LON, a, b)) for fl in AZ["files"].values() for a, b in fl]
    r = run(recorded(), lat=AZ_LAT + 0.3, lon=AZ_LON, nws=EMPTY_NWS, radius_mi=3)
    if any(d <= 3 for d in pts):
        pytest.skip("fixture has flashes within 3 mi of the shifted point")
    assert r["lightning_status"] == "nearby" and r["all_clear_at"] is None


def test_gap_in_files_means_unknown_not_clear(recorded):
    files = dict(sorted(AZ["files"].items())[:40] + sorted(AZ["files"].items())[50:])
    r = run(recorded(files), lat=AZ_LAT + 2.0, nws=EMPTY_NWS)
    assert r["glm"]["status"] == "partial" and r["lightning_status"] == "unknown"
    # flashes in what we did read still count
    assert run(recorded(files))["lightning_status"] == "active"


def test_stale_data_means_unknown(recorded):
    r = run(recorded(), lat=AZ_LAT + 2.0, now=AZ_NOW + timedelta(minutes=10), nws=EMPTY_NWS)
    assert r["lightning_status"] == "unknown"


def test_listing_failure_falls_back_to_nws_warning(recorded):
    r = run(recorded(fail_list=True))
    assert r["glm"]["status"] == "unavailable"
    assert r["lightning_status"] == "active" and "Severe Thunderstorm Warning" in r["reason"] and r["all_clear_at"] is None
    assert run(recorded(fail_list=True), nws=EMPTY_NWS)["lightning_status"] == "unknown"


def test_nws_failure_does_not_break_answer(recorded):
    def boom(la, lo):
        raise OSError("nws down")
    r = lightning.check_lightning(AZ_LAT, AZ_LON, now=AZ_NOW, glm_source=recorded(), nws_fetch=boom, store=MemoryCache())
    assert r["nws"]["status"] == "unavailable" and r["lightning_status"] == "active"


def test_nws_alert_classification_on_recorded_payloads():
    assert [a["level"] for a in lightning.nws_lightning_alerts(SVR)] == ["active"]
    assert lightning.nws_lightning_alerts(AUSTIN) == []  # Air Quality Alert only
    assert lightning.nws_lightning_alerts(SWS) == []      # these statements don't mention lightning
    sws = json.loads(json.dumps(SWS))
    sws["features"][0]["properties"]["description"] += " Frequent cloud to ground lightning is occurring with this storm."
    got = lightning.nws_lightning_alerts(sws)
    assert len(got) == 1 and got[0]["level"] == "nearby"


class SyntheticSource:
    """Keys every 20 s ending at `now`; flashes placed at chosen minutes-ago."""

    def __init__(self, now, flashes):
        self.now, self.keys, self.data = now, [], {}
        for i in range(95):
            end = now - timedelta(seconds=20 * i + 25)
            start = end - timedelta(seconds=20)
            k = (f"GLM-L2-LCFA/{start:%Y}/{start.timetuple().tm_yday:03d}/{start:%H}/OR_GLM-L2-LCFA_G19_"
                 f"s{start:%Y}{start.timetuple().tm_yday:03d}{start:%H%M%S}0_e{end:%Y}{end.timetuple().tm_yday:03d}{end:%H%M%S}0_c0.nc")
            self.keys.append(k)
            self.data[k] = [pt for mins, pt in flashes if abs((now - end).total_seconds() - mins * 60) < 10]

    def list_keys(self, sat, start, end):
        return self.keys

    def get(self, sat, key):
        return json.dumps(self.data[key]).encode()


def test_each_flash_restarts_the_30_minute_clock(monkeypatch):
    monkeypatch.setattr(glm, "read_flashes", lambda b: [tuple(x) for x in json.loads(b)])
    now = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    lat, lon = 30.2464, -97.7142
    five_mi = (lat + 5 / 69.0, lon)
    src = SyntheticSource(now, [(25, five_mi), (5, five_mi)])
    r = lightning.check_lightning(lat, lon, now=now, glm_source=src, nws_fetch=lambda a, b: EMPTY_NWS, store=MemoryCache())
    assert r["lightning_status"] == "active" and r["flashes"]["within_radius"] == 2
    last = datetime.fromisoformat(r["last_flash"]["time"].replace("Z", "+00:00"))
    assert abs((now - last).total_seconds() - 300) < 30
    assert r["all_clear_at"] == lightning._iso(last + timedelta(minutes=30))
    # 35 minutes after the last flash, the same storm is outside the window: clear
    later = SyntheticSource(now + timedelta(minutes=36), [(41, five_mi)])
    r2 = lightning.check_lightning(lat, lon, now=now + timedelta(minutes=36), glm_source=later,
                                   nws_fetch=lambda a, b: EMPTY_NWS, store=MemoryCache())
    assert r2["lightning_status"] == "clear"


def test_shared_cache_buckets_avoid_refetching(recorded):
    store, src = MemoryCache(), recorded()
    run(src, store=store)
    in_window = [k for k in AZ["files"] if glm.file_times(k)[1] >= AZ_NOW - timedelta(minutes=30)]
    assert src.gets == len(in_window) == 88
    assert any(k.startswith("glm:west:") for k in store._data)
    lightning._MEMO.clear()  # a different instance: only the shared cache
    src2 = recorded()
    r = run(src2, store=store)
    assert src2.gets == 0 and r["lightning_status"] == "active"


def test_fetch_budget_gives_partial_not_clear(recorded):
    r = run(recorded(delay=0.2), lat=AZ_LAT + 2.0, nws=EMPTY_NWS, budget_s=0.5)
    assert r["glm"]["status"] in ("partial", "unavailable") and r["lightning_status"] == "unknown"


def test_real_bytes_fetch_path():
    class RealBytes:
        def list_keys(self, sat, start, end):
            return [REAL_NC.name.replace("OR_", "GLM-L2-LCFA/2026/294/14/OR_")]

        def get(self, sat, key):
            return REAL_NC.read_bytes()
    w = lightning.glm_window("west", datetime(2026, 10, 21, 14, 1, tzinfo=timezone.utc), source=RealBytes(), store=MemoryCache())
    assert w["read"] == 1 and w["fetched"] == 1
    assert all(lightning.KEEP_BOX[0] <= a <= lightning.KEEP_BOX[1] for _t, fl in w["files"].values() for a, _b in fl)


def test_outside_north_america_is_unknown():
    r = lightning.check_lightning(-33.9, 151.2, nws_fetch=lambda a, b: EMPTY_NWS, store=MemoryCache())
    assert r["lightning_status"] == "unknown" and "outside" in r["glm"]["error"]


# --- HTTP / MCP -----------------------------------------------------------------------------------

def test_http_endpoint(recorded):
    from rainout_agent.lightning_api import handle_lightning_request

    deps = dict(glm_source=recorded(), nws_fetch=lambda a, b: SVR, store=MemoryCache(), now=AZ_NOW)
    st, ct, body, cc = handle_lightning_request("GET", "/v1/lightning", f"lat={AZ_LAT}&lon={AZ_LON}", **deps)
    out = json.loads(body)
    assert st == 200 and ct == "application/json" and out["lightning_status"] == "active" and "max-age=30" in cc
    st, _, body, _ = handle_lightning_request("GET", "/v1/lightning", "", **deps)
    assert st == 400 and json.loads(body)["advisory"] == lightning.ADVISORY
    st, _, body, _ = handle_lightning_request("GET", "/v1/lightning", "field_id=Krieg", glm_source=recorded(fail_list=True),
                                              nws_fetch=lambda a, b: EMPTY_NWS, store=MemoryCache())
    out = json.loads(body)
    assert st == 200 and out["location"]["label"] and out["lightning_status"] == "unknown"
    assert handle_lightning_request("GET", "/v1/lightning", "field_id=nope-nowhere")[0] == 400


def test_ingest_auth(recorded, monkeypatch):
    from rainout_agent.lightning_api import handle_lightning_request

    deps = dict(glm_source=recorded(), store=MemoryCache())
    assert handle_lightning_request("GET", "/v1/lightning/ingest", "", {}, **deps)[0] == 503
    monkeypatch.setenv("RAINOUT_DISPATCH_KEY", "k" * 24)
    assert handle_lightning_request("GET", "/v1/lightning/ingest", "key=wrong", {}, **deps)[0] == 403
    st, _, body, _ = handle_lightning_request("GET", "/v1/lightning/ingest", "key=" + "k" * 24, {}, **deps)
    assert st == 200 and set(json.loads(body)["satellites"]) == {"east", "west"}
    monkeypatch.setenv("CRON_SECRET", "c" * 24)
    assert handle_lightning_request("POST", "/v1/lightning/ingest", "", {"Authorization": "Bearer " + "c" * 24}, **deps)[0] == 200


def test_vercel_route_and_metrics_label():
    from rainout_agent import metrics

    cfg = json.loads((Path(__file__).parent.parent / "vercel.json").read_text())
    import re
    dests = [r["dest"] for r in cfg["routes"] if re.fullmatch(r["src"], "/v1/lightning")]
    assert dests and dests[0] == "/api/rain.py"
    assert [r["dest"] for r in cfg["routes"] if re.fullmatch(r["src"], "/v1/lightning/ingest")][0] == "/api/rain.py"
    assert metrics.endpoint_for("/v1/lightning?lat=1") == "lightning"


def test_mcp_get_lightning(monkeypatch):
    from rainout_agent import mcp_server

    tool = next(t for t in mcp_server.TOOLS if t["name"] == "get_lightning")
    assert "Not a safety device" in tool["description"] and tool["annotations"]["readOnlyHint"] is True
    seen = {}

    def fake(lat, lon, **kw):
        seen.update(lat=lat, lon=lon, **kw)
        return {"lightning_status": "clear", "location": {"lat": float(lat), "lon": float(lon)}}
    monkeypatch.setattr(lightning, "check_lightning", fake)
    out = mcp_server.call_tool("get_lightning", {"field_id": "Krieg", "radius_mi": 8})
    assert seen["radius_mi"] == 8 and seen["label"]
    assert mcp_server.call_tool("get_lightning", {}).get("isError") is True


# --- play overrides -------------------------------------------------------------------------------

def _block(status="active", clear_in_min=20, now=None):
    now = now or datetime.now(timezone.utc)
    return {"lightning_status": status, "reason": "Lightning 4 mi away.", "advisory": lightning.ADVISORY,
            "all_clear_at": lightning._iso(now + timedelta(minutes=clear_in_min)) if status == "active" else None,
            "last_flash": {"time": lightning._iso(now - timedelta(minutes=10)), "distance_mi": 4.0}, "nws_warnings": []}


def test_for_point_only_near_game_time(monkeypatch):
    calls = []
    monkeypatch.setattr(lightning, "check_lightning", lambda *a, **k: calls.append(1) or {
        "lightning_status": "clear", "location": {"lat": 1, "lon": 2}, "sources": [], "nws": {"alerts": []}})
    now = datetime.now(timezone.utc)
    assert lightning.for_point(30, -97, now + timedelta(days=2), now=now) is None and not calls
    assert lightning.for_point(30, -97, now + timedelta(hours=1), now=now)["lightning_status"] == "clear"
    assert lightning.for_point(30, -97, now - timedelta(hours=2), now=now)["lightning_status"] == "clear"


def test_status_play_becomes_lightning_delay(monkeypatch):
    from rainout_agent.status_api import build_status_result

    now = datetime.now(timezone.utc)
    game = (now + timedelta(minutes=10)).astimezone(timezone(timedelta(hours=-5))).isoformat(timespec="seconds")
    weather = {"rain_chance_percent": 5, "thunderstorm_likely": False, "source": "test", "source_id": "nws"}
    monkeypatch.setattr(lightning, "CHECK", lambda *a, **k: _block(now=now))
    r = build_status_result("Krieg", game, weather=weather, official_status="unknown")
    assert r["lightning"]["lightning_status"] == "active"
    assert r["recommendation"].startswith("lightning delay") and r["lightning_delay"]["recommendation_before"]
    assert lightning.ADVISORY in r["reason"] and r["spoken_answer"].startswith("Lightning delay")
    # official cancellation wins; the lightning block is still shown
    r = build_status_result("Krieg", game, weather=weather, official_status="cancelled")
    assert "lightning_delay" not in r and r["lightning"]["lightning_status"] == "active"
    # nearby: shown, no override
    monkeypatch.setattr(lightning, "CHECK", lambda *a, **k: _block("nearby", now=now))
    r = build_status_result("Krieg", game, weather=weather, official_status="unknown")
    assert "lightning_delay" not in r and r["lightning"]["lightning_status"] == "nearby"


def test_go_no_go_go_becomes_wait_only_before_the_all_clear(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(lightning, "CHECK", lambda *a, **k: _block(now=now))

    def res(start):
        return {"decision": "go", "reason": "Looks fine.", "time": {"start": start.isoformat()},
                "location": {"lat": 30.2, "lon": -97.7}, "recheck_at": None}
    r = lightning.apply_to_decision(res(now + timedelta(minutes=5)), now=now)
    assert r["decision"] == "wait" and r["lightning_delay"]["decision_before"] == "go" and r["recheck_at"] == r["lightning_delay"]["until"]
    r = lightning.apply_to_decision(res(now - timedelta(minutes=30)), now=now)  # under way
    assert r["decision"] == "wait"
    r = lightning.apply_to_decision(res(now + timedelta(hours=2)), now=now)  # starts after the all-clear
    assert r["decision"] == "go" and r["lightning"]["lightning_status"] == "active"
    nogo = dict(res(now), decision="no_go")
    assert lightning.apply_to_decision(nogo, now=now)["decision"] == "no_go"


def test_should_we_cancel_play_becomes_delay_to_all_clear():
    from rainout_agent.should_we_cancel import should_we_cancel

    now = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    until = lightning._iso(now + timedelta(minutes=25))

    def decide(act, **kw):
        return {"decision": "wait", "probability_percent": 90, "reason": "Lightning delay until at least x.",
                "time": {"start": (now + timedelta(minutes=10)).isoformat()}, "recheck_at": until,
                "lightning_delay": {"active": True, "decision_before": "go", "until": until},
                "lightning": _block(now=now), "details": {"official_status": "unknown"}}
    out = should_we_cancel("softball", venue="Krieg", time="x", now=now, decide=decide)
    assert out["recommendation"] == "delay" and out["delay_to"].startswith("2026-07-04T22:25")
    assert out["lightning"]["lightning_status"] == "active"


def test_disable_switch(monkeypatch):
    monkeypatch.setattr(lightning, "CHECK", lambda *a, **k: _block())
    monkeypatch.setenv("RAINOUT_LIGHTNING", "off")
    assert lightning.lookup(30, -97, None) is None

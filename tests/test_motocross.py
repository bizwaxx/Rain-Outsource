"""Motocross race checker: sources, merge, odds, status, history, API and MCP.

Fixtures in tests/fixtures/motocross are trimmed copies of the public pages'
structure (Swan MX schedule, MX Parks ICS, Texas statewide series, RideMX,
Cycle Ranch Shopify collection). No network is used.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rainout_agent import mcp_server, motocross as m, mx_sources as mx
from rainout_agent.cache import MemoryCache, set_cache
from rainout_agent.safe_fetch import FetchResult
from rainout_agent.tracks_api import handle_tracks_request, route

FIX = Path(__file__).parent / "fixtures" / "motocross"
NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)  # Fri Oct 16, noon CDT
CT = ZoneInfo("America/Chicago")
EMPTY_ICS = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"


@pytest.fixture(autouse=True)
def memory_cache():
    set_cache(MemoryCache())
    yield
    set_cache(None)


class FakeHTTP:
    """Serves fixtures for configured hosts; anything else is an empty page."""

    def __init__(self, overrides=None, fail=()):
        self.calls, self.overrides, self.fail = [], dict(overrides or {}), tuple(fail)

    def __call__(self, url, timeout=6.0, accept="text/html,*/*"):
        self.calls.append(url)
        for needle in self.fail:
            if needle in url:
                raise mx.MXSourceError(f"{needle}: TimeoutError")
        for needle, body in self.overrides.items():
            if needle in url:
                return body if isinstance(body, bytes) else body.encode()
        if "swanmx.com/race-schedule" in url:
            return (FIX / "swan_schedule.html").read_bytes()
        if "feed.ics?track=swan-mx-tyler" in url:
            return (FIX / "swan_mxparks.ics").read_bytes()
        if "centraltexas" in url:
            return (FIX / "tx_central.html").read_bytes()
        if "ridemx.com" in url:
            return (FIX / "ridemx_quiet.html").read_bytes()
        if "cycleranchmx.com" in url:
            return (FIX / "cycleranch_products.json").read_bytes()
        if "feed.ics" in url:
            return EMPTY_ICS
        if "wp-json" in url:
            return b"[]"
        return b"<html><body><p>Nothing here</p></body></html>"


def fake_weather(pop=5, rain_by_hour=None, thunder=False):
    """NWS-like hourly periods for 7 days plus Open-Meteo-like amounts (past 3 days + 16)."""
    rain_by_hour = rain_by_hour or (lambda t: 0.0)
    start = NOW.replace(minute=0) - timedelta(days=3)
    rows = [{"time": (start + timedelta(hours=i)).isoformat(), "precip_in": rain_by_hour(start + timedelta(hours=i)),
             "pop": pop, "code": 95 if thunder else 3} for i in range(24 * 19)]
    periods = []
    for i in range(24 * 7):
        ps = NOW.replace(minute=0) + timedelta(hours=i)
        periods.append({"startTime": ps.isoformat(), "endTime": (ps + timedelta(hours=1)).isoformat(),
                        "probabilityOfPrecipitation": {"value": pop},
                        "shortForecast": "Chance Showers And Thunderstorms" if thunder else "Sunny"})
    return {"periods": periods, "source": "nws"}, {"rows": rows, "source": "Open-Meteo (test)"}


@pytest.fixture
def live(monkeypatch):
    """Patch network and weather; returns a setter for weather and the fake HTTP."""
    http = FakeHTTP()
    monkeypatch.setattr(mx, "_http_get", http)
    state = {}

    def set_weather(**kw):
        state["hourly"], state["amounts"] = fake_weather(**kw)

    set_weather()
    monkeypatch.setattr(m, "get_hourly", lambda field: state["hourly"])
    monkeypatch.setattr(m, "get_amounts", lambda field: state["amounts"])
    return http, set_weather


# ------------------------------------------------------------------ config
def test_ten_tracks_with_public_sources_and_history_fields():
    ts = m.tracks()
    assert len(ts) == 10
    assert {t["short_id"] for t in ts} == {"freestone", "swan", "murphys", "3palms", "cycleranch", "dadecity",
                                           "orlando", "mesa", "omc", "sweet16"}
    for t in ts:
        assert t["event_sources"] or t["series_sources"], t["short_id"]
        assert all(s["url"].startswith("https://") for s in t["event_sources"])
        for h in t.get("history", []):
            assert h["source_url"].startswith("http") and (h.get("date") or h.get("date_text")) and h["outcome"]
        assert t.get("history") or t.get("history_coverage"), t["short_id"]


def test_resolve_track_by_id_short_id_and_alias():
    assert m.resolve_track("tyler-tx-swan-mx")["short_id"] == "swan"
    assert m.resolve_track("swan")["short_id"] == "swan"
    with pytest.raises(m.UnknownTrack):
        m.resolve_track("not-a-track")
    found = m.list_tracks("Swan MX Raceway Park")
    assert found["tracks"][0]["short_id"] == "swan" and found["next_step"] in ("confirm_with_user", "use_track")
    assert m.list_tracks("zzzz unknown motoplex")["next_step"] == "ask_user_for_events_url"
    assert m.list_tracks(None, "FL")["counts"]["confirmed"] == 3
    assert m.list_tracks(None, None)["count"] == 10  # default: confirmed only


# ----------------------------------------------------------------- parsers
@pytest.mark.parametrize("text,start,end,year", [
    ("October 16-18", (10, 16), (10, 18), None),
    ("Dec 12 & 13, 2026", (12, 12), (12, 13), 2026),
    ("Oct 30 - Nov 1", (10, 30), (11, 1), None),
    ("Round 19 - 10/17", (10, 17), (10, 17), None),
    ("Sept. 19th", (9, 19), (9, 19), None),
])
def test_find_date_range(text, start, end, year):
    found = mx.find_date_range(text)
    assert (found["start"], found["end"], found["year"]) == (start, end, year)


def test_dated_items_year_heading_and_rollover():
    segs = ["2026 Events", "Dec 12-13", "Texas State Championship Finale", "Jan 2-3", "FMF Texas Winter Series Rd 1"]
    items = mx.dated_items(segs, name_side="after", today=date(2026, 10, 16))
    assert [(i["start"].isoformat(), i["name"]) for i in items] == [
        ("2026-12-12", "Texas State Championship Finale"), ("2027-01-02", "FMF Texas Winter Series Rd 1")]


def test_text_dates_section_ignores_scripts_and_outside_text():
    track = m.resolve_track("swan")
    src = track["event_sources"][0]
    evs = mx.events_from_text((FIX / "swan_schedule.html").read_text(), track, src, CT, date(2026, 10, 16))
    assert [e["name"] for e in evs] == ["ROUND 5 RESCHEDULE - Lone Star Day & Night Series",
                                       "ROUND 7 - Lone Star Night Series Final & 8th Annual Red Dirt Fall Classic"]
    assert evs[0]["start"].date() == date(2026, 10, 16) and evs[0]["time_estimated"]


def test_ics_keeps_cancelled_status_and_whole_day_listings():
    track = m.resolve_track("swan")
    evs = mx.events_from_ics((FIX / "swan_mxparks.ics").read_text(), track, track["event_sources"][1], CT)
    by = {e["name"]: e for e in evs}
    assert by["Winter Warmup"]["status_text"] == "cancelled"
    # midnight-to-23:59 listings have no real start time
    assert by["Winter Warmup"]["time_estimated"] and by["Winter Warmup"]["start"].hour == 8
    assert by["Winter Warmup"]["end"].date() == date(2026, 12, 6)
    rd5 = by["Lone Star Day and Night Series Round 5 (Rescheduled)"]
    assert not rd5["time_estimated"] and rd5["start"].astimezone(CT).hour == 17


def test_shopify_collection_filters_tags_and_dates():
    track = m.resolve_track("cycleranch")
    src = track["event_sources"][0]
    evs = mx.events_from_shopify(json.loads((FIX / "cycleranch_products.json").read_text()), track, src, CT, date(2026, 10, 16))
    assert [(e["name"], e["start"].date().isoformat(), e["end"].date().isoformat()) for e in evs] == [
        ("USA MX CHAMPIONSHIPS", "2026-11-26", "2026-11-29"), ("WINTER SERIES ROUND 1", "2027-01-09", "2027-01-10")]
    assert evs[0]["source"]["event_url"] == "https://cycleranchmx.com/products/usa-mx"


def test_series_columns_keep_only_this_track():
    cfg = mx.load_config()
    evs = mx.events_from_columns((FIX / "tx_central.html").read_text(), m.resolve_track("swan"),
                                 cfg["series"]["tx_statewide_central"], CT, date(2026, 10, 16))
    assert [(e["name"], e["start"].date().isoformat()) for e in evs] == [
        ("AMA Texas State Championship Series (Central) Round 2", "2026-10-17")]


def test_clean_name_softens_shouting_but_keeps_acronyms():
    assert m._clean_name("USA MX CHAMPIONSHIPS") == "USA MX Championships"
    assert m._clean_name("ROUND 7 - Lone Star Night Series") == "Round 7 - Lone Star Night Series"
    assert m._clean_name("GATR PRO-AM") == "GATR Pro-Am"
    assert m._clean_name("2026 FMF Texas Winter Series") == "FMF Texas Winter Series"


# ------------------------------------------------------------------- merge
def test_track_events_merges_sources_into_one_race(live):
    res = m.track_events("swan", now=NOW)
    names = [e["name"] for e in res["events"]]
    assert names[0] == "Lone Star Day and Night Series Round 5 (Rescheduled)"
    rd5 = res["events"][0]
    assert len(rd5["sources"]) == 3  # track site, MX Parks, Texas statewide series
    assert {s["name"] for s in rd5["sources"]} >= {"Swan MX race schedule", "MX Parks track calendar"}
    assert rd5["kind"] == "race" and rd5["start"].startswith("2026-10-16T17:00")
    assert "Open Practice" not in names  # practice is opt-in
    assert any(e["name"].startswith("Round 7") for e in res["events"])
    assert all(s["ok"] for s in res["sources_checked"])
    assert res["calendar_ics_url"].endswith("/v1/tracks/tyler-tx-swan-mx/calendar.ics")
    with_practice = m.track_events("swan", include_practice=True, now=NOW)
    assert "Open Practice" in [e["name"] for e in with_practice["events"]]


def test_listing_cancelled_event_has_zero_odds(live):
    res = m.track_events("swan", from_param="2026-12-01", to_param="2026-12-31", include_practice=True, now=NOW)
    ev = next(e for e in res["events"] if e["name"] == "Winter Warmup")
    assert ev["official_status"] == "cancelled" and ev["play_probability"] == 0
    assert "cancelled" in ev["reason"]


def test_one_bad_source_does_not_sink_the_answer(live):
    http, _ = live
    http.fail = ("centraltexas",)
    res = m.track_events("swan", now=NOW)
    assert res["events"] and any(not s["ok"] for s in res["sources_checked"])


# -------------------------------------------------------------------- odds
def test_dry_weekend_gives_high_odds_and_one_sentence(live):
    res = m.check_race("swan", date_param="2026-10-17", now=NOW)
    assert res["found"] and res["next_step"] == "offer_calendar_add"
    ev = res["event"]
    assert ev["play_probability"] >= 90 and ev["headline_date"] == "2026-10-17"
    assert len(re.findall(r"[.!?](?:\s|$)", res["answer"])) == 1
    assert "Lone Star" in res["answer"] and "%" in res["answer"]
    cal = ev["calendar_event"]
    assert cal["iCalUID"] == f"{ev['event_id']}@rainout-agent-source.vercel.app"
    assert cal["start"]["dateTime"].startswith("2026-10-16T17:00")
    assert "BEGIN:VEVENT" in ev["ics"] and ev["event_id"] in ev["ics"]


def test_heavy_prior_day_rain_lowers_odds(live):
    _, set_weather = live
    dry = m.check_race("swan", date_param="2026-10-17", now=NOW)["event"]["play_probability"]
    soaked_day = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    set_weather(pop=10, rain_by_hour=lambda t: 0.12 if soaked_day <= t < soaked_day + timedelta(hours=20) else 0.0)
    set_cache(MemoryCache())
    res = m.check_race("swan", date_param="2026-10-17", now=NOW)
    day = next(d for d in res["event"]["days"] if d["date"] == "2026-10-17")
    assert day["weather"]["prior_48h_rain_in"] >= 2.0
    assert day["model"]["soaked_track_penalty"] > 20
    assert res["event"]["play_probability"] < dry - 20
    assert "soaking" in res["answer"]


def test_soil_changes_soak_penalty():
    clay = m.race_odds("unknown", 10, False, 30, None, 1.5, 0.0, "clay")
    sand = m.race_odds("unknown", 10, False, 30, None, 1.5, 0.0, "sand")
    assert clay["play_probability"] < sand["play_probability"]
    assert m.race_odds("cancelled", 0, False, 30, None, 0, 0, "sand")["play_probability"] == 0
    assert m.race_odds("uncertain", 0, False, 30, None, 0, 0, "sand")["play_probability"] <= 50


def test_far_out_race_uses_climatology(live):
    res = m.track_events("cycleranch", now=NOW)
    usa = next(e for e in res["events"] if e["name"] == "USA MX Championships")
    days = usa["days"]
    assert {d["basis"] for d in days} == {"climatology"}
    assert 60 <= usa["play_probability"] <= 96
    assert "too far out" in usa["reason"]
    assert usa["calendar_event"]["start"] == {"date": "2026-11-26"}


def test_model_thunder_beyond_a_week_is_ignored():
    hourly = None
    ws = NOW + timedelta(days=12)
    rows = [{"time": (ws - timedelta(days=3) + timedelta(hours=i)).isoformat(), "precip_in": 0, "pop": 45, "code": 95}
            for i in range(24 * 5)]
    stats = m._window_stats(ws.date(), ws, ws + timedelta(hours=8), hourly, {"rows": rows}, NOW)
    assert stats["rain_chance_percent"] == 45 and stats["thunderstorm_likely"] is False
    near = NOW + timedelta(days=2)
    rows = [{"time": (near - timedelta(days=3) + timedelta(hours=i)).isoformat(), "precip_in": 0, "pop": 45, "code": 95}
            for i in range(24 * 5)]
    assert m._window_stats(near.date(), near, near + timedelta(hours=8), None, {"rows": rows}, NOW)["thunderstorm_likely"]


def test_climate_odds_combines_race_day_and_day_before():
    c = m.climate_odds(m.resolve_track("freestone"), date(2026, 5, 16))
    p = c["heavy_rain_day_percent"] / 100 / m.SOIL_FACTORS["loam_sand"]
    assert c["play_probability"] == round(96 * (1 - 0.75 * (1 - (1 - p) ** 2)))


# ------------------------------------------------------------------ status
def _ridemx(title, body, age):
    return f"<html><body><div>Latest update</div><div>{title}</div><div>{body}</div><div>{age}</div></body></html>"


def test_recent_dated_cancel_notice_cancels(live):
    http, _ = live
    http.overrides["ridemx.com"] = _ridemx("Race update", "Round 5 racing on Oct 17 is cancelled due to rain.", "2 hours ago")
    ev = m.check_race("swan", date_param="2026-10-17", now=NOW)["event"]
    assert ev["official_status"] == "cancelled" and ev["play_probability"] == 0
    assert ev["status_evidence"][0]["source"] == "RideMX track updates"


def test_vague_notice_is_uncertain_and_capped(live):
    http, _ = live
    http.overrides["ridemx.com"] = _ridemx("Weather", "Rain may cancel this weekend's race, watch for updates", "1 hour ago")
    ev = m.check_race("swan", date_param="2026-10-17", now=NOW)["event"]
    assert ev["official_status"] == "uncertain" and ev["play_probability"] <= 50
    assert "uncertain" in ev["reason"]


@pytest.mark.parametrize("body,age", [
    ("Race on Oct 17 cancelled due to rain", "3 weeks ago"),       # too old
    ("Practice cancelled today, track closed for prep", "1 hour ago"),  # practice only
    ("Race on Nov 14 is cancelled", "1 hour ago"),                 # other date
])
def test_irrelevant_notices_are_ignored(live, body, age):
    http, _ = live
    http.overrides["ridemx.com"] = _ridemx("Update", body, age)
    ev = m.check_race("swan", date_param="2026-10-17", now=NOW)["event"]
    assert ev["official_status"] == "unknown"


def test_opening_hours_closed_is_not_a_notice():
    assert mx.notices_from_page("<p>Monday: Closed</p><p>Tuesday: Closed</p>") == []
    assert mx.notices_from_page("<p>Saturday's race has been postponed</p>")[0]["text"].startswith("Saturday")


# --------------------------------------------------------- no events / user URL
def test_no_events_asks_for_link_and_never_invents(live):
    res = m.track_events("murphys", now=NOW)
    assert res["events"] == [] and res["next_step"] == "ask_user_for_events_url"
    chk = m.check_race("murphys", date_param="saturday", now=NOW)
    assert chk["found"] is False and chk["next_step"] == "ask_user_for_events_url" and chk["event"] is None
    assert "link" in chk["answer"]


def test_user_supplied_ics_is_used(live):
    calls = []

    def fetcher(url):
        calls.append(url)
        return FetchResult(url=url, status=200, content_type="text/calendar", body=(FIX / "user_events.ics").read_bytes())

    res = m.check_race("murphys", date_param="2026-10-24", events_url="https://example.org/fall.ics", now=NOW, fetcher=fetcher)
    assert calls == ["https://example.org/fall.ics"]
    assert res["found"] and res["event"]["name"] == "Murphy's MX Fall Classic"
    assert res["event"]["sources"][0]["name"] == "Schedule link you provided"


def test_unknown_event_id_and_bad_date(live):
    with pytest.raises(m.TracksError):
        m.check_race("swan", event_id="swan-nope", now=NOW)
    with pytest.raises(m.TracksError):
        m.track_events("swan", date_param="next blue moon", now=NOW)
    with pytest.raises(m.TracksError):
        m.check_race("swan", now=NOW)


# ----------------------------------------------------------------- history
def test_history_has_monthly_noaa_and_cited_cancellations():
    h = m.track_history("freestone")
    monthly = h["noaa_rain_history"]["monthly"]
    assert len(monthly) == 12 and all(0 < r["climatology_race_odds"] <= 96 for r in monthly)
    assert h["noaa_rain_history"]["station"]["id"]
    docs = h["documented_events"]
    assert len(docs) >= 10 and all(d["source_url"].startswith("http") for d in docs)
    assert any((d.get("date") or "").startswith("2013-11") and d["outcome"] == "cancelled" for d in docs)
    assert h["summary"]["weather_related"] >= 5
    assert "not that races never got rained out" in h["coverage"]["gaps"]


def test_history_marks_gaps_when_nothing_documented():
    h = m.track_history("cycleranch")
    assert h["documented_events"] == [] and h["coverage"]["note"]
    assert h["summary"]["documented_race_cancellations_or_moves"] == 0


# --------------------------------------------------------------------- HTTP
def _call(path, qs="", method="GET", **kw):
    status, ctype, body, cache = handle_tracks_request(method, path, qs, **kw)
    return status, ctype, (json.loads(body) if ctype == "application/json" else body.decode()), cache


def test_routes():
    assert route("/v1/tracks") == ("list", None)
    assert route("/v1/tracks/swan/events") == ("events", "swan")
    assert route("/v1/tracks/swan/check/") == ("check", "swan")
    assert route("/v1/tracks/swan/calendar.ics") == ("calendar", "swan")
    assert route("/v1/tracks/swan/other") is None


def test_http_endpoints(live, monkeypatch):
    monkeypatch.setattr(m, "datetime", _FrozenDatetime)
    s, _, body, cache = _call("/v1/tracks", "q=freestone")
    assert s == 200 and body["tracks"][0]["short_id"] == "freestone" and "s-maxage=600" in cache
    s, _, body, cache = _call("/v1/tracks/swan/events", "from=2026-10-16&to=2026-10-31")
    assert s == 200 and body["events"][0]["name"].startswith("Lone Star") and "s-maxage=300" in cache
    s, _, body, _ = _call("/v1/tracks/tyler-tx-swan-mx/check", "date=2026-10-17")
    assert s == 200 and body["found"] and body["event"]["calendar_event"]["iCalUID"]
    s, _, body, cache = _call("/v1/tracks/freestone/history")
    assert s == 200 and body["documented_events"] and "max-age=3600" in cache
    s, ctype, text, cache = _call("/v1/tracks/swan/calendar.ics")
    assert s == 200 and ctype.startswith("text/calendar") and "BEGIN:VEVENT" in text and "max-age=900" in cache


def test_http_errors(live):
    s, _, body, cache = _call("/v1/tracks/nowhere-mx/events")
    assert s == 404 and body["error"] == "unknown_track" and body["next_step"] == "find_track" and cache == "no-store"
    s, _, body, _ = _call("/v1/tracks/swan/check")
    assert s == 400 and body["error"] == "invalid_request"
    assert _call("/v1/tracks/swan/events", method="POST")[0] == 405
    assert _call("/v1/tracks/swan/nope")[0] == 404


def test_http_502_has_next_step(live, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(m, "gather_events", boom)
    s, _, body, _ = _call("/v1/tracks/swan/events")
    assert s == 502 and body["next_step"] == "ask_user_for_events_url" and body["spoken_answer"]


def test_wsgi_and_vercel_wiring(live):
    from rainout_agent.http_app import application

    seen = {}

    def sr(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/tracks", "QUERY_STRING": "state=OK"}, sr))
    assert seen["status"].startswith("200") and json.loads(body)["counts"]["confirmed"] == 2
    vercel = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
    dests = {r["src"]: r["dest"] for r in vercel["routes"]}
    assert dests["/v1/tracks/?"] == "/api/tracks.py"
    assert dests["/v1/tracks/([^/]+)/(events|check|history)"] == "/api/tracks.py"
    assert {"src": "api/tracks.py", "use": "@vercel/python"} in vercel["builds"]
    srcs = [r["src"] for r in vercel["routes"]]
    assert srcs.index("/v1/tracks/?") < srcs.index("/v1/stats")


def test_openapi_and_llms_document_tracks():
    root = Path(__file__).parents[1]
    spec = (root / "openapi.yaml").read_text()
    assert spec == (root / "docs" / "openapi.yaml").read_text()
    for op in ("listTracks", "getTrackEvents", "checkRace", "getTrackHistory", "getTrackCalendar"):
        assert f"operationId: {op}" in spec
    llms = (root / "llms.txt").read_text()
    for tool in ("find_track", "get_track_events", "check_race", "get_track_history", "/v1/tracks"):
        assert tool in llms


# ---------------------------------------------------------------------- MCP
def _rpc(name, args):
    return mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})["result"]


def test_mcp_track_tools(live, monkeypatch):
    monkeypatch.setattr(m, "datetime", _FrozenDatetime)
    names = [t["name"] for t in mcp_server.TOOLS]
    assert {"find_track", "get_track_events", "check_race", "get_track_history"} <= set(names)
    for t in mcp_server.TOOLS:
        if t["name"] in ("get_track_events", "check_race"):
            assert "ALWAYS ask the user before adding" in t["description"]
        if t["name"] == "find_track":
            assert "Confirm the track with the user" in t["description"]
    found = _rpc("find_track", {"query": "Swan"})
    assert not found["isError"] and found["structuredContent"]["tracks"][0]["track_id"] == "tyler-tx-swan-mx"
    chk = _rpc("check_race", {"track_id": "swan", "date": "2026-10-17"})
    assert not chk["isError"] and chk["structuredContent"]["found"]
    evs = _rpc("get_track_events", {"track_id": "swan", "include_practice": True})
    assert any(e["kind"] == "practice" for e in evs["structuredContent"]["events"])
    hist = _rpc("get_track_history", {"track_id": "freestone"})
    assert hist["structuredContent"]["documented_events"]
    bad = _rpc("check_race", {"track_id": "nowhere"})
    assert bad["isError"] and "find_track" in bad["content"][0]["text"]
    assert _rpc("check_race", {})["isError"]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)

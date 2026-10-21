import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import cache, outcomes, reports
from rainout_agent.outcomes_api import handle_outcomes_request

NOW = datetime(2026, 6, 8, 3, 0, tzinfo=timezone.utc)          # 10 pm CT on Jun 7
GAME = datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)         # 7 pm CT on Jun 7
FIELD = "austin-tx-downs-field-ambl"


@pytest.fixture(autouse=True)
def clean_local():
    reports._LOCAL_STORE._data.clear() if hasattr(reports._LOCAL_STORE, "_data") else None
    reports._LOCAL_STORE.__init__(max_entries=4000)
    yield


def snap(pct=85, soak=20, when=GAME - timedelta(hours=3), test=False, official="unknown"):
    resp = {"field_id": FIELD, "game_time_play_probability_percent": pct, "official_status": official,
            "model": {"version": "v2", "soak_penalty": soak, "drainage": "slow"},
            "venue_profile": {"surface": "dirt_infield", "maintained": False}}
    return outcomes.snapshot_prediction(resp, {"timezone": "America/Chicago"}, GAME, now=when, test=test)


def submit(outcome="played", client="1.2.3.4", now=NOW, **kw):
    return outcomes.submit_outcome({"field_id": "Downs", "date": "2026-06-07", "outcome": outcome, **kw}, client=client, now=now)


def test_snapshot_only_before_start_and_throttled():
    assert snap(when=GAME - timedelta(hours=3))
    assert not snap(when=GAME - timedelta(hours=2, minutes=50))       # within 30 min
    assert not snap(when=GAME + timedelta(minutes=5))                  # never after the start
    assert snap(pct=70, when=GAME - timedelta(hours=1))


def test_outcome_attaches_the_pre_event_snapshot():
    snap(pct=85)
    r = submit("cancelled")
    assert r["accepted"] and not r["test"]
    assert r["outcome"]["prediction"]["play_probability_percent"] == 85
    assert "ip" not in json.dumps(r["outcome"]).lower() and "1.2.3.4" not in json.dumps(r)


def test_validation_and_range():
    with pytest.raises(reports.ReportError) as e:
        submit("maybe")
    assert e.value.error == "invalid_outcome"
    with pytest.raises(reports.ReportError) as e:
        outcomes.submit_outcome({"field_id": "Downs", "date": "2026-05-01", "outcome": "played"}, now=NOW)
    assert e.value.error == "date_out_of_range"
    with pytest.raises(reports.ReportError) as e:
        outcomes.submit_outcome({"field_id": "Downs", "date": "2026-06-09", "outcome": "played"}, now=NOW)
    assert e.value.error == "date_out_of_range"
    with pytest.raises(reports.ReportError) as e:
        outcomes.submit_outcome({"field_id": "nope-nowhere", "outcome": "played"}, now=NOW)
    assert e.value.status == 404


def test_rate_limit_per_venue():
    for _ in range(3):
        submit(clock=lambda: 1000.0)
    with pytest.raises(reports.ReportError) as e:
        submit(clock=lambda: 1000.0)
    assert e.value.status == 429


def test_test_entries_stay_local_and_are_excluded():
    class FakeShared(cache.MemoryCache):       # stands in for Upstash: test entries must never land here
        name = "upstash"
    shared = FakeShared()
    cache.set_cache(shared)
    r = submit("cancelled", note="STAGING CHECKLIST TEST REPORT (automated)")
    assert r["test"] and r["storage"]["backend"] == "memory" and "test" in r["storage"]["note"].lower()
    assert shared.get(outcomes._venue_key(FIELD)) is None and shared.get("outcomes:all") is None
    assert outcomes.all_events() == [] and outcomes.venue_outcomes(FIELD) == []
    assert outcomes.venue_outcomes(FIELD, include_test=True)
    real = submit("played", client="5.6.7.8")
    assert real["storage"]["backend"] == "upstash" and len(shared.get(outcomes._venue_key(FIELD))) == 1


def test_memory_needs_min_count_and_is_bounded_and_explained():
    ev = lambda out, p=50, soak=20: {"outcome": out, "prediction": {"play_probability_percent": p, "soak_penalty": soak,
                                                                    "official_status": "unknown"}}
    m = outcomes.memory_from_events([ev("played"), ev("played")])
    assert not m["active"] and m["soak_multiplier"] == 1.0 and "2 of 2" in m["explain"]
    m = outcomes.memory_from_events([ev("played")] * 8)
    assert m["active"] and outcomes.MEMORY_BOUNDS[0] <= m["soak_multiplier"] < 1.0
    assert "played through 8 of 8 reported wet days" in m["explain"]
    m = outcomes.memory_from_events([ev("not_played", p=90)] * 8)
    assert m["active"] and m["soak_multiplier"] == outcomes.MEMORY_BOUNDS[1]
    dry = outcomes.memory_from_events([ev("played", soak=0)] * 8)
    assert dry["wet_events"] == 0 and not dry["active"]


def test_feed_and_scoreboard():
    snap(pct=85)
    submit("cancelled", client="a")
    submit("cancelled", client="b")
    submit("played", client="c")
    events = outcomes.all_events()
    assert len(events) == 1 and events[0]["outcome"] == "not_played" and events[0]["reports"] == 3
    board = outcomes.scoreboard()
    assert board["overall"]["events"] == 1 and board["overall"]["confident_wrong"] == 1
    atom = outcomes.feed_atom("https://x")
    assert atom.startswith("<?xml") and "we said 85% before the start" in atom
    assert "<table>" in outcomes.scoreboard_html()


def test_http_routes():
    status, headers, body = handle_outcomes_request("POST", "/v1/outcomes", "", json.dumps(
        {"field_id": "Downs", "date": "2026-06-07", "outcome": "played"}).encode(), "application/json", {}, "9.9.9.9", now=NOW)
    assert status == 201
    status, headers, body = handle_outcomes_request("POST", "/v1/outcomes", "", json.dumps(
        {"field_id": "Downs", "date": "2026-06-07", "outcome": "played"}).encode(), "application/json",
        {"User-Agent": "rainout-test-staging-checklist/1.0"}, "9.9.9.9", now=NOW)
    assert status == 201 and json.loads(body)["test"] is True
    assert handle_outcomes_request("GET", "/v1/outcomes", "", b"")[0] == 200
    assert handle_outcomes_request("GET", "/v1/outcomes", "field_id=Downs", b"")[0] == 200
    s, h, b = handle_outcomes_request("GET", "/v1/outcomes/feed.atom", "", b"")
    assert s == 200 and "atom" in h["Content-Type"]
    s, h, b = handle_outcomes_request("GET", "/scoreboard", "", b"")
    assert s == 200 and "text/html" in h["Content-Type"]
    assert handle_outcomes_request("PUT", "/v1/outcomes", "", b"")[0] == 405


def test_mcp_report_outcome_tool():
    from rainout_agent import mcp_server

    out = mcp_server.call_tool("report_outcome", {"field_id": "Downs", "outcome": "played", "date": datetime.now(timezone.utc).astimezone().date().isoformat()})
    assert not out.get("isError")

"""should_we_cancel: organizer decision layer on top of go_no_go."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import mcp_server, should_we_cancel as swc, weather
from rainout_agent.cache import MemoryCache, set_cache
from rainout_agent.construction_api import handle_construction_request
from tests.construction_fakes import NOW, SiteFake

CDT = timezone(timedelta(hours=-5))


def fake(table):
    """table(start_dt) -> (decision, percent[, official_status])."""
    calls = []

    def decide(activity, *, time=None, now=None, **kw):
        start = datetime.fromisoformat(time).astimezone(CDT)
        row = table(start)
        calls.append(start)
        return {"activity": activity, "decision": row[0], "probability_percent": row[1], "reason": f"{row[1]}% fake.",
                "event_confirmed": None, "weather_decision": row[0], "routed_to": "get_venue_status",
                "recheck_at": (start - timedelta(hours=12)).isoformat() if (start - now).total_seconds() > 30 * 3600
                else max(start - timedelta(hours=2), now + timedelta(minutes=30)).astimezone(CDT).isoformat(),
                "time": {"start": start.isoformat()}, "location": {"venue": "Fake Park"}, "sources": [{"name": "fake"}],
                "details": {"official_status": row[2] if len(row) > 2 else "unknown"}}
    decide.calls = calls
    return decide


def ask(hours_out, table, **kw):
    start = (NOW + timedelta(hours=hours_out)).astimezone(CDT).isoformat()
    d = fake(table)
    r = swc.should_we_cancel("softball", venue="Fake Park", time=start, now=NOW, decide=d, **kw)
    return r, d


def test_go_is_play():
    r, _ = ask(30, lambda t: ("go", 92))
    assert r["recommendation"] == "play" and r["play_probability_percent"] == 92 and r["check_again_at"]
    assert r["reason"].startswith("Play:") and r["based_on"]["tool"] == "go_no_go" and r["detail"] == "basic"


def test_far_out_no_go_waits():
    r, _ = ask(60, lambda t: ("no_go", 30))
    assert r["recommendation"] == "wait_until" and r["wait_until"] == r["check_again_at"]
    assert "Too early" in r["reason"]


def test_hopeless_within_a_day_cancels_early():
    r, _ = ask(20, lambda t: ("no_go", 15))
    assert r["recommendation"] == "cancel" and r["check_again_at"] is None


def test_near_no_go_delays_when_it_clears():
    start = (NOW + timedelta(hours=3)).astimezone(CDT)
    r, d = ask(3, lambda t: ("no_go", 30) if t < start + timedelta(hours=2) else ("go", 80))
    assert r["recommendation"] == "delay"
    assert datetime.fromisoformat(r["delay_to"]) == start + timedelta(hours=2)
    assert "Delay to" in r["reason"] and len(d.calls) == 3


def test_near_no_go_without_window_cancels():
    r, d = ask(3, lambda t: ("no_go", 20))
    assert r["recommendation"] == "cancel" and len(d.calls) == 1 + swc.DELAY_SCAN_HOURS


@pytest.mark.parametrize("p,expected", [(60, "play"), (45, "cancel")])
def test_last_call_iffy(p, expected):
    r, _ = ask(1, lambda t: ("wait", p))
    assert r["recommendation"] == expected


def test_iffy_far_out_waits_until_before_start():
    r, _ = ask(10, lambda t: ("wait", 50))
    start = NOW + timedelta(hours=10)
    assert r["recommendation"] == "wait_until"
    assert datetime.fromisoformat(r["wait_until"]) <= start - timedelta(hours=2)


def test_official_cancel_wins():
    r, _ = ask(5, lambda t: ("no_go", 0, "cancelled"))
    assert r["recommendation"] == "cancel" and "official status" in r["reason"]


def test_unlisted_race_never_says_play():
    d = fake(lambda t: ("no_event_found", 90))
    r = swc.should_we_cancel(track_id="swan-mx", time=(NOW + timedelta(hours=48)).isoformat(), now=NOW, decide=d)
    assert r["recommendation"] == "wait_until" and "can't find" in r["reason"]


def test_full_detail_needs_paid_key(monkeypatch):
    r, _ = ask(30, lambda t: ("go", 90), detail="full", headers={"X-API-Key": "rk_test_x"})
    assert r["detail"] == "basic" and "paid API key" in r["full_detail"]
    monkeypatch.setattr(swc, "_paid", lambda h: (True, None))
    r, _ = ask(30, lambda t: ("go", 90 - abs(t.hour - 12)), detail="full", headers={"X-API-Key": "rk_test_x"})
    assert r["detail"] == "full" and len(r["start_options"]) == 7
    assert r["best_start"]["play_probability_percent"] >= r["play_probability_percent"]
    assert r["notify_message"].startswith("We're ON") and r["decision_deadline"]


def test_paid_helper_never_raises(monkeypatch):
    import sys

    assert swc._paid(None) == (False, None)
    monkeypatch.setitem(sys.modules, "rainout_agent.api_keys", None)  # paid-tier PR not merged: basic only
    assert swc._paid({"X-API-Key": "rk_test_x"}) == (False, None)


def test_rest_route_and_cache_headers(monkeypatch):
    monkeypatch.setattr(swc.gng, "go_no_go", fake(lambda t: ("go", 88)))
    q = f"venue=Fake%20Park&time={(NOW + timedelta(hours=30)).astimezone(CDT).isoformat().replace('+', '%2B')}"
    monkeypatch.setattr(swc, "_now", lambda: NOW)
    status, _, body, cache = handle_construction_request("GET", "/v1/should-we-cancel", q)
    data = json.loads(body)
    assert status == 200 and data["recommendation"] == "play" and cache.startswith("public")
    status, _, body, cache = handle_construction_request("GET", "/v1/should-we-cancel", q + "&detail=full",
                                                         headers={"X-API-Key": "rk"})
    assert status == 200 and cache == "no-store"
    status, _, body, _ = handle_construction_request("GET", "/v1/should-we-cancel", "time=now")
    assert status == 400 and "team_id" in json.loads(body)["message"]


def test_mcp_tool(monkeypatch):
    monkeypatch.setattr(swc.gng, "go_no_go", fake(lambda t: ("no_go", 10)))
    monkeypatch.setattr(swc, "_now", lambda: NOW)
    out = mcp_server.call_tool("should_we_cancel", {"venue": "Fake Park",
                                                    "time": (NOW + timedelta(hours=12)).isoformat()})
    data = out.get("structuredContent") or json.loads(out["content"][0]["text"])
    assert data["recommendation"] == "cancel"
    out = mcp_server.call_tool("should_we_cancel", {"time": "now"})
    assert out["isError"]


def test_real_go_no_go_location_path(monkeypatch):
    set_cache(MemoryCache())
    monkeypatch.setattr(weather, "_http_get_json", SiteFake())
    r = swc.should_we_cancel("soccer", lat=30.27, lon=-97.74, time="2026-10-22 18:00", now=NOW)
    assert r["recommendation"] == "play" and r["based_on"]["routed_to"] == "weather_only_play_odds"
    wet = SiteFake(lambda t: {"pop": 95, "code": 95})
    monkeypatch.setattr(weather, "_http_get_json", wet)
    set_cache(MemoryCache())
    r = swc.should_we_cancel("soccer", lat=30.27, lon=-97.74, time="2026-10-21 10:00", now=NOW)
    assert r["recommendation"] == "cancel" and r["play_probability_percent"] < 40

"""Offline tests for the staging checklist (scripts/staging_checklist.py, staging/*)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from staging import backtest as bt
from staging import checks as C
from staging import report as R


def test_fixtures_are_sourced_and_well_formed():
    d = bt.load_fixtures()
    ids = [f["id"] for f in d["fixtures"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 20
    for f in d["fixtures"]:
        assert f["outcome"] in ("played", "not_played"), f["id"]
        assert f["field_type"] in ("turf", "grass_soccer", "dirt_infield", "football", "motocross", "construction"), f["id"]
        assert f["source_url"].startswith("https://"), f["id"]
        assert f["cause"], f["id"]
        datetime.fromisoformat(f["start"])
        if f["field_type"] == "motocross":
            assert f["track"] and f["end"]
        else:
            assert -90 <= f["lat"] <= 90 and -180 <= f["lon"] <= 180 and ZoneInfo(f["tz"])
    assert {f["field_type"] for f in d["fixtures"]} >= {"turf", "grass_soccer", "dirt_infield", "motocross", "construction"}
    assert d["excluded"], "unverifiable events must be listed, not dropped silently"


@pytest.mark.parametrize("outcome,p,result,cw", [
    ("played", 96, "correct", False), ("played", 55, "hedged", False), ("played", 15, "wrong", True),
    ("not_played", 10, "correct", False), ("not_played", 85, "wrong", True), ("not_played", 72, "wrong", False),
])
def test_grade(outcome, p, result, cw):
    g = bt.grade(outcome, p)
    assert g["result"] == result and g["confident_wrong"] is cw


def test_grade_uses_explicit_decision_and_no_call():
    assert bt.grade("not_played", 56, "wait")["result"] == "hedged"
    assert bt.grade("played", None)["result"] == "no_call"


def test_proxy_pop_and_band():
    assert [bt.proxy_pop(x) for x in (0, 0.2, 1, 5, 20)] == [5, 35, 60, 80, 95]
    assert (bt.band(70), bt.band(69), bt.band(39)) == ("go", "wait", "no_go")


def _rows(start: datetime, hours: int, **row):
    t = start.astimezone(timezone.utc).replace(minute=0)
    return {t + timedelta(hours=i): dict(row) for i in range(hours)}


def test_pick_rows_prefers_archived_forecast_then_proxies():
    s = datetime(2025, 5, 1, 18, tzinfo=ZoneInfo("America/Chicago"))
    e = s + timedelta(hours=2)
    wx = {"forecast": _rows(s, 3, pop=40, mm=0.0), "observed": {}}
    assert bt.pick_rows(wx, s, e)[1] == "archived_forecast"
    wx = {"forecast": _rows(s, 3, pop=None, mm=3.0), "observed": {}}
    rows, grade = bt.pick_rows(wx, s, e)
    assert grade == "forecast_amount_proxy" and rows[s.astimezone(timezone.utc)]["pop"] == 80
    wx = {"forecast": {}, "observed": _rows(s, 3, mm=0.0)}
    assert bt.pick_rows(wx, s, e)[1] == "observed_proxy"
    assert bt.pick_rows({"forecast": {}, "observed": {}}, s, e)[1] == "missing"


def test_score_sports_matches_production_model():
    from rainout_agent.play_probability import calculate_play_probability

    fx = {"start": "2025-05-01T18:00", "tz": "America/Chicago", "duration_h": 2}
    s = datetime(2025, 5, 1, 18, tzinfo=ZoneInfo("America/Chicago"))
    out = bt.score_sports(fx, {"forecast": _rows(s, 3, pop=50, mm=1.0, thunder=False), "observed": {}})
    assert out["p"] == calculate_play_probability("unknown", 50, False, 2.0, None)["play_probability_percent"] == 60
    assert out["weather_grade"] == "archived_forecast"


def test_behaviour_checks_and_report_precedence():
    beh = {b["check"]: b["ok"] for b in bt.behaviour_checks({"field_type": "turf"}, {"p": 60, "rain_chance_percent": 50})}
    assert beh == {"official_cancel_wins": True, "cancel_reports_lower_odds": True, "playing_reports_raise_odds": True}
    assert bt.report_precedence_check()["ok"] is True


def test_notice_replay_reads_a_dated_wordpress_post():
    from rainout_agent import motocross as mx

    track = {t["short_id"]: t for t in mx.tracks()}["freestone"]
    fx = {"start": "2024-05-18T17:00", "end": "2024-05-18T23:00", "event_name": "FMF Texas Night Series", "source_url": "https://freestonemx.com/latest-news/x/"}
    payload = [{"date": "2024-05-17T10:00:00", "link": "https://freestonemx.com/latest-news/x/", "title": {"rendered": "TNS May 18 Canceled"},
                "content": {"rendered": "<p>The Texas Night Series race on May 18 is canceled due to rain.</p>"}}]
    assert bt.notice_replay(fx, track, payload)["ok"] is True
    assert bt.notice_replay(fx, track, [])["ok"] is None


def _status(now: datetime, **over):
    j = {"field_id": "x", "field": "X", "official_status": "unknown", "official_status_check": {"outcome": "matched"},
         "official_status_last_checked": now.isoformat(), "last_checked": now.isoformat(), "game_time_play_probability_percent": 80,
         "rain_chance_percent": 10, "reason": "r", "verification_level": "confirmed", "weather_source_id": "nws",
         "forecast_period_start": (now + timedelta(hours=24)).isoformat(), "community_reports": {"count": 0},
         "official_status_source_url": "https://x", "official_status_source_name": "X"}
    j.update(over)
    return j


def test_validate_status():
    now = datetime.now(timezone.utc)
    gt = (now + timedelta(hours=24)).isoformat()
    assert C.validate_status(_status(now), gt, now) == []
    old = (now - timedelta(hours=5)).isoformat()
    probs = C.validate_status(_status(now, official_status_last_checked=old, game_time_play_probability_percent=140), gt, now)
    assert any("stale official" in p for p in probs) and any("bad probability" in p for p in probs)
    assert any("odds" in p for p in C.validate_status(_status(now, official_status="cancelled", game_time_play_probability_percent=30), gt, now))
    # a venue with no source is never "stale"
    assert C.validate_status(_status(now, official_status_last_checked=old, official_status_check={"outcome": "no_source"}), gt, now) == []


def test_valid_ics():
    good = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261021T180000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    assert C.valid_ics(good) == (True, 1)
    assert C.valid_ics("<html>")[0] is False
    assert C.valid_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR")[0] is False


def test_sample_confirmed_covers_every_source_and_city():
    fields = [{"field_id": f"f{i}", "city": c, "official_status_source_url": u, "status_source_type": "page"}
              for i, (c, u) in enumerate([("A", "https://one.org/x"), ("A", "https://one.org/y"), ("B", "https://two.org"), ("C", "")])]
    picked = {f["field_id"] for f in C.sample_confirmed(fields, "sample")}
    assert picked == {"f0", "f2", "f3"}
    assert len(C.sample_confirmed(fields, "full")) == 4


def test_switch_contract():
    now = datetime.now(timezone.utc)
    assert C.switch_contract(_status(now)) == []
    j = _status(now)
    del j["community_reports"]
    assert C.switch_contract(j) == ["community_reports"]


def _check(status, severity="blocker", cid="x"):
    return C.Check(cid, "a", "production", status, "d", "o", "f", severity).row()


def test_verdict():
    assert R.verdict([_check("pass", "")], None)[0] == "GREEN LIGHT"
    v, punch, gaps = R.verdict([_check("pass", ""), _check("not_built", "known_gap", "not_built:Email alerts")], None)
    assert v == "GREEN LIGHT" and gaps and not punch
    assert R.verdict([_check("fail")], None)[0] == "PUNCH LIST"
    assert R.verdict([_check("warn", "info")], None)[0] == "GREEN LIGHT"
    bad_bt = {"summary": {"overall": {"confident_wrong": 1, "directional_accuracy": 90}}, "precedence": {"ok": True, "detail": ""},
              "results": [{"id": "g", "p": 90, "outcome": "not_played", "confident_wrong": True, "behaviour": []}]}
    v, punch, _ = R.verdict([_check("pass", "")], bad_bt)
    assert v == "PUNCH LIST" and punch[0]["id"] == "backtest:confident_wrong"


def test_target_add_and_render():
    t = C.Target("https://example.invalid", "production", "production", http=lambda *a, **k: C.Resp(200, b"{}", {}, 0.1))
    t.add("ok", "core", True, "fine")
    t.add("bad", "core", False, "broken", owner="API", fix="fix it")
    md = R.render({"mode": "sample", "targets": [{"label": "production", "kind": "production", "base": t.base}]}, [c.row() for c in t.checks], None)
    assert md.startswith("# Rain-Outsource staging checklist: **PUNCH LIST**")
    assert "| 1 | **bad**" in md and "fix it" in md


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_switch_harness_with_minimal_script():
    js = """(function(){ var notifyTimer=null; var last={};
      document.addEventListener('DOMContentLoaded', function(){
        document.getElementById('notifybtn').addEventListener('click', function(){ Notification.requestPermission().then(function(){ notifyTimer=1; }); });
        document.getElementById('refreshbtn').addEventListener('click', function(){
          fetch('/v1/status?field_id=a').then(function(r){return r.json();}).then(function(d){
            if (last.s && notifyTimer && last.s !== d.official_status) new Notification('changed', {});
            if (!d.error) last.s = d.official_status; }); });
        fetch('/v1/status?field_id=a').then(function(r){return r.json();}).then(function(d){ if (!d.error) last.s = d.official_status; });
      }); })();"""
    res = C.run_switch_harness(js)
    by = {s["name"]: s["notified"] for s in res["scenarios"]}
    assert by["official status unknown -> cancelled"] is True and by["no change"] is False


def test_core_mcp_tools_exist_in_code():
    from rainout_agent.mcp_server import TOOLS

    assert set(C.CORE_MCP_TOOLS) <= {t["name"] for t in TOOLS}


def test_checklist_traffic_counts_as_internal_in_usage_metrics():
    from rainout_agent import metrics

    assert metrics.classify({"User-Agent": C.UA}) == "internal"

"""Community cancellation reports: validation, rate limits, storage and odds."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import cache, mcp_server, reports
from rainout_agent.reports_api import handle_reports_request
from rainout_agent.status_api import build_status_result, list_supported_fields

NOW = datetime(2026, 6, 5, 22, 0, tzinfo=timezone.utc)  # 5 pm CDT
GAME = "2026-06-05T19:00:00-05:00"
KRIEG = "austin-tx-krieg-field-softball-complex"
WEATHER = {"rain_chance_percent": 40, "thunderstorm_likely": False, "source": "test", "last_checked": "x"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    clock = {"t": NOW.timestamp()}
    reports._LOCAL_STORE.clear()
    cache.set_cache(cache.MemoryCache())
    monkeypatch.setattr(reports, "_utcnow", lambda: datetime.fromtimestamp(clock["t"], timezone.utc))
    monkeypatch.setattr(reports.time, "time", lambda: clock["t"])
    yield clock
    reports._LOCAL_STORE.clear()
    cache.set_cache(None)


def post(data, client="1.2.3.4", raw=None, ctype="application/json"):
    status, headers, body = handle_reports_request("POST", "", raw if raw is not None else json.dumps(data).encode(), ctype,
                                                   {"X-Forwarded-For": f"{client}, 10.0.0.1"})
    return status, headers, json.loads(body)


def status(official="unknown"):
    return build_status_result(field_query="Krieg", game_time=GAME, weather=dict(WEATHER), official_status=official)


def test_submit_and_read_back():
    code, _, data = post({"field_id": "Krieg", "status": "Rained out", "game_time": GAME, "note": "called at\n6:40"})
    assert code == 201 and data["accepted"]
    r = data["report"]
    assert r["field_id"] == KRIEG and r["status"] == "cancelled" and r["game_date"] == "2026-06-05" and r["note"] == "called at 6:40"
    assert data["recent"]["by_status"]["cancelled"] == 1 and data["storage"]["backend"] == "memory"
    code, _, body = handle_reports_request("GET", f"field_id={KRIEG}&game_time={GAME.replace('+', '%2B')}", b"")
    got = json.loads(body)
    assert code == 200 and got["summary"]["count"] == 1 and got["reports"][0]["id"] == r["id"]
    assert "1.2.3.4" not in json.dumps(got)  # no client address stored or echoed


@pytest.mark.parametrize("data,code,error", [
    ({"status": "cancelled"}, 400, "missing_field_id"),
    ({"field_id": "no such field anywhere zz", "status": "cancelled"}, 404, "unknown_field"),
    ({"field_id": "Krieg", "status": "maybe"}, 400, "invalid_status"),
    ({"field_id": "Krieg", "status": "cancelled", "game_time": "tomorrow"}, 400, "invalid_game_time"),
    ({"field_id": "Krieg", "status": "cancelled", "game_time": "2026-06-05T19:00:00"}, 400, "invalid_game_time"),
    ({"field_id": "Krieg", "status": "cancelled", "game_time": "2026-06-08T19:00:00-05:00"}, 400, "game_time_out_of_range"),
    ({"field_id": "Krieg", "status": "cancelled", "game_time": "2026-06-04T19:00:00-05:00"}, 400, "game_time_out_of_range"),
    ({"field_id": "Krieg", "status": "cancelled", "note": "x" * 281}, 400, "note_too_long"),
    ({"field_id": "Krieg", "status": "cancelled", "note": 5}, 400, "invalid_note"),
    (["Krieg"], 400, "invalid_body"),
])
def test_validation(data, code, error):
    got_code, _, body = post(data)
    assert (got_code, body["error"]) == (code, error)


def test_bad_json_big_body_and_methods():
    assert post(None, raw=b"{nope")[0] == 400
    assert post(None, raw=b"x" * 5000)[0] == 413
    assert handle_reports_request("DELETE", "", b"")[0] == 405
    assert handle_reports_request("OPTIONS", "", b"")[0] == 204
    assert handle_reports_request("GET", "", b"")[0] == 400
    form = post(None, raw=b"field_id=Krieg&status=delayed", ctype="application/x-www-form-urlencoded")
    assert form[0] == 201 and form[2]["report"]["status"] == "delayed"


def test_rate_limits(fresh):
    for _ in range(3):
        assert post({"field_id": "Krieg", "status": "cancelled"})[0] == 201
    code, headers, body = post({"field_id": "Krieg", "status": "cancelled"})
    assert code == 429 and body["error"] == "rate_limited" and int(headers["Retry-After"]) > 0
    assert post({"field_id": "Krieg", "status": "cancelled"}, client="5.6.7.8")[0] == 201  # other clients unaffected
    others = [f["field_id"] for f in list_supported_fields() if f["field_id"] != KRIEG][:8]
    for field in others[:7]:
        assert post({"field_id": field, "status": "playing"})[0] == 201
    assert post({"field_id": others[7], "status": "playing"})[2]["error"] == "rate_limited"  # 10 per hour overall
    fresh["t"] += 3600
    assert post({"field_id": "Krieg", "status": "cancelled"})[0] == 201


def test_reports_expire(fresh):
    post({"field_id": "Krieg", "status": "cancelled", "game_time": GAME})
    fresh["t"] += 13 * 3600  # still stored, but older than the 12 h odds window
    assert reports.summarize(KRIEG, "2026-06-05")["count"] == 0 and len(reports.recent_reports(KRIEG)) == 1
    fresh["t"] += 12 * 3600
    assert reports.recent_reports(KRIEG) == []


def test_adjust_probability_math():
    s = lambda **c: {"count": sum(c.values()), "by_status": {k: c.get(k, 0) for k in reports.STATUSES}}
    assert reports.adjust_probability(80, s()) == (80, None)
    assert reports.adjust_probability(80, s(cancelled=1))[0] == 60
    assert reports.adjust_probability(80, s(cancelled=9))[0] == 16  # capped at an 80% cut
    assert reports.adjust_probability(80, s(delayed=1))[0] == 70
    assert reports.adjust_probability(60, s(playing=1))[0] == 66
    assert reports.adjust_probability(60, s(playing=9))[0] == 80  # closes at most half the gap
    assert reports.adjust_probability(70, s(cancelled=1, playing=1)) == (70, None)


def test_status_odds_use_reports_only_when_official_unknown():
    base = status()
    assert base["community_reports"]["count"] == 0 and not base["community_reports"]["applied"]
    before = base["game_time_play_probability_percent"]
    post({"field_id": "Krieg", "status": "cancelled", "game_time": GAME})
    post({"field_id": "Krieg", "status": "cancelled", "game_time": GAME}, client="9.9.9.9")
    after = status()
    block = after["community_reports"]
    assert block["applied"] and block["count"] == 2 and block["play_probability_before_reports"] == before
    assert after["game_time_play_probability_percent"] == round(before * 0.5)
    assert "2 recent user report(s)" in after["spoken_answer"] and "user report" in after["reason"]
    official = status("on")
    assert official["community_reports"]["count"] == 2 and not official["community_reports"]["applied"]
    assert official["game_time_play_probability_percent"] == status("on")["game_time_play_probability_percent"]
    other_day = build_status_result(field_query="Krieg", game_time="2026-06-06T19:00:00-05:00", weather=dict(WEATHER),
                                    official_status="unknown")
    assert other_day["community_reports"]["count"] == 0


def test_shared_backend_is_used_when_configured():
    class Shared(cache.MemoryCache):
        name = "upstash"

    shared = Shared()
    cache.set_cache(shared)
    post({"field_id": "Krieg", "status": "field_closed"})
    assert shared.get(f"reports:{KRIEG}")[0]["status"] == "field_closed" and len(reports._LOCAL_STORE) == 0


def rpc_call(args, ip="7.7.7.7"):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "report_cancellation", "arguments": args}}
    _, _, body = mcp_server.handle_http("POST", {"X-Forwarded-For": ip}, json.dumps(msg).encode())
    return json.loads(body)["result"]


def test_mcp_report_tool():
    res = rpc_call({"field_id": "Krieg", "status": "cancelled", "game_time": GAME})
    assert res["isError"] is False and res["structuredContent"]["report"]["status"] == "cancelled"
    bad = rpc_call({"field_id": "Krieg", "status": "nope"})
    assert bad["isError"] and "status must be one of" in bad["content"][0]["text"]
    for _ in range(2):
        rpc_call({"field_id": "Krieg", "status": "cancelled"})
    limited = rpc_call({"field_id": "Krieg", "status": "cancelled"})
    assert limited["isError"] and "Retry after" in limited["content"][0]["text"]
    assert rpc_call({"field_id": "Krieg", "status": "cancelled"}, ip="8.8.8.8")["isError"] is False


def test_vercel_status_function_serves_reports():
    import importlib
    from pathlib import Path

    routes = {r["src"]: r["dest"] for r in json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())["routes"]}
    assert routes["/v1/reports/?"] == "/api/status.py"
    mod = importlib.import_module("api.status")
    body = json.dumps({"field_id": "Krieg", "status": "cancelled"}).encode()
    h = mod.handler.__new__(mod.handler)
    h.path, h.client_address = "/v1/reports", ("4.4.4.4", 1)
    h.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    h.rfile, h.wfile, sent = io.BytesIO(body), io.BytesIO(), {}
    h.send_response = lambda code, *a: sent.__setitem__("code", code)
    h.send_header = lambda k, v: sent.__setitem__(k, v)
    h.end_headers = lambda: None
    h.do_POST()
    assert sent["code"] == 201 and sent["Access-Control-Allow-Origin"] == "*"
    assert json.loads(h.wfile.getvalue())["report"]["field_id"] == KRIEG

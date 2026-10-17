"""MCP Streamable HTTP server: protocol handling and tools (league data from fixtures)."""
import io
import json
from pathlib import Path

import pytest

from rainout_agent import mcp_server, team_sources as ts
from tests.test_teams import ATX_BOYS, GENERALS, FakeLeagueHTTP, make_checker


def rpc(method, params=None, msg_id=1, headers=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    status, hdrs, body = mcp_server.handle_http("POST", {"Accept": "application/json, text/event-stream", **(headers or {})},
                                               json.dumps(msg).encode())
    return status, hdrs, (json.loads(body) if body else None)


@pytest.fixture
def league_http(monkeypatch):
    fake = FakeLeagueHTTP()
    monkeypatch.setattr(ts, "_http_get", fake)
    from rainout_agent import teams

    real = teams.score_games
    monkeypatch.setattr(teams, "score_games", lambda team, raw, now, checker=None: real(team, raw, now=now, checker=make_checker()))
    return fake


def test_initialize_negotiates_version_and_gives_instructions():
    status, hdrs, data = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert status == 200 and hdrs["Content-Type"] == "application/json"
    res = data["result"]
    assert res["protocolVersion"] == "2025-06-18" and hdrs["MCP-Protocol-Version"] == "2025-06-18"
    assert res["capabilities"]["tools"] == {"listChanged": False}
    assert res["serverInfo"]["name"] == "rainout-source"
    assert "asking the user first" in res["instructions"] and "confirm" in res["instructions"]
    _, _, data = rpc("initialize", {"protocolVersion": "1999-01-01"})
    assert data["result"]["protocolVersion"] == mcp_server.SUPPORTED_VERSIONS[0]


def test_notifications_get_202_and_ping_works():
    status, _, body = rpc("notifications/initialized", msg_id=None)
    assert status == 202 and body is None
    assert rpc("ping")[2]["result"] == {}


def test_tools_list_has_ten_read_only_tools_with_calendar_rule():
    tools = {t["name"]: t for t in rpc("tools/list")[2]["result"]["tools"]}
    assert set(tools) == {"find_team", "get_games", "check_game", "get_venue_status", "check_schedule",
                          "find_track", "get_track_events", "check_race", "get_track_history", "get_rain_chance"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert "anyOf" not in t["inputSchema"] and "oneOf" not in t["inputSchema"]
        assert t["annotations"]["readOnlyHint"] is True
    assert "Confirm the team match with the user" in tools["find_team"]["description"]
    assert "schedule_url" in tools["find_team"]["description"]
    for name in ("get_games", "check_game"):
        assert "ALWAYS ask the user before adding" in tools[name]["description"]


def test_find_team_tool(league_http):
    _, _, data = rpc("tools/call", {"name": "find_team", "arguments": {"query": "ATX Boys", "venue": "Craig Fields", "day": "friday"}})
    res = data["result"]
    assert res["isError"] is False
    assert res["structuredContent"]["candidates"][0]["team_id"] == ATX_BOYS
    assert json.loads(res["content"][0]["text"])["next_step"] == "confirm_with_user"


def test_find_team_unknown_asks_for_link(league_http):
    res = rpc("tools/call", {"name": "find_team", "arguments": {"query": "Mudcats"}})[2]["result"]
    assert res["structuredContent"]["next_step"] == "ask_user_for_schedule_url"


def test_get_games_and_check_game_tools(league_http):
    res = rpc("tools/call", {"name": "get_games", "arguments": {"team_id": GENERALS, "from": "2026-10-01", "to": "2026-10-31"}})[2]["result"]
    games = res["structuredContent"]["games"]
    assert any(g["league_status"] == "cancelled" for g in games)
    assert all("calendar_event" in g for g in games)
    res = rpc("tools/call", {"name": "check_game", "arguments": {"team_id": ATX_BOYS, "date": "2026-10-16"}})[2]["result"]
    sc = res["structuredContent"]
    assert sc["game"]["opponent"] == "The Vatos" and sc["answer"].startswith("Yes: ")
    assert sc["game"]["calendar_event"]["iCalUID"].endswith("@rainout-agent-source.vercel.app")


def test_tool_errors_are_reported_to_the_model(league_http):
    res = rpc("tools/call", {"name": "get_games", "arguments": {}})[2]["result"]
    assert res["isError"] is True and "team_id is required" in res["content"][0]["text"]
    res = rpc("tools/call", {"name": "get_games", "arguments": {"team_id": "tsl-340-744621-1"}})[2]["result"]
    assert res["isError"] is True and "find_team" in res["content"][0]["text"]
    res = rpc("tools/call", {"name": "check_game", "arguments": {}})[2]["result"]
    assert res["isError"] is True
    data = rpc("tools/call", {"name": "nope", "arguments": {}})[2]
    assert data["error"]["code"] == -32602


def test_source_outage_tells_agent_to_ask_for_link(monkeypatch):
    monkeypatch.setattr(ts, "_http_get", FakeLeagueHTTP(fail=True))
    res = rpc("tools/call", {"name": "get_games", "arguments": {"team_id": ATX_BOYS}})[2]["result"]
    assert res["isError"] is True and "schedule link" in res["content"][0]["text"]


def test_get_venue_status_tool(monkeypatch):
    monkeypatch.setattr("rainout_agent.status_api.fetch_nws_weather", lambda field, gt: {
        "rain_chance_percent": 5, "thunderstorm_likely": False, "source": "National Weather Service API", "source_id": "nws"})
    monkeypatch.setattr("rainout_agent.status_api.fetch_official_rainout_status", lambda field: {"official_status": "unknown", "checked": False})
    res = rpc("tools/call", {"name": "get_venue_status", "arguments": {"venue": "Craig Fields", "game_time": "2026-10-16T20:20:00-05:00"}})[2]["result"]
    assert res["structuredContent"]["field_id"] == "austin-tx-krieg-field-softball-complex"
    res = rpc("tools/call", {"name": "get_venue_status", "arguments": {"venue": "Nowhere Park", "game_time": "2026-10-16T20:20:00-05:00"}})[2]["result"]
    assert res["isError"] is True


def test_check_schedule_tool(monkeypatch):
    from rainout_agent import schedule

    monkeypatch.setattr(schedule, "fetch_hourly_forecast", lambda field, timeout: {"periods": [], "source": "x", "source_id": "nws"})
    res = rpc("tools/call", {"name": "check_schedule", "arguments": {"games": [{"start": "2030-05-01T19:00:00-05:00", "venue": "Krieg"}]}})[2]["result"]
    assert res["isError"] is False and res["structuredContent"]["summary"]["games_in_input"] == 1
    assert rpc("tools/call", {"name": "check_schedule", "arguments": {}})[2]["result"]["isError"] is True


def test_http_transport_edges(monkeypatch):
    status, hdrs, body = mcp_server.handle_http("GET", {}, b"")
    assert status == 405 and hdrs["Allow"] == "POST, OPTIONS"
    assert mcp_server.handle_http("OPTIONS", {}, b"")[0] == 204
    assert mcp_server.handle_http("POST", {}, b"{bad")[0] == 400
    assert json.loads(mcp_server.handle_http("POST", {}, b'{"x":1}')[2])["error"]["code"] == -32600
    status, _, body = mcp_server.handle_http("POST", {}, json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                                                     {"jsonrpc": "2.0", "method": "notifications/initialized"}]).encode())
    assert status == 200 and json.loads(body) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert mcp_server.handle_http("POST", {}, b"x" * (mcp_server.MAX_BODY + 1))[0] == 413
    assert rpc("resources/list")[2]["result"] == {"resources": []}
    assert rpc("foo/bar")[2]["error"]["code"] == -32601


def test_origin_guard(monkeypatch):
    ok = lambda origin: mcp_server.handle_http("POST", {"Origin": origin}, b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0]
    assert ok("https://chatgpt.com") == 200 and ok("https://claude.ai") == 200
    assert ok("http://localhost:3000") == 403 and ok("http://127.0.0.1:8080") == 403 and ok("http://192.168.1.5") == 403
    monkeypatch.setenv("RAINOUT_MCP_ALLOW_LOCAL", "1")
    assert ok("http://localhost:3000") == 200
    monkeypatch.setenv("RAINOUT_MCP_ALLOWED_ORIGINS", "https://a.example")
    assert ok("https://a.example") == 200 and ok("https://b.example") == 403


def test_vercel_mcp_handler_and_routes():
    import api.mcp as mod

    cfg = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
    srcs = {r["src"]: r["dest"] for r in cfg["routes"]}
    assert srcs["/mcp/?"] == "/api/mcp.py" and srcs["/llms\\.txt"] == "/api/llms.py"
    body = b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}'
    h = mod.handler.__new__(mod.handler)
    h.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    h.rfile, h.wfile, sent = io.BytesIO(body), io.BytesIO(), {}
    h.send_response = lambda code, *a: sent.__setitem__("code", code)
    h.send_header = lambda k, v: sent.__setitem__(k, v)
    h.end_headers = lambda: None
    h.do_POST()
    assert sent["code"] == 200 and sent["Cache-Control"] == "no-store"
    assert len(json.loads(h.wfile.getvalue())["result"]["tools"]) == 10


def test_wsgi_mcp_and_llms_txt():
    from rainout_agent.http_app import application

    out = {}

    def sr(status, headers):
        out["status"], out["headers"] = status, dict(headers)

    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    resp = b"".join(application({"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "QUERY_STRING": "", "CONTENT_LENGTH": str(len(body)),
                                 "wsgi.input": io.BytesIO(body), "HTTP_ACCEPT": "application/json, text/event-stream"}, sr))
    assert out["status"].startswith("200") and json.loads(resp)["result"] == {}
    text = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/llms.txt", "QUERY_STRING": ""}, sr)).decode()
    assert out["headers"]["Content-Type"].startswith("text/plain")
    assert text.startswith("# Rainout Source") and "/mcp" in text and "ask the user before adding" in text

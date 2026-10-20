import io
import json
from datetime import datetime, timezone

import pytest

from rainout_agent import cache, metrics
from rainout_agent.admin_api import handle_admin_usage

ADMIN = "a" * 24


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    metrics.LOCAL.__init__()
    metrics._seen.clear()
    metrics._expired.clear()
    monkeypatch.setenv("RAINOUT_INTERNAL_TOKEN", "internal-secret-123")
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "salt-xyz")
    monkeypatch.setenv("RAINOUT_ADMIN_TOKEN", ADMIN)


def test_classify():
    assert metrics.classify({"X-Rainout-Internal": "internal-secret-123"}) == "internal"
    assert metrics.classify({"X-Rainout-Internal": "wrong", "User-Agent": "curl/8"}) == "external"
    assert metrics.classify({"User-Agent": "rainout-smoke/1.0"}) == "internal"
    assert metrics.classify({"User-Agent": "Mozilla/5.0 (compatible; UptimeRobot/2.0)"}) == "monitor"
    assert metrics.classify({"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}) == "bot"
    assert metrics.classify({"User-Agent": "python-httpx/0.27"}) == "external"
    assert metrics.classify({}) == "external"


def test_endpoint_labels_are_bounded():
    assert metrics.endpoint_for("/v1/status?x=1") == "status"
    assert metrics.endpoint_for("/v1/teams/tsl-1-2/games") == "teams.games"
    assert metrics.endpoint_for("/v1/teams/abc/calendar.ics") == "teams.calendar"
    assert metrics.endpoint_for("/v1/tracks/t1/check") == "tracks.check"
    assert metrics.endpoint_for("/mcp") == "mcp"
    assert metrics.endpoint_for("/wp-admin/../etc") == "other"


def test_daily_chicago_date():
    late = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)  # 10:30 PM Oct 19 in Chicago
    assert metrics.today(late) == "2026-10-19"


def test_commands_per_request_and_no_raw_ip():
    h = {"User-Agent": "agent/1", "X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    metrics.record("status", h, background=False)
    first = list(metrics.LAST_COMMANDS)
    ops = [c[0] for c in first]
    assert ops.count("HINCRBY") == 1 and ops.count("PFADD") == 1 and ops.count("EXPIRE") == 2
    assert "203.0.113.7" not in json.dumps(first)
    metrics.record("status", h, background=False)
    assert [c[0] for c in metrics.LAST_COMMANDS] == ["HINCRBY"]  # repeat client: 1 command
    metrics.record("status", {"User-Agent": "rainout-smoke/1"}, background=False)
    assert [c[0] for c in metrics.LAST_COMMANDS] == ["HINCRBY"]  # internal: no unique tracking


def test_hash_rotates_daily():
    assert metrics.client_hash("1.2.3.4", "2026-10-19") != metrics.client_hash("1.2.3.4", "2026-10-20")
    assert metrics.client_hash("1.2.3.4", "2026-10-19") == metrics.client_hash("1.2.3.4", "2026-10-19")


def test_counting_never_raises(monkeypatch):
    def boom(cmds):
        raise RuntimeError("redis down")
    monkeypatch.setattr(metrics, "_run", boom)
    metrics.record("status", {"User-Agent": "x"}, background=False)  # no exception
    monkeypatch.setattr(metrics, "classify", lambda h: 1 / 0)
    assert metrics.record("status", {}) is None


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS", "off")
    assert metrics.record("status", {}, background=False) is None
    assert metrics.LOCAL.hashes == {}


def test_upstash_single_pipeline_call(monkeypatch):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, json.loads(body)))
        return 200, json.dumps([{"result": 1}] * len(json.loads(body))).encode()

    up = cache.UpstashCache("https://up.example", "tok", transport=transport)
    tiered = cache.TieredCache(cache.MemoryCache(), up)
    monkeypatch.setattr(metrics, "get_cache", lambda: tiered)
    metrics.record("mcp", {"User-Agent": "a"}, client="198.51.100.1", tools=["get_status", "check_game"],
                   background=False)
    assert len(calls) == 1 and calls[0][1] == "https://up.example/pipeline"
    assert [c[0] for c in calls[0][2]].count("HINCRBY") == 3


def test_admin_usage_json_and_text(monkeypatch):
    for ip in ("1.1.1.1", "2.2.2.2", "1.1.1.1"):
        metrics.record("status", {"User-Agent": "agent", "X-Forwarded-For": ip}, background=False)
    metrics.record("mcp", {"User-Agent": "agent", "X-Forwarded-For": "1.1.1.1"}, tools=["get_status"],
                   background=False)
    metrics.record("health", {"X-Rainout-Internal": "internal-secret-123"}, background=False)
    metrics.record("health", {"User-Agent": "UptimeRobot/2.0"}, client="9.9.9.9", background=False)
    metrics.record("keys", {"User-Agent": "agent"}, events=["free_key_signup"], background=False)
    status, ctype, body = handle_admin_usage("days=2", {"Authorization": f"Bearer {ADMIN}"})
    assert status == 200
    data = json.loads(body)
    day = data["daily"][0]
    assert day["requests"]["external"] == {"status": 3, "mcp": 1, "keys": 1}
    assert day["requests_total"] == {"external": 5, "internal": 1, "monitor": 1, "bot": 0}
    assert day["mcp_tools"]["external"] == {"get_status": 1}
    assert day["unique_clients"]["external"] == 2 and day["unique_clients"]["monitor"] == 1
    assert day["free_keys_signed_up"] == 1 and len(data["daily"]) == 2
    status, ctype, body = handle_admin_usage("days=1&format=text", {"X-Admin-Token": ADMIN})
    assert status == 200 and ctype.startswith("text/plain")
    text = body.decode()
    assert "Requests: 5 outside | 1 ours | 1 monitors | 0 bots" in text
    assert "Unique outside clients: 2" in text and "Free keys signed up: 1" in text


def test_admin_auth(monkeypatch):
    assert handle_admin_usage("", {})[0] == 401
    assert handle_admin_usage("", {"Authorization": "Bearer nope"})[0] == 401
    assert handle_admin_usage("days=abc", {"X-Admin-Token": ADMIN})[0] == 400
    monkeypatch.delenv("RAINOUT_ADMIN_TOKEN")
    assert handle_admin_usage("", {"X-Admin-Token": ADMIN})[0] == 503


def test_instrumented_handler_counts_and_captures_tools():
    from api import mcp as mcp_api

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

    class Fake(mcp_api.handler):
        def __init__(self):
            self.path = "/mcp"
            self.headers = {"Content-Length": str(len(body)), "User-Agent": "agent", "Content-Type": "application/json"}
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.client_address = ("192.0.2.5", 1)
            self.requestline = "POST /mcp HTTP/1.1"
            self.request_version = "HTTP/1.1"
            self.command = "POST"

        def log_message(self, *a):
            pass

    class H(dict):
        def items(self):
            return super().items()

        def get(self, k, d=None):
            for kk, v in super().items():
                if kk.lower() == k.lower():
                    return v
            return d

    f = Fake()
    f.headers = H(f.headers)
    f.do_POST()
    assert ["HINCRBY", f"{cache.KEY_PREFIX}metrics:{metrics.today()}", "req|mcp|external", 1] in metrics.LAST_COMMANDS

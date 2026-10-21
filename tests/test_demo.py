import io
import json

import pytest

from rainout_agent import demo

FAKE_STATUS = {"field": "Krieg Field Softball Complex", "official_status": "unknown",
               "official_status_source_url": "https://www.austintexas.gov/department/athletics",
               "official_status_source_name": "Austin Parks and Recreation Athletics page",
               "game_time_play_probability_percent": 91, "rain_chance_percent": 10,
               "thunderstorm_likely": False, "forecast": "Mostly Clear", "weather_source": "National Weather Service API"}


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    monkeypatch.delenv("RAINOUT_DEMO_NOTIFY_TO", raising=False)
    monkeypatch.setattr(demo, "weather_card", lambda gt: dict(FAKE_STATUS))
    demo.reset_local()
    yield
    demo.reset_local()


def _post(path, data, accept="application/json"):
    body = json.dumps(data).encode()
    return demo.handle("POST", path, "", body, "application/json", accept)


def test_page_has_banner_official_first_and_estimate_label():
    code, headers, body = demo.handle("GET", "/demo")
    text = body.decode()
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "DEMO / staging" in text and "estimates only" in text
    assert "ATX Boys" in text and 'name="viewport"' in text and "noindex" in text
    assert text.index("Official status") < text.index("ESTIMATE") < text.index("Make the call")
    assert "91% chance the game is played" in text and "UNKNOWN" in text


def test_one_tap_call_creates_timestamped_record_shown_in_central_time():
    code, _, body = _post("/demo/call", {"call": "rainout"})
    rec = json.loads(body)["record"]
    assert code == 201 and rec["demo"] and rec["league"] == "ATX Boys" and rec["call_label"] == "Rained out"
    assert rec["recorded_at"].endswith("+00:00") and rec["recorded_at_ct"].endswith(" CT")
    calls = json.loads(demo.handle("GET", "/demo/calls")[2])["calls"]
    assert calls[0]["id"] == rec["id"]
    assert rec["recorded_at_ct"] in demo.handle("GET", "/demo")[2].decode()


def test_form_post_redirects_back_with_flash():
    code, headers, _ = demo.handle("POST", "/demo/call", "", b"call=on", "application/x-www-form-urlencoded", "text/html")
    assert code == 303 and headers["Location"].startswith("/demo?flash=Saved")


def test_bad_call_and_bad_time_are_400():
    assert _post("/demo/call", {"call": "maybe"})[0] == 400
    assert _post("/demo/call", {"call": "on", "game_time": "2026-10-21T19:00"})[0] == 400
    assert demo.handle("GET", "/demo/call")[0] == 405


def test_ct_conversion():
    assert demo.ct("2026-10-21T18:31:05+00:00") == "Wed Oct 21, 1:31:05 PM CT"


def test_notification_is_a_stub_for_jimmy_only(monkeypatch):
    monkeypatch.setenv("RAINOUT_DEMO_NOTIFY_TO", "jimmy@example.com")
    _post("/demo/call", {"call": "delay"})
    # a recipient in the request is ignored
    code, _, body = _post("/demo/notify", {"to": "someone@else.com"})
    msg = json.loads(body)["message"]
    assert code == 201 and msg["to_label"] == "Jimmy (owner) only" and msg["to"] == "j***@example.com"
    assert "not sent" in msg["status"] and "Delayed" in msg["body"] and msg["subject"].startswith("[TEST]")
    out = json.loads(demo.handle("GET", "/demo/outbox")[2])
    assert out["sent"] is False and out["messages"][0]["id"] == msg["id"]
    assert "someone@else.com" not in json.dumps(out)


def test_demo_is_off_in_production(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    for path in demo.ROUTES:
        assert demo.handle("GET", path)[0] == 404
    assert _post("/demo/call", {"call": "on"})[0] == 404


def test_demo_writes_only_use_demo_keys_on_preview(monkeypatch):
    sent = []

    class FakeRedis:
        name = "memory+upstash"

        def pipeline(self, cmds):
            sent.extend(cmds)
            return [[] for _ in cmds]

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setattr("rainout_agent.cache.get_cache", lambda: FakeRedis())
    _post("/demo/call", {"call": "on"})
    _post("/demo/notify", {})
    assert sent and all(str(c[1]).startswith("rainout:demo:") for c in sent)
    assert any(c[0] == "EXPIRE" for c in sent)
    monkeypatch.setenv("VERCEL_ENV", "development")
    assert demo._redis() is None


def test_metrics_off_on_preview(monkeypatch):
    from rainout_agent import metrics

    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert metrics.enabled() is False
    monkeypatch.setenv("RAINOUT_METRICS_PREVIEW", "on")
    assert metrics.enabled() is True
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.delenv("RAINOUT_METRICS_PREVIEW")
    assert metrics.enabled() is True


def test_vercel_index_function_serves_demo_get_and_post():
    from pathlib import Path

    from api.index import handler

    routes = {r["src"]: r["dest"] for r in json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())["routes"]}
    assert routes["/demo(/.*)?"] == "/api/index.py"

    def run(method, path, body=b"", ctype=""):
        h = handler.__new__(handler)
        h.path, h.client_address = path, ("127.0.0.1", 1)
        h.headers = {"Content-Length": str(len(body)), "Content-Type": ctype, "Accept": "application/json"}
        h.rfile, h.wfile, sent = io.BytesIO(body), io.BytesIO(), {}
        h.send_response = lambda code, *a: sent.__setitem__("code", code)
        h.send_header = lambda k, v: sent.__setitem__(k, v)
        h.end_headers = lambda: None
        getattr(h, "do_" + method)()
        return sent, h.wfile.getvalue()

    sent, _ = run("POST", "/demo/call", json.dumps({"call": "on"}).encode(), "application/json")
    assert sent["code"] == 201
    sent, body = run("GET", "/demo")
    assert sent["code"] == 200 and b"ATX Boys" in body

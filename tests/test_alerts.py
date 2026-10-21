import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rainout_agent import alerts, alerts_api, reports
from rainout_agent.cache import MemoryCache

CT = timezone(timedelta(hours=-5))
START = datetime(2026, 10, 23, 19, 0, tzinfo=CT)          # Fri 7 PM local
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=CT)            # Wed noon, two days before


@pytest.fixture(autouse=True)
def store(monkeypatch):
    mem = MemoryCache(max_entries=5000)
    monkeypatch.setattr(reports, "_store", lambda: mem)
    monkeypatch.setattr(reports, "_LOCAL_STORE", mem)
    monkeypatch.delenv("RAINOUT_ALERTS_LIVE", raising=False)
    for var in ("RAINOUT_INTERNAL_TOKEN", "CRON_SECRET", "RAINOUT_DISPATCH_KEY"):
        monkeypatch.delenv(var, raising=False)
    alerts.set_email_sender(None)
    return mem


class FakeEngine:
    """Stands in for schedule.check_schedule: no network, controllable odds and official status."""

    def __init__(self, pct=90, official="unknown"):
        self.pct, self.official, self.calls = pct, official, 0

    def __call__(self, games, now=None, window_days=14, **kw):
        self.calls += 1
        out = []
        for i, g in enumerate(games):
            start = g["start"] if isinstance(g["start"], datetime) else datetime.fromisoformat(str(g["start"]))
            out.append({"index": i, "uid": f"g{i}@test", "title": g.get("title") or "Game", "start": start.isoformat(),
                        "venue": {"field_id": "austin-tx-krieg-field-softball-complex", "name": "Krieg Field Softball Complex"},
                        "status": "evaluated", "official_status": self.official, "play_probability_percent": self.pct,
                        "rain_chance_percent": 100 - self.pct, "reason": "Test reason.", "rainout_phone": "512-978-2680",
                        "recommendation": "check official source", "at_risk": self.pct < 60})
        return {"generated_at": (now or NOW).isoformat(), "games": out, "summary": {"evaluated": len(out)}}


def sub(channel="webhook", **extra):
    data = {"channel": channel, "field_id": "Krieg", "game_time": START.isoformat(), "title": "Hawks vs Owls", **extra}
    if channel == "webhook":
        data.setdefault("webhook_url", "https://hooks.example.org/rain")
    if channel == "email":
        data.setdefault("email", "Coach@Example.org")
    return alerts.subscribe(data, client="203.0.113.9", now=NOW)


def run(at, engine, poster=None):
    return alerts.dispatch(now=at, checker=engine, webhook_poster=poster or (lambda *a, **k: pytest.fail("no real POST in tests")))


# ------------------------------------------------------------------ subscribe / privacy
def test_webhook_subscription_stores_minimum_and_returns_secret_once(store):
    res = sub()
    assert res["status"] == "active" and res["mode"] == "test"
    assert res["webhook_secret"].startswith("whsec_") and res["token"]
    rec = alerts.load(res["id"])
    assert rec["token_hash"] != res["token"] and res["token"] not in json.dumps(rec)   # only the hash is stored
    assert set(rec) <= {"id", "channel", "created_at", "sent", "last", "confirmed", "kind", "field_id", "game_time", "title",
                        "expires_at", "webhook_url", "webhook_secret", "token_hash", "unsub_token", "test"}
    assert datetime.fromisoformat(rec["expires_at"]) == START + timedelta(days=1)


@pytest.mark.parametrize("url", ["http://hooks.example.org/x", "https://127.0.0.1/x", "https://10.1.2.3/x",
                                 "https://169.254.169.254/latest", "https://localhost/x", "https://foo.internal/x",
                                 "https://user:pw@example.org/x", "https://example.org:8443/x", "ftp://example.org/x"])
def test_webhook_url_safety(url):
    with pytest.raises(alerts.AlertError) as err:
        alerts.validate_webhook_url(url)
    assert err.value.error in {"unsafe_webhook_url", "invalid_webhook_url"}


def test_default_poster_refuses_private_targets_without_network():
    res = alerts.post_webhook("https://127.0.0.1/hook", b"{}", "s", sleep=lambda s: None)
    assert res == {"delivered": False, "attempts": [res["attempts"][0]], "gave_up": "unsafe_url"}


@pytest.mark.parametrize("bad", [{"channel": "sms"}, {"channel": "email", "email": "nope", "field_id": "Krieg", "game_time": START.isoformat()},
                                 {"channel": "webhook", "webhook_url": "https://hooks.example.org", "field_id": "Krieg", "game_time": "tomorrow"},
                                 {"channel": "calendar", "field_id": "No Such Field Anywhere", "game_time": START.isoformat()},
                                 {"channel": "calendar", "ics_url": "https://192.168.1.5/cal.ics"}])
def test_subscribe_rejects_bad_input(bad):
    with pytest.raises(alerts.AlertError):
        alerts.subscribe(bad, now=NOW)


def test_subscribe_rate_limit():
    for _ in range(alerts.SUBSCRIBE_PER_HOUR):
        alerts.subscribe({"channel": "calendar", "field_id": "Krieg", "game_time": START.isoformat()}, client="198.51.100.1", now=NOW, clock=lambda: 0)
    with pytest.raises(alerts.AlertError) as err:
        alerts.subscribe({"channel": "calendar", "field_id": "Krieg", "game_time": START.isoformat()}, client="198.51.100.1", now=NOW, clock=lambda: 0)
    assert err.value.status == 429


# ------------------------------------------------------------------ email stub + double opt-in
def test_email_is_stub_double_opt_in_and_never_sends():
    res = sub("email")
    assert res["status"] == "pending_confirmation"
    box = alerts.outbox()
    assert box[0]["trigger"] == "confirm" and box[0]["result"] == {"sent": False, "stub": True, "sender": "stub"}
    assert box[0]["to"] == "coach@example.org" and "/v1/alerts/confirm?id=" in box[0]["text"]
    assert "Unsubscribe" in box[0]["text"]
    engine = FakeEngine()
    assert run(START - timedelta(hours=2), engine)["skipped_unconfirmed"] == 1 and engine.calls == 0
    code = box[0]["text"].split("code=")[1].split()[0]
    with pytest.raises(alerts.AlertError):
        alerts.confirm(res["id"], "wrong-code")
    assert alerts.confirm(res["id"], code)["confirmed"]
    out = run(START - timedelta(hours=2), engine)
    assert out["sent"] == 1 and out["email_sender"] == "stub"
    msg = alerts.outbox()[0]
    assert msg["trigger"] == "three_hours" and msg["result"]["sent"] is False and msg["test_mode"] is True
    assert "90% chance of play" in msg["subject"] and "Unsubscribe: " in msg["text"]


def test_custom_email_sender_is_pluggable():
    sent = []

    class Fake:
        name = "fake"

        def send(self, message):
            sent.append(message)
            return {"sent": True, "sender": "fake"}

    alerts.set_email_sender(Fake())
    sub("email")
    assert sent and sent[0]["to"] == "coach@example.org" and sent[0]["subject"].startswith("Confirm")


# ------------------------------------------------------------------ triggers
def test_day_before_three_hours_and_no_duplicates():
    res = sub()
    engine = FakeEngine()
    assert run(NOW, engine)["sent"] == 0                                  # two days out: nothing due
    day_before = START.replace(day=22, hour=18, minute=5)
    out = run(day_before, engine)
    assert out["by_trigger"]["day_before"] == 1
    assert run(day_before + timedelta(minutes=10), engine)["sent"] == 0   # once only
    out = run(START - timedelta(hours=2, minutes=50), engine)
    assert out["by_trigger"]["three_hours"] == 1
    assert run(START - timedelta(hours=1), engine)["sent"] == 0
    rec = alerts.load(res["id"])
    assert set(rec["sent"]) == {"day_before:g0@test", "three_hours:g0@test"}


def test_missed_day_before_is_superseded_by_three_hours():
    sub()
    out = run(START - timedelta(hours=1), FakeEngine())  # a once-a-day cron that ran late
    assert out["by_trigger"] == {"day_before": 0, "three_hours": 1, "change": 0}


def test_change_on_odds_crossing_50_and_official_flip():
    sub()
    engine = FakeEngine(pct=80)
    run(NOW, engine)                       # baseline stored, nothing sent
    engine.pct = 70
    assert run(NOW + timedelta(hours=1), engine)["sent"] == 0      # moved but didn't cross 50
    engine.pct = 40
    out = run(NOW + timedelta(hours=2), engine)
    assert out["by_trigger"]["change"] == 1
    assert "from 70% to 40%" in alerts.outbox()[0]["body"]
    engine.official = "cancelled"
    out = run(NOW + timedelta(hours=3), engine)
    assert out["by_trigger"]["change"] == 1 and "Official status changed to cancelled" in alerts.outbox()[0]["body"]


def test_change_on_new_player_report(store):
    sub()
    engine = FakeEngine()
    at = START - timedelta(hours=6)
    run(at, engine)
    reports.submit_report({"field_id": "Krieg", "status": "cancelled", "game_time": START.isoformat()}, client="198.51.100.7",
                          now=at.astimezone(timezone.utc))
    out = run(at + timedelta(minutes=5), engine)
    assert out["by_trigger"]["change"] == 1
    assert "new report" in json.loads(alerts.outbox()[0]["body"])["change"]


def test_expired_subscriptions_are_deleted():
    res = sub()
    out = run(START + timedelta(days=2), FakeEngine())
    assert out["expired"] == 1 and alerts.load(res["id"]) is None


# ------------------------------------------------------------------ webhooks: test mode, signing, retries (local receiver only)
class Receiver(BaseHTTPRequestHandler):
    hits: list = []
    fail_first = 0

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.hits.append({"headers": dict(self.headers.items()), "body": body})
        code = 500 if len(Receiver.hits) <= Receiver.fail_first else 200
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def receiver():
    Receiver.hits, Receiver.fail_first = [], 0
    srv = HTTPServer(("127.0.0.1", 0), Receiver)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _local_post(srv):
    port = srv.server_address[1]
    return lambda url, body, secret: alerts.post_webhook(
        f"http://hooks.test:{port}/rain", body, secret, validator=lambda u: u, resolver=lambda h, p: "127.0.0.1",
        sleep=lambda s: None)


def test_webhook_test_mode_stores_signed_payload_without_posting():
    res = sub()
    run(START - timedelta(hours=2), FakeEngine())
    msg = alerts.outbox()[0]
    assert msg["result"]["test_mode"] is True and msg["result"]["delivered"] is False
    body = msg["body"].encode()
    ts = int(msg["signature"].split(",")[0][2:])
    assert alerts.verify(res["webhook_secret"], msg["signature"], body, now=ts)
    payload = json.loads(body)
    assert payload["type"] == "rainout.alert" and payload["event"]["play_probability_percent"] == 90
    assert payload["unsubscribe_url"].startswith(alerts.BASE_URL + "/v1/alerts/unsubscribe?id=")


def test_webhook_live_mode_posts_signed_with_retries(receiver, monkeypatch):
    monkeypatch.setenv("RAINOUT_ALERTS_LIVE", "1")
    Receiver.fail_first = 2
    res = sub()
    out = alerts.dispatch(now=START - timedelta(hours=2), checker=FakeEngine(), webhook_poster=_local_post(receiver))
    assert out["sent"] == 1 and out["mode"] == "live"
    assert len(Receiver.hits) == 3
    last = Receiver.hits[-1]
    assert last["headers"]["X-Rainout-Attempt"] == "3"
    assert alerts.verify(res["webhook_secret"], last["headers"]["X-Rainout-Signature"], last["body"])
    assert not alerts.verify("whsec_wrong", last["headers"]["X-Rainout-Signature"], last["body"])
    assert alerts.outbox()[0]["result"]["delivered"] is True


def test_webhook_gives_up_after_retries(receiver):
    Receiver.fail_first = 99
    res = _local_post(receiver)("u", b"{}", "s")
    assert res["delivered"] is False and res["gave_up"] == "retries_exhausted" and len(Receiver.hits) == alerts.WEBHOOK_RETRIES


# ------------------------------------------------------------------ calendar feed
def test_calendar_feed_has_odds_and_refreshes_on_fetch():
    res = sub("calendar")
    engine = FakeEngine(pct=35)
    rec = alerts.authorize(res["id"], res["token"])
    ics = alerts.calendar_feed(rec, now=NOW, checker=engine)
    assert "BEGIN:VCALENDAR" in ics and "X-WR-CALNAME:Rain-Outsource alerts" in ics and "RAIN RISK: 35% play" in ics
    engine.pct = 92
    assert "[92% play]" in alerts.calendar_feed(rec, now=NOW, checker=engine) and engine.calls == 2


def test_team_ics_subscription_reuses_schedule_parser():
    from rainout_agent.safe_fetch import FetchResult

    cal = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART:20261023T190000\r\nSUMMARY:Hawks vs Owls\r\n"
           "LOCATION:Krieg\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    res = alerts.subscribe({"channel": "calendar", "ics_url": "https://league.example.org/team.ics"}, now=NOW)
    rec = alerts.authorize(res["id"], res["token"])
    seen = {}

    def engine(games, now=None, window_days=14, **kw):
        seen["games"] = games
        return FakeEngine()(games, now=now)

    ics = alerts.calendar_feed(rec, now=NOW, checker=engine, fetcher=lambda url: FetchResult(url, 200, "text/calendar", cal.encode()))
    assert seen["games"][0]["title"] == "Hawks vs Owls" and "Hawks vs Owls" in ics


# ------------------------------------------------------------------ HTTP layer
def call(method, path, query="", body=None, headers=None, **deps):
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
    ctype = "application/json" if isinstance(body, dict) else "application/x-www-form-urlencoded"
    status, hdrs, payload = alerts_api.handle(method, path, query, raw, ctype, headers or {}, client="192.0.2.1", **deps)
    try:
        return status, hdrs, json.loads(payload)
    except ValueError:
        return status, hdrs, payload.decode()


def test_http_subscribe_status_calendar_and_unsubscribe(monkeypatch):
    monkeypatch.setattr(alerts, "_now", lambda: NOW)
    status, _, res = call("POST", "/v1/alerts/subscribe", body={"channel": "calendar", "field_id": "Krieg", "game_time": START.isoformat()})
    assert status == 201 and res["status"] == "active"
    status, _, info = call("GET", f"/v1/alerts/{res['id']}", f"token={res['token']}")
    assert status == 200 and info["field_id"] == "austin-tx-krieg-field-softball-complex" and "token_hash" not in info
    assert call("GET", f"/v1/alerts/{res['id']}", "token=nope")[0] == 404
    status, hdrs, ics = call("GET", f"/v1/alerts/{res['id']}/calendar.ics", f"token={res['token']}", checker=FakeEngine())
    assert status == 200 and hdrs["Content-Type"].startswith("text/calendar") and "90% play" in ics
    # GET on the unsubscribe link only shows a button (link previewers must not unsubscribe)
    unsub_q = res["unsubscribe_url"].split("?", 1)[1]
    status, _, page = call("GET", "/v1/alerts/unsubscribe", unsub_q, headers={"Accept": "text/html"})
    assert status == 200 and "<form method=post" in page and alerts.load(res["id"])
    status, _, page = call("POST", "/v1/alerts/unsubscribe", body=unsub_q.encode())
    assert status == 200 and "Unsubscribed" in page and alerts.load(res["id"]) is None


def test_dispatch_requires_a_token(monkeypatch):
    assert call("POST", "/v1/alerts/dispatch")[0] == 503                       # nothing configured
    monkeypatch.setenv("RAINOUT_INTERNAL_TOKEN", "int-secret")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("RAINOUT_DISPATCH_KEY", "ping-key")
    assert call("POST", "/v1/alerts/dispatch", headers={"X-Rainout-Internal": "wrong"})[0] == 403
    for kw in ({"headers": {"X-Rainout-Internal": "int-secret"}}, {"headers": {"Authorization": "Bearer cron-secret"}},
               {"query": "key=ping-key"}):
        status, _, out = call("GET", "/v1/alerts/dispatch", checker=FakeEngine(), **kw)
        assert status == 200 and out["mode"] == "test"
    assert call("GET", "/v1/alerts/outbox", headers={"X-Rainout-Internal": "int-secret"})[0] == 200
    assert call("GET", "/v1/alerts/outbox")[0] == 403


def test_vercel_route_and_handler():
    import re
    from pathlib import Path

    from api.fields import handler

    routes = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())["routes"]
    for p in ("/v1/alerts", "/v1/alerts/subscribe", "/v1/alerts/dispatch", "/v1/alerts/abc/calendar.ics"):
        assert next(r["dest"] for r in routes if re.fullmatch(r["src"], p)) == "/api/fields.py"
    assert hasattr(handler, "do_POST") and hasattr(handler, "do_DELETE")
    builds = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())["builds"]
    assert not any(b["src"] == "api/alerts.py" for b in builds)  # alerts share api/fields.py (Hobby: 12 functions)


def test_fields_function_serves_alerts_and_fields_locally():
    """One Vercel function answers both /v1/fields and /v1/alerts/* (loopback only, no network)."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from api.fields import handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/v1/fields?sport=softball&limit=1", timeout=10) as r:
            assert r.status == 200 and "fields" in json.loads(r.read())
        try:
            with urllib.request.urlopen(f"{base}/v1/alerts/outbox", timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 403  # handled by the alerts code (internal-only), not a 404/405
        req = urllib.request.Request(f"{base}/v1/fields", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("POST /v1/fields should be 405")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
    finally:
        srv.shutdown()

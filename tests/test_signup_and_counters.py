import json

import pytest

from rainout_agent import api_keys, cache, plans, premium_api, signup


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("RAINOUT_API_KEY_SECRET", "q" * 40)
    api_keys._LOCAL_STORE.clear()
    yield
    api_keys._LOCAL_STORE.clear()


def create(body=None, headers=None, form=False, client="1.2.3.4"):
    h = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})}
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    status, out, payload = premium_api.handle_premium_request("POST", "/v1/keys", "", raw, h, client=client)
    try:
        return status, out, json.loads(payload)
    except ValueError:
        return status, out, payload.decode()


def test_free_key_no_card_no_email():
    status, _, data = create()
    assert status == 201 and data["plan"] == "free_key" and data["api_key"].startswith("rk_test_")
    info = api_keys.verify_key(data["api_key"])
    assert info.plan == "free_key" and info.subscription is None
    status, _, acct = premium_api.handle_premium_request("GET", "/v1/account", "", b"", {"X-API-Key": data["api_key"]})
    acct = json.loads(acct)
    assert status == 200 and acct["usage"]["limits"]["batch_max_items"] == 10
    assert {p["id"] for p in acct["upgrade"]["plans"]} == {"coach", "league", "org"}  # upgrade offered after signup


def test_free_key_beats_anonymous_on_batch():
    key = create()[2]["api_key"]
    items = [{"field_id": f"f{i}", "game_time": "2026-10-24T18:30:00-05:00"} for i in range(8)]
    fake = lambda item: {"game_time_play_probability_percent": 90}  # noqa: E731
    anon = premium_api.handle_premium_request("POST", "/v1/batch", "", json.dumps({"items": items}).encode(), {}, status_fn=fake)
    keyed = premium_api.handle_premium_request("POST", "/v1/batch", "", json.dumps({"items": items}).encode(),
                                               {"X-API-Key": key}, status_fn=fake)
    assert anon[0] == 413 and keyed[0] == 200 and json.loads(keyed[2])["plan"] == "free_key"
    assert plans.PLANS["free_key"]["batch_max_items"] > plans.PLANS["free"]["batch_max_items"]


def test_signup_abuse_limits():
    for _ in range(signup.PER_CLIENT_PER_DAY):
        assert create()[0] == 201
    status, headers, data = create()
    assert status == 429 and data["error"] == "signup_limited" and int(headers["Retry-After"]) > 0
    assert create(client="5.6.7.8")[0] == 201  # other networks unaffected
    assert create({"email": "a@example.com"}, client="9.9.9.1")[0] == 201
    assert create({"email": "A@example.com "}, client="9.9.9.2")[0] == 201
    assert create({"email": "a@example.com"}, client="9.9.9.3")[0] == 429  # per-email (case-insensitive)


def test_signup_validation_and_honeypot():
    assert create({"email": "not-an-email"})[0] == 400
    assert create(b"label=x&website=spam", form=True)[0] == 400
    assert create(b"[1,2]")[0] == 400


def test_signup_form_and_page():
    status, _, html = create(b"label=Tigers+U10&email=", form=True)
    assert status == 201 and "rk_test_" in html
    status, _, page = premium_api.handle_premium_request("GET", "/v1/keys", "", b"", {})
    assert status == 200 and b'action="/v1/keys"' in page and b"name=website" in page


def test_signup_off_without_secret(monkeypatch):
    monkeypatch.delenv("RAINOUT_API_KEY_SECRET")
    status, _, data = create()
    assert status == 503 and data["error"] == "keys_not_configured"


def test_global_signup_cap(monkeypatch):
    monkeypatch.setattr(signup, "GLOBAL_PER_DAY", 2)
    assert create(client="a")[0] == 201 and create(client="b")[0] == 201
    assert create(client="c")[0] == 429


# ---------------------------------------------------------------- counters
def test_memory_incr_is_atomic_and_expires():
    t = [1000.0]
    c = cache.MemoryCache(clock=lambda: t[0])
    assert c.incr("k", 2, 10) == 2 and c.incr("k", 3, 10) == 5
    t[0] += 11
    assert c.get("k") is None and c.incr("k", 1, 10) == 1


class FakeUpstash:
    def __init__(self):
        self.data, self.calls = {}, []

    def __call__(self, method, url, headers, body, timeout):
        cmd = json.loads(body)
        self.calls.append(cmd)
        op, key = cmd[0], cmd[1]
        if op == "INCRBY":
            self.data[key] = int(self.data.get(key, 0)) + int(cmd[2])
            return 200, json.dumps({"result": self.data[key]}).encode()
        if op == "EXPIRE":
            return 200, b'{"result": 1}'
        if op == "SET":
            self.data[key] = cmd[2]
            return 200, b'{"result": "OK"}'
        if op == "GET":
            v = self.data.get(key)
            return 200, json.dumps({"result": None if v is None else str(v)}).encode()
        return 400, b'{"error": "bad"}'


def test_tiered_counters_use_upstash_incrby_and_fresh_reads():
    fake = FakeUpstash()
    tiered = cache.TieredCache(cache.MemoryCache(), cache.UpstashCache("https://u", "t", transport=fake))
    assert tiered.incr("usage:k:mo", 3, 100) == 3 and tiered.incr("usage:k:mo", 2, 100) == 5
    assert [c[0] for c in fake.calls] == ["INCRBY", "EXPIRE", "INCRBY"]  # TTL set once, on create
    fake.data["rainout:v1:usage:k:mo"] = 9  # another instance counted more
    assert tiered.get_fresh("usage:k:mo") == 9


def test_usage_and_signup_use_shared_store_when_upstash_configured(monkeypatch):
    fake = FakeUpstash()
    shared = cache.TieredCache(cache.MemoryCache(), cache.UpstashCache("https://u", "t", transport=fake))
    cache.set_cache(shared)
    assert api_keys.store() is shared
    status, _, data = create(client="7.7.7.7")
    assert status == 201
    assert any(k.startswith("rainout:v1:signup:client:") for k in fake.data)
    assert any(k.startswith("rainout:v1:apikey:meta:") for k in fake.data)


def test_tiered_incr_falls_back_to_memory_when_upstash_down():
    def down(*a):
        raise OSError("down")
    tiered = cache.TieredCache(cache.MemoryCache(), cache.UpstashCache("https://u", "t", transport=down))
    assert tiered.incr("c", 1, 60) == 1 and tiered.incr("c", 1, 60) == 2

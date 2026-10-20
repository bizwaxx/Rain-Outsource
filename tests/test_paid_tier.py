import json

import pytest

from rainout_agent import api_keys, plans, premium_api, usage

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("RAINOUT_API_KEY_SECRET", SECRET)
    api_keys._LOCAL_STORE.clear()
    monkeypatch.setattr(api_keys, "SUBSCRIPTION_RESOLVER", None)
    yield
    api_keys._LOCAL_STORE.clear()


def fake_status(item):
    return {"field_id": item["field_id"], "game_time_play_probability_percent": 50 if "rain" in item["field_id"] else 95}


def fake_team(item):
    return {"team": {"id": item["team_id"]}, "count": 1, "games": [{"at_risk": False}], "next_game": None, "message": "ok"}


def post(items, key=None, **kw):
    headers = {"X-API-Key": key} if key else {}
    status, headers, body = premium_api.handle_premium_request(
        "POST", "/v1/batch", "", json.dumps({"items": items}).encode(), headers,
        status_fn=kw.get("status_fn", fake_status), team_fn=kw.get("team_fn", fake_team))
    return status, headers, json.loads(body)


def games(n):
    return [{"field_id": f"f{i}", "game_time": "2026-10-20T19:00:00-05:00"} for i in range(n)]


# ------------------------------------------------------------------ keys
def test_key_roundtrip_and_tamper():
    key = api_keys.mint_key("league", subscription="sub_123", customer="cus_1")
    assert key.startswith("rk_test_")
    info = api_keys.verify_key(key)
    assert (info.plan, info.subscription, info.mode) == ("league", "sub_123", "test")
    head, sig = key.rsplit(".", 1)
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.verify_key(head + "." + ("A" if sig[0] != "A" else "B") + sig[1:])
    assert exc.value.code == "invalid_api_key"
    with pytest.raises(api_keys.KeyError_):
        api_keys.verify_key(key, env={"RAINOUT_API_KEY_SECRET": "x" * 40})


def test_key_requires_secret_and_paid_plan(monkeypatch):
    with pytest.raises(api_keys.KeyError_):
        api_keys.mint_key("free")
    monkeypatch.setenv("RAINOUT_API_KEY_SECRET", "short")
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.mint_key("coach")
    assert exc.value.code == "keys_not_configured"


def test_expiry_revocation_and_subscription_standing():
    key = api_keys.mint_key("coach", expires_at=1000)
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.verify_key(key, now=2000)
    assert exc.value.code == "expired_api_key"

    key = api_keys.mint_key("coach", key_id="k1", subscription="sub_9")
    api_keys.set_subscription_state("sub_9", "past_due", "league")
    info = api_keys.verify_key(key)
    assert info.plan == "league" and info.standing == "past_due" and info.notes  # upgraded plan + dunning grace
    api_keys.set_subscription_state("sub_9", "canceled")
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.verify_key(key)
    assert exc.value.code == "subscription_inactive"
    api_keys.set_subscription_state("sub_9", "active")
    api_keys.revoke("k1")
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.verify_key(key)
    assert exc.value.code == "revoked_api_key"


def test_resolver_used_on_cache_miss_and_fails_open(monkeypatch):
    calls = []
    monkeypatch.setattr(api_keys, "SUBSCRIPTION_RESOLVER", lambda sub: calls.append(sub) or {"status": "unpaid", "plan": "coach"})
    key = api_keys.mint_key("coach", subscription="sub_x")
    with pytest.raises(api_keys.KeyError_):
        api_keys.verify_key(key)
    with pytest.raises(api_keys.KeyError_):
        api_keys.verify_key(key)
    assert calls == ["sub_x"]  # cached after the first lookup

    def boom(sub):
        raise OSError("stripe down")
    monkeypatch.setattr(api_keys, "SUBSCRIPTION_RESOLVER", boom)
    assert api_keys.verify_key(api_keys.mint_key("coach", subscription="sub_y")).plan == "coach"


def test_key_from_headers():
    assert api_keys.key_from_headers({"x-api-key": " rk_test_a.b "}) == "rk_test_a.b"
    assert api_keys.key_from_headers({"Authorization": "Bearer rk_test_a.b"}) == "rk_test_a.b"
    assert api_keys.key_from_headers({"Authorization": "Bearer something-else"}) is None
    assert api_keys.key_from_headers({}) is None


# ----------------------------------------------------------------- usage
def test_rate_limit_and_quota(monkeypatch):
    info = api_keys.verify_key(api_keys.mint_key("coach", key_id="kq"))
    monkeypatch.setitem(plans.PLANS["coach"], "rate_per_minute", 2)
    monkeypatch.setitem(plans.PLANS["coach"], "monthly_units", 5)
    t = 1_800_000_000.0
    usage.check_and_count(info, 2, "batch", now_s=t)
    usage.check_and_count(info, 2, "batch", now_s=t + 1)
    with pytest.raises(usage.LimitExceeded) as exc:
        usage.check_and_count(info, 1, "batch", now_s=t + 2)
    assert exc.value.code == "rate_limited" and 0 < exc.value.retry_after <= 60
    with pytest.raises(usage.LimitExceeded) as exc:
        usage.check_and_count(info, 2, "batch", now_s=t + 120)
    assert exc.value.code == "quota_exceeded"
    s = usage.check_and_count(info, 1, "batch", now_s=t + 180)
    assert s["this_month"]["units"] == 5 and s["this_month"]["remaining"] == 0
    assert s["by_endpoint_this_month"] == {"batch": 5}
    assert sum(d["units"] for d in s["last_7_days"]) == 5


# ------------------------------------------------------------------ http
def test_plans_endpoint_lists_free_and_paid():
    status, headers, body = premium_api.handle_premium_request("GET", "/v1/plans", "", b"", {})
    data = json.loads(body)
    ids = [p["id"] for p in data["plans"]]
    assert status == 200 and ids[0] == "free" and {"coach", "league", "org"} <= set(ids)
    prices = [p["price_usd_month"] for p in data["plans"] if p["requires_key"]]
    assert all(9 <= p <= 99 for p in prices)
    free = data["plans"][0]
    assert free["rate_per_minute"] is None and free["monthly_units"] is None
    for paid in data["plans"][1:]:  # paid plans always give more than free
        assert paid["batch_max_items"] > free["batch_max_items"] and paid["max_teams_per_call"] > free["max_teams_per_call"]
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_free_batch_without_key():
    status, headers, data = post(games(2) + [{"team_id": "tsl-1-2-3"}])
    assert status == 200 and data["plan"] == "free" and data["ok"] == 3
    status, _, data = post(games(4))
    assert status == 413 and data["error"] == "batch_too_large"
    status, _, data = post([{"team_id": "a"}, {"team_id": "b"}])
    assert status == 413


def test_paid_batch_counts_usage_and_flags_risk():
    key = api_keys.mint_key("coach", key_id="kb")
    items = games(10) + [{"field_id": "rainy", "game_time": "2026-10-20T19:00:00-05:00"}, {"bogus": 1}]
    status, headers, data = post(items, key)
    assert status == 200 and data["plan"] == "coach" and data["count"] == 12 and data["ok"] == 11
    assert data["at_risk_indexes"] == [10]
    assert data["results"][11]["error"] == "invalid_item"
    assert headers["X-Rainout-Plan"] == "coach" and headers["X-RateLimit-Limit"] == "60"
    assert data["usage"]["this_month"]["units"] == 12
    status, _, acct = premium_api.handle_premium_request("GET", "/v1/account", "", b"", {"X-API-Key": key})
    acct = json.loads(acct)
    assert status == 200 and acct["usage"]["this_month"]["units"] == 12 and acct["key"]["key_id"] == "kb"


def test_batch_over_plan_size_and_errors_isolated():
    key = api_keys.mint_key("coach")
    status, _, data = post(games(26), key)
    assert status == 413

    def broken(item):
        raise RuntimeError("weather down")
    status, _, data = post(games(2), key, status_fn=broken)
    assert status == 200 and data["ok"] == 0 and data["results"][0]["error"] == "RuntimeError"


def test_bad_key_is_rejected_on_paid_endpoints_only(monkeypatch):
    status, _, data = post(games(1), "rk_test_bad.sig")
    assert status == 401 and data["error"] == "invalid_api_key"
    status, _, body = premium_api.handle_premium_request("GET", "/v1/account", "", b"", {})
    assert status == 401
    monkeypatch.delenv("RAINOUT_API_KEY_SECRET")
    status, _, data = post(games(1), "rk_test_x.y")
    assert status == 503 and data["error"] == "keys_not_configured"
    status, _, data = post(games(1))  # free tier still works with keys disabled
    assert status == 200


def test_rate_limited_batch_returns_429(monkeypatch):
    monkeypatch.setitem(plans.PLANS["coach"], "rate_per_minute", 1)
    key = api_keys.mint_key("coach")
    assert post(games(1), key)[0] == 200
    status, headers, data = post(games(1), key)
    assert status == 429 and data["error"] == "rate_limited" and int(headers["Retry-After"]) > 0


def test_existing_endpoints_ignore_keys_and_stay_unrouted():
    """The free public API is not touched: existing routes do not go through the paid layer."""
    cfg = json.load(open("vercel.json"))
    assert not any("premium" in b["src"] for b in cfg["builds"])  # no extra Vercel function
    premium_routes = {r["src"] for r in cfg["routes"] if r["src"].startswith(("/v1/plans", "/v1/account", "/v1/batch", "/v1/billing", "/v1/premium"))}
    assert premium_routes == {"/v1/plans/?", "/v1/account/?", "/v1/batch/?", "/v1/billing/(checkout|success|portal|webhook)/?", "/v1/premium/batch/?"}
    assert all(r["dest"] == "/api/status.py" for r in cfg["routes"] if r["src"] in premium_routes)
    for path in ("/v1/status", "/v1/reports", "/v1/fields", "/v1/rain", "/mcp"):
        assert not premium_api.is_premium_path(path)
    status, _, _ = premium_api.handle_premium_request("GET", "/v1/status", "", b"", {})
    assert status == 404


def test_wsgi_routes_paid_paths():
    import io

    from rainout_agent.http_app import application

    out = {}
    body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/plans", "QUERY_STRING": "", "wsgi.input": io.BytesIO()},
                                lambda s, h: out.update(status=s)))
    assert out["status"].startswith("200") and json.loads(body)["plans"]


def test_vercel_status_function_serves_paid_paths_and_status_unchanged():
    import io

    from api.status import handler

    def call(path, method="GET", body=b""):
        h = handler.__new__(handler)
        h.path, h.wfile, h.rfile = path, io.BytesIO(), io.BytesIO(body)
        out = {"headers": {}}
        h.headers = {"Content-Length": str(len(body))}
        h.send_response = lambda code, msg=None: out.update(code=code)
        h.send_header = lambda k, v: out["headers"].update({k: v})
        h.end_headers = lambda: None
        getattr(h, f"do_{method}")()
        return out["code"], json.loads(h.wfile.getvalue() or b"{}")

    assert call("/v1/plans")[0] == 200
    assert call("/v1/status")[1] == {"error": "missing_field_id"}  # existing behaviour untouched
    status, data = call("/v1/batch", "POST", json.dumps({"items": [{"bogus": 1}]}).encode())
    assert status == 200 and data["plan"] == "free" and data["results"][0]["error"] == "invalid_item"

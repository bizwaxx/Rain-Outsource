import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from rainout_agent import api_keys, billing, premium_api

ENV = {
    "STRIPE_SECRET_KEY": "sk_test_123",
    "STRIPE_WEBHOOK_SECRET": "whsec_test",
    "STRIPE_PRICE_COACH": "price_coach",
    "STRIPE_PRICE_LEAGUE": "price_league",
    "STRIPE_PRICE_ORG": "price_org",
    "RAINOUT_API_KEY_SECRET": "k" * 40,
}


class FakeStripe:
    """Minimal in-process Stripe for unit tests (same request shapes stripe-mock accepts)."""

    def __init__(self):
        self.calls = []
        self.subs = {"sub_1": {"id": "sub_1", "object": "subscription", "status": "active", "customer": "cus_1",
                               "metadata": {"plan": "coach"}, "items": {"data": [{"price": {"id": "price_league"}}]}}}

    def __call__(self, method, url, headers, body):
        u = urlparse(url)
        form = parse_qs((body or b"").decode()) if body else parse_qs(u.query)
        self.calls.append((method, u.path, form, headers))
        assert headers["Authorization"] == "Bearer sk_test_123"
        if u.path == "/v1/checkout/sessions" and method == "POST":
            return 200, json.dumps({"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}).encode()
        if u.path == "/v1/checkout/sessions/cs_test_1":
            return 200, json.dumps({"id": "cs_test_1", "status": "complete", "customer": "cus_1",
                                    "metadata": {"plan": "coach"}, "subscription": self.subs["sub_1"]}).encode()
        if u.path == "/v1/checkout/sessions/cs_test_open":
            return 200, json.dumps({"id": "cs_test_open", "status": "open", "subscription": None}).encode()
        if u.path.startswith("/v1/subscriptions/"):
            sub = self.subs.get(u.path.rsplit("/", 1)[1])
            return (200, json.dumps(sub).encode()) if sub else (404, b'{"error":{"message":"No such subscription"}}')
        if u.path == "/v1/billing_portal/sessions":
            return 200, json.dumps({"url": "https://billing.stripe.com/p/session/test_1"}).encode()
        return 404, b'{"error":{"message":"unknown"}}'


@pytest.fixture
def stripe_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    fake = FakeStripe()
    monkeypatch.setattr(billing, "TRANSPORT", fake)
    api_keys._LOCAL_STORE.clear()
    yield fake
    api_keys._LOCAL_STORE.clear()
    api_keys.SUBSCRIPTION_RESOLVER = None


def req(method, path, body=b"", headers=None, query=""):
    status, h, payload = premium_api.handle_premium_request(method, path, query, body, headers or {})
    try:
        return status, h, json.loads(payload)
    except ValueError:
        return status, h, payload.decode()


def webhook(event, ts=None, secret="whsec_test"):
    body = json.dumps(event).encode()
    sig = billing.sign_payload(body, secret, ts or int(time.time()))
    return req("POST", "/v1/billing/webhook", body, {"Stripe-Signature": sig})


# ------------------------------------------------------------ live guard
def test_live_key_refused_without_allow_live():
    with pytest.raises(billing.BillingError) as exc:
        billing.mode({"STRIPE_SECRET_KEY": "sk_live_abc"})
    assert exc.value.code == "live_key_refused"
    with pytest.raises(billing.BillingError):
        billing.mode({"STRIPE_SECRET_KEY": "rk_live_abc", "ALLOW_LIVE": "0"})
    assert billing.mode({"STRIPE_SECRET_KEY": "sk_live_abc", "ALLOW_LIVE": "true"}) == "live"
    assert billing.mode({"STRIPE_SECRET_KEY": "sk_test_abc"}) == "test"
    with pytest.raises(billing.BillingError) as exc:
        billing.mode({})
    assert exc.value.code == "billing_not_configured"


def test_live_key_blocks_every_billing_endpoint(stripe_env, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_nope")
    for method, path in (("GET", "/v1/billing/checkout"), ("POST", "/v1/billing/checkout"),
                         ("GET", "/v1/billing/success"), ("POST", "/v1/billing/webhook")):
        status, _, data = req(method, path, b'{"plan":"coach"}')
        assert status == 503 and data["error"] == "live_key_refused", path
    assert stripe_env.calls == []  # never talked to Stripe


def test_billing_off_without_key(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    status, _, data = req("POST", "/v1/billing/checkout", b'{"plan":"coach"}')
    assert status == 503 and data["error"] == "billing_not_configured"


# ------------------------------------------------------------ checkout
def test_checkout_creates_subscription_session(stripe_env):
    status, _, data = req("POST", "/v1/billing/checkout", b'{"plan":"league"}', {"Content-Type": "application/json"})
    assert status == 200 and data["checkout_url"].startswith("https://checkout.stripe.com/") and data["mode"] == "test"
    method, path, form, _ = stripe_env.calls[0]
    assert (method, path) == ("POST", "/v1/checkout/sessions")
    assert form["mode"] == ["subscription"] and form["line_items[0][price]"] == ["price_league"]
    assert form["subscription_data[metadata][plan]"] == ["league"]
    assert "{CHECKOUT_SESSION_ID}" in form["success_url"][0]


def test_checkout_form_redirects_and_page_lists_plans(stripe_env):
    status, headers, _ = req("POST", "/v1/billing/checkout", b"plan=coach", {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303 and headers["Location"].startswith("https://checkout.stripe.com/")
    status, _, html = req("GET", "/v1/billing/checkout")
    assert status == 200 and "Coach" in html and "League" in html and 'action="/v1/billing/checkout"' in html


def test_checkout_rejects_unknown_plan_and_missing_price(stripe_env, monkeypatch):
    assert req("POST", "/v1/billing/checkout", b'{"plan":"free"}')[0] == 400
    monkeypatch.delenv("STRIPE_PRICE_ORG")
    status, _, data = req("POST", "/v1/billing/checkout", b'{"plan":"org"}')
    assert status == 503 and data["error"] == "price_not_configured"


def test_success_page_issues_key_that_works(stripe_env):
    status, _, data = req("GET", "/v1/billing/success", query="session_id=cs_test_1")
    assert status == 200 and data["plan"] == "league"  # plan from the price, not stale metadata
    key = data["api_key"]
    assert key.startswith("rk_test_")
    info = api_keys.verify_key(key)
    assert info.subscription == "sub_1" and info.customer == "cus_1"
    # Same key on reload (deterministic key id per subscription)
    assert req("GET", "/v1/billing/success", query="session_id=cs_test_1")[2]["api_key"] == key
    status, _, acct = req("GET", "/v1/account", headers={"X-API-Key": key})
    assert status == 200 and acct["key"]["plan"] == "league"


def test_success_requires_complete_session(stripe_env):
    assert req("GET", "/v1/billing/success", query="session_id=cs_test_open")[0] == 409
    assert req("GET", "/v1/billing/success", query="session_id=nope")[0] == 400


def test_portal_needs_key_and_uses_customer(stripe_env):
    assert req("GET", "/v1/billing/portal")[0] == 401
    key = billing.key_for_subscription("sub_1", "cus_1", "coach")
    status, _, data = req("POST", "/v1/billing/portal", headers={"X-API-Key": key})
    assert status == 200 and data["portal_url"].startswith("https://billing.stripe.com/")
    assert stripe_env.calls[-1][2]["customer"] == ["cus_1"]


# ------------------------------------------------------------- webhook
def test_webhook_signature_checks(stripe_env):
    event = {"id": "evt_1", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "status": "canceled"}}}
    body = json.dumps(event).encode()
    bad = billing.sign_payload(body, "whsec_wrong", int(time.time()))
    assert req("POST", "/v1/billing/webhook", body, {"Stripe-Signature": bad})[2]["error"] == "bad_signature"
    assert req("POST", "/v1/billing/webhook", body, {})[0] == 400
    status, _, data = webhook(event, ts=int(time.time()) - 3600)
    assert status == 400 and data["error"] == "stale_signature"
    # tampered body with a valid header for the original body
    sig = billing.sign_payload(body, "whsec_test", int(time.time()))
    assert req("POST", "/v1/billing/webhook", body.replace(b"sub_1", b"sub_2"), {"Stripe-Signature": sig})[0] == 400


def test_webhook_lifecycle_issues_grace_and_revokes(stripe_env):
    key = billing.key_for_subscription("sub_1", "cus_1", "coach")
    kid = api_keys.verify_key(key).key_id

    status, _, data = webhook({"id": "evt_a", "type": "checkout.session.completed",
                               "data": {"object": {"mode": "subscription", "subscription": "sub_1", "metadata": {"plan": "coach"}}}})
    assert status == 200 and data["action"].startswith("key_issued")

    status, _, data = webhook({"id": "evt_b", "type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_1"}}})
    assert data["action"] == "key_grace_past_due"
    info = api_keys.verify_key(key)
    assert info.standing == "past_due" and info.notes

    webhook({"id": "evt_c", "type": "invoice.paid", "data": {"object": {"subscription": "sub_1"}}})
    assert api_keys.verify_key(key).standing == "active"

    status, _, data = webhook({"id": "evt_d", "type": "customer.subscription.updated",
                               "data": {"object": {"id": "sub_1", "status": "unpaid", "items": {"data": []}}}})
    assert data["action"] == "key_revoked" and api_keys.is_revoked(kid)
    with pytest.raises(api_keys.KeyError_):
        api_keys.verify_key(key)

    webhook({"id": "evt_e", "type": "customer.subscription.updated",
             "data": {"object": {"id": "sub_1", "status": "active", "items": {"data": [{"price": {"id": "price_org"}}]}}}})
    assert api_keys.verify_key(key).plan == "org"  # reactivated + upgraded in the portal

    status, _, data = webhook({"id": "evt_f", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    assert data["action"] == "key_revoked"
    status, _, data = webhook({"id": "evt_f", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    assert data["action"] == "duplicate_ignored"
    status, _, data = webhook({"id": "evt_g", "type": "charge.refunded", "data": {"object": {}}})
    assert status == 200 and data["action"] == "ignored"


def test_resolver_rechecks_stripe_after_cache_expiry(stripe_env):
    billing.install_resolver()
    key = billing.key_for_subscription("sub_1", "cus_1", "coach")
    assert api_keys.verify_key(key).plan == "league"  # read from Stripe on cache miss
    stripe_env.subs["sub_1"]["status"] = "canceled"
    api_keys._LOCAL_STORE.clear()  # simulate the 10-minute subscription cache expiring
    with pytest.raises(api_keys.KeyError_) as exc:
        api_keys.verify_key(key)
    assert exc.value.code == "subscription_inactive"


def test_form_encoding_matches_stripe_conventions():
    pairs = billing._encode({"line_items": [{"price": "p", "quantity": 1}], "expand": ["subscription"], "x": True, "n": None})
    assert pairs == [("line_items[0][price]", "p"), ("line_items[0][quantity]", "1"), ("expand[0]", "subscription"), ("x", "true")]

"""Optional: run against stripe-mock (https://github.com/stripe/stripe-mock), which validates
request parameters against Stripe's OpenAPI spec. Skipped unless STRIPE_MOCK_URL is set:

    ./stripe-mock -http-port 12111 &
    STRIPE_MOCK_URL=http://localhost:12111 pytest tests/test_billing_stripe_mock.py
"""
import os

import pytest

from rainout_agent import api_keys, billing

MOCK = os.environ.get("STRIPE_MOCK_URL")
pytestmark = pytest.mark.skipif(not MOCK, reason="set STRIPE_MOCK_URL to run against stripe-mock")


@pytest.fixture
def env():
    return {"STRIPE_API_BASE": MOCK or "", "STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_PRICE_COACH": "price_coach",
            "RAINOUT_API_KEY_SECRET": "m" * 40}


def test_checkout_request_is_valid_for_stripe(env):
    session = billing.create_checkout("coach", email="coach@example.com", env=env)
    assert session["checkout_url"].startswith("https://checkout.stripe.com/") and session["session_id"].startswith("cs_")


def test_success_lookup_portal_and_subscription_requests_are_valid(env):
    with pytest.raises(billing.BillingError) as exc:  # stripe-mock fixtures are never "complete"
        billing.checkout_success("cs_test_1", env=env)
    assert exc.value.code == "checkout_incomplete"
    assert billing.subscription_state_from_stripe("sub_123", env=env)["status"]
    info = api_keys.KeyInfo(key_id="x", plan="coach", mode="test", customer="cus_123")
    assert billing.create_portal(info, env=env)["portal_url"]

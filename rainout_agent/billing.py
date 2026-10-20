"""Stripe billing (TEST MODE by default): Checkout, subscriptions, portal, webhooks.

Plain REST over urllib (no SDK). Env vars:

* ``STRIPE_SECRET_KEY``      sk_test_... (or rk_test_...). A live key (sk_live_/rk_live_)
  is refused unless ``ALLOW_LIVE`` is set to 1/true/yes.
* ``STRIPE_WEBHOOK_SECRET``  whsec_... for the /v1/billing/webhook endpoint.
* ``STRIPE_PRICE_COACH``, ``STRIPE_PRICE_LEAGUE``, ``STRIPE_PRICE_ORG``  price ids.
* ``STRIPE_API_BASE``        optional, e.g. http://localhost:12111 for stripe-mock.
* ``RAINOUT_PUBLIC_URL``     optional base for success/return URLs.
* ``RAINOUT_API_KEY_SECRET`` signs the API keys handed to subscribers (api_keys.py).

Failed-card retries (Smart Retries) and dunning emails are Stripe settings (see
docs/billing-setup.md). This module maps the resulting subscription states to keys:
active/trialing/past_due keep working (past_due = retrying), unpaid/canceled revoke.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from rainout_agent import api_keys, plans

DEFAULT_BASE = "https://api.stripe.com"
PUBLIC_URL = "https://rainout-agent-source.vercel.app"
SIGNATURE_TOLERANCE_S = 300
PRICE_ENV = {"coach": "STRIPE_PRICE_COACH", "league": "STRIPE_PRICE_LEAGUE", "org": "STRIPE_PRICE_ORG"}
REVOKE_STATUSES = {"canceled", "unpaid", "incomplete_expired"}
TRUTHY = {"1", "true", "yes"}

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class BillingError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ) if env is None else env


def mode(env: dict[str, str] | None = None) -> str:
    """'test', 'live' (only with ALLOW_LIVE), or raise BillingError when billing must not run."""
    e = _env(env)
    key = e.get("STRIPE_SECRET_KEY") or ""
    if not key:
        raise BillingError(503, "billing_not_configured", "Billing is not set up on this deployment yet (no Stripe test key).")
    if key.startswith(("sk_live_", "rk_live_")):
        if (e.get("ALLOW_LIVE") or "").strip().lower() not in TRUTHY:
            raise BillingError(503, "live_key_refused", "A live Stripe key is configured but ALLOW_LIVE is not set; billing is disabled.")
        return "live"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    raise BillingError(503, "billing_not_configured", "STRIPE_SECRET_KEY is not a Stripe secret key.")


def price_for(plan: str, env: dict[str, str] | None = None) -> str:
    if plan not in PRICE_ENV:
        raise BillingError(400, "invalid_plan", f"Choose one of: {', '.join(PRICE_ENV)}.")
    price = _env(env).get(PRICE_ENV[plan]) or ""
    if not price.startswith("price_"):
        raise BillingError(503, "price_not_configured", f"Set {PRICE_ENV[plan]} to the Stripe price id for {plan}.")
    return price


def plan_for_price(price_id: str | None, env: dict[str, str] | None = None) -> str | None:
    e = _env(env)
    for plan, var in PRICE_ENV.items():
        if price_id and e.get(var) == price_id:
            return plan
    return None


def _encode(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out += _encode(value, name)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                out += _encode(v, f"{name}[{i}]") if isinstance(v, dict) else [(f"{name}[{i}]", str(v))]
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        elif value is not None:
            out.append((name, str(value)))
    return out


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed Stripe host
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


TRANSPORT: Transport = _urllib_transport


def stripe(method: str, path: str, params: dict[str, Any] | None = None, env: dict[str, str] | None = None,
           idempotency_key: str | None = None) -> dict[str, Any]:
    mode(env)  # refuses live keys without ALLOW_LIVE
    e = _env(env)
    base = (e.get("STRIPE_API_BASE") or DEFAULT_BASE).rstrip("/")
    encoded = urllib.parse.urlencode(_encode(params or {}))
    headers = {"Authorization": f"Bearer {e['STRIPE_SECRET_KEY']}", "Stripe-Version": "2024-06-20"}
    url, body = f"{base}{path}", None
    if method == "GET":
        url += f"?{encoded}" if encoded else ""
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = encoded.encode()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
    status, raw = TRANSPORT(method, url, headers, body)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    if status >= 400:
        msg = (data.get("error") or {}).get("message") or f"Stripe returned {status}"
        raise BillingError(502, "stripe_error", msg)
    return data


# ------------------------------------------------------------------ flows
def public_url(env: dict[str, str] | None = None) -> str:
    return (_env(env).get("RAINOUT_PUBLIC_URL") or PUBLIC_URL).rstrip("/")


def create_checkout(plan: str, email: str | None = None, env: dict[str, str] | None = None,
                    from_key: api_keys.KeyInfo | None = None) -> dict[str, Any]:
    mode(env)
    price = price_for(plan, env)
    base = public_url(env)
    meta = {"plan": plan, "product": "rainout-source"}
    if from_key:
        meta["from_key_id"] = from_key.key_id
    session = stripe("POST", "/v1/checkout/sessions", {
        "mode": "subscription",
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": f"{base}/v1/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/v1/billing/checkout?canceled=1",
        "allow_promotion_codes": True,
        "billing_address_collection": "auto",
        "customer_email": None if (from_key and from_key.customer) else (email or None),  # Stripe allows one of the two
        "client_reference_id": from_key.key_id if from_key else None,
        "customer": from_key.customer if from_key and from_key.customer else None,
        "metadata": meta,
        "subscription_data": {"metadata": meta},
    }, env)
    return {"checkout_url": session.get("url"), "session_id": session.get("id"), "plan": plan, "mode": mode(env)}


def _sub_plan(sub: dict[str, Any], env: dict[str, str] | None) -> str | None:
    items = ((sub.get("items") or {}).get("data") or [])
    for item in items:
        plan = plan_for_price(((item.get("price") or {}).get("id")), env)
        if plan:
            return plan
    plan = (sub.get("metadata") or {}).get("plan")
    return plan if plan in plans.PAID else None


def key_for_subscription(sub_id: str, customer: str | None, plan: str, env: dict[str, str] | None = None) -> str:
    key_mode = "live" if mode(env) == "live" else "test"
    return api_keys.mint_key(plan, mode=key_mode, subscription=sub_id, customer=customer,
                             key_id=api_keys.key_id_for_subscription(sub_id, env), env=env)


def checkout_success(session_id: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    mode(env)
    if not session_id.startswith("cs_"):
        raise BillingError(400, "invalid_session", "Missing or invalid session_id.")
    session = stripe("GET", f"/v1/checkout/sessions/{urllib.parse.quote(session_id)}", {"expand": ["subscription"]}, env)
    sub = session.get("subscription")
    if session.get("status") != "complete" or not sub:
        raise BillingError(409, "checkout_incomplete", "This checkout is not complete yet. Finish payment, then reload this page.")
    sub = sub if isinstance(sub, dict) else stripe("GET", f"/v1/subscriptions/{sub}", None, env)
    plan = _sub_plan(sub, env) or (session.get("metadata") or {}).get("plan")
    if plan not in plans.PAID:
        raise BillingError(502, "unknown_plan", "Could not tell which plan this subscription is for.")
    status = sub.get("status") or "active"
    api_keys.set_subscription_state(sub["id"], status, plan)
    key = key_for_subscription(sub["id"], session.get("customer") or sub.get("customer"), plan, env)
    return {"api_key": key, "plan": plan, "subscription_status": status, "mode": mode(env),
            "how_to_use": "Send it as the X-API-Key header on /v1/batch and other key endpoints. Keep it secret. "
                          "Your free key keeps working at the free-key level.",
            "dashboard": f"{public_url(env)}/v1/account", "manage_billing": f"{public_url(env)}/v1/billing/portal"}


def create_portal(info: api_keys.KeyInfo, env: dict[str, str] | None = None) -> dict[str, Any]:
    mode(env)
    if not info.customer:
        raise BillingError(400, "no_customer", "This key is not tied to a Stripe customer.")
    session = stripe("POST", "/v1/billing_portal/sessions", {"customer": info.customer,
                                                            "return_url": f"{public_url(env)}/v1/plans"}, env)
    return {"portal_url": session.get("url")}


def subscription_state_from_stripe(sub_id: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    sub = stripe("GET", f"/v1/subscriptions/{urllib.parse.quote(sub_id)}", None, env)
    return {"status": sub.get("status"), "plan": _sub_plan(sub, env)} if sub.get("id") else None


def install_resolver(env: dict[str, str] | None = None) -> bool:
    """Let api_keys re-check subscriptions with Stripe when billing is configured."""
    try:
        mode(env)
    except BillingError:
        api_keys.SUBSCRIPTION_RESOLVER = None
        return False
    api_keys.SUBSCRIPTION_RESOLVER = lambda sub_id: subscription_state_from_stripe(sub_id, env)
    return True


# ---------------------------------------------------------------- webhook
def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None,
                     tolerance: int = SIGNATURE_TOLERANCE_S) -> None:
    """Stripe's scheme: HMAC-SHA256 of "<t>.<raw body>" with the endpoint secret, hex in v1=."""
    if not secret:
        raise BillingError(503, "webhook_not_configured", "STRIPE_WEBHOOK_SECRET is not set.")
    parts: dict[str, list[str]] = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise BillingError(400, "bad_signature", "Missing or malformed Stripe-Signature header.") from exc
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError(400, "bad_signature", "Stripe signature does not match.")
    if abs((now or time.time()) - ts) > tolerance:
        raise BillingError(400, "stale_signature", "Stripe signature timestamp is outside the tolerance.")


def sign_payload(payload: bytes, secret: str, ts: int) -> str:
    """Build a Stripe-Signature header (tests and local tooling)."""
    return f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + payload, hashlib.sha256).hexdigest()}"


def _apply_subscription(sub_id: str, status: str, plan: str | None, env: dict[str, str] | None) -> str:
    kid = api_keys.key_id_for_subscription(sub_id, env)
    api_keys.set_subscription_state(sub_id, status, plan)
    if status in REVOKE_STATUSES:
        api_keys.revoke(kid, f"subscription_{status}")
        return "key_revoked"
    api_keys.unrevoke(kid)
    return "key_active" if status in {"active", "trialing"} else f"key_grace_{status}"


def handle_webhook(payload: bytes, signature: str, env: dict[str, str] | None = None, now: float | None = None) -> dict[str, Any]:
    mode(env)
    verify_signature(payload, signature, _env(env).get("STRIPE_WEBHOOK_SECRET") or "", now)
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise BillingError(400, "invalid_json", "Webhook body is not JSON.") from exc
    event_id, etype = event.get("id"), event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    seen_key = f"billing:event:{event_id}"
    if event_id and api_keys.store().get(seen_key):
        return {"received": True, "type": etype, "action": "duplicate_ignored"}
    action = "ignored"
    if etype == "checkout.session.completed" and obj.get("mode") == "subscription" and obj.get("subscription"):
        sub_id = obj["subscription"] if isinstance(obj["subscription"], str) else obj["subscription"].get("id")
        plan = (obj.get("metadata") or {}).get("plan")
        action = "key_issued:" + _apply_subscription(sub_id, "active", plan if plan in plans.PAID else None, env)
    elif etype in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        status = "canceled" if etype.endswith("deleted") else (obj.get("status") or "active")
        action = _apply_subscription(obj["id"], status, _sub_plan(obj, env), env)
    elif etype == "invoice.payment_failed" and obj.get("subscription"):
        # Smart Retries keeps retrying; keep the key working until Stripe marks it unpaid/canceled.
        action = _apply_subscription(obj["subscription"], "past_due", None, env)
    elif etype in {"invoice.paid", "invoice.payment_succeeded"} and obj.get("subscription"):
        action = _apply_subscription(obj["subscription"], "active", None, env)
    if event_id:
        api_keys.store().set(seen_key, True, 7 * 86400)
    return {"received": True, "type": etype, "action": action}

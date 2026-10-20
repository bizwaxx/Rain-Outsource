"""Signed API keys for the paid tier.

Keys are self-contained and signed with HMAC-SHA256 using ``RAINOUT_API_KEY_SECRET``,
so checking one needs no database::

    rk_<mode>_<base64url(json payload)>.<base64url(signature)>

The payload holds a key id, plan, optional Stripe subscription id and optional
expiry. A key is honoured only if the signature checks out, it has not expired,
its key id is not on the revocation list, and (for subscription keys) the
subscription is in good standing.

Revocations and subscription state live in the existing cache layer: memory by
default, or the optional shared Upstash/Supabase backend when one is configured.
With memory only, a revocation reaches other instances when their cached
subscription state expires (``SUBSCRIPTION_TTL_S``) and is re-read from the
billing resolver (Stripe in test mode, see billing.py).

No key means the free tier. Existing endpoints never require a key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rainout_agent import plans
from rainout_agent.cache import Cache, MemoryCache, get_cache

KEY_RE_PREFIX = "rk_"
MODES = ("test", "live")
MIN_SECRET_LEN = 32
SUBSCRIPTION_TTL_S = 600
REVOCATION_TTL_S = 400 * 86400
GOOD_STANDING = {"active", "trialing", "past_due"}  # past_due = Stripe is retrying the card (dunning grace)

_LOCAL_STORE = MemoryCache(max_entries=4096)
# Billing registers a resolver: subscription_id -> {"status": ..., "plan": ...} or None.
SUBSCRIPTION_RESOLVER: Callable[[str], dict[str, Any] | None] | None = None


class KeyError_(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class KeyInfo:
    key_id: str
    plan: str
    mode: str
    subscription: str | None = None
    customer: str | None = None
    expires_at: int | None = None
    standing: str = "active"
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"key_id": self.key_id, "plan": self.plan, "mode": self.mode, "subscription": self.subscription,
                "expires_at": self.expires_at, "standing": self.standing, "notes": self.notes}


def store() -> Cache:
    cache = get_cache()
    return _LOCAL_STORE if cache.name in {"memory", "none"} else cache


def _secret(env: dict[str, str] | None = None) -> bytes | None:
    value = (env if env is not None else os.environ).get("RAINOUT_API_KEY_SECRET") or ""
    return value.encode() if len(value) >= MIN_SECRET_LEN else None


def configured(env: dict[str, str] | None = None) -> bool:
    return _secret(env) is not None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, mode: str, body: str) -> str:
    return _b64(hmac.new(secret, f"{mode}.{body}".encode(), hashlib.sha256).digest()[:24])


def mint_key(plan: str, *, mode: str = "test", subscription: str | None = None, customer: str | None = None,
             key_id: str | None = None, expires_at: int | None = None, env: dict[str, str] | None = None) -> str:
    secret = _secret(env)
    if secret is None:
        raise KeyError_("keys_not_configured", "Set RAINOUT_API_KEY_SECRET (32+ characters) to issue keys.")
    if plan not in plans.PAID:
        raise KeyError_("invalid_plan", f"Unknown paid plan {plan!r}.")
    if mode not in MODES:
        raise KeyError_("invalid_mode", "mode must be test or live.")
    payload = {"v": 1, "kid": key_id or secrets.token_hex(8), "plan": plan}
    if subscription:
        payload["sub"] = subscription
    if customer:
        payload["cus"] = customer
    if expires_at:
        payload["exp"] = int(expires_at)
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{KEY_RE_PREFIX}{mode}_{body}.{_sign(secret, mode, body)}"


def key_id_for_subscription(subscription: str, env: dict[str, str] | None = None) -> str:
    """Stable key id per subscription, so the checkout success page can show the same key again."""
    secret = _secret(env) or b""
    return hmac.new(secret, f"kid.{subscription}".encode(), hashlib.sha256).hexdigest()[:16]


def key_from_headers(headers: dict[str, str] | None) -> str | None:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    key = (h.get("x-api-key") or "").strip()
    if key:
        return key
    auth = (h.get("authorization") or "").strip()
    if auth.lower().startswith("bearer ") and auth[7:].strip().startswith(KEY_RE_PREFIX):
        return auth[7:].strip()
    return None


def revoke(key_id: str, reason: str = "revoked") -> None:
    store().set(f"apikey:revoked:{key_id}", {"reason": reason, "at": int(time.time())}, REVOCATION_TTL_S)


def unrevoke(key_id: str) -> None:
    store().set(f"apikey:revoked:{key_id}", None, 1)


def is_revoked(key_id: str) -> bool:
    return bool(store().get(f"apikey:revoked:{key_id}"))


def set_subscription_state(subscription: str, status: str, plan: str | None = None, ttl: float = SUBSCRIPTION_TTL_S) -> None:
    store().set(f"billing:sub:{subscription}", {"status": status, "plan": plan}, ttl)


def subscription_state(subscription: str) -> dict[str, Any] | None:
    cached = store().get(f"billing:sub:{subscription}")
    if cached is not None:
        return cached
    if SUBSCRIPTION_RESOLVER is None:
        return None
    try:
        state = SUBSCRIPTION_RESOLVER(subscription)
    except Exception:  # billing outage: fall back to the signed key (fail open for paying customers)
        return None
    if state:
        set_subscription_state(subscription, state.get("status") or "unknown", state.get("plan"))
    return state


def verify_key(token: str, *, env: dict[str, str] | None = None, now: float | None = None) -> KeyInfo:
    secret = _secret(env)
    if secret is None:
        raise KeyError_("keys_not_configured", "Paid API keys are not enabled on this deployment.")
    if not token or not token.startswith(KEY_RE_PREFIX) or "." not in token:
        raise KeyError_("invalid_api_key", "That is not a Rainout Source API key.")
    head, sig = token.rsplit(".", 1)
    try:
        mode, body = head[len(KEY_RE_PREFIX):].split("_", 1)
    except ValueError as exc:
        raise KeyError_("invalid_api_key", "That is not a Rainout Source API key.") from exc
    if mode not in MODES or not hmac.compare_digest(_sign(secret, mode, body), sig):
        raise KeyError_("invalid_api_key", "API key signature is not valid.")
    try:
        payload = json.loads(_unb64(body))
    except ValueError as exc:
        raise KeyError_("invalid_api_key", "API key payload is unreadable.") from exc
    info = KeyInfo(key_id=str(payload["kid"]), plan=str(payload.get("plan")), mode=mode,
                   subscription=payload.get("sub"), customer=payload.get("cus"), expires_at=payload.get("exp"))
    if info.plan not in plans.PAID:
        raise KeyError_("invalid_api_key", "API key plan is unknown.")
    if info.expires_at and (now or time.time()) >= info.expires_at:
        raise KeyError_("expired_api_key", "This API key has expired.")
    if is_revoked(info.key_id):
        raise KeyError_("revoked_api_key", "This API key was revoked (subscription ended or key rotated).")
    if info.subscription:
        state = subscription_state(info.subscription)
        if state is not None:
            status = state.get("status")
            if status not in GOOD_STANDING:
                raise KeyError_("subscription_inactive", f"The subscription for this key is {status}. Update billing in the customer portal.")
            info.standing = status
            if status == "past_due":
                info.notes.append("Payment failed; Stripe is retrying the card. Update it in the customer portal to keep access.")
            if state.get("plan") in plans.PAID:
                info.plan = state["plan"]
    return info


def resolve_request(headers: dict[str, str] | None, env: dict[str, str] | None = None) -> tuple[KeyInfo | None, KeyError_ | None]:
    """(KeyInfo, None) for a valid key, (None, error) for a bad one, (None, None) for no key (free tier)."""
    token = key_from_headers(headers)
    if not token:
        return None, None
    try:
        return verify_key(token, env=env), None
    except KeyError_ as exc:
        return None, exc

"""Free self-serve API keys (no card): POST /v1/keys.

Abuse limits (counters in the shared cache, atomic on Upstash):
* at most ``PER_CLIENT_PER_DAY`` keys per client IP (hashed, never stored raw) per day,
* at most ``PER_EMAIL_PER_DAY`` keys per email (hashed) per day,
* at most ``GLOBAL_PER_DAY`` new free keys per day across the service,
* a hidden honeypot form field (``website``) that must stay empty.
Email is optional. If given it is kept only so upgrades and notices can reach the
holder; it is never shown back or used for anything else.
Anonymous (no-key) access is unaffected by all of this.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from rainout_agent import api_keys, plans

PER_CLIENT_PER_DAY = 3
PER_EMAIL_PER_DAY = 2
GLOBAL_PER_DAY = 500
META_TTL_S = 400 * 86400
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[A-Za-z]{2,}$")


class SignupError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _h(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:20]


def issue_free_key(data: dict[str, Any], client: str | None, now: float | None = None) -> dict[str, Any]:
    if not api_keys.configured():
        raise SignupError(503, "keys_not_configured", "Free keys are not enabled on this deployment yet. The API works without a key.")
    if (data.get("website") or "").strip():
        raise SignupError(400, "rejected", "Signup rejected.")
    email = (data.get("email") or "").strip()
    if email and (len(email) > 254 or not EMAIL_RE.match(email)):
        raise SignupError(400, "invalid_email", "That email does not look valid. Email is optional; leave it out if you like.")
    label = re.sub(r"[^\w .@-]", "", str(data.get("label") or data.get("name") or ""))[:60] or None
    now = now or time.time()
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
    retry = 86400 - int(now % 86400)
    store = api_keys.store()
    checks = [(f"signup:client:{_h(client or 'anonymous')}:{day}", PER_CLIENT_PER_DAY, "Too many keys from this network today.")]
    if email:
        checks.append((f"signup:email:{_h(email)}:{day}", PER_EMAIL_PER_DAY, "Too many keys for this email today."))
    checks.append((f"signup:global:{day}", GLOBAL_PER_DAY, "Free signups are paused for today; try again tomorrow."))
    for key, limit, msg in checks:
        if int(store.get_fresh(key) or 0) >= limit:
            raise SignupError(429, "signup_limited", msg + " The API still works without a key.", retry)
    for key, _, _ in checks:
        store.incr(key, 1, 2 * 86400)
    key_id = "f" + secrets.token_hex(8)
    token = api_keys.mint_key(plans.FREE_KEY, key_id=key_id)
    store.set(f"apikey:meta:{key_id}", {"created_at": int(now), "label": label, "email": email or None,
                                        "client": _h(client or "anonymous")}, META_TTL_S)
    plan = plans.get_plan(plans.FREE_KEY)
    return {
        "api_key": token,
        "key_id": key_id,
        "plan": plans.FREE_KEY,
        "limits": {k: plan[k] for k in ("rate_per_minute", "monthly_units", "batch_max_items", "max_teams_per_call")},
        "how_to_use": "Send it as the X-API-Key header (or Authorization: Bearer). Keep it secret; it is shown only once.",
        "dashboard": "GET /v1/account with X-API-Key",
        "anonymous_access": "Every existing endpoint still works without a key; the key adds a dashboard and bigger batches.",
        "upgrade": "See /v1/plans for paid plans once you need more.",
    }

"""Plans for the hosted API: the free public tier plus paid API-key plans.

The anonymous tier is exactly today's public API: every existing endpoint stays
open with no key and no new limits. A free self-serve key (no card) adds a
dashboard and bigger batches; paid plans add capability on new endpoints
(batch checks, a whole league in one call, richer decisions, a usage dashboard,
an SLA) and per-key limits that only apply to those paid features.

Prices are proposals; Stripe price IDs come from env vars (see docs/billing-setup.md).
"""
from __future__ import annotations

from typing import Any

FREE = "free"

PLANS: dict[str, dict[str, Any]] = {
    FREE: {
        "name": "Free",
        "price_usd_month": 0,
        "requires_key": False,
        "rate_per_minute": None,  # existing endpoints: no limit, same as before paid plans existed
        "monthly_units": None,
        "batch_max_items": 3,
        "max_teams_per_call": 1,
        "history_years": 10,
        "decision_detail": "basic",
        "alert_watches": 0,
        "sla": None,
        "support": "GitHub issues",
        "features": [
            "Every existing endpoint (status, rain, fields, teams, tracks, reports, MCP) with no key and no new limits",
            "Batch checks of up to 3 games per call",
            "should_we_cancel: play / delay / cancel recommendation",
        ],
    },
    "free_key": {
        "name": "Free key",
        "price_usd_month": 0,
        "requires_key": True,
        "self_serve": True,
        "rate_per_minute": 30,
        "monthly_units": 3_000,
        "batch_max_items": 10,
        "max_teams_per_call": 3,
        "history_years": 10,
        "decision_detail": "basic",
        "alert_watches": 0,
        "sla": None,
        "support": "GitHub issues",
        "features": [
            "Instant, no card: POST /v1/keys (email optional)",
            "Batch checks of up to 10 games or 3 teams per call (anonymous: 3 games / 1 team)",
            "Usage dashboard (GET /v1/account)",
            "Upgrade to a paid plan any time from the dashboard",
        ],
    },
    "coach": {
        "name": "Coach",
        "price_usd_month": 9,
        "requires_key": True,
        "rate_per_minute": 60,
        "monthly_units": 20_000,
        "batch_max_items": 25,
        "max_teams_per_call": 10,
        "history_years": 10,
        "decision_detail": "full",
        "alert_watches": 10,
        "sla": None,
        "support": "Email, best effort",
        "features": [
            "Batch checks of up to 25 games or 10 teams per call",
            "should_we_cancel with an hour-by-hour window, decision deadline and next-check time",
            "Usage dashboard (GET /v1/account)",
            "Webhook alerts when play odds change (10 watches; once alerts ship)",
        ],
    },
    "league": {
        "name": "League",
        "price_usd_month": 29,
        "requires_key": True,
        "rate_per_minute": 300,
        "monthly_units": 150_000,
        "batch_max_items": 100,
        "max_teams_per_call": 60,
        "history_years": 10,
        "decision_detail": "full",
        "alert_watches": 100,
        "sla": None,
        "support": "Email, next business day",
        "features": [
            "A whole league in one call: up to 60 teams or 100 games per batch",
            "Everything in Coach",
            "Webhook alerts when play odds change (100 watches; once alerts ship)",
        ],
    },
    "org": {
        "name": "Organization",
        "price_usd_month": 99,
        "requires_key": True,
        "rate_per_minute": 1_200,
        "monthly_units": 1_000_000,
        "batch_max_items": 250,
        "max_teams_per_call": 250,
        "history_years": 10,
        "decision_detail": "full",
        "alert_watches": 1_000,
        "sla": "99.5% monthly uptime target with service credits (only once hosting is on a paid Vercel plan)",
        "support": "Priority email",
        "features": [
            "Batches of up to 250 games or 250 teams",
            "Everything in League",
            "Uptime SLA and priority support",
        ],
    },
}

# Pay per call with x402 (USDC; Base Sepolia testnet only for now). No account or key.
PAY_PER_CALL = {
    "name": "Pay per call (x402)",
    "price_usd_month": None,
    "price_per_call_usdc": "0.02",
    "requires_key": False,
    "rate_per_minute": None,
    "monthly_units": None,
    "batch_max_items": 50,
    "max_teams_per_call": 25,
    "history_years": 10,
    "decision_detail": "full",
    "alert_watches": 0,
    "sla": None,
    "support": "GitHub issues",
    "features": [
        "POST /v1/premium/batch: up to 50 games or 25 teams per call for 0.02 USDC, paid with x402",
        "No signup: any x402 v2 client with a wallet can pay (testnet only until launch)",
    ],
}
PLANS["pay_per_call"] = PAY_PER_CALL

KEY_PLANS = tuple(name for name, plan in PLANS.items() if plan["requires_key"])  # plans that use an API key
PAID = tuple(name for name in KEY_PLANS if (PLANS[name]["price_usd_month"] or 0) > 0)  # Stripe subscriptions
FREE_KEY = "free_key"


def get_plan(name: str | None) -> dict[str, Any]:
    return PLANS.get(name or FREE) or PLANS[FREE]


def public_plans() -> list[dict[str, Any]]:
    return [{"id": pid, **plan} for pid, plan in PLANS.items()]

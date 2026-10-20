"""Per-key rate limits, monthly quotas and usage counters for paid features.

Counters live in the cache layer. With the shared Upstash/KV backend (production and
preview), they use atomic INCRBY and are exact across all instances. With memory only,
each serverless instance counts on its own (approximate, generous).
Anonymous (no-key) traffic is never counted or limited here.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from rainout_agent import plans
from rainout_agent.api_keys import KeyInfo, store

MONTH_TTL = 40 * 86400
DAY_TTL = 8 * 86400


class LimitExceeded(Exception):
    def __init__(self, code: str, message: str, retry_after: int, usage: dict[str, Any]):
        super().__init__(message)
        self.code, self.message, self.retry_after, self.usage = code, message, retry_after, usage


def _keys(kid: str, now_s: float) -> dict[str, str]:
    dt = datetime.fromtimestamp(now_s, timezone.utc)
    return {"minute": f"usage:{kid}:m:{int(now_s // 60)}", "day": f"usage:{kid}:d:{dt:%Y%m%d}",
            "month": f"usage:{kid}:mo:{dt:%Y%m}"}


def _get(key: str) -> int:
    return int(store().get_fresh(key) or 0)


def _month_reset(now_s: float) -> datetime:
    dt = datetime.fromtimestamp(now_s, timezone.utc)
    return datetime(dt.year + (dt.month == 12), dt.month % 12 + 1, 1, tzinfo=timezone.utc)


def check_and_count(info: KeyInfo, units: int, endpoint: str, now_s: float | None = None) -> dict[str, Any]:
    """Count ``units`` against the key, or raise LimitExceeded without counting."""
    now_s = time.time() if now_s is None else now_s
    plan = plans.get_plan(info.plan)
    keys = _keys(info.key_id, now_s)
    s = store()
    if plan["monthly_units"] and _get(keys["month"]) + units > plan["monthly_units"]:
        retry = int((_month_reset(now_s).timestamp() - now_s))
        raise LimitExceeded("quota_exceeded", f"{plan['name']} includes {plan['monthly_units']} units per month; this request needs {units}.",
                            retry, summary(info, now_s))
    if plan["rate_per_minute"]:
        minute = s.incr(keys["minute"], 1, 120)  # atomic on Upstash; rejected requests count toward the minute
        if minute > plan["rate_per_minute"]:
            raise LimitExceeded("rate_limited", f"{plan['name']} allows {plan['rate_per_minute']} requests per minute.",
                                60 - int(now_s % 60), summary(info, now_s))
    else:
        s.incr(keys["minute"], 1, 120)
    s.incr(keys["day"], units, DAY_TTL)
    s.incr(keys["month"], units, MONTH_TTL)
    s.incr(f"{keys['month']}:ep:{endpoint}", units, MONTH_TTL)
    endpoints = set(s.get_fresh(f"{keys['month']}:eps") or [])
    if endpoint not in endpoints:
        s.set(f"{keys['month']}:eps", sorted(endpoints | {endpoint}), MONTH_TTL)
    return summary(info, now_s)


def summary(info: KeyInfo, now_s: float | None = None) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    plan = plans.get_plan(info.plan)
    keys = _keys(info.key_id, now_s)
    month = _get(keys["month"])
    days = []
    for back in range(6, -1, -1):
        t = now_s - back * 86400
        days.append({"date": datetime.fromtimestamp(t, timezone.utc).date().isoformat(), "units": _get(_keys(info.key_id, t)["day"])})
    eps = store().get_fresh(f"{keys['month']}:eps") or []
    return {
        "plan": info.plan,
        "plan_name": plan["name"],
        "limits": {"rate_per_minute": plan["rate_per_minute"], "monthly_units": plan["monthly_units"],
                   "batch_max_items": plan["batch_max_items"], "max_teams_per_call": plan["max_teams_per_call"]},
        "this_minute": {"requests": _get(keys["minute"]), "resets_in_seconds": 60 - int(now_s % 60)},
        "this_month": {"units": month, "remaining": max(plan["monthly_units"] - month, 0) if plan["monthly_units"] else None,
                       "resets_at": _month_reset(now_s).isoformat()},
        "last_7_days": days,
        "by_endpoint_this_month": {ep: _get(f"{keys['month']}:ep:{ep}") for ep in eps},
        "counting": "per-instance (in-memory cache); approximate" if store().name == "memory" else f"exact, shared ({store().name})",
    }


def rate_headers(usage: dict[str, Any]) -> dict[str, str]:
    lim = usage["limits"]
    out = {"X-Rainout-Plan": usage["plan"]}
    if lim["rate_per_minute"]:
        out["X-RateLimit-Limit"] = str(lim["rate_per_minute"])
        out["X-RateLimit-Remaining"] = str(max(lim["rate_per_minute"] - usage["this_minute"]["requests"], 0))
    if lim["monthly_units"]:
        out["X-Quota-Remaining"] = str(usage["this_month"]["remaining"])
    return out

"""GET /v1/admin/usage?days=N[&format=text]: aggregate usage counters (admin token required)."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any
from urllib.parse import parse_qs

from rainout_agent import metrics

MAX_DAYS = 90


def _token(headers: dict[str, str] | None) -> str:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    auth = (h.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (h.get("x-admin-token") or "").strip()


def handle_admin_usage(query: str, headers: dict[str, str] | None) -> tuple[int, str, bytes]:
    def err(status: int, code: str, message: str) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps({"error": code, "message": message}).encode()

    admin = os.environ.get("RAINOUT_ADMIN_TOKEN") or ""
    if len(admin) < 16:
        return err(503, "admin_not_configured", "Set RAINOUT_ADMIN_TOKEN (16+ characters) to enable this endpoint.")
    sent = _token(headers)
    if not sent or not hmac.compare_digest(sent, admin):
        return err(401, "unauthorized", "Send Authorization: Bearer <admin token>.")
    params = {k: v[0] for k, v in parse_qs(query or "").items()}
    try:
        days = max(1, min(MAX_DAYS, int(params.get("days") or 7)))
    except ValueError:
        return err(400, "invalid_days", f"days must be 1-{MAX_DAYS}.")
    try:
        data = metrics.read_days(days)
    except Exception as exc:  # store unreachable
        return err(502, "metrics_unavailable", f"Could not read counters ({exc.__class__.__name__}).")
    if (params.get("format") or "").lower() == "text":
        text = "\n\n".join(metrics.text_summary(d) for d in data) + "\n"
        return 200, "text/plain; charset=utf-8", text.encode()
    totals: dict[str, Any] = {c: sum(d["requests_total"][c] for d in data) for c in metrics.CLASSES}
    return 200, "application/json", json.dumps({
        "days": days, "timezone": "America/Chicago", "totals": totals,
        "free_keys_signed_up": sum(d["free_keys_signed_up"] for d in data),
        "unique_clients_note": "Per-day HyperLogLog estimates of daily-salted IP hashes; days cannot be summed into uniques.",
        "daily": data}).encode()

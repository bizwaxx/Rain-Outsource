"""HTTP layer for the paid-tier endpoints (served by the api/status.py function).

* ``GET  /v1/plans``    public plan list (free vs paid limits and features).
* ``GET  /v1/account``  usage dashboard for the key holder (needs a key).
* ``/v1/billing/*``      Stripe Checkout, success page (shows the key), portal, webhook.
* ``POST /v1/batch``    many games and/or teams in one call. Free: up to 3 items
  with no key. Paid keys: up to the plan's batch size, counted against quotas.

Existing endpoints are not routed here and never require a key.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import parse_qs

from rainout_agent import api_keys, billing, plans, usage, x402

MAX_BODY = 512_000  # Stripe webhooks can be large; batch bodies are capped at BATCH_MAX_BODY
BATCH_MAX_BODY = 64_000
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key, Authorization, PAYMENT-SIGNATURE",
    "Access-Control-Expose-Headers": "X-Rainout-Plan, X-RateLimit-Limit, X-RateLimit-Remaining, X-Quota-Remaining, Retry-After, PAYMENT-REQUIRED, PAYMENT-RESPONSE",
}
STATUS_KEYS = ("field_id", "field", "game_time", "official_status", "game_time_play_probability_percent", "rain_chance_percent",
               "thunderstorm_likely", "risk_level", "recommendation", "reason", "spoken_answer",
               "official_status_source_url", "source_reliability", "last_checked", "error")
GAME_KEYS = ("game_id", "opponent", "start", "when_spoken", "venue", "league_status", "play_probability", "risk_level",
             "at_risk", "reason")

Response = tuple[int, dict[str, str], bytes]
PREMIUM_PATHS = {"/v1/plans", "/v1/account", "/v1/batch"}
PREMIUM_PREFIXES = ("/v1/billing/", "/v1/premium/")


def is_premium_path(path: str) -> bool:
    p = path.rstrip("/")
    return p in PREMIUM_PATHS or p.startswith(PREMIUM_PREFIXES)


def _json(status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> Response:
    return status, {"Content-Type": "application/json", "Cache-Control": "no-store", **CORS_HEADERS, **(extra or {})}, \
        json.dumps(payload, default=str).encode()


def _error(status: int, code: str, message: str, extra: dict[str, str] | None = None, **fields: Any) -> Response:
    return _json(status, {"error": code, "message": message, "plans_url": "https://rainout-agent-source.vercel.app/v1/plans",
                          **fields}, extra)


def _key_error(exc: api_keys.KeyError_) -> Response:
    status = 503 if exc.code == "keys_not_configured" else 401 if exc.code == "invalid_api_key" else 403
    return _error(status, exc.code, exc.message,
                  next_step="Remove the X-API-Key header to use the free tier, or fix the key.")


def _status_item(item: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent.status_api import build_status_result

    full = build_status_result(field_query=item["field_id"], game_time=item["game_time"])
    return {k: full.get(k) for k in STATUS_KEYS if k in full}


def _team_item(item: dict[str, Any]) -> dict[str, Any]:
    from rainout_agent import teams

    data = teams.team_games(item["team_id"], date_param=item.get("date"), from_param=item.get("from"), to_param=item.get("to"))
    trim = lambda g: {k: g.get(k) for k in GAME_KEYS} if g else None  # noqa: E731
    return {"team": data["team"], "range": data["range"], "count": data["count"], "games": [trim(g) for g in data["games"]],
            "next_game": trim(data.get("next_game")), "message": data["message"]}


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Each item must be an object.")
    if raw.get("team_id"):
        return {"kind": "team", "team_id": str(raw["team_id"]), **{k: raw[k] for k in ("date", "from", "to") if raw.get(k)}}
    field = raw.get("field_id") or raw.get("venue") or raw.get("field")
    if field and raw.get("game_time"):
        return {"kind": "game", "field_id": str(field), "game_time": str(raw["game_time"])}
    raise ValueError("Each item needs team_id, or field_id (or venue) plus game_time.")


def batch(items: list[Any], plan_id: str, *, status_fn: Callable[[dict], dict] = _status_item,
          team_fn: Callable[[dict], dict] = _team_item, workers: int = 8) -> list[dict[str, Any]]:
    def run(pair: tuple[int, Any]) -> dict[str, Any]:
        idx, raw = pair
        try:
            item = _normalize(raw)
        except ValueError as exc:
            return {"index": idx, "ok": False, "error": "invalid_item", "message": str(exc)}
        try:
            result = team_fn(item) if item["kind"] == "team" else status_fn(item)
        except Exception as exc:  # one bad item never fails the batch
            return {"index": idx, "ok": False, "input": item, "error": exc.__class__.__name__, "message": str(exc)[:300]}
        ok = not (isinstance(result, dict) and result.get("error"))
        return {"index": idx, "ok": ok, "input": item, "result": result}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        return list(pool.map(run, enumerate(items)))


def handle_premium_request(method: str, path: str, query: str, body: bytes, headers: dict[str, str] | None = None,
                           **kw: Any) -> Response:
    path = path.rstrip("/") or "/"
    billing.install_resolver()
    if method == "OPTIONS":
        return 204, {**CORS_HEADERS, "Cache-Control": "no-store"}, b""
    if path == "/v1/plans":
        return _json(200, {"plans": plans.public_plans(),
                           "free_tier": "Every existing endpoint stays free and open with no key and no new limits.",
                           "how_to_use_a_key": "Send X-API-Key: <key> (or Authorization: Bearer <key>). No key = free tier.",
                           "signup": "https://rainout-agent-source.vercel.app/v1/billing/checkout?plan=coach",
                           "units": "1 unit = 1 game or 1 team checked in a paid call."})
    if path == "/v1/account":
        info, err = api_keys.resolve_request(headers)
        if err:
            return _key_error(err)
        if not info:
            return _error(401, "api_key_required", "Send X-API-Key to see usage for your key. The free tier needs no key.")
        return _json(200, {"key": info.public(), "usage": usage.summary(info), "plan": plans.get_plan(info.plan)})
    if path == "/v1/batch":
        return _batch(method, body, headers, kw)
    if path == "/v1/premium/batch":
        return _premium_batch(method, body, headers, kw)
    if path.startswith("/v1/billing/"):
        try:
            return _billing(method, path, query, body, headers)
        except billing.BillingError as exc:
            return _error(exc.status, exc.code, exc.message)
    return _error(404, "not_found", "Unknown paid-tier path. See /v1/plans.")


def _batch(method: str, body: bytes, headers: dict[str, str] | None, kw: dict[str, Any]) -> Response:
    if method != "POST":
        return _error(405, "method_not_allowed", 'POST {"items": [{"field_id": "Krieg", "game_time": "..."}, {"team_id": "..."}]}')
    if len(body) > BATCH_MAX_BODY:
        return _error(413, "body_too_large", f"Body must be under {BATCH_MAX_BODY} bytes.")
    info, err = api_keys.resolve_request(headers)
    if err:
        return _key_error(err)
    plan_id = info.plan if info else plans.FREE
    plan = plans.get_plan(plan_id)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _error(400, "invalid_json", "Body must be JSON.")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return _error(400, "missing_items", "Pass items: a non-empty list of {field_id, game_time} or {team_id, date?}.")
    teams_n = sum(1 for i in items if isinstance(i, dict) and i.get("team_id"))
    if len(items) > plan["batch_max_items"] or teams_n > plan["max_teams_per_call"]:
        return _error(413, "batch_too_large",
                      f"The {plan['name']} plan allows {plan['batch_max_items']} items and {plan['max_teams_per_call']} team(s) per call.",
                      plan=plan_id, upgrade="See /v1/plans; larger batches are a paid feature." if not info else None)
    extra: dict[str, str] = {"X-Rainout-Plan": plan_id}
    use = None
    if info:
        try:
            use = usage.check_and_count(info, len(items), "batch")
        except usage.LimitExceeded as exc:
            return _error(429, exc.code, exc.message, {"Retry-After": str(exc.retry_after), "X-Rainout-Plan": plan_id},
                          retry_after_seconds=exc.retry_after, usage=exc.usage,
                          free_tier="Existing single-game endpoints stay free; drop the key to use them.")
        extra.update(usage.rate_headers(use))
    results = batch(items, plan_id, **{k: v for k, v in kw.items() if k in {"status_fn", "team_fn"}})
    at_risk = [r["index"] for r in results if r.get("ok") and _at_risk(r["result"])]
    payload = {"plan": plan_id, "count": len(results), "ok": sum(1 for r in results if r.get("ok")),
               "at_risk_indexes": at_risk, "results": results}
    if use:
        payload["usage"] = {"this_month": use["this_month"], "limits": use["limits"]}
    if info and info.notes:
        payload["notes"] = info.notes
    return _json(200, payload, extra)


def _at_risk(result: dict[str, Any]) -> bool:
    if "games" in result:
        return any((g or {}).get("at_risk") for g in result["games"] + [result.get("next_game")])
    pct = result.get("game_time_play_probability_percent")
    return pct is not None and pct < 70


def _html(status: int, html: str) -> Response:
    return status, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", **CORS_HEADERS}, html.encode()


def _checkout_page(canceled: bool) -> Response:
    rows = "".join(
        f'<form method="post" action="/v1/billing/checkout"><input type="hidden" name="plan" value="{pid}">'
        f'<h2>{p["name"]} &mdash; ${p["price_usd_month"]}/month</h2><ul>{"".join(f"<li>{f}</li>" for f in p["features"])}</ul>'
        f'<button type="submit">Subscribe to {p["name"]}</button></form>'
        for pid, p in plans.PLANS.items() if p["requires_key"])
    note = "<p>Checkout was canceled; nothing was charged.</p>" if canceled else ""
    return _html(200, "<!doctype html><meta charset=utf-8><title>Rainout Source plans</title>"
                      "<h1>Rainout Source paid plans</h1>" + note +
                      "<p>The free public API stays free with no key. Paid plans add batch checks, whole-league calls and more.</p>"
                      + rows)


def _billing(method: str, path: str, query: str, body: bytes, headers: dict[str, str] | None) -> Response:
    params = {k: v[0] for k, v in parse_qs(query or "", keep_blank_values=True).items()}
    action = path[len("/v1/billing/"):]
    if action == "webhook":
        if method != "POST":
            return _error(405, "method_not_allowed", "Stripe sends POST.")
        h = {k.lower(): v for k, v in (headers or {}).items()}
        return _json(200, billing.handle_webhook(body, h.get("stripe-signature") or ""))
    if action == "checkout":
        if method == "GET":
            billing.mode()  # 503 when billing is off or a live key is refused
            return _checkout_page(bool(params.get("canceled")))
        if method != "POST":
            return _error(405, "method_not_allowed", "POST plan=coach|league|org.")
        h = {k.lower(): v for k, v in (headers or {}).items()}
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()} \
            if "form" in (h.get("content-type") or "") else (json.loads(body or b"{}") if body.strip() else {})
        plan = (form.get("plan") or params.get("plan") or "").strip().lower()
        session = billing.create_checkout(plan, email=form.get("email") or None)
        if "form" in (h.get("content-type") or ""):
            return 303, {"Location": session["checkout_url"], "Cache-Control": "no-store", **CORS_HEADERS}, b""
        return _json(200, session)
    if action == "success":
        result = billing.checkout_success(params.get("session_id") or "")
        return _json(200, result)
    if action == "portal":
        info, err = api_keys.resolve_request(headers)
        if err:
            return _key_error(err)
        if not info:
            return _error(401, "api_key_required", "Send X-API-Key to open the billing portal for your subscription.")
        return _json(200, billing.create_portal(info))
    return _error(404, "not_found", "Billing paths: checkout, success, portal, webhook.")


def _premium_batch(method: str, body: bytes, headers: dict[str, str] | None, kw: dict[str, Any]) -> Response:
    """Same as /v1/batch, but callers without a key can pay per call with x402 (testnet)."""
    if api_keys.key_from_headers(headers):
        return _batch(method, body, headers, kw)
    if method != "POST":
        return _error(405, "method_not_allowed", 'POST {"items": [...]} with a PAYMENT-SIGNATURE header (x402 v2).')
    if len(body) > BATCH_MAX_BODY:
        return _error(413, "body_too_large", f"Body must be under {BATCH_MAX_BODY} bytes.")
    plan = plans.PAY_PER_CALL
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _error(400, "invalid_json", "Body must be JSON.")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return _error(400, "missing_items", "Pass items: a non-empty list of {field_id, game_time} or {team_id, date?}.")
    teams_n = sum(1 for i in items if isinstance(i, dict) and i.get("team_id"))
    if len(items) > plan["batch_max_items"] or teams_n > plan["max_teams_per_call"]:
        return _error(413, "batch_too_large", f"Pay-per-call allows {plan['batch_max_items']} items and {plan['max_teams_per_call']} teams.")

    def run() -> Response:
        results = batch(items, "pay_per_call", **{k: v for k, v in kw.items() if k in {"status_fn", "team_fn"}})
        if not any(r.get("ok") for r in results):
            return _error(502, "no_results", "Every item failed, so no payment was taken. Check the items and retry.",
                          results=results)
        at_risk = [r["index"] for r in results if r.get("ok") and _at_risk(r["result"])]
        return _json(200, {"plan": "pay_per_call", "count": len(results), "ok": sum(1 for r in results if r.get("ok")),
                           "at_risk_indexes": at_risk, "results": results}, {"X-Rainout-Plan": "pay_per_call"})

    return x402.paywall(headers, f"{billing.public_url()}/v1/premium/batch",
                        f"Batch rainout check: up to {plan['batch_max_items']} games or {plan['max_teams_per_call']} teams", run,
                        env=kw.get("env"), now=kw.get("now"))

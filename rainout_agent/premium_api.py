"""HTTP layer for the paid-tier endpoints (one Vercel function: api/premium.py).

* ``GET  /v1/plans``    public plan list (free vs paid limits and features).
* ``GET  /v1/account``  usage dashboard for the key holder (needs a key).
* ``POST /v1/batch``    many games and/or teams in one call. Free: up to 3 items
  with no key. Paid keys: up to the plan's batch size, counted against quotas.

Existing endpoints are not routed here and never require a key.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import parse_qs

from rainout_agent import api_keys, plans, usage

MAX_BODY = 64_000
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key, Authorization",
    "Access-Control-Expose-Headers": "X-Rainout-Plan, X-RateLimit-Limit, X-RateLimit-Remaining, X-Quota-Remaining, Retry-After",
}
STATUS_KEYS = ("field_id", "field", "game_time", "official_status", "game_time_play_probability_percent", "rain_chance_percent",
               "thunderstorm_likely", "risk_level", "recommendation", "reason", "spoken_answer",
               "official_status_source_url", "source_reliability", "last_checked", "error")
GAME_KEYS = ("game_id", "opponent", "start", "when_spoken", "venue", "league_status", "play_probability", "risk_level",
             "at_risk", "reason")

Response = tuple[int, dict[str, str], bytes]


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
    return _error(404, "not_found", "Unknown paid-tier path. See /v1/plans.")


def _batch(method: str, body: bytes, headers: dict[str, str] | None, kw: dict[str, Any]) -> Response:
    if method != "POST":
        return _error(405, "method_not_allowed", 'POST {"items": [{"field_id": "Krieg", "game_time": "..."}, {"team_id": "..."}]}')
    if len(body) > MAX_BODY:
        return _error(413, "body_too_large", f"Body must be under {MAX_BODY} bytes.")
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

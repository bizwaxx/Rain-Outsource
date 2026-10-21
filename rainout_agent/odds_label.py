"""Every odds answer says plainly that the odds are an estimate, puts the official status first and links to the
accuracy note. Applied once where each answer is serialised (status, schedule, teams, tracks, MCP tools)."""
from __future__ import annotations

from typing import Any

ACCURACY_URL = "https://rainout-agent-source.vercel.app/accuracy"
ODDS_NOTE = ("These odds are an estimate from the forecast and rain history, not an official call. "
             "The official status comes first and always wins.")
ODDS_KEYS = ("game_time_play_probability_percent", "play_probability_percent", "play_probability")


def _has_odds(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        return any(k in value and value[k] is not None for k in ODDS_KEYS) or any(_has_odds(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_has_odds(v, depth + 1) for v in value[:200])
    return False


def apply(payload: Any) -> Any:
    """Label a JSON answer that carries odds. Returns the same dict (official status keys moved first)."""
    if not isinstance(payload, dict) or "error" in payload or not _has_odds(payload):
        return payload
    first = [k for k in ("creator",) if k in payload] + [k for k in payload if k.startswith("official_status")]
    ordered = {k: payload[k] for k in first}
    ordered.update({"odds_are": "estimate", "odds_note": ODDS_NOTE, "accuracy_url": ACCURACY_URL})
    ordered.update({k: v for k, v in payload.items() if k not in ordered})
    spoken = ordered.get("spoken_answer")
    if isinstance(spoken, str) and spoken and "estimate" not in spoken.lower():
        ordered["spoken_answer"] = spoken.rstrip() + " These odds are an estimate; the official status always wins."
    payload.clear()
    payload.update(ordered)
    return payload

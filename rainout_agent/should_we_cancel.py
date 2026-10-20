"""should_we_cancel: the organizer's call (play / delay / cancel / wait_until) on top of go_no_go.

go_no_go answers "is it on?" for anyone (go | wait | no_go | no_event_found). An organizer asks a
different question, "what should I do, and when?", and time to start matters as much as the
odds: a 35% game three days out is "wait until the forecast firms up", the same 35% an hour
before first pitch is "delay" (if it clears soon) or "cancel". This module only adds that
decision layer. Odds, official status, NOAA history and sources all come from go_no_go
unchanged, so the two tools never disagree on the facts.

recommendation:
* play       go_no_go says go.
* delay      bad or iffy now, but a later start within a few hours looks like a go (delay_to).
* cancel     officially cancelled already, or bad close to start with no workable delay.
* wait_until too early to call; check again at ``wait_until``.

Basic (free, REST and MCP): recommendation, play probability, plain reason, check_again_at.
Full (paid API key, counted as 3 units): adds hour-by-hour start options, a call-by deadline
and a ready-to-send message for players or crew.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from rainout_agent import go_no_go as gng
from rainout_agent import rain

RECOMMENDATIONS = ("play", "delay", "cancel", "wait_until")
CANCELLED = {"cancelled", "canceled", "closed", "field_closed", "field closed", "postponed", "forfeit"}
NEAR_HOURS = 6          # within this, a no_go is decided now (delay or cancel)
LAST_CALL_HOURS = 2     # within this, an iffy "wait" is decided now
EARLY_CANCEL_PERCENT = 25  # a no_go this unlikely within 24 h: cancel early so nobody travels
DELAY_SCAN_HOURS = 3
LEAD_HOURS = {"motocross": 12, "concrete_pour": 12}  # call-by lead; sports/other work default 2 h
FULL_UNITS = 3
UPGRADE_NOTE = ("A paid API key adds hour-by-hour start options, a call-by deadline and a ready-to-send team "
                "message (detail=full). See /v1/plans.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None, tz: Any) -> str | None:
    return dt.astimezone(tz).replace(microsecond=0).isoformat() if dt else None


def _team_game(team_id: str, time: str | None, now: datetime) -> dict[str, Any]:
    from rainout_agent import team_sources, teams

    when = (time or "today").strip()
    date_part = when[:10] if len(when) >= 10 and when[4] == "-" else when
    try:
        data = teams.team_games(team_id, date_param=date_part, now=now)
    except teams.TeamsError as exc:
        raise rain.RainError(str(exc)) from exc
    except team_sources.UnknownTeam as exc:
        raise rain.RainError("Unknown team_id. Call find_team first.") from exc
    games = data.get("games") or []
    if not games:
        raise rain.RainError(data.get("message") or "No game for that team on that date.")
    if len(when) > 10:  # a clock time was given: nearest game
        try:
            target = datetime.fromisoformat(when.replace(" ", "T"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=datetime.fromisoformat(games[0]["start"]).tzinfo)
            games = sorted(games, key=lambda g: abs((datetime.fromisoformat(g["start"]) - target).total_seconds()))
        except ValueError:
            pass
    return {"game": games[0], "team": data.get("team") or {}}


def _paid(headers: dict[str, str] | None) -> tuple[bool, str | None]:
    """(allowed, note). Needs the API-key module from the paid-tier PR; without it, basic only."""
    if not headers:
        return False, None
    try:
        from rainout_agent import api_keys, plans, usage  # type: ignore[attr-defined]
    except ImportError:
        return False, None
    try:
        info, err = api_keys.resolve_request(headers)
        if err or info is None:
            return False, (err.message + " Showing the free answer.") if err else None
        if info.plan not in plans.PAID:
            return False, "Your key is on the free plan; detail=full needs a paid plan (/v1/plans)."
        usage.check_and_count(info, FULL_UNITS, "should_we_cancel")
        return True, None
    except Exception as exc:  # quota, rate limit or store trouble: never worse than free
        return False, f"{getattr(exc, 'args', ['Paid detail unavailable'])[0]} Showing the free answer."


def should_we_cancel(activity: str | None = None, *, venue: str | None = None, team_id: str | None = None,
                     track_id: str | None = None, location: str | None = None, lat: Any = None, lon: Any = None,
                     pick: Any = None, time: str | None = None, duration_hours: Any = None, detail: str | None = None,
                     headers: dict[str, str] | None = None, now: datetime | None = None,
                     decide: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    decide = decide or gng.go_no_go
    now = (now or _now()).replace(microsecond=0)
    game = team = None
    if team_id:
        found = _team_game(team_id, time, now)
        game, team = found["game"], found["team"]
        v = game.get("venue") or {}
        venue = v.get("field_id") or venue
        if not venue and not (location or lat not in (None, "")):
            location = v.get("address") or None
        time = game["start"]
    act = activity or (team or {}).get("sport") or ("motocross" if track_id else "softball")
    if not (venue or track_id or location or (lat not in (None, "") and lon not in (None, ""))):
        raise rain.RainError("Pass venue (find_venue), team_id (find_team), track_id (find_track), or a location.")

    def run(at: str | None) -> dict[str, Any]:
        return decide(act, venue=venue, track_id=track_id, location=location, lat=lat, lon=lon, pick=pick,
                      time=at, duration_hours=duration_hours, now=now)

    base = run(time)
    if base.get("ambiguous"):
        return base
    start = datetime.fromisoformat(base["time"]["start"])
    tz = start.tzinfo or timezone.utc
    hours_out = (start - now).total_seconds() / 3600
    p = base.get("probability_percent")
    decision = base.get("decision")
    official = str((base.get("details") or {}).get("official_status") or "unknown").lower()
    recheck = datetime.fromisoformat(base["recheck_at"]) if base.get("recheck_at") else None
    what = "race" if act == "motocross" else "game" if act in gng.SPORTS else "work" if act != "concrete_pour" else "pour"
    rec, delay_to, wait_until, check_again = None, None, None, recheck
    facts = base.get("reason") or ""

    def delay_slot() -> tuple[datetime, dict[str, Any]] | None:
        for k in range(1, DELAY_SCAN_HOURS + 1):
            t = start + timedelta(hours=k)
            try:
                r = run(_iso(t, tz))
            except Exception:
                break
            if r.get("decision") == "go":
                return t, r
        return None

    league_cancelled = bool(game and str(game.get("league_status") or "").lower() in CANCELLED)
    if league_cancelled or official in CANCELLED:
        rec, check_again = "cancel", None
        src = "the league schedule" if league_cancelled else "the official status page"
        reason = f"It's already called off on {src} ({game.get('league_status') if league_cancelled else official}). Tell everyone it's off."
    elif decision == "no_event_found":
        rec, wait_until = "wait_until", recheck
        reason = f"I can't find this {what} listed, so I won't say it's on. Confirm with the organizer or track first. {facts}"
    elif decision == "go":
        rec = "play"
        reason = f"Play: about {p}% chance the {what} goes ahead. {facts}"
        check_again = recheck if hours_out > LAST_CALL_HOURS else None
    elif decision == "no_go":
        if hours_out <= NEAR_HOURS:
            slot = delay_slot()
            if slot:
                rec, delay_to = "delay", slot[0]
                reason = (f"Delay to {slot[0].astimezone(tz).strftime('%-I:%M %p')}: the start looks bad ({p}% chance), "
                          f"but it clears enough by then ({slot[1].get('probability_percent')}%). {facts}")
                check_again = max(now + timedelta(minutes=30), slot[0] - timedelta(hours=1))
            else:
                rec, check_again = "cancel", None
                reason = f"Cancel: only about {p}% chance the {what} can happen and no clear window in the next {DELAY_SCAN_HOURS} hours. {facts}"
        elif hours_out <= 24 and p is not None and p < EARLY_CANCEL_PERCENT:
            rec, check_again = "cancel", None
            reason = f"Cancel early: only about {p}% chance, so calling it now saves everyone the trip. {facts}"
        else:
            rec, wait_until = "wait_until", recheck
            reason = f"Too early to call: it looks bad now ({p}%), but forecasts this far out change. {facts}"
    else:  # wait or unknown odds
        if hours_out <= LAST_CALL_HOURS:
            slot = delay_slot()
            if slot:
                rec, delay_to = "delay", slot[0]
                reason = f"Delay to {slot[0].astimezone(tz).strftime('%-I:%M %p')}: it's iffy at the start ({p}%) and better then. {facts}"
                check_again = max(now + timedelta(minutes=30), slot[0] - timedelta(hours=1))
            elif (p or 0) >= 55:
                rec, check_again = "play", now + timedelta(minutes=30)
                reason = f"Play, but watch the sky: about {p}% chance and no better slot soon. {facts}"
            else:
                rec, check_again = "cancel", None
                reason = f"Cancel: only about {p}% chance right before the start and no better slot soon. {facts}"
        else:
            lead = LEAD_HOURS.get(act, LAST_CALL_HOURS)
            wait_until = min(x for x in (recheck, start - timedelta(hours=lead)) if x) if recheck else start - timedelta(hours=lead)
            wait_until = max(wait_until, now + timedelta(minutes=30))
            rec = "wait_until"
            reason = f"Not decided yet: about {p}% chance. Check again at {wait_until.astimezone(tz).strftime('%a %-I:%M %p')}. {facts}"
    if rec == "wait_until":
        check_again = wait_until

    out: dict[str, Any] = {
        "recommendation": rec,
        "play_probability_percent": p,
        "reason": reason.strip(),
        "check_again_at": _iso(check_again, tz),
        "delay_to": _iso(delay_to, tz),
        "wait_until": _iso(wait_until, tz) if rec == "wait_until" else None,
        "event": {"activity": base.get("activity", act), "start": base["time"]["start"], "location": base.get("location"),
                  **({"team": game.get("team"), "opponent": game.get("opponent"), "game_id": game.get("game_id")} if game else {})},
        "based_on": {"tool": "go_no_go", "decision": decision, "weather_decision": base.get("weather_decision"),
                     "event_confirmed": base.get("event_confirmed"), "routed_to": base.get("routed_to"),
                     "official_status": official},
        "sources": base.get("sources") or [],
        "detail": "basic",
    }
    want_full = str(detail or "").lower() == "full"
    allowed, note = _paid(headers) if want_full else (False, None)
    if want_full and allowed:
        out.update(_full(run, start, now, tz, act, rec, p, delay_to, out))
    else:
        out["full_detail"] = note or UPGRADE_NOTE
    return out


def _full(run: Callable[[str | None], dict[str, Any]], start: datetime, now: datetime, tz: Any, act: str, rec: str,
          p: Any, delay_to: datetime | None, out: dict[str, Any]) -> dict[str, Any]:
    options = []
    for k in range(-2, 5):
        t = start + timedelta(hours=k)
        if t < now:
            continue
        try:
            r = run(_iso(t, tz))
        except Exception:
            continue
        options.append({"start": _iso(t, tz), "offset_hours": k, "decision": r.get("decision"),
                        "play_probability_percent": r.get("probability_percent")})
    best = max((o for o in options if o["play_probability_percent"] is not None),
               key=lambda o: (o["play_probability_percent"], -abs(o["offset_hours"])), default=None)
    lead = LEAD_HOURS.get(act, LAST_CALL_HOURS)
    deadline = max(now, start - timedelta(hours=lead))
    when = start.astimezone(tz).strftime("%a %b %-d, %-I:%M %p")
    msg = {
        "play": f"We're ON for {when}. Weather looks OK ({p}% chance it goes ahead). We'll update you if that changes.",
        "delay": f"Weather delay: we're moving the {when} start to {delay_to.astimezone(tz).strftime('%-I:%M %p') if delay_to else 'later'}. Watch for updates.",
        "cancel": f"CANCELLED: {when} is off because of weather. We'll share a makeup date soon.",
        "wait_until": f"Heads up: weather may affect {when}. We'll make the call by {deadline.astimezone(tz).strftime('%a %-I:%M %p')}.",
    }[rec]
    return {"detail": "full", "start_options": options, "best_start": best,
            "decision_deadline": _iso(deadline, tz), "notify_message": msg}

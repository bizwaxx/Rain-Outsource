import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import teamsnap
from rainout_agent.teamsnap import (
    ESTIMATE_LABEL, FixtureSource, TeamSnapError, TokenSource, default_source, events_to_games, parse_collection, team_odds,
)

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
TEAM = "9000001"


def periods(rain):
    base = NOW.replace(minute=0)
    return [{"startTime": (base + timedelta(hours=h)).isoformat(), "endTime": (base + timedelta(hours=h + 1)).isoformat(),
             "probabilityOfPrecipitation": {"value": rain}, "shortForecast": "Rain" if rain >= 60 else "Sunny"}
            for h in range(156)]


def run(rain=10, official="unknown"):
    calls = []

    def official_provider(field, timeout):
        calls.append(field["id"])
        return {"official_status": official, "checked": True}

    return team_odds(TEAM, source=FixtureSource(), now=NOW,
                     forecast_provider=lambda f, t: periods(rain(f) if callable(rain) else rain),
                     official_provider=official_provider), calls


def test_parse_collection_flattens_items():
    doc = {"collection": {"items": [{"href": "h/1", "data": [{"name": "id", "value": 1}, {"name": "is_game", "value": True}]}]}}
    assert parse_collection(doc) == [{"id": 1, "is_game": True, "_href": "h/1"}]


def test_parse_collection_rejects_errors_and_non_collections():
    with pytest.raises(TeamSnapError):
        parse_collection({"collection": {"error": {"title": "Unauthorized"}}})
    with pytest.raises(TeamSnapError):
        parse_collection({"nope": 1})


def test_events_to_games_keeps_games_only_and_maps_location_opponent_cancel():
    src = FixtureSource()
    games = events_to_games(src.events(TEAM), src.locations(TEAM), src.opponents(TEAM), "Hawks")
    assert [g["teamsnap_event_id"] for g in games] == [5001, 5003, 5004, 5005]  # practice 5002 dropped
    first, cancelled = games[0], games[1]
    assert first["home"] == "Hawks" and first["away"] == "Bats"
    assert first["venue_query"] == "Krieg Field Softball Complex"
    assert first["end"] - first["start"] == timedelta(minutes=90)
    assert cancelled["calendar_cancelled"] and cancelled["home"] == "Comets"  # away game


def test_team_odds_official_status_first_and_estimate_label():
    result, _ = run(rain=10)
    assert result["data_source"] == "fixtures"
    assert result["team"]["name"].startswith("ATX Hawks")
    games = result["games"]
    assert len(games) == 4
    for g in games:
        assert list(g)[:2] == ["official_status", "play_odds"]
        assert g["play_odds"]["label"] == "estimate"
    sunny = games[0]
    assert sunny["official_status"]["status"] == "not posted"
    assert sunny["play_odds"]["note"] == ESTIMATE_LABEL
    assert sunny["play_odds"]["percent"] >= 80


def test_teamsnap_cancel_wins_over_weather():
    result, _ = run(rain=0)
    cancelled = next(g for g in result["games"] if g["teamsnap_event_id"] == 5003)
    assert cancelled["official_status"] == {"status": "cancelled", "source": "TeamSnap (marked cancelled by the team)"}
    assert cancelled["play_odds"]["percent"] == 0


def test_rain_lowers_estimate_and_unknown_venue_gets_no_estimate():
    result, _ = run(rain=lambda f: 90 if "krieg" in f["id"] else 10)
    krieg = result["games"][0]
    assert krieg["play_odds"]["percent"] < 60
    backyard = next(g for g in result["games"] if g["teamsnap_event_id"] == 5005)
    assert backyard["play_odds"]["percent"] is None and backyard["venue"] is None


def test_venue_official_status_is_reported_when_posted():
    near = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)  # 3 h before the Krieg game, inside the poll window
    result = team_odds(TEAM, source=FixtureSource(), now=near,
                       forecast_provider=lambda f, t: periods(10),
                       official_provider=lambda f, t: {"official_status": "cancelled", "checked": True})
    krieg = result["games"][0]
    assert krieg["official_status"]["status"] == "cancelled"
    assert krieg["official_status"]["source"].startswith("https://www.austintexas.gov")
    assert krieg["play_odds"]["percent"] == 0


def test_default_source_uses_fixtures_without_token_and_api_with_it():
    assert isinstance(default_source({}), FixtureSource)
    src = default_source({"TEAMSNAP_TOKEN": "secret-abc"})
    assert isinstance(src, TokenSource)
    assert "secret-abc" not in repr(src)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_token_source_sends_bearer_to_teamsnap_only_and_parses():
    seen = []
    body = (teamsnap.FIXTURE_DIR / "events.json").read_bytes()

    def opener(req, timeout):
        seen.append(req)
        return FakeResp(body)

    src = TokenSource("secret-abc", opener=opener)
    rows = src.events(TEAM, started_after=NOW)
    assert len(rows) == 5
    req = seen[0]
    assert req.full_url.startswith("https://api.teamsnap.com/v3/events/search?team_id=9000001")
    assert "started_after=2026-10-30T15%3A00%3A00Z" in req.full_url
    assert req.get_header("Authorization") == "Bearer secret-abc"
    assert req.get_header("Accept") == "application/vnd.collection+json"


def test_token_source_errors_do_not_leak_token():
    def opener(req, timeout):
        raise OSError("connection refused for " + req.get_header("Authorization"))

    with pytest.raises(TeamSnapError) as exc:
        TokenSource("secret-abc", opener=opener).locations(TEAM)
    assert "secret-abc" not in str(exc.value) and exc.value.__cause__ is None


def test_cli_demo_runs_offline(capsys, monkeypatch):
    monkeypatch.delenv("TEAMSNAP_TOKEN", raising=False)
    import importlib.util
    spec = importlib.util.spec_from_file_location("teamsnap_odds", teamsnap.FIXTURE_DIR.parents[2] / "scripts" / "teamsnap_odds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data_source"] == "fixtures" and len(out["games"]) == 4
    assert any("Demo forecast" in w for w in out["warnings"])

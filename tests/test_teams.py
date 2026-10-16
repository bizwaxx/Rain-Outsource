"""Team search and per-team games (fixtures are real public league data from Oct 15, 2026)."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rainout_agent import team_sources as ts
from rainout_agent import teams
from rainout_agent.safe_fetch import FetchResult
from rainout_agent.schedule import check_schedule, parse_ics
from rainout_agent.teams_api import handle_teams_request, route

FIX = Path(__file__).parent / "fixtures" / "teams"
NOW = datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)  # Thu Oct 15, 4 PM CDT
ATX_BOYS = "tsl-340-744621-4306365"
GENERALS = "ambl-2026fall-30"


class FakeLeagueHTTP:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, timeout=6.0, accept="application/json"):
        self.calls.append(url)
        if self.fail:
            raise ts.SourceError("cms-api.teamsideline.com: TimeoutError")
        if url.startswith(ts.TSL_API + "/Schedules?externalUID=340"):
            return (FIX / "tsl_schedules_340.json").read_bytes()
        if "/Teams?" in url:
            return (FIX / "tsl_teams_744621.json").read_bytes() if "divisionId=744621" in url else b"[]"
        if "/Schedules/Games?" in url:
            return (FIX / "tsl_games_744621.json").read_bytes() if "divisionId=744621" in url else b"[]"
        if url == "https://www.austinmetrobaseball.com/schedule.php":
            return (FIX / "ambl_schedule_index.html").read_bytes()
        if "austinmetrobaseball.com/schedule.php?" in url:
            if "month=10" in url and "team=30" in url:
                return (FIX / "ambl_generals_2026_10.html").read_bytes()
            return b"<html><table></table></html>"
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def league_http(monkeypatch):
    fake = FakeLeagueHTTP()
    monkeypatch.setattr(ts, "_http_get", fake)
    return fake


def forecast(rain=10, text="Partly Cloudy"):
    def provider(field, timeout):
        base = NOW.replace(minute=0)
        return [{"startTime": (base + timedelta(hours=i)).isoformat(), "endTime": (base + timedelta(hours=i + 1)).isoformat(),
                 "probabilityOfPrecipitation": {"value": rain}, "shortForecast": text, "detailedForecast": ""} for i in range(160)]
    return provider


def make_checker(rain=10, text="Partly Cloudy", official="unknown"):
    def checker(games, **kw):
        return check_schedule(games, forecast_provider=forecast(rain, text),
                              official_provider=lambda f, t: {"official_status": official, "checked": True}, **kw)
    return checker


# ------------------------------------------------------------------- search
def test_search_finds_atx_boys_at_craig_fields_friday(league_http):
    r = teams.search_teams("ATX Boys", venue="Craig Fields", sport="softball", day="friday")
    top = r["candidates"][0]
    assert top["team_id"] == ATX_BOYS and top["name"] == "ATX Boys"
    assert top["confidence"] >= 0.85
    assert top["venue"]["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert top["day"] == "friday" and top["sport"] == "softball"
    assert top["division"] == "Friday Softball 503 Men 0HR South"
    assert top["schedule_source_url"] == "https://www.capitalcityathletics.com/schedule?divisionid=744621"
    assert r["next_step"] == "confirm_with_user"
    assert r["query"]["venue_resolved"]["name"] == "Krieg Field Softball Complex"
    assert all(c["name"] != "Bad Boyz" for c in r["candidates"])  # no weak look-alikes


def test_day_filter_skips_other_nights(league_http):
    teams.search_teams("ATX Boys", day="friday")
    team_calls = [u for u in league_http.calls if "/Teams?" in u]
    assert team_calls and all("divisionId=7446" in u for u in team_calls)
    assert not any("divisionId=744574" in u for u in team_calls)  # Tuesday division not fetched


def test_search_finds_ambl_generals(league_http):
    r = teams.search_teams("The Generals", sport="baseball")
    assert r["candidates"][0]["team_id"] == GENERALS
    assert r["candidates"][0]["league"] == "Austin Metro Baseball League (AMBL)"
    assert r["candidates"][0]["season"] == "Fall 2026"


def test_unknown_team_asks_user_for_schedule_link(league_http):
    r = teams.search_teams("Mudcats", venue="Krieg")
    assert r["count"] == 0 and r["next_step"] == "ask_user_for_schedule_url"
    assert "schedule link" in r["message"]


def test_sport_filter_excludes_other_leagues(league_http):
    r = teams.search_teams("Generals", sport="softball")
    assert r["count"] == 0
    assert all(s["source"] != "ambl" for s in r["searched_sources"])


def test_search_requires_query_or_url():
    with pytest.raises(teams.TeamsError):
        teams.search_teams("")
    with pytest.raises(teams.TeamsError):
        teams.search_teams("x", day="someday")


def test_team_lists_are_cached(league_http):
    teams.search_teams("ATX Boys", day="friday")
    n = len(league_http.calls)
    teams.search_teams("ATX Goonz", day="friday")
    assert len(league_http.calls) == n


def test_source_outage_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(ts, "_http_get", FakeLeagueHTTP(fail=True))
    r = teams.search_teams("ATX Boys")
    assert r["next_step"] == "ask_user_for_schedule_url"
    assert {s["status"] for s in r["searched_sources"]} == {"error"}


def test_name_similarity():
    assert ts.name_similarity("ATX Boys", "atx boys") == 1.0
    assert ts.name_similarity("The Generals", "Generals") == 1.0
    assert ts.name_similarity("Mudcats", "Muddawgs") < 0.7
    assert ts.parse_day("Fri") == "friday" and ts.parse_day("Friday Softball") == "friday"


# -------------------------------------------------------------- adapters
def test_tsl_games_for_atx_boys(league_http):
    team, games = ts.tsl_games(ATX_BOYS)
    assert team["name"] == "ATX Boys" and len(games) == 8
    g = next(x for x in games if x["start"].date().isoformat() == "2026-10-16")
    assert g["opponent"] == "The Vatos" and g["home_away"] == "away"
    assert g["start"].isoformat() == "2026-10-16T20:20:00-05:00" and g["end"].isoformat() == "2026-10-16T21:15:00-05:00"
    assert g["field_label"] == "Krieg 05" and g["venue_field_id"] == "austin-tx-krieg-field-softball-complex"
    statuses = {x["start"].date().isoformat(): x["league_status"] for x in games}
    assert statuses["2026-09-25"] == "final" and statuses["2026-10-09"] == "forfeit"
    nov = next(x for x in games if x["start"].month == 11)
    assert nov["start"].utcoffset() == timedelta(hours=-6)  # DST handled


def test_tsl_status_mapping():
    assert ts._tsl_status("") == "scheduled"
    assert ts._tsl_status("Rainout") == "cancelled"
    assert ts._tsl_status("Cancelled - Weather") == "cancelled"
    assert ts._tsl_status("Postponed") == "postponed"
    assert ts._tsl_status("Forfeit by home") == "forfeit"


def test_ambl_schedule_parsing_and_statuses(league_http):
    team, games = ts.ambl_games(GENERALS)
    assert team["name"] == "Generals"
    by_day = {g["start"].day: g for g in games}
    assert by_day[4]["league_status"] == "cancelled" and "Rained out" in by_day[4]["league_status_text"]
    assert by_day[4]["start"].hour == 19  # "7:00" is evening
    assert by_day[10]["league_status"] == "final" and by_day[10]["venue_field_id"] == "georgetown-tx-san-gabriel-park-gyba-field-8-ambl"
    assert by_day[18]["opponent"] == "Royals" and by_day[18]["home_away"] == "away"
    assert by_day[18]["venue_field_id"] == "austin-tx-downs-field-ambl"
    assert by_day[25]["start"].hour == 13 and by_day[25]["home_away"] == "home"


def test_ambl_morning_times():
    page = "<tr><td></td><td>1</td><td>3</td><td>Sunday</td><td>10:00</td><td>A</td><td></td><td>B</td><td></td><td>Downs</td><td>A</td></tr>"
    assert ts.parse_ambl_schedule(page, 2026, 10)[0]["hour"] == 10


def test_bad_team_ids():
    for bad in ("tsl-1-2", "ambl-2026summer-1", "url-%%%"):
        with pytest.raises(ts.UnknownTeam):
            teams.load_team(bad)


# ----------------------------------------------------------------- games
def test_do_i_have_a_game_today_points_to_next_game(league_http):
    r = teams.team_games(ATX_BOYS, date_param="today", now=NOW, checker=make_checker(rain=5))
    assert r["count"] == 0
    assert r["message"] == "ATX Boys has no games today. Next game: tomorrow at 8:20 PM vs The Vatos."
    g = r["next_game"]
    assert g["play_probability"] == 96
    assert g["timezone"] == "America/Chicago"
    assert g["reason"].count(". ") == 0 and g["reason"].endswith(".")
    assert "Krieg Field Softball Complex" in g["reason"] and "96%" in g["reason"]
    assert g["status_source"]["league_schedule"]["status"] == "Scheduled"
    assert g["status_source"]["weather"] == "National Weather Service API"


def test_games_on_date_with_rain_risk(league_http):
    r = teams.team_games(ATX_BOYS, date_param="2026-10-16", now=NOW, checker=make_checker(rain=80, text="Thunderstorms"))
    g = r["games"][0]
    assert g["play_probability"] <= 50 and g["at_risk"] is True
    assert "thunderstorms are likely" in g["reason"]


def test_calendar_event_is_google_calendar_shape(league_http):
    g = teams.team_games(ATX_BOYS, date_param="2026-10-16", now=NOW, checker=make_checker())["games"][0]
    ev = g["calendar_event"]
    assert ev["summary"] == "ATX Boys vs The Vatos"
    assert ev["start"] == {"dateTime": "2026-10-16T20:20:00-05:00", "timeZone": "America/Chicago"}
    assert ev["end"]["dateTime"] == "2026-10-16T21:15:00-05:00"
    assert ev["location"].startswith("Krieg 05, Krieg Field Softball Complex, 515 South Pleasant Valley Road")
    assert "Play odds: 96%" in ev["description"] and "512-978-2680" in ev["description"]
    assert re.fullmatch(r"tsl-340-\d+@rainout-agent-source\.vercel\.app", ev["iCalUID"])
    again = teams.team_games(ATX_BOYS, date_param="2026-10-16", now=NOW + timedelta(hours=1), checker=make_checker(rain=40))
    assert again["games"][0]["calendar_event"]["iCalUID"] == ev["iCalUID"]  # stable across checks
    parsed = parse_ics(g["ics"])
    assert len(parsed) == 1 and parsed[0]["id"] == ev["iCalUID"]
    assert parsed[0]["start"] == datetime(2026, 10, 17, 1, 20, tzinfo=timezone.utc)


def test_played_forfeit_and_rainout_games(league_http):
    r = teams.team_games(ATX_BOYS, from_param="2026-09-20", to_param="2026-10-10", now=NOW, checker=make_checker())
    by = {g["start"][:10]: g for g in r["games"]}
    assert by["2026-09-25"]["play_probability"] == 100 and "already played" in by["2026-09-25"]["reason"]
    assert by["2026-10-09"]["play_probability"] == 0 and by["2026-10-09"]["calendar_event"]["status"] == "cancelled"
    amb = teams.team_games(GENERALS, from_param="2026-10-01", to_param="2026-10-31", now=NOW, checker=make_checker())
    rain = next(g for g in amb["games"] if g["start"].startswith("2026-10-04"))
    assert rain["play_probability"] == 0 and "rained out" in rain["reason"]
    future = next(g for g in amb["games"] if g["start"].startswith("2026-10-25"))
    assert future["play_probability"] is not None  # history outlook beyond the forecast


def test_range_validation():
    tz = teams.ZoneInfo("America/Chicago")
    assert teams.resolve_range("today", None, None, tz, NOW)[0].isoformat() == "2026-10-15"
    with pytest.raises(teams.TeamsError):
        teams.resolve_range(None, "2026-10-01", "2026-12-31", tz, NOW)
    with pytest.raises(teams.TeamsError):
        teams.resolve_range("next week", None, None, tz, NOW)


def test_team_calendar_ics(league_http):
    text = teams.team_calendar(ATX_BOYS, now=NOW, checker=make_checker())
    events = parse_ics(text)
    assert "X-WR-CALNAME:ATX Boys" in text
    assert len(events) == 7  # Oct 9 (7-day lookback) through Nov 20
    assert all(e["id"].endswith("@rainout-agent-source.vercel.app") for e in events)


# --------------------------------------------------------------- generic URL
ICS_FEED = """BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:g1@league\r\nSUMMARY:ATX Boys vs Sluggers\r\nDTSTART:20261016T013000Z\r\nDTEND:20261016T023000Z\r\nLOCATION:Krieg Fields - Field 3\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:g2@league\r\nSUMMARY:Bombers at ATX Boys\r\nDTSTART:20261023T013000Z\r\nLOCATION:Havins\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""


def fake_fetcher(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in pages:
            raise AssertionError(url)
        body, ctype = pages[url]
        return FetchResult(url=url, status=200, content_type=ctype, body=body.encode())
    fetch.calls = calls
    return fetch


def test_schedule_url_ics_feed_end_to_end():
    url = "https://league.example.org/team.ics"
    fetch = fake_fetcher({url: (ICS_FEED, "text/calendar")})
    r = teams.search_teams("ATX Boys", schedule_url=url, fetcher=fetch)
    top = r["candidates"][0]
    assert top["name"] == "ATX Boys" and top["team_id"].startswith("url-") and top["platform"] == "generic"
    assert r["searched_sources"][0]["extraction"] == "ics"
    assert ts.decode_url_team(top["team_id"]) == (url, "ATX Boys")
    games = teams.team_games(top["team_id"], from_param="2026-10-15", to_param="2026-10-25", now=NOW, fetcher=fetch, checker=make_checker())
    assert [g["opponent"] for g in games["games"]] == ["Sluggers", "Bombers"]
    assert games["games"][0]["venue"]["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert games["games"][1]["home_away"] == "home" and games["games"][1]["end_estimated"] is True


def test_schedule_url_html_table_and_linked_ics_and_jsonld():
    table = """<table><tr><th>Date</th><th>Time</th><th>Home</th><th>Away</th><th>Field</th></tr>
      <tr><td>10/16/2026</td><td>8:20 PM</td><td>ATX Boys</td><td>The Vatos</td><td>Krieg 5</td></tr></table>"""
    fetch = fake_fetcher({"https://a.example.org/s": (table, "text/html")})
    r = teams.search_teams("ATX Boys", schedule_url="https://a.example.org/s", fetcher=fetch)
    assert r["candidates"][0]["name"] == "ATX Boys" and r["searched_sources"][0]["extraction"] == "html_table"

    page = '<a href="/cal/team.ics">Subscribe</a>'
    fetch = fake_fetcher({"https://b.example.org/s": (page, "text/html"), "https://b.example.org/cal/team.ics": (ICS_FEED, "text/calendar")})
    r = teams.search_teams("ATX Boys", schedule_url="https://b.example.org/s", fetcher=fetch)
    assert r["searched_sources"][0]["extraction"] == "linked_ics" and r["count"] >= 1

    ld = {"@context": "https://schema.org", "@type": "SportsEvent", "name": "ATX Boys vs Rizk Takers",
          "startDate": "2026-10-23T18:30:00-05:00", "location": {"@type": "Place", "name": "Krieg Fields"}}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    fetch = fake_fetcher({"https://c.example.org/s": (page, "text/html")})
    r = teams.search_teams("Rizk Takers", schedule_url="https://c.example.org/s", fetcher=fetch)
    assert r["candidates"][0]["name"] == "Rizk Takers" and r["searched_sources"][0]["extraction"] == "json_ld"


def test_schedule_url_with_nothing_parseable_falls_back_cleanly(monkeypatch):
    monkeypatch.delenv("RAINOUT_LLM_API_KEY", raising=False)
    fetch = fake_fetcher({"https://d.example.org/s": ("<p>Schedule coming soon</p>", "text/html")})
    r = teams.search_teams("ATX Boys", schedule_url="https://d.example.org/s", fetcher=fetch)
    assert r["count"] == 0 and r["next_step"] == "ask_user_for_schedule_url"


def test_optional_llm_extractor(monkeypatch):
    page = "<p>Fri Oct 16 8:20pm ATX Boys vs The Vatos, Krieg 5</p>"
    monkeypatch.setenv("RAINOUT_LLM_API_KEY", "test-key")
    sent = []

    def transport(req):
        sent.append(json.loads(req.data))
        assert req.headers["Authorization"] == "Bearer test-key"
        content = json.dumps({"games": [{"date": "2026-10-16", "time": "20:20", "home": "The Vatos", "away": "ATX Boys", "location": "Krieg 5"}]})
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    monkeypatch.setattr(ts, "_llm_transport", transport)
    fetch = fake_fetcher({"https://e.example.org/s": (page, "text/html")})
    r = teams.search_teams("ATX Boys", schedule_url="https://e.example.org/s", fetcher=fetch)
    assert r["candidates"][0]["name"] == "ATX Boys" and r["searched_sources"][0]["extraction"] == "llm"
    assert "ATX Boys vs The Vatos" in sent[0]["messages"][0]["content"]

    def broken(req):
        raise TimeoutError()

    monkeypatch.setattr(ts, "_llm_transport", broken)
    fetch = fake_fetcher({"https://f.example.org/s": (page, "text/html")})
    r = teams.search_teams("ATX Boys", schedule_url="https://f.example.org/s", fetcher=fetch)
    assert r["count"] == 0 and r["next_step"] == "ask_user_for_schedule_url"


def test_llm_not_called_without_key(monkeypatch):
    monkeypatch.delenv("RAINOUT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(ts, "_llm_transport", lambda req: (_ for _ in ()).throw(AssertionError("called")))
    assert ts._llm_extract("text", "https://x.example") == []


def test_schedule_url_on_known_platform_uses_adapter(league_http):
    fetch = fake_fetcher({})
    r = teams.search_teams("ATX Boys", schedule_url="https://www.capitalcityathletics.com/schedule?divisionid=744621", fetcher=fetch)
    assert r["candidates"][0]["team_id"] == ATX_BOYS and fetch.calls == []
    r = teams.search_teams("Generals", schedule_url="https://www.austinmetrobaseball.com/schedule.php?team=30", fetcher=fetch)
    assert r["candidates"][0]["team_id"] == GENERALS


def test_unsafe_schedule_url_is_rejected():
    status, _, body, cache = handle_teams_request("GET", "/v1/teams/search", "q=x&schedule_url=http://127.0.0.1/admin")
    assert status == 400 and json.loads(body)["error"] == "invalid_request" and cache == "no-store"


def test_url_team_id_rejects_private_targets():
    import base64
    raw = base64.urlsafe_b64encode(json.dumps({"u": "http://169.254.169.254/latest", "t": "x"}).encode()).decode().rstrip("=")
    with pytest.raises(ts.UnknownTeam):
        ts.decode_url_team("url-" + raw)


# -------------------------------------------------------------------- HTTP
def test_routes():
    assert route("/v1/teams/search") == ("search", None)
    assert route(f"/v1/teams/{ATX_BOYS}/games") == ("games", ATX_BOYS)
    assert route(f"/v1/teams/{ATX_BOYS}/calendar.ics") == ("calendar", ATX_BOYS)
    assert route("/v1/teams/x/other") is None


def test_http_search_games_and_calendar(league_http, monkeypatch):
    monkeypatch.setattr(teams, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: NOW)}))
    status, ctype, body, cache = handle_teams_request("GET", "/v1/teams/search", "q=ATX+Boys&venue=Craig+Fields&day=friday")
    assert status == 200 and cache.startswith("public") and json.loads(body)["candidates"][0]["team_id"] == ATX_BOYS
    status, ctype, body, cache = handle_teams_request("GET", f"/v1/teams/{ATX_BOYS}/games", "date=2026-10-16", checker=make_checker())
    data = json.loads(body)
    assert status == 200 and data["games"][0]["opponent"] == "The Vatos" and "max-age=120" in cache
    status, ctype, body, cache = handle_teams_request("GET", f"/v1/teams/{ATX_BOYS}/calendar.ics", "", checker=make_checker())
    assert status == 200 and ctype.startswith("text/calendar") and body.startswith(b"BEGIN:VCALENDAR")


def test_http_errors(monkeypatch):
    status, _, body, _ = handle_teams_request("GET", "/v1/teams/not-a-team/games", "")
    assert status == 404 and json.loads(body)["next_step"] == "search_teams"
    status, _, body, _ = handle_teams_request("GET", "/v1/teams/search", "")
    assert status == 400
    monkeypatch.setattr(ts, "_http_get", FakeLeagueHTTP(fail=True))
    status, _, body, _ = handle_teams_request("GET", f"/v1/teams/{ATX_BOYS}/games", "")
    data = json.loads(body)
    assert status == 502 and data["next_step"] == "ask_user_for_schedule_url" and "link" in data["spoken_answer"]
    status, _, _, _ = handle_teams_request("POST", "/v1/teams/search", "q=x")
    assert status == 405


def test_vercel_routes_and_handler(league_http):
    import io
    import api.teams as mod

    cfg = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
    srcs = {r["src"]: r["dest"] for r in cfg["routes"]}
    assert srcs["/v1/teams/search"] == "/api/teams.py" and srcs["/v1/teams/([^/]+)/games"] == "/api/teams.py"
    h = mod.handler.__new__(mod.handler)
    h.path, h.wfile, sent = "/v1/teams/search?q=Generals&sport=baseball", io.BytesIO(), {}
    h.send_response = lambda code, *a: sent.__setitem__("code", code)
    h.send_header = lambda k, v: sent.__setitem__(k, v)
    h.end_headers = lambda: None
    h.do_GET()
    assert sent["code"] == 200 and sent["Cache-Control"].startswith("public")
    assert json.loads(h.wfile.getvalue())["candidates"][0]["team_id"] == GENERALS


def test_craig_fields_is_the_krieg_venue():
    from rainout_agent.status_api import load_field, resolve_field_id
    from rainout_agent.venue_schema import validate_venue

    fid = resolve_field_id("Craig Fields")
    assert fid == "austin-tx-krieg-field-softball-complex"
    field = load_field(fid)
    assert validate_venue(field) == []
    assert field["coordinates_source"]["method"] == "official_venue_source"

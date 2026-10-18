import functools
import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from rainout_agent import schedule
from rainout_agent.http_app import application
from rainout_agent.safe_fetch import FetchError, FetchResult
from rainout_agent.schedule import (
    ScheduleInputError,
    build_risk_ics,
    check_schedule,
    match_venue,
    parse_csv_games,
    parse_ics,
    parse_json_games,
)
from rainout_agent.schedule_api import handle_schedule_request

NOW = datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc)  # 10:00 AM CDT
CDT = timezone(timedelta(hours=-5))


def make_periods(rain=10, forecast="Sunny", hours=156, start=NOW):
    base = start.replace(minute=0, second=0, microsecond=0)
    return [
        {
            "startTime": (base + timedelta(hours=h)).isoformat(),
            "endTime": (base + timedelta(hours=h + 1)).isoformat(),
            "probabilityOfPrecipitation": {"value": rain},
            "shortForecast": forecast,
        }
        for h in range(hours)
    ]


class Providers:
    def __init__(self, rain=10, forecast="Sunny", official=None, fail=False, sleep=0):
        self.rain, self.forecast, self.official, self.fail, self.sleep = rain, forecast, official, fail, sleep
        self.forecast_calls, self.official_calls = [], []

    def forecast_provider(self, field, timeout):
        self.forecast_calls.append(field["id"])
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail:
            raise RuntimeError("boom")
        rain = self.rain(field) if callable(self.rain) else self.rain
        return make_periods(rain, self.forecast)

    def official_provider(self, field, timeout):
        self.official_calls.append(field["id"])
        return {"official_status": self.official or "unknown", "checked": True}


def run(games, providers=None, **kwargs):
    providers = providers or Providers()
    return check_schedule(
        games,
        now=NOW,
        forecast_provider=providers.forecast_provider,
        official_provider=providers.official_provider,
        **kwargs,
    )


def game(venue, start, **extra):
    return {"venue_query": venue, "start": start, "title": extra.pop("title", f"Game at {venue}"), **extra}


# ------------------------------------------------------------------- ICS parse
ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:g1@league\r\nDTSTART;TZID=America/Chicago:20260509T190000\r\n"
    "DTEND;TZID=America/Chicago:20260509T203000\r\nSUMMARY:Hawks vs Bats\\, Div 2\r\n"
    "LOCATION:Krieg Field Softball Complex\\, 515 S Pleasant Valley Rd\r\n"
    "DESCRIPTION:Bring a long\r\n  folded description\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:alarm text\r\nEND:VALARM\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:g2\r\nDTSTART:20260510T140000Z\r\nSUMMARY:Practice @ Havins\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:g3\r\nDTSTART;VALUE=DATE:20260511\r\nSUMMARY:Tournament\r\nLOCATION:Onion Creek Soccer Complex\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:g4\r\nDTSTART;TZID=\"Central Standard Time\":20260509T180000\r\nSTATUS:CANCELLED\r\n"
    "SUMMARY:Rained out\r\nLOCATION:Downs Field\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_parse_ics_handles_tzid_utc_dates_folding_escapes_and_nested_components():
    events = parse_ics(ICS)
    assert [e["id"] for e in events] == ["g1@league", "g2", "g3", "g4"]
    g1, g2, g3, g4 = events
    assert g1["start"] == datetime(2026, 5, 9, 19, 0, tzinfo=g1["start"].tzinfo)
    assert g1["start"].utcoffset() == timedelta(hours=-5)
    assert g1["title"] == "Hawks vs Bats, Div 2"
    assert g1["location"].startswith("Krieg Field Softball Complex, 515")
    assert g1["description"] == "Bring a long folded description"
    assert g2["start"] == datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc) and g2["recurring"]
    assert g3["start"].isoformat() == "2026-05-11"
    assert g4["calendar_cancelled"] and g4["start"].utcoffset() == timedelta(hours=-5)


def test_parse_ics_rejects_non_calendars():
    with pytest.raises(ScheduleInputError):
        parse_ics("<html>nope</html>")


# --------------------------------------------------------------- JSON and CSV
def test_parse_json_games_reads_iso_date_time_and_teams():
    games = parse_json_games([
        {"venue": "Krieg", "start": "2026-05-09T19:00:00-05:00", "home": "A", "away": "B"},
        {"field_id": "Havins", "date": "2026-05-10", "time": "6:30 PM", "teams": "Hawks vs Bats", "id": 7},
        {"location": "Downs", "game_time": "2026-05-11 18:00"},
    ])
    assert games[0]["title"] == "A vs B" and games[0]["start"].utcoffset() == timedelta(hours=-5)
    assert games[1]["home"] == "Hawks" and games[1]["away"] == "Bats" and games[1]["id"] == "7"
    assert games[1]["start"] == datetime(2026, 5, 10, 18, 30) and games[1]["start_floating"]
    assert games[2]["venue_query"] == "Downs"
    with pytest.raises(ScheduleInputError):
        parse_json_games([{"venue": "Krieg", "start": "someday"}])
    with pytest.raises(ScheduleInputError):
        parse_json_games({"venue": "Krieg"})


def test_parse_csv_games():
    text = "\ufeffvenue,date,time,home,away\nKrieg,2026-05-09,19:00,Hawks,Bats\n,,,,\nHavins,05/10/2026,6:30 PM,Owls,Cats\n"
    games = parse_csv_games(text)
    assert len(games) == 2
    assert games[0]["venue_query"] == "Krieg" and games[0]["home"] == "Hawks"
    assert games[1]["start"] == datetime(2026, 5, 10, 18, 30)


# ------------------------------------------------------------- venue matching
def test_match_venue_exact_and_inside_free_text():
    assert match_venue("Krieg") == ("austin-tx-krieg-field-softball-complex", "exact")
    assert match_venue("Krieg Field Softball Complex, 515 S Pleasant Valley Rd, Austin")[0] == "austin-tx-krieg-field-softball-complex"
    assert match_venue(None, "U10 Practice @ Havins Softball Complex")[1] == "alias_in_text"
    assert match_venue("Some random park") == (None, None)
    assert match_venue("Westbrook") == (None, None)  # no partial-word matches


# ------------------------------------------------------------------ evaluation
def test_rainy_forecast_flags_game_at_risk_with_history_calibration():
    p = Providers(rain=80)
    report = run([game("Krieg", datetime(2026, 5, 9, 19, 0, tzinfo=CDT))], p)
    g = report["games"][0]
    assert g["basis"] == "nws_forecast+history"
    assert g["rain_chance_percent"] == 80
    assert g["at_risk"] and any("rain chance 80%" in r for r in g["risk_reasons"])
    assert g["assumes_game_scheduled"] is True
    assert g["reason"].startswith("Official status has not been posted. Rain chance is 80%")
    assert g["official_status"] == "unknown" and g["play_probability_percent"] <= 60
    assert g["historical_calibration"]["station_id"] == "USW00013904"
    assert report["summary"]["at_risk"] == 1 and report["summary"]["with_nws_forecast"] == 1


def test_thunderstorms_flag_risk_and_dry_forecast_does_not():
    storm = run([game("Krieg", NOW + timedelta(hours=5))], Providers(rain=30, forecast="Chance Showers And Thunderstorms"))
    assert storm["games"][0]["at_risk"] and "thunderstorms likely" in storm["games"][0]["risk_reasons"]
    dry = run([game("Krieg", NOW + timedelta(hours=5))], Providers(rain=5))
    assert not dry["games"][0]["at_risk"] and dry["games"][0]["play_probability_percent"] >= 90


def test_risk_threshold_is_configurable():
    g = [game("Krieg", NOW + timedelta(hours=30))]
    assert not run(g, Providers(rain=40), risk_threshold=60)["games"][0]["at_risk"]
    assert run(g, Providers(rain=40), risk_threshold=90)["games"][0]["at_risk"]


def test_games_beyond_forecast_get_history_outlook_and_window_skips():
    games = [
        game("Krieg", NOW + timedelta(days=10)),
        game("Krieg", NOW - timedelta(days=2)),
        game("Krieg", NOW + timedelta(days=40)),
    ]
    p = Providers()
    report = run(games, p)
    assert report["summary"]["skipped_past"] == 1 and report["summary"]["skipped_beyond_window"] == 1
    g = report["games"][0]
    assert g["basis"] == "history_only" and g["rain_chance_percent"] is None
    assert 50 < g["play_probability_percent"] < 100
    assert p.forecast_calls == []  # nothing within the NWS horizon


def test_forecast_is_fetched_once_per_venue_and_unmatched_games_reported():
    p = Providers()
    games = [game("Krieg", NOW + timedelta(hours=h)) for h in (5, 29, 53)] + [game("Nowhere Park", NOW + timedelta(hours=5))]
    report = run(games, p)
    assert p.forecast_calls == ["austin-tx-krieg-field-softball-complex"]
    assert report["summary"]["unmatched_venue"] == 1
    unmatched = [g for g in report["games"] if g["status"] == "unmatched_venue"][0]
    assert unmatched["venue"] is None and not unmatched["at_risk"]


def test_default_venue_and_floating_times_use_venue_timezone():
    report = run([{"title": "Practice", "start": datetime(2026, 5, 10, 9, 0), "start_floating": True}], default_venue="Kendall Soccer Park")
    g = report["games"][0]
    assert g["venue"]["field_id"] == "miami-fl-kendall-soccer-park" and g["venue"]["match_method"] == "default_venue"
    assert g["start"] == "2026-05-10T09:00:00-04:00"
    with pytest.raises(ScheduleInputError):
        run([], default_venue="Atlantis")


def test_official_status_is_polled_only_for_near_games_at_polled_venues():
    p = Providers(rain=5, official="cancelled")
    report = run([game("Havins", NOW + timedelta(hours=3)), game("Havins", NOW + timedelta(days=3)), game("Freestone", NOW + timedelta(hours=3))], p)
    assert p.official_calls == ["austin-tx-havins-softball-complex"]  # Freestone has poll_official_status false
    near = report["games"][0]
    assert near["official_status"] == "cancelled" and near["play_probability_percent"] == 0 and near["at_risk"]
    assert "official status is cancelled" in near["risk_reasons"]


def test_forecast_failure_and_timeouts_fall_back_to_history_with_warning():
    failed = run([game("Krieg", NOW + timedelta(hours=5))], Providers(fail=True))
    assert failed["games"][0]["basis"] == "history_only"
    assert any("Could not fetch the forecast" in w for w in failed["warnings"])
    slow = run([game("Krieg", NOW + timedelta(hours=5))], Providers(sleep=1.0), budget_s=0.6)
    assert slow["games"][0]["basis"] == "history_only"
    assert any("Timed out" in w for w in slow["warnings"])


def test_calendar_cancelled_events_and_all_day_events():
    events = parse_ics(ICS)
    report = run(events)
    by_uid = {g["uid"]: g for g in report["games"]}
    assert by_uid["g4"]["play_probability_percent"] == 0 and by_uid["g4"]["at_risk"]
    assert by_uid["g3"]["all_day"] and by_uid["g3"]["start"].startswith("2026-05-11T12:00:00-05:00")
    weekly = [g for g in report["games"] if g["uid"].startswith("g2#")]
    assert weekly and all(g["note"].startswith("Occurrence of a repeating") for g in weekly)
    assert weekly[0]["venue"]["field_id"] == "austin-tx-havins-softball-complex"
    assert weekly[0]["recurrence"]["rule"] == "FREQ=WEEKLY" and report["summary"]["repeating_occurrences"] == len(weekly)
    starts = [datetime.fromisoformat(g["start"]) for g in report["games"]]
    assert starts == sorted(starts)


def test_too_many_games_are_capped(monkeypatch):
    monkeypatch.setattr(schedule, "MAX_EVALUATED_GAMES", 3)
    report = run([game("Krieg", NOW + timedelta(hours=h)) for h in range(1, 8)])
    assert report["summary"]["games_in_window"] == 3 and report["warnings"]


# ------------------------------------------------------------------ ICS output
def test_risk_ics_is_folded_and_round_trips():
    report = run(parse_ics(ICS), Providers(rain=80))
    text = build_risk_ics(report)
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n"))
    assert "[RAIN RISK" in text and "CATEGORIES:RAIN RISK" in text
    again = parse_ics(text)
    assert {e["id"] for e in again} == {g["uid"] for g in report["games"]}
    assert any("Rainout line: 512-978-2680" in (e.get("description") or "") for e in again)


# --------------------------------------------------------------------- API glue
def _checker(providers=None):
    providers = providers or Providers(rain=80)
    return functools.partial(
        check_schedule, now=NOW, forecast_provider=providers.forecast_provider, official_provider=providers.official_provider
    )


def _call(method, query="", body=b"", ctype="", fetcher=None, providers=None):
    kwargs = {"checker": _checker(providers)}
    if fetcher:
        kwargs["fetcher"] = fetcher
    status, content_type, payload = handle_schedule_request(method, query, body, ctype, **kwargs)
    return status, content_type, payload


def test_get_with_ics_url_uses_fetcher_and_supports_ics_output():
    seen = []

    def fetcher(url):
        seen.append(url)
        return FetchResult(url, 200, "text/calendar", ICS.encode())

    status, ctype, body = _call("GET", "ics_url=https%3A%2F%2Fleague.example%2Fcal.ics&risk_threshold=70", fetcher=fetcher)
    assert status == 200 and ctype == "application/json"
    report = json.loads(body)
    assert seen == ["https://league.example/cal.ics"]
    assert report["risk_threshold_percent"] == 70 and report["summary"]["games_in_input"] == 4
    status, ctype, body = _call("GET", "ics_url=https://league.example/cal.ics&format=ics", fetcher=fetcher)
    assert status == 200 and ctype.startswith("text/calendar") and body.startswith(b"BEGIN:VCALENDAR")


def test_get_rejects_unsafe_urls_and_reports_fetch_failures():
    status, _, body = _call("GET", "ics_url=http://169.254.169.254/latest/meta-data")
    assert status == 400 and json.loads(body)["error"] == "unsafe_url"
    status, _, body = _call("GET", "ics_url=file:///etc/passwd")
    assert status == 400 and json.loads(body)["error"] == "unsafe_url"

    def failing(url):
        raise FetchError("timed out")

    status, _, body = _call("GET", "ics_url=https://league.example/cal.ics", fetcher=failing)
    assert status == 502 and json.loads(body)["error"] == "ics_fetch_failed"

    def html(url):
        return FetchResult(url, 200, "text/html", b"<html></html>")

    status, _, body = _call("GET", "ics_url=https://league.example/", fetcher=html)
    assert status == 400 and json.loads(body)["error"] == "invalid_schedule"


def test_post_accepts_json_list_ics_and_csv():
    games = [{"venue": "Krieg", "start": "2026-05-09T19:00:00-05:00", "home": "A", "away": "B"}]
    for body, ctype in (
        (json.dumps({"games": games}).encode(), "application/json"),
        (json.dumps(games).encode(), "application/json; charset=utf-8"),
        (b"venue,start,home,away\nKrieg,2026-05-09T19:00:00-05:00,A,B\n", "text/csv"),
    ):
        status, _, payload = _call("POST", "", body, ctype)
        report = json.loads(payload)
        assert status == 200 and report["summary"]["evaluated"] == 1 and report["games"][0]["at_risk"]
    status, _, payload = _call("POST", "", ICS.encode(), "text/calendar")
    assert status == 200 and json.loads(payload)["summary"]["games_in_input"] == 4
    status, _, payload = _call("POST", "", json.dumps({"ics": ICS, "format": "ics"}).encode(), "application/json")
    assert status == 200 and payload.startswith(b"BEGIN:VCALENDAR")


def test_bad_requests():
    assert _call("GET")[0] == 400
    assert json.loads(_call("POST", "", b"{not json", "application/json")[2])["error"] == "invalid_json"
    assert json.loads(_call("GET", "ics_url=https://x.example/a.ics&format=pdf")[2])["error"] == "invalid_format"
    assert json.loads(_call("POST", "risk_threshold=high", b'{"games": []}', "application/json")[2])["error"] == "invalid_schedule"
    assert _call("POST", "", b"x" * (schedule.MAX_INPUT_BYTES + 1), "text/csv")[0] == 413
    assert _call("DELETE")[0] == 405


def test_vercel_handler_post(monkeypatch):
    from api.schedule import handler

    monkeypatch.setattr("rainout_agent.schedule_api.schedule.check_schedule", _checker())
    body = json.dumps({"games": [{"venue": "Krieg", "start": "2026-05-09T19:00:00-05:00"}]}).encode()
    instance = handler.__new__(handler)
    instance.path = "/v1/schedule/check?risk_threshold=50"
    instance.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    instance.rfile = io.BytesIO(body)
    instance.wfile = io.BytesIO()
    sent = {}
    instance.send_response = lambda code, message=None: sent.setdefault("code", code)
    instance.send_header = lambda k, v: sent.setdefault(k, v)
    instance.end_headers = lambda: None
    instance.do_POST()
    assert sent["code"] in (200,)
    report = json.loads(instance.wfile.getvalue())
    assert report["risk_threshold_percent"] == 50 and report["summary"]["evaluated"] == 1
    assert report["games"][0]["rain_chance_percent"] == 80
    assert sent["Access-Control-Allow-Origin"] == "*"


def test_local_http_app_schedule_routes():
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b'{"games": []}'
    out = b"".join(application({
        "REQUEST_METHOD": "POST", "PATH_INFO": "/v1/schedule/check", "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(body),
    }, start_response))
    assert captured["status"] == "200 OK" and json.loads(out)["summary"]["games_in_input"] == 0
    out = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/schedule/check", "QUERY_STRING": "ics_url=http://127.0.0.1/x.ics"}, start_response))
    assert captured["status"] == "400 Bad Request" and json.loads(out)["error"] == "unsafe_url"


def test_unknown_status_scoring_matches_status_endpoint_model():
    from rainout_agent.play_probability import calculate_play_probability

    g = [game("Krieg", NOW + timedelta(hours=5))]
    for rain in (5, 30, 50, 70):
        out = run(g, Providers(rain=rain))["games"][0]
        expected = calculate_play_probability("unknown", rain, False, 5, historical_calibration=None)
        assert out["official_status"] == "unknown"
        assert out["play_probability_percent"] >= expected["play_probability_percent"]  # NOAA calibration only softens plain rain
    dry = run(g, Providers(rain=5))["games"][0]
    assert dry["play_probability_percent"] == 96 and not dry["at_risk"]
    wet = run(g, Providers(rain=100))["games"][0]
    assert wet["at_risk"]


def test_delayed_official_status_is_flagged_at_risk():
    out = run([game("Krieg", NOW + timedelta(hours=5))], Providers(rain=5, official="delayed"))["games"][0]
    assert out["play_probability_percent"] == 50 and out["at_risk"]


def test_history_outlook_uses_same_baseline():
    out = run([game("Krieg", NOW + timedelta(days=10))])["games"][0]
    assert out["basis"] == "history_only" and out["play_probability_percent"] <= 96

"""Official status page adapters and the fallback when a page can't be read."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from rainout_agent import official_sources as o
from rainout_agent.status_api import fetch_official_rainout_status, load_field

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)      # Sat 5 pm CDT
TONIGHT = datetime(2026, 10, 17, 19, 0, tzinfo=CT)


def field(**kw):
    base = {"id": "x", "field_name": "Brushy Creek Sports Park Softball Fields", "aliases": ["Brushy Creek"],
            "official_status_source_url": "https://city.example/status", "rainout_phone": "512-555-0100"}
    return {**base, **kw}


def run(html, f=None, game=TONIGHT, now=NOW, url=None):
    f = f or field()
    page = o.page_items(url or f["official_status_source_url"], html, CT, now)
    return o.decide(page, f, game, now)


SPORTSCONNECT = """<div class="skFieldStatus"><h2>Field Status</h2><ul class="skField">
<li class="skItem skFirst {org_cls}"><p class="skName">Balcones Youth Sports</p><span class="skDate">Updated: {date}</span>
<ul class="skSubField"><li class="skLocationItem skItem skFirst {a}"><span class="skCurrent">Senior Field</span></li>
<li class="skLocationItem skItem skAlt {b}"><span class="skCurrent">T-Ball Field</span></li></ul></li>
<li class="skItem skAlt skLast skOpen"><p class="skName">Minor B</p><span class="skDate">Updated: {date}</span>
<ul class="skSubField"><li class="skLocationItem skItem skOpen"><span class="skCurrent">Field 1</span></li></ul></li></ul></div>"""


def test_sportsconnect_widget_by_org_and_freshness():
    f = field(field_name="Balcones Youth Sports", aliases=["Balcones"])
    html = SPORTSCONNECT.format(org_cls="skClose", a="skClose", b="skClose", date="10/17/26 1:25PM")
    r = run(html, f)
    assert (r["official_status"], r["outcome"], r["parser"]) == ("field_closed", "matched", "sportsconnect")
    assert r["posted_at"].startswith("2026-10-17T13:25") and all("Minor B" not in m["name"] for m in r["matched"])
    mixed = run(SPORTSCONNECT.format(org_cls="skClose", a="skOpen", b="skClose", date="10/17/2026 01:25PM"), f)
    assert mixed["official_status"] == "mixed"
    # Widgets show the current state; the date is the last change, so a few days old is fine...
    older = run(SPORTSCONNECT.format(org_cls="skClose", a="skClose", b="skClose", date="10/14/26 1:25PM"), f)
    assert older["official_status"] == "field_closed" and "unchanged since Oct 14" in older["message"]
    # ...but more than two weeks without a change may be a forgotten widget.
    stale = run(SPORTSCONNECT.format(org_cls="skClose", a="skClose", b="skClose", date="10/01/26 1:25PM"), f)
    assert (stale["official_status"], stale["outcome"], stale["posted_status"]) == ("unknown", "stale", "field_closed")
    assert "Call the rainout line (512-555-0100)" in stale["message"]


def test_sportsconnect_single_org_page_needs_no_name_match():
    html = SPORTSCONNECT.split('<li class="skItem skAlt skLast skOpen">')[0].format(
        org_cls="skOpen", a="skOpen", b="skOpen", date="10/17/2026 08:11AM") + "</ul>"
    r = run(html, field(field_name="Northwest Little League", aliases=["NWLL"]))
    assert r["official_status"] == "on"


CITY = """<h1>Athletic Field Status - 10/17/26 as of 12:45 p.m.</h1>
<p>Brushy Creek Sports Park Multipurpose Fields: OPEN</p>
<p>Brushy Creek Sports Park Softball Fields: CLOSED</p>
<p>Elizabeth Milburn Park Multipurpose Fields: OPEN</p>
<h2>Athletic Field Rental Information</h2><p>Registration is open for spring leagues.</p>"""


def test_city_field_list_matches_only_this_venue():
    f = field(official_status_match=["Brushy Creek Sports Park Softball"])
    r = run(CITY, f)
    assert (r["official_status"], r["parser"], r["outcome"]) == ("field_closed", "field_list", "matched")
    assert [m["name"] for m in r["matched"]] == ["Brushy Creek Sports Park Softball Fields"]
    milburn = run(CITY, field(field_name="Elizabeth Milburn Park", aliases=[], official_status_match=["Elizabeth Milburn"]))
    assert milburn["official_status"] == "on"
    other = run(CITY, field(field_name="Lakeline Park", aliases=[], official_status_match=["Lakeline"]))
    assert (other["official_status"], other["outcome"]) == ("unknown", "no_match")
    far = run(CITY, f, game=TONIGHT + timedelta(days=3))
    assert (far["official_status"], far["outcome"]) == ("unknown", "not_posted_yet")
    undated = run(CITY.split("</h1>", 1)[1], f)
    assert (undated["official_status"], undated["outcome"]) == ("unknown", "undated")


LEAGUE = """<p>Athletic Field Status</p><p>Updated for: 10/17/2026</p>
<p>BICENTENNIAL PARK - BASEBALL</p><p>FIELD 1 - OPEN</p><p>FIELD 2 - CLOSED</p>
<p>NORTH PARK - SOCCER</p><p>FIELD 1A - CLOSED</p>"""


def test_league_page_with_park_headings():
    r = run(LEAGUE, field(field_name="North Park", aliases=[], official_status_match=["North Park"]))
    assert r["official_status"] == "field_closed" and r["matched"][0]["name"] == "NORTH PARK - SOCCER / FIELD 1A"
    assert run(LEAGUE, field(field_name="Bicentennial Park", aliases=[], official_status_match=["Bicentennial"]))["official_status"] == "mixed"


def test_day_by_day_statuses_pick_the_game_weekday():
    html = ("<p>As of Friday, Oct. 16 at 11:35 a.m., the field status for Oct. 16 - Oct. 18 is as follows:</p>"
            "<p>Bee Creek Park (Jane Pulley Softball)</p><p>Field 1: Fri/Sat: Open | Sun: Closed</p>")
    f = field(field_name="Bee Creek Park", aliases=[], official_status_match=["Bee Creek"])
    assert run(html, f)["official_status"] == "on"                                   # Saturday
    sunday = run(html, f, game=TONIGHT + timedelta(days=1), now=NOW + timedelta(hours=2))
    assert sunday["official_status"] == "field_closed" and sunday["matched"][0]["label"] == "Sunday: fields closed"


def test_single_venue_table_page_with_scope_all():
    html = ("<p>Last updated on: 10-17-2026 09:14 AM</p><table><tr><td>Baseball: 7, 12-15</td><td>Open</td>"
            "<td>Subject to closure at time of play.</td></tr><tr><td>Softball: 8, 9</td><td>Open</td><td></td></tr></table>")
    r = run(html, field(field_name="Town and Country", aliases=[], official_status_scope="all"))
    assert (r["official_status"], len(r["matched"])) == ("on", 2)


AMBL = """<nav><a>Rainout</a></nav><h3>Saturday, October 17</h3><p>No games scheduled</p>
<h3>Sunday, October 18</h3><p>Downs</p><p>Rams vs Owls 1:00 PM</p><p>999</p><p>Anderson</p><p>Bats vs Cats 4:00 PM</p><p>Weather Impact: 3%</p>
<h3>Monday, October 19</h3><p>Downs</p><p>Hawks vs Jays</p>
<h3>How the model works</h3><p>Official rainout wins: once a game is recorded as rainout code 999, its Weather Impact is 100%. Postponement code 777 ...</p>"""


def test_ambl_only_counts_rainout_codes_in_the_game_day_section():
    downs, anderson = field(field_name="Downs Field", official_status_match=["Downs"]), field(field_name="Anderson", official_status_match=["Anderson"])
    url = "https://www.austinmetrobaseball.com/rainout.php"
    sunday = datetime(2026, 10, 18, 13, 0, tzinfo=CT)
    assert run(AMBL, downs, game=sunday, url=url)["official_status"] == "cancelled"
    assert run(AMBL, anderson, game=sunday, url=url)["outcome"] == "no_status"
    assert run(AMBL, downs, url=url)["outcome"] == "no_game_listed"         # Saturday: no games
    monday = run(AMBL, downs, game=datetime(2026, 10, 19, 19, 0, tzinfo=CT), url=url)
    assert (monday["official_status"], monday["outcome"]) == ("unknown", "no_status")
    assert run(AMBL, downs, game=datetime(2026, 10, 25, 19, 0, tzinfo=CT), url=url)["outcome"] == "not_posted"


def test_real_ambl_page_shape_is_not_a_cancellation():
    # The old keyword parser saw "Rainout" in the menu and help text and always said cancelled.
    f = load_field("austin-tx-downs-field-ambl")
    html = "<a>Rainout</a><p>Official rainout wins: once a game is recorded as rainout code 999 ...</p>"
    r = run(html, f, url=f["official_status_source_url"])
    assert r["official_status"] == "unknown" and r["outcome"] == "unparseable"


def test_announcements_and_their_traps():
    f = field(official_status_scope="page")
    assert run("<p>All games are cancelled tonight due to wet fields.</p>", f)["official_status"] == "cancelled"
    assert run("<p>If games are cancelled tonight, make-up dates follow.</p>", f)["outcome"] == "unparseable"
    # Page-wide notices only count for venues that opted in (or name the venue).
    assert run("<p>All games are cancelled tonight.</p>", field())["outcome"] == "no_match"
    assert run("<p>All games at Brushy Creek are cancelled tonight.</p>", field())["official_status"] == "cancelled"


def test_cached_pages_are_not_mutated_by_day_logic():
    html = "<p>As of Oct. 17 at 9:00 a.m.</p><p>Bee Creek Park</p><p>Field 1: Sat: Open | Sun: Closed</p>"
    f = field(field_name="Bee Creek Park", aliases=[], official_status_match=["Bee Creek"])
    page = o.page_items(f["official_status_source_url"], html, CT, NOW)
    o.decide(page, f, TONIGHT + timedelta(days=1), NOW)
    assert page["items"][0]["status"] is None
    assert o.decide(page, f, TONIGHT, NOW)["official_status"] == "on"


# ------------------------------------------------------------------ fetch, cache and fallback
def test_blocked_unreachable_and_cached(monkeypatch):
    calls = []

    def blocked(url, timeout):
        calls.append(url)
        return 403, "<title>Attention Required! | Cloudflare</title> Sorry, you have been blocked"

    f = field(official_status_match=["Brushy Creek Sports Park Softball"])
    r = o.check_official_status(f, TONIGHT, now=NOW, fetch=blocked)
    assert (r["official_status"], r["outcome"], r["checked"]) == ("unknown", "blocked", False)
    assert "blocks automated checks" in r["message"] and "512-555-0100" in r["message"]
    assert o.check_official_status(f, TONIGHT, now=NOW, fetch=blocked)["cache"] == "hit" and len(calls) == 1

    def down(url, timeout):
        raise OSError("timeout")

    r = o.check_official_status(field(official_status_source_url="https://down.example/"), TONIGHT, now=NOW, fetch=down)
    assert r["outcome"] == "unavailable" and "couldn't be reached" in r["message"]

    pages = []

    def ok(url, timeout):
        pages.append(url)
        return 200, CITY

    g = field(official_status_source_url="https://ok.example/", official_status_match=["Brushy Creek Sports Park Softball"])
    assert o.check_official_status(g, TONIGHT, now=NOW, fetch=ok)["official_status"] == "field_closed"
    second = o.check_official_status(g, TONIGHT, now=NOW, fetch=ok)
    assert second["cache"] == "hit" and second["official_status"] == "field_closed" and len(pages) == 1


def test_status_api_reports_the_check(monkeypatch):
    f = {**load_field("cedar-park-tx-brushy-creek-sports-park-softball-fields"), "poll_official_status": True}
    monkeypatch.setattr(o, "fetch_page", lambda url, timeout: (403, "Attention Required! | Cloudflare"))
    r = fetch_official_rainout_status(f, game_time="2026-10-17T19:00:00-05:00")
    # a 403 is "official source unreadable" plus the link, not an error
    assert r["official_status"] == "unknown" and "error" not in r
    assert r["check"]["outcome"] == "blocked" and r["check"]["readable"] is False
    assert r["check"]["message"].startswith("Official source unreadable") and f["official_status_source_url"] in r["check"]["message"]
    info = fetch_official_rainout_status(load_field("austin-tx-western-hills-little-league"))
    assert info["check"]["outcome"] == "not_polled" and "isn't read automatically" in info["check"]["message"]


@pytest.mark.parametrize("text,expected", [
    ("10/16/26 12:10 pm", datetime(2026, 10, 16, 12, 10, tzinfo=CT)),
    ("Updated: 10/15/2026 08:11AM", datetime(2026, 10, 15, 8, 11, tzinfo=CT)),
    ("Last updated on: 10-16-2026 09:14 AM", datetime(2026, 10, 16, 9, 14, tzinfo=CT)),
    ("As of Friday, Oct. 16 at 11:35 a.m.", datetime(2026, 10, 16, 11, 35, tzinfo=CT)),
    ("Athletic Field Status - 10/7/26 as of 12:45 p.m.", datetime(2026, 10, 7, 12, 45, tzinfo=CT)),
    ("December 30", datetime(2025, 12, 30, 12, 0, tzinfo=CT)),
    ("no date here", None),
])
def test_posted_time_formats(text, expected):
    assert o.parse_posted_time(text, CT, NOW) == expected


def test_status_words():
    assert o.normalize_status("It's A Go!") == "on"
    assert o.normalize_status("Rained Out") == "cancelled"
    assert o.normalize_status("Closed - wet grounds") == "field_closed"
    assert o.normalize_status("Lightning delay") == "delayed"
    assert o.normalize_status("Not determined yet, check back at 3") is None


def test_spoken_answers_for_delayed_and_mixed():
    from rainout_agent.status_api import build_status_result

    weather = {"rain_chance_percent": 30, "thunderstorm_likely": False, "source": "test", "last_checked": "x"}
    mixed = build_status_result("Krieg", "2026-10-17T19:00:00-05:00", weather=dict(weather), official_status="mixed")
    assert "partly closed" in mixed["spoken_answer"] and mixed["game_time_play_probability_percent"] <= 50
    delayed = build_status_result("Krieg", "2026-10-17T19:00:00-05:00", weather=dict(weather), official_status="delayed")
    assert "is delayed" in delayed["spoken_answer"]


# ------------------------------------------------------------------ Rainout Line and RainedOut
def _rol(name, label, message, ts, cls=0):
    return (f'<div class="trigger" style="z-index:1000;" title="{label} - {message}&lt;div class=\'tipTs\'&gt;{ts}&lt;/div&gt;">'
            f'<div class="gridcell1 tborder"></div><div class="floatleft gridcell2"><h5><a href="https://rainoutline.com/search/extension/1/2">{name}</a></h5></div></div>'
            f'<div class="slidingDiv"><span class="status{cls}">{label}</span> {message} <span class="clue">{ts}</span></div>')


ROL = (_rol("Sunday Softball at Havins Fields", "It's A Go!", "Games are on as scheduled.", "10/17/26 12:10 pm")
       + _rol("Saturday Softball at Havins Fields", "Cancelled", "Fields are wet.", "10/17/26 3:05 pm", 2)
       + _rol("Saturday Kickball at Zilker", "It's A Go!", "On.", "10/17/26 3:05 pm")
       + _rol("Warren Sports Complex - Diamond Fields", "Info", "Closed for Seeding/Maintenance", "10/5/26 9:26 am", 3))


def test_rainoutline_extensions_by_venue_and_weekday():
    url = "https://rainoutline.com/search/dnis/5124000060/"
    assert o.data_url(url) == "https://rainoutline.com/search/dnis_refresh/5124000060/extension/1"
    havins = field(field_name="Havins Softball Complex", aliases=["Havins"], official_status_source_url=url)
    sat = run(ROL, havins)
    assert (sat["official_status"], sat["parser"]) == ("cancelled", "rainoutline")
    assert sat["matched"][0]["message"] == "Fields are wet." and sat["posted_at"].startswith("2026-10-17T15:05")
    sun = run(ROL, havins, game=TONIGHT + timedelta(days=1))
    assert sun["official_status"] == "on"
    mon = run(ROL, havins, game=TONIGHT + timedelta(days=2))
    assert mon["outcome"] in {"no_match", "not_posted_yet"}
    # Info/Notice entries are shown but are not a status.
    warren = run(ROL, field(field_name="Warren Sports Complex", aliases=[], official_status_source_url=url))
    assert warren["outcome"] == "no_status" and "Seeding" in warren["matched"][0]["message"]
    # Last week's posting doesn't carry over.
    old = run(ROL.replace("10/17/26", "10/10/26"), havins)
    assert old["outcome"] == "stale" and old["posted_status"] == "cancelled"


RAINEDOUT = """<h2 class="title">Springfield Parks Athletics</h2><div class="purple bold"><ul class="alerts">{items}</ul></div>"""


def test_rainedout_alert_messages():
    url = "https://www.rainedout.net/team_page.php?a=abc"
    f = field(field_name="Springfield Parks", aliases=[], official_status_source_url=url, official_status_scope="page")
    none = run(RAINEDOUT.format(items="<li>[No messages to show]</li>"), f, url=url)
    assert (none["official_status"], none["outcome"]) == ("unknown", "no_alerts")
    msgs = ("<li>10/17/2026 2:15 PM - All fields closed tonight due to rain.</li>"
            "<li>10/16/2026 3:00 PM - All fields open.</li>")
    r = run(RAINEDOUT.format(items=msgs), f, url=url)
    assert (r["official_status"], r["parser"]) == ("field_closed", "rainedout")
    reopened = ("<li>10/17/2026 4:30 PM - Fields reopened, games on as scheduled.</li>"
                "<li>10/17/2026 2:15 PM - All fields closed due to rain.</li>")
    assert run(RAINEDOUT.format(items=reopened), f, url=url)["official_status"] == "on"
    # Without opting in, an organization-wide message only counts when it names the venue.
    other = field(field_name="Lakeside Park", aliases=[], official_status_source_url=url)
    assert run(RAINEDOUT.format(items=msgs), other, url=url)["outcome"] == "no_match"


def test_link_only_venues_are_not_polled_and_say_why(monkeypatch):
    monkeypatch.setattr(o, "fetch_page", lambda url, timeout: (_ for _ in ()).throw(AssertionError("link-only venues must not be fetched")))
    krieg = fetch_official_rainout_status(load_field("austin-tx-krieg-field-softball-complex"))
    assert krieg["check"]["outcome"] == "not_polled" and krieg["check"]["link_only"] is True
    assert "512-978-2680" in krieg["check"]["message"] and "rainout line" in krieg["check"]["note"]
    for fid in ("cedar-park-tx-brushy-creek-sports-park-softball-fields", "cedar-park-tx-elizabeth-milburn-park-multipurpose-fields"):
        r = fetch_official_rainout_status(load_field(fid))
        assert r["check"]["outcome"] == "not_polled" and "403" in r["check"]["note"]
        assert load_field(fid)["official_status_source_url"] in r["check"]["message"]

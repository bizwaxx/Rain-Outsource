"""Regression tests for the motocross notice parser, built from real freestonemx.com rainout posts.

Each event below really did not run. The parser sees the post as production does (WordPress feed through
mx_sources.notices_from_wp) two hours before the start, and must not let a press dateline or a make-up date
hide the cancellation.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rainout_agent import motocross as m, mx_sources as mx

CT = ZoneInfo("America/Chicago")
POSTS = json.loads((Path(__file__).parent / "fixtures" / "mx_posts" / "freestonemx_rainouts.json").read_text())["posts"]
TRACKS = {t["short_id"]: t for t in m.tracks()}


def _event(name, start, end):
    return {"name": name, "sources": [{"name": "test", "url": "https://example.test/", "listed_as": name}],
            "start": start, "end": end, "listing_cancelled": False, "day_labels": {}}


def _replay(fid):
    fx = POSTS[fid]
    ws = datetime.fromisoformat(fx["start"]).replace(tzinfo=CT)
    we = datetime.fromisoformat(fx["end"]).replace(tzinfo=CT)
    notices = [{**n, "source_name": "Freestone Raceway news", "source_url": n.get("url")} for n in mx.notices_from_wp([fx["post"]])]
    now = max(ws - timedelta(hours=2), notices[0]["posted"])
    return m.official_status(TRACKS[fx["track"]], _event(fx["event_name"], ws, we), notices, now)


BEFORE_FIX = {  # before this fix (first-date-only parser)
    "freestone-2012-08-18": "cancelled",    # dateline + "Tonight's" + "rescheduled for Sunday, December 2nd"
    "freestone-2013-11-02": "unknown",      # dateline Nov 1 (day before)
    "freestone-2016-08-13": "unknown",      # "next weekend, August 20th"
    "freestone-2017-06-24": "uncertain",    # undated, posted race morning
    "freestone-2017-07-01": "uncertain",    # undated, "Rd 6"
    "freestone-2023-06-10": "unknown",      # make-up "next round at Freestone July 8th"
    "freestone-2024-05-18": "cancelled",    # "this weekend May 18th"
    "freestone-practice-2025-03-29": "unknown",  # practice event, "MARCH 29-30"
    "swan-2015-05-16": "unknown",           # dateline May 15, "Saturday, May 16th", "June 20th" next round
    "murphys-2024-04-27": "uncertain",      # undated, posted the evening before, "Rnd 2"
}


@pytest.mark.parametrize("fid", sorted(BEFORE_FIX))
def test_real_rainout_posts_cancel_their_event(fid):
    assert _replay(fid)["status"] == "cancelled"


def test_dateline_tonight_post_still_cancels_and_makeup_date_does_not():
    fx = POSTS["freestone-2012-08-18"]
    notices = [{**n, "source_name": "Freestone Raceway news"} for n in mx.notices_from_wp([fx["post"]])]
    track = TRACKS["freestone"]
    makeup = _event("AllStar Amateurs", datetime(2012, 12, 2, 8, tzinfo=CT), datetime(2012, 12, 2, 17, tzinfo=CT))
    # The make-up date (Dec 2) is not cancelled by the August rainout post (and the post is too old anyway).
    assert m.official_status(track, makeup, notices, datetime(2012, 12, 2, 6, tzinfo=CT))["status"] == "unknown"
    assert m.notice_dates(fx["post"]["title"]["rendered"], notices[0]["text"], notices[0]["posted"], makeup) == "other"


def _post(title, body, posted):
    return [{"title": title, "text": body, "posted": posted, "source_name": "Freestone Raceway news"}]


SAT = _event("FMF Texas Night Series Round 6", datetime(2026, 6, 20, 17, tzinfo=CT), datetime(2026, 6, 20, 23, tzinfo=CT))


def test_tonights_race_rained_out_without_any_date():
    st = m.official_status(TRACKS["freestone"], SAT, _post("Rained out", "Tonight's race rained out. Stay safe.",
                                                           datetime(2026, 6, 20, 14, tzinfo=CT)), datetime(2026, 6, 20, 15, tzinfo=CT))
    assert st["status"] == "cancelled"


def test_tonight_posted_the_day_before_is_about_another_day():
    st = m.official_status(TRACKS["freestone"], SAT, _post("Rained out", "Tonight's race rained out.",
                                                           datetime(2026, 6, 19, 14, tzinfo=CT)), datetime(2026, 6, 19, 15, tzinfo=CT))
    assert st["status"] == "unknown"


@pytest.mark.parametrize("title,body,expected", [
    ("Round 6 update", "FMF Texas Night Series Round 6 is rescheduled for July 11th.", "uncertain"),             # only a make-up date: hedge, never cancel
    ("Rnd 2 canceled", "Rnd 2 of the FMF Texas Night Series at Murphy's MX is canceled.", "unknown"),  # another track
    ("Rd 5 canceled", "Rd 5 of the FMF Texas Night Series is canceled due to weather.", "unknown"),    # another round
    ("Weather", "Round 6 of the FMF series may be canceled, watch for updates.", "uncertain"),         # hedged
    ("Round 6 canceled", "Round 6 of the FMF Texas Night Series is canceled June 27th.", "unknown"),   # other date
])
def test_conservative_cases(title, body, expected):
    now = datetime(2026, 6, 20, 9, tzinfo=CT)
    assert m.official_status(TRACKS["freestone"], SAT, _post(title, body, datetime(2026, 6, 20, 8, tzinfo=CT)), now)["status"] == expected

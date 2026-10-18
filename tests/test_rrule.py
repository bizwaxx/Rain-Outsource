"""RRULE expansion for the schedule checker."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from rainout_agent import rrule
from rainout_agent.schedule import check_schedule, parse_ics

CT = ZoneInfo("America/Chicago")
W0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
W1 = datetime(2027, 1, 1, tzinfo=timezone.utc)


def occ(start, rule, w0=W0, w1=W1, ex=None):
    return rrule.occurrences(start, rule, w0, w1, ex)


def test_weekly_byday_interval_count_and_dst_wall_clock():
    start = datetime(2026, 3, 3, 19, 0, tzinfo=CT)  # Tuesday, before DST starts Mar 8
    got = occ(start, "FREQ=WEEKLY;BYDAY=TU,TH;COUNT=4")
    assert [d.day for d in got] == [3, 5, 10, 12]
    assert all(d.hour == 19 for d in got)  # 7 pm before and after the DST change
    assert got[0].utcoffset() == timedelta(hours=-6) and got[2].utcoffset() == timedelta(hours=-5)
    every_other = occ(start, "FREQ=WEEKLY;INTERVAL=2;COUNT=3")
    assert [d.date() for d in every_other] == [date(2026, 3, 3), date(2026, 3, 17), date(2026, 3, 31)]


def test_until_exdate_and_window():
    start = datetime(2026, 4, 1, 18, 0, tzinfo=CT)
    got = occ(start, "FREQ=DAILY;UNTIL=20260405T235959Z", ex=[datetime(2026, 4, 3, 18, 0, tzinfo=CT)])
    assert [d.day for d in got] == [1, 2, 4, 5]
    windowed = occ(start, "FREQ=DAILY", w0=datetime(2026, 4, 10, tzinfo=timezone.utc), w1=datetime(2026, 4, 12, 12, tzinfo=timezone.utc))
    assert [d.day for d in windowed] == [10, 11]
    # COUNT counts from DTSTART even when the window starts later
    assert occ(start, "FREQ=DAILY;COUNT=5", w0=datetime(2026, 4, 4, tzinfo=timezone.utc)) == [
        datetime(2026, 4, 4, 18, 0, tzinfo=CT), datetime(2026, 4, 5, 18, 0, tzinfo=CT)]


def test_monthly_and_yearly_rules():
    start = datetime(2026, 1, 10, 9, 0, tzinfo=CT)  # 2nd Saturday of January
    got = occ(start, "FREQ=MONTHLY;BYDAY=2SA;COUNT=3")
    assert [d.date() for d in got] == [date(2026, 1, 10), date(2026, 2, 14), date(2026, 3, 14)]
    last_fri = occ(datetime(2026, 1, 30, 20, tzinfo=CT), "FREQ=MONTHLY;BYDAY=-1FR;COUNT=2")
    assert [d.date() for d in last_fri] == [date(2026, 1, 30), date(2026, 2, 27)]
    the31st = occ(datetime(2026, 1, 31, 8, tzinfo=CT), "FREQ=MONTHLY;COUNT=3")  # skips months without a 31st
    assert [d.date() for d in the31st] == [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)]
    md = occ(datetime(2026, 1, 1, 8, tzinfo=CT), "FREQ=MONTHLY;BYMONTHDAY=1,-1;COUNT=3")
    assert [d.date() for d in md] == [date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)]
    yearly = occ(datetime(2026, 5, 9, 8, tzinfo=CT), "FREQ=YEARLY;BYMONTH=5,6", w1=datetime(2028, 1, 1, tzinfo=timezone.utc))
    assert [d.date() for d in yearly] == [date(2026, 5, 9), date(2026, 6, 9), date(2027, 5, 9), date(2027, 6, 9)]
    assert occ(date(2026, 6, 6), "FREQ=WEEKLY;COUNT=2") == [date(2026, 6, 6), date(2026, 6, 13)]


@pytest.mark.parametrize("rule", ["FREQ=HOURLY", "FREQ=WEEKLY;BYSETPOS=1", "FREQ=WEEKLY;BYDAY=2TU", "INTERVAL=2"])
def test_unsupported_rules(rule):
    with pytest.raises(rrule.UnsupportedRule):
        rrule.parse_rule(rule)


ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:league-tue\r\nDTSTART;TZID=America/Chicago:20260505T190000\r\n"
    "DTEND;TZID=America/Chicago:20260505T200000\r\nRRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=6\r\n"
    "EXDATE;TZID=America/Chicago:20260512T190000\r\nSUMMARY:Bats vs Hawks\r\nLOCATION:Krieg Field Softball Complex\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:league-tue\r\nRECURRENCE-ID;TZID=America/Chicago:20260519T190000\r\n"
    "DTSTART;TZID=America/Chicago:20260520T190000\r\nDTEND;TZID=America/Chicago:20260520T200000\r\n"
    "SUMMARY:Bats vs Hawks (moved)\r\nLOCATION:Krieg Field Softball Complex\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:odd\r\nDTSTART:20260506T000000Z\r\nRRULE:FREQ=WEEKLY;BYSETPOS=1;BYDAY=MO\r\n"
    "SUMMARY:Odd rule\r\nLOCATION:Downs Field\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


class _NoFetch:
    def forecast(self, field, timeout):
        raise RuntimeError("offline")

    def official(self, field, timeout):
        return {"official_status": "unknown"}


def test_schedule_expands_repeating_ics_events():
    events = parse_ics(ICS)
    assert events[0]["rrule"] and events[0]["exdates"] and events[1]["recurrence_id"]
    p = _NoFetch()
    report = check_schedule(events, now=datetime(2026, 5, 1, 12, tzinfo=timezone.utc), window_days=31,
                            forecast_provider=p.forecast, official_provider=p.official)
    starts = {g["uid"]: g["start"][:10] for g in report["games"]}
    weekly = sorted(v for k, v in starts.items() if k.startswith("league-tue#"))
    # May 5, (May 12 excluded), (May 19 moved to May 20), May 26; June 2 is past the 31-day window
    assert weekly == ["2026-05-05", "2026-05-26"]
    assert starts["league-tue"] == "2026-05-20"  # the override stands alone
    one = next(g for g in report["games"] if g["uid"] == "league-tue#2026-05-26T19:00:00-05:00")
    assert one["end"].startswith("2026-05-26T20:00:00") and one["recurrence"]["series_id"] == "league-tue"
    odd = next(g for g in report["games"] if g["uid"] == "odd")
    assert "can't expand" in odd["note"] and any("BYSETPOS" in w for w in report["warnings"])
    assert report["summary"]["games_in_input"] == 3 and report["summary"]["repeating_occurrences"] == 2

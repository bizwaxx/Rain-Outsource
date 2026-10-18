"""Expand repeating iCalendar events (RFC 5545 RRULE) inside a time window.

Supported: FREQ=DAILY|WEEKLY|MONTHLY|YEARLY with INTERVAL, COUNT, UNTIL, BYDAY
(incl. ordinals like 2SA or -1FR for monthly/yearly), BYMONTHDAY, BYMONTH and
WKST (ignored; weeks start Monday), plus EXDATE and RECURRENCE-ID overrides.
Anything else (BYSETPOS, BYHOUR, BYWEEKNO, BYYEARDAY, sub-daily FREQ) is reported
as unsupported and the caller keeps only the first occurrence.

Occurrences keep DTSTART's wall-clock time in its own time zone, so a 7 pm game
stays at 7 pm across daylight-saving changes.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SUPPORTED = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
MAX_PERIODS = 5000          # safety cap on loop iterations per rule
MAX_OCCURRENCES = 400       # cap on occurrences returned per event


class UnsupportedRule(ValueError):
    pass


def parse_rule(text: str) -> dict[str, Any]:
    parts = {}
    for chunk in (text or "").strip().split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    if not parts.get("FREQ"):
        raise UnsupportedRule("RRULE has no FREQ")
    extra = set(parts) - SUPPORTED
    if extra:
        raise UnsupportedRule(f"unsupported RRULE parts: {', '.join(sorted(extra))}")
    if parts["FREQ"] not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise UnsupportedRule(f"unsupported FREQ={parts['FREQ']}")
    rule: dict[str, Any] = {"freq": parts["FREQ"]}
    try:
        rule["interval"] = max(1, int(parts.get("INTERVAL", "1")))
        rule["count"] = int(parts["COUNT"]) if "COUNT" in parts else None
        rule["bymonthday"] = [int(x) for x in parts["BYMONTHDAY"].split(",")] if "BYMONTHDAY" in parts else []
        rule["bymonth"] = [int(x) for x in parts["BYMONTH"].split(",")] if "BYMONTH" in parts else []
    except ValueError as exc:
        raise UnsupportedRule("malformed RRULE number") from exc
    byday = []
    for item in filter(None, parts.get("BYDAY", "").split(",")):
        m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item)
        if not m:
            raise UnsupportedRule(f"malformed BYDAY {item}")
        byday.append((int(m.group(1)) if m.group(1) else None, WEEKDAYS[m.group(2)]))
    if any(n is not None for n, _ in byday) and rule["freq"] in ("DAILY", "WEEKLY"):
        raise UnsupportedRule("ordinal BYDAY needs FREQ=MONTHLY or YEARLY")
    rule["byday"] = byday
    rule["until"] = _parse_until(parts["UNTIL"]) if "UNTIL" in parts else None
    return rule


def _parse_until(value: str) -> datetime | date:
    try:
        if len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()
        if value.endswith("Z"):
            return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise UnsupportedRule("malformed UNTIL") from exc


def _at(day: date, start: datetime | date) -> datetime | date:
    if isinstance(start, datetime):
        return datetime.combine(day, start.timetz()).replace(tzinfo=start.tzinfo)
    return day


def _month_days(year: int, month: int, rule: dict[str, Any], start: datetime | date) -> list[date]:
    last = calendar.monthrange(year, month)[1]
    days: set[int] = set()
    if rule["byday"]:
        for n, wd in rule["byday"]:
            matches = [d for d in range(1, last + 1) if date(year, month, d).weekday() == wd]
            if n is None:
                days.update(matches)
            elif 1 <= abs(n) <= len(matches):
                days.add(matches[n - 1] if n > 0 else matches[n])
        if rule["bymonthday"]:  # both given: intersection (RFC 5545)
            md = {d if d > 0 else last + 1 + d for d in rule["bymonthday"]}
            days &= md
    elif rule["bymonthday"]:
        days = {d if d > 0 else last + 1 + d for d in rule["bymonthday"] if 1 <= abs(d) <= last}
    else:
        if start.day <= last:
            days = {start.day}
    return [date(year, month, d) for d in sorted(days)]


def _candidates(rule: dict[str, Any], start: datetime | date):
    """Yield candidate days period by period (in order), starting at DTSTART's period."""
    sd = start.date() if isinstance(start, datetime) else start
    iv = rule["interval"]
    for k in range(MAX_PERIODS):
        if rule["freq"] == "DAILY":
            days = [sd + timedelta(days=k * iv)]
        elif rule["freq"] == "WEEKLY":
            monday = sd - timedelta(days=sd.weekday()) + timedelta(weeks=k * iv)
            wds = sorted({wd for _, wd in rule["byday"]}) or [sd.weekday()]
            days = [monday + timedelta(days=wd) for wd in wds]
        elif rule["freq"] == "MONTHLY":
            m0 = sd.year * 12 + sd.month - 1 + k * iv
            days = _month_days(m0 // 12, m0 % 12 + 1, rule, sd)
        else:  # YEARLY
            year = sd.year + k * iv
            months = rule["bymonth"] or [sd.month]
            days = []
            for mo in sorted(months):
                if rule["byday"] or rule["bymonthday"]:
                    days += _month_days(year, mo, rule, sd)
                elif sd.day <= calendar.monthrange(year, mo)[1]:
                    days.append(date(year, mo, sd.day))
        if rule["bymonth"] and rule["freq"] != "YEARLY":
            days = [d for d in days if d.month in rule["bymonth"]]
        yield days


def _key(value: datetime | date) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value
    return value


def occurrences(start: datetime | date, rule_text: str, window_start: datetime, window_end: datetime,
                exdates: list[datetime | date] | None = None) -> list[datetime | date]:
    """Occurrence starts of a recurring event that fall inside [window_start, window_end]."""
    rule = parse_rule(rule_text)
    until = rule["until"]
    ex = {_key(x) for x in (exdates or [])}

    def comparable(v):  # compare dates and naive datetimes against aware window bounds
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=window_start.tzinfo)
        return datetime.combine(v, datetime.min.time().replace(hour=12), tzinfo=window_start.tzinfo)

    out: list[datetime | date] = []
    produced = 0
    for days in _candidates(rule, start):
        for day in days:
            occ = _at(day, start)
            if comparable(occ) < comparable(start):
                continue
            if until is not None:
                u = until if not (isinstance(until, date) and not isinstance(until, datetime)) else until
                if isinstance(u, datetime) and isinstance(occ, datetime):
                    if comparable(occ) > (u if u.tzinfo else u.replace(tzinfo=occ.tzinfo)):
                        return out
                elif (occ.date() if isinstance(occ, datetime) else occ) > (u.date() if isinstance(u, datetime) else u):
                    return out
            produced += 1
            if rule["count"] is not None and produced > rule["count"]:
                return out
            c = comparable(occ)
            if c > window_end:
                return out
            if c >= window_start and _key(occ) not in ex:
                out.append(occ)
                if len(out) >= MAX_OCCURRENCES:
                    return out
    return out


def expand_events(events: list[dict[str, Any]], window_start: datetime, window_end: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace each recurring event with its occurrences in the window.

    Overrides (same UID with RECURRENCE-ID) replace the matching occurrence. Events with an
    unsupported rule stay as one event with ``recurrence_unsupported`` set.
    """
    overrides: dict[str, set] = {}
    for ev in events:
        if ev.get("recurrence_id") is not None and ev.get("id"):
            overrides.setdefault(ev["id"], set()).add(_key(ev["recurrence_id"]))
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ev in events:
        rule_text = ev.get("rrule")
        if not rule_text or ev.get("recurrence_id") is not None or ev.get("start") is None:
            out.append(ev)
            continue
        try:
            starts = occurrences(ev["start"], rule_text, window_start, window_end, ev.get("exdates"))
        except UnsupportedRule as exc:
            out.append({**ev, "recurrence_unsupported": str(exc)})
            warnings.append(f"{ev.get('title') or ev.get('id') or 'An event'}: {exc}; only the first occurrence was checked.")
            continue
        duration = (ev["end"] - ev["start"]) if ev.get("end") is not None and type(ev["end"]) is type(ev["start"]) else None
        skip = overrides.get(ev.get("id") or "", set())
        for occ in starts:
            if _key(occ) in skip:
                continue
            item = {**ev, "start": occ, "recurrence": {"rule": rule_text, "series_id": ev.get("id"), "first_start": _iso(ev["start"])}}
            item.pop("exdates", None)
            if duration is not None:
                item["end"] = occ + duration
            if ev.get("id"):
                item["id"] = f"{ev['id']}#{_iso(occ)}"
            out.append(item)
    return out, warnings


def _iso(v: datetime | date) -> str:
    return v.isoformat()

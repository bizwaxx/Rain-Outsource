"""Public event, status and series sources for motocross tracks.

Everything here reads public pages that load without a login, and only
returns what the page says: no event is ever made up. Sources are configured
per track in ``data/motocross/tracks.json``:

* ``ics``: an iCalendar feed (MX Parks publishes one per track).
* ``text_dates``: a section of a track web page that lists dates next to event
  names ("Dec 12-13 | Texas State Championship", "Round 19 - October 17").
* ``shopify_collection``: a Shopify store collection whose product titles carry
  event dates (Cycle Ranch sells race entries this way).
* series ``columns`` / ``series_lines``: series calendars that list every
  round with its track; only rounds at the requested track are kept.
* status ``ridemx`` / ``wp_posts`` / ``page_notices``: recent notices that may
  say an event is cancelled or postponed.

Configured hosts are fixed, so they are read with a plain GET (monkeypatched in
tests). A user-supplied ``events_url`` always goes through the SSRF-safe fetcher.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rainout_agent.cache import get_cache

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "motocross" / "tracks.json"
# Some track hosts (Swan MX) refuse user agents containing "compatible;", so this
# one names us plainly instead.
USER_AGENT = "Mozilla/5.0 RainoutSource/1.0 (+https://rainout-agent-source.vercel.app)"
FETCH_TIMEOUT_S = 6.0
EVENTS_TTL_S = 1800
STATUS_TTL_S = 600


class MXSourceError(RuntimeError):
    """A configured public source could not be read."""


# ------------------------------------------------------------------ fetching
def _http_get(url: str, timeout: float = FETCH_TIMEOUT_S, accept: str = "text/html,*/*") -> bytes:
    """GET from a fixed, configured public host (monkeypatched in tests)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(4_000_000)
    except Exception as exc:  # network, HTTP errors, timeouts
        raise MXSourceError(f"{urllib.parse.urlsplit(url).netloc}: {exc.__class__.__name__}") from exc


def _cached(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    cache = get_cache()
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = producer()
    cache.set(key, value, ttl)
    return value


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


# ------------------------------------------------------------- HTML -> text
class _TextSegments(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = re.sub(r"\s+", " ", data.replace("\u200b", "")).strip()
            if text:
                self.parts.append(text)


def html_segments(page: str) -> list[str]:
    parser = _TextSegments()
    try:
        parser.feed(page)
        parser.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    return parser.parts


def section(segments: list[str], start: str | None, end: str | None) -> list[str]:
    """Segments between the first match of ``start`` and the next match of ``end``."""
    if not start:
        return segments
    out: list[str] = []
    inside = False
    for seg in segments:
        if not inside:
            m = re.search(start, seg, re.I)
            if m:
                inside = True
                rest = seg[m.end():].strip(" |:-–")
                if rest:
                    out.append(rest)
            continue
        if end and re.search(end, seg, re.I):
            break
        out.append(seg)
    return out


# ------------------------------------------------------------- date phrases
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_SEP = r"\s*(?:-|–|—|&|,|and|to|thru|through)\s*"
DATE_RE = re.compile(
    rf"\b(?P<body>{_MON}\s*{_DAY}(?:{_SEP}(?:{_MON}\s*)?{_DAY}(?!\d))*)(?:,?\s*(?P<year>20\d\d))?\b", re.I)
NUMERIC_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:\s*-\s*(?P<d2>\d{1,2}))?(?:,?\s*(?P<year>20\d\d))?\b")
YEAR_RE = re.compile(r"\b(20\d\d)\b")


def _tokens(body: str) -> list[tuple[int | None, int]]:
    out: list[tuple[int | None, int]] = []
    for m in re.finditer(rf"(?P<mon>{_MON})?\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?", body, re.I):
        mon = m.group("mon")
        out.append((MONTHS[mon[:3].lower()] if mon else None, int(m.group("day"))))
    return out


def find_date_range(text: str) -> dict[str, Any] | None:
    """First date or date range in ``text``: {start:(m,d), end:(m,d), year, span:(i,j)}."""
    best = None
    m = DATE_RE.search(text)
    if m:
        toks = _tokens(m.group("body"))
        if toks and toks[0][0]:
            month = toks[0][0]
            seq = []
            for mon, day in toks:
                month = mon or month
                seq.append((month, day))
            best = {"start": seq[0], "end": seq[-1], "year": int(m.group("year")) if m.group("year") else None, "span": m.span()}
    n = NUMERIC_RE.search(text)
    if n and (best is None or n.start() < best["span"][0]):
        mo, d1 = int(n.group("m")), int(n.group("d"))
        d2 = int(n.group("d2")) if n.group("d2") else d1
        if 1 <= mo <= 12:
            best = {"start": (mo, d1), "end": (mo, d2), "year": int(n.group("year")) if n.group("year") else None, "span": n.span()}
    return best


def _make_dates(found: dict[str, Any], year: int) -> tuple[date, date] | None:
    try:
        start = date(year, *found["start"])
        end_year = year + 1 if found["end"][0] < found["start"][0] else year
        end = date(end_year, *found["end"])
    except ValueError:
        return None
    if end < start or (end - start).days > 10:
        return None
    return start, end


def dated_items(segments: list[str], *, name_side: str = "after", today: date,
                year_hint: int | None = None) -> list[dict[str, Any]]:
    """Pair each date phrase in ``segments`` with its event name.

    Years: an explicit year wins; otherwise the latest year mentioned in the
    section so far (a heading like "2027 Events"), rolling forward when the
    months go backwards in a chronological list; otherwise ``year_hint`` or
    today's year.
    """
    items: list[dict[str, Any]] = []
    year = year_hint
    last_month = None
    pending_name: str | None = None
    for i, seg in enumerate(segments):
        found = find_date_range(seg)
        if not found:
            years = YEAR_RE.findall(seg)
            if years and len(seg) <= 40 and not re.search(r"\$|copyright|©", seg, re.I):
                year = int(years[0])
                last_month = None
            if name_side == "after" and items and items[-1]["name"] is None and _looks_like_name(seg):
                items[-1]["name"] = seg
            elif name_side == "after" and items and seg.startswith("(") and items[-1]["name"]:
                items[-1]["name"] += " " + seg
            pending_name = seg if _looks_like_name(seg) else pending_name
            continue
        y = found["year"]
        if y is None:
            y = year or today.year
            if last_month and found["start"][0] < last_month and year is not None:
                y = year + 1
                year = y
        else:
            year = y
        last_month = found["start"][0]
        rng = _make_dates(found, y)
        if not rng:
            continue
        a, b = found["span"]
        same = (seg[:a] + " " + seg[b:]).strip(" -–|:,.")
        same = re.sub(r"\s{2,}", " ", same)
        if name_side == "same":
            name = same if _looks_like_name(same) else None
        elif name_side == "before":
            name = pending_name
        else:
            name = same if _looks_like_name(same) and len(same) > 12 else None
        items.append({"start": rng[0], "end": rng[1], "name": name, "text": seg})
        pending_name = None
    return [it for it in items if it["name"]]


def _looks_like_name(seg: str | None) -> bool:
    if not seg:
        return False
    s = seg.strip()
    if len(s) < 4 or len(re.findall(r"[A-Za-z]", s)) < 4:
        return False
    if re.fullmatch(r"[A-F]", s) or re.match(r"^(\$|tyler|share|register|click|view|buy|merch)", s, re.I):
        return False
    if re.fullmatch(r"[A-Za-z .']+,\s*[A-Z]{2}\b.*", s) and len(s) < 30:
        return False
    return True


# ----------------------------------------------------------------- adapters
def _local(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, dtime(h, m), tz)


def _default_window(name: str, src: dict[str, Any]) -> tuple[str, str, str]:
    if src.get("default_start"):
        return src["default_start"], src.get("default_end", "17:00"), src.get("time_note") or "Start time from the track's usual schedule."
    if re.search(r"night|lights|full moon", name, re.I):
        return "17:00", "23:00", "No start time published; assumed an evening program for a night race."
    return "08:00", "17:00", "No start time published; assumed a daytime program (gates and practice from about 8 AM)."


def _raw_event(track: dict[str, Any], src: dict[str, Any], name: str, start: datetime, end: datetime,
               time_estimated: bool, time_note: str | None, url: str | None, status_text: str | None = None) -> dict[str, Any]:
    return {
        "name": re.sub(r"\s+", " ", html_lib.unescape(name)).strip(" -|"),
        "start": start, "end": end, "time_estimated": time_estimated, "time_note": time_note,
        "source": {"id": src.get("id", src.get("type")), "name": src.get("name"), "url": src.get("page_url") or src.get("url"), "event_url": url},
        "status_text": status_text,
    }


def _ics_text(value: str) -> str:
    return re.sub(r"\\([,;\\nN])", r"\1", value).strip().lower()


def events_from_ics(text: str, track: dict[str, Any], src: dict[str, Any], tz: ZoneInfo) -> list[dict[str, Any]]:
    from rainout_agent.schedule import parse_ics

    out = []
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    # parse_ics drops STATUS, so read it per VEVENT and match on (summary, start date).
    statuses: dict[tuple[str, str], str] = {}
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.S):
        summ = re.search(r"^SUMMARY[^:\r\n]*:([^\r\n]+)", block, re.M)
        start_raw = re.search(r"^DTSTART[^:\r\n]*:(\d{8})", block, re.M)
        st = re.search(r"^STATUS[^:\r\n]*:([^\r\n]+)", block, re.M)
        if summ and start_raw and st:
            statuses[(_ics_text(summ.group(1)), start_raw.group(1))] = st.group(1).strip().lower()
    for ev in parse_ics(text):
        start = ev.get("start")
        if not isinstance(start, datetime):
            if isinstance(start, date):
                s_day = start
                end_day = ev.get("end") if isinstance(ev.get("end"), date) else s_day + timedelta(days=1)
                hs, he, note = _default_window(ev.get("title") or "", src)
                out.append(_raw_event(track, src, ev.get("title") or "Event", _local(s_day, hs, tz),
                                      _local(end_day - timedelta(days=1), he, tz), True, note, ev.get("url")))
            continue
        end = ev.get("end") if isinstance(ev.get("end"), datetime) else start + timedelta(hours=8)
        if start.tzinfo is None:
            start, end = start.replace(tzinfo=tz), end.replace(tzinfo=tz)
        status = statuses.get((_ics_text(ev.get("title") or ""), start.strftime("%Y%m%d")))
        if status is None and start.tzinfo is not None:
            status = statuses.get((_ics_text(ev.get("title") or ""), start.astimezone(timezone.utc).strftime("%Y%m%d")))
        ls, le = start.astimezone(tz), end.astimezone(tz)
        if ls.time() == dtime(0, 0) and (le.time() >= dtime(23, 59) or le.time() == dtime(0, 0)):
            # A whole-day listing encoded as midnight-to-midnight: no real start time.
            last = le.date() - timedelta(days=1) if le.time() == dtime(0, 0) else le.date()
            hs, he, note = _default_window(ev.get("title") or "", src)
            out.append(_raw_event(track, src, ev.get("title") or "Event", _local(ls.date(), hs, tz), _local(max(last, ls.date()), he, tz),
                                  True, note, ev.get("url"), status_text=("cancelled" if status == "cancelled" else None)))
            continue
        out.append(_raw_event(track, src, ev.get("title") or "Event", start.astimezone(tz), end.astimezone(tz), False, None,
                              ev.get("url"), status_text=("cancelled" if status == "cancelled" else None)))
    return out


def events_from_text(page: str, track: dict[str, Any], src: dict[str, Any], tz: ZoneInfo, today: date) -> list[dict[str, Any]]:
    segs = section(html_segments(page), src.get("section_start"), src.get("section_end"))
    out = []
    for it in dated_items(segs, name_side=src.get("name_side", "after"), today=today):
        name = it["name"]
        if src.get("name_prefix"):
            name = f"{src['name_prefix']} {name}"
        hs, he, note = _default_window(name, src)
        out.append(_raw_event(track, src, name, _local(it["start"], hs, tz), _local(it["end"], he, tz), True, note, src.get("url"),
                              status_text=it["text"]))
    return out


def events_from_shopify(payload: Any, track: dict[str, Any], src: dict[str, Any], tz: ZoneInfo, today: date) -> list[dict[str, Any]]:
    out = []
    tags = {t.lower() for t in src.get("tags", [])}
    for prod in (payload or {}).get("products", []):
        if tags and not tags & {t.lower() for t in prod.get("tags", [])}:
            continue
        title = re.sub(r"\(copy\)", "", prod.get("title") or "", flags=re.I)
        segs = [s.strip() for s in title.split("|") if s.strip()]
        found = None
        for seg in segs:
            found = find_date_range(seg)
            if found:
                break
        if not found:
            continue
        year = found["year"]
        if year is None:
            pub = (prod.get("published_at") or "")[:4]
            year = int(pub) if pub.isdigit() else today.year
            if date(year, *found["start"]) < date.fromisoformat((prod.get("published_at") or f"{year}-01-01")[:10]):
                year += 1
        rng = _make_dates(found, year)
        if not rng:
            continue
        name = re.sub(r"\b(ADMISSION|TICKETING|&)\b", " ", segs[0], flags=re.I) if segs else "Event"
        name = re.sub(r"\s{2,}", " ", name).strip(" &|")
        hs, he, note = _default_window(name, src)
        url = f"https://{urllib.parse.urlsplit(src['url']).netloc}/products/{prod.get('handle')}"
        out.append(_raw_event(track, src, name, _local(rng[0], hs, tz), _local(rng[1], he, tz), True, note, url, status_text=title))
    return out


def events_from_columns(page: str, track: dict[str, Any], series: dict[str, Any], tz: ZoneInfo, today: date) -> list[dict[str, Any]]:
    """Wix-style series pages: a run of date cells followed by a run of venue cells, in round order."""
    segs = html_segments(page)
    dates = [s for s in segs if re.match(series["date_regex"], s, re.I)]
    venues = [s for s in segs if re.search(series["venue_regex"], s)]
    years = [int(y) for s in segs for y in YEAR_RE.findall(s)]
    year = max(set(years), key=years.count) if years else today.year
    out = []
    aliases = [a.lower() for a in track.get("aliases", [])] + [track["name"].lower()]
    for idx, (dseg, vseg) in enumerate(zip(dates, venues), start=1):
        if "shootout" in vseg.lower():
            continue
        vnorm = vseg.lower().replace("’", "'")
        if not any(re.search(rf"\b{re.escape(a)}\b", vnorm) for a in aliases if len(a) > 3):
            continue
        found = find_date_range(dseg)
        rng = _make_dates(found, year) if found else None
        if not rng:
            continue
        name = f"{series['name']} {series.get('round_label', 'Round')} {idx}"
        hs, he, note = _default_window(name, {})
        src = {"id": "series", "name": series["name"], "url": series["url"]}
        out.append(_raw_event(track, src, name, _local(rng[0], hs, tz), _local(rng[1], he, tz), True, note, series["url"], status_text=vseg))
    return out


def events_from_series_lines(page: str, track: dict[str, Any], series: dict[str, Any], tz: ZoneInfo, today: date) -> list[dict[str, Any]]:
    segs = section(html_segments(page), series.get("section_start"), series.get("section_end"))
    head = " ".join(segs[:3])
    years = YEAR_RE.findall(" ".join(html_segments(page)[:400]))
    year = int(years[0]) if years else today.year
    aliases = [a.lower() for a in track.get("aliases", []) if len(a) > 3]
    out = []
    for seg in segs:
        m = re.match(series["line_regex"], seg, re.I)
        if not m:
            continue
        rest = m.group(2)
        if not any(a in rest.lower() for a in aliases):
            continue
        found = find_date_range(rest)
        rng = _make_dates(found, found["year"] or year) if found else None
        if not rng:
            continue
        name = f"{series['name']} Round {m.group(1)}"
        hs, he, note = _default_window(name, {})
        src = {"id": "series", "name": series["name"], "url": series["url"]}
        out.append(_raw_event(track, src, name, _local(rng[0], hs, tz), _local(rng[1], he, tz), True, note, series["url"], status_text=seg))
    del head
    return out


def read_source(track: dict[str, Any], src: dict[str, Any], tz: ZoneInfo, today: date) -> list[dict[str, Any]]:
    kind = src["type"]
    key = f"mx:src:{track['field_id']}:{src.get('id') or src['url']}:{today.isoformat()}"

    def produce():
        raw = _http_get(src["url"], accept="text/calendar,*/*" if kind == "ics" else "application/json,text/html,*/*")
        text = raw.decode("utf-8", "replace")
        if kind == "ics":
            evs = events_from_ics(text, track, src, tz)
        elif kind == "text_dates":
            evs = events_from_text(text, track, src, tz, today)
        elif kind == "shopify_collection":
            evs = events_from_shopify(json.loads(text), track, src, tz, today)
        elif kind == "columns":
            evs = events_from_columns(text, track, src, tz, today)
        elif kind == "series_lines":
            evs = events_from_series_lines(text, track, src, tz, today)
        else:
            evs = []
        return [_ser(e) for e in evs]

    return [_deser(e) for e in _cached(key, EVENTS_TTL_S, produce)]


def _ser(ev: dict[str, Any]) -> dict[str, Any]:
    return {**ev, "start": ev["start"].isoformat(), "end": ev["end"].isoformat()}


def _deser(ev: dict[str, Any]) -> dict[str, Any]:
    return {**ev, "start": datetime.fromisoformat(ev["start"]), "end": datetime.fromisoformat(ev["end"])}


def events_from_user_url(url: str, track: dict[str, Any], tz: ZoneInfo, today: date, fetcher) -> list[dict[str, Any]]:
    """User-supplied events page or calendar feed (SSRF-safe fetcher)."""
    from rainout_agent.team_sources import _events_from_jsonld

    res = fetcher(url)
    body = res.body if hasattr(res, "body") else res
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    src = {"id": "user_url", "type": "user_url", "name": "Schedule link you provided", "url": url}
    if "BEGIN:VCALENDAR" in text[:2000].upper():
        return events_from_ics(text, track, src, tz)
    evs = []
    for ev in _events_from_jsonld(text):
        start = ev["start"] if ev["start"].tzinfo else ev["start"].replace(tzinfo=tz)
        end = ev.get("end") or start + timedelta(hours=8)
        end = end if end.tzinfo else end.replace(tzinfo=tz)
        evs.append(_raw_event(track, src, ev.get("title") or "Event", start.astimezone(tz), end.astimezone(tz), False, None, url))
    if evs:
        return evs
    for name_side in ("same", "after"):
        found = events_from_text(text, track, {**src, "name_side": name_side}, tz, today)
        if found:
            return found
    return []


# ------------------------------------------------------------- status notices
NOTICE_RE = re.compile(r"\b(cancel\w*|postponed|rained out|rain ?out|rescheduled|no racing|track (?:is )?closed)\b", re.I)
# Static page text: only explicit cancellation words (opening-hours "Closed" lines and
# "rescheduled to this date" listings are not notices).
PAGE_NOTICE_RE = re.compile(r"\b(cancel+ed|canceled|postponed|rained out|no racing)\b", re.I)
GOOD_RE = re.compile(r"\b(racing is on|we(?:'| a)re racing|is a go|still on|rain or shine|track is open)\b", re.I)


def _age_to_datetime(text: str, now: datetime) -> datetime | None:
    m = re.search(r"(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?\s+ago", text, re.I)
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
    unit = m.group(2).lower()
    delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n),
             "week": timedelta(weeks=n), "month": timedelta(days=30 * n), "year": timedelta(days=365 * n)}[unit]
    return now - delta


def notices_from_ridemx(page: str, now: datetime) -> list[dict[str, Any]]:
    segs = html_segments(page)
    out = []
    for i, seg in enumerate(segs):
        if seg.lower() == "latest update" and i + 2 < len(segs):
            title, body = segs[i + 1], segs[i + 2]
            age = segs[i + 3] if i + 3 < len(segs) else ""
            out.append({"title": title, "text": body, "posted": (_age_to_datetime(age, now) or None)})
    return out


def notices_from_wp(payload: Any) -> list[dict[str, Any]]:
    out = []
    for p in payload if isinstance(payload, list) else []:
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", (p.get("content") or {}).get("rendered", "")))).strip()
        try:
            posted = datetime.fromisoformat(p["date"]).replace(tzinfo=timezone.utc) if p.get("date") else None
        except ValueError:
            posted = None
        out.append({"title": html_lib.unescape((p.get("title") or {}).get("rendered", "")), "text": text[:600],
                    "posted": posted, "url": p.get("link")})
    return out


def notices_from_page(page: str) -> list[dict[str, Any]]:
    out = []
    for seg in html_segments(page):
        if PAGE_NOTICE_RE.search(seg) and len(seg) < 400:
            out.append({"title": "", "text": seg, "posted": None})
    return out


def read_notices(track: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (notices, checked_sources). Unreadable sources are reported, not raised."""
    notices: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for src in track.get("status_sources", []):
        key = f"mx:status:{track['field_id']}:{src['url']}"
        try:
            def produce(src=src):
                raw = _http_get(src["url"], accept="application/json,text/html,*/*").decode("utf-8", "replace")
                if src["type"] == "ridemx":
                    found = notices_from_ridemx(raw, now)
                elif src["type"] == "wp_posts":
                    found = notices_from_wp(json.loads(raw))
                else:
                    found = notices_from_page(raw)
                return [{**n, "posted": n["posted"].isoformat() if n.get("posted") else None} for n in found]
            found = _cached(key, STATUS_TTL_S, produce)
            checked.append({"name": src["name"], "url": src.get("page_url") or src["url"], "ok": True})
            for n in found:
                notices.append({**n, "source_name": src["name"], "source_url": n.get("url") or src.get("page_url") or src["url"],
                                "posted": datetime.fromisoformat(n["posted"]) if n.get("posted") else None})
        except Exception as exc:  # unreadable source: say so, keep going
            checked.append({"name": src["name"], "url": src.get("page_url") or src["url"], "ok": False, "error": str(exc)[:120]})
    return notices, checked


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

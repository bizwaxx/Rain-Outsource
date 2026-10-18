"""Read official field status from common rainout and parks pages, without guessing.

Each page is handled by a small adapter that turns it into status items
(name, status, posted time, message). The items that name this venue decide the
official status. Anything the adapters can't read clearly falls back to
``unknown`` with an ``outcome`` and a plain-language ``message`` that says why
and where to check instead. Free: plain HTTP GET of public pages, no paid AI or API.

Adapters (see docs/official-status.md):

* ``sportsconnect`` - Blue Sombrero / SportsConnect "Field Status" widgets (skOpen/skClose)
* ``ambl`` - Austin Metro Baseball League rainout.php (weather outlook + rainout codes)
* ``field_list`` - city parks and league pages that list "Field name: OPEN/CLOSED"
* ``announcement`` - explicit page-wide notices such as "All games are cancelled tonight"
"""
from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

USER_AGENT = "Rainout Source by JEEZ Labs (public pilot)"
MAX_PAGE_BYTES = 2_000_000
FRESH_HOURS = 36          # a posted status older than this is "stale"
POSTED_AHEAD_HOURS = 36   # pages post today's status; games further out aren't decided yet
PAGE_CACHE_TTL_S = 300
FAILURE_CACHE_TTL_S = 120

STATUSES = ("on", "cancelled", "field_closed", "delayed")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
GENERIC_WORDS = {"field", "fields", "park", "complex", "sports", "sport", "softball", "baseball", "soccer", "the", "and",
                 "of", "at", "little", "league", "youth", "association", "club", "city", "tx", "austin", "facility",
                 "facilities", "athletic", "athletics", "metro", "metropolitan", "center", "stadium", "diamonds"}


class Unparseable(ValueError):
    """The page isn't in a format any adapter understands."""


# --------------------------------------------------------------------- text helpers
def html_to_lines(html: str) -> list[str]:
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<\s*(td|th)[^>]*>", " | ", text)
    text = re.sub(r"(?i)<\s*(br|/?p|/?div|/?li|/?tr|/?h[1-6]|/?ul|/?ol|/?table|/?section|/?article|/?header|/?footer)\b[^>]*>", "\n", text)
    text = unescape(re.sub(r"<[^>]+>", " ", text)).replace("\xa0", " ")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip(" |")
        line = re.sub(r"(\s*\|\s*)+", " | ", line)
        if line:
            lines.append(line)
    return lines


def normalize_status(label: str) -> str | None:
    """Map a posted label/message to on, cancelled, field_closed or delayed (None = not a status)."""
    t = (label or "").lower()
    if re.search(r"\b(not determined|undetermined|to be determined|tbd|check back|pending|undecided|no decision|info|notice)\b", t):
        return None
    if re.search(r"\b(cancel+ed|cancel+ations?|rained?[ -]?out|rainout|postponed|suspended|called off|no games?)\b", t):
        return "cancelled"
    if re.search(r"\b(delay(ed)?|on hold|late start|weather hold|lightning)\b", t):
        return "delayed"
    if re.search(r"\b(clos(ed|ure)|unplayable|not playable)\b", t):
        return "field_closed"
    if re.search(r"\b(open(ed)?|it'?s a go|games? (are |is )?on|on as scheduled|as scheduled|playable|go)\b", t):
        return "on"
    return None


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_TIME = r"(?:\s*(?:at|@|-)?\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?)?"


def parse_posted_time(text: str, tz: ZoneInfo, now: datetime) -> datetime | None:
    """First date (and optional time) in a status line: 10/16/26 12:10 pm, 10-16-2026 09:14 AM, Oct. 16 at 11:35 a.m."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b" + _TIME, text, re.I)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year += 2000 if year < 100 else 0
        hour, minute, ampm = m.group(4), m.group(5), m.group(6)
    else:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b" + _TIME, text, re.I)
        if not m:
            return None
        month, day = MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        local_now = now.astimezone(tz)
        year = int(m.group(3)) if m.group(3) else local_now.year
        hour, minute, ampm = m.group(4), m.group(5), m.group(6)
        if not m.group(3):  # no year: pick the year that puts the date closest to now
            try:
                if datetime(year, month, day, tzinfo=tz) - local_now > timedelta(days=180):
                    year -= 1
            except ValueError:
                return None
    h = 12
    mi = 0
    if hour:
        h = int(hour) % 12 + (12 if ampm.lower() == "p" else 0)
        mi = int(minute or 0)
    try:
        return datetime(year, month, day, h, mi, tzinfo=tz)
    except ValueError:
        return None


def match_terms(field: dict[str, Any]) -> list[str]:
    """Names that identify this venue on a status page (``official_status_match`` wins)."""
    explicit = field.get("official_status_match")
    if explicit:
        return [t.lower() for t in explicit if isinstance(t, str) and t.strip()]
    terms = []
    for name in [field.get("field_name") or ""] + list(field.get("aliases") or []):
        words = [w for w in re.findall(r"[a-z0-9&']+", name.lower()) if w not in GENERIC_WORDS and not w.isdigit()]
        if words and "-" not in name:
            terms.append(" ".join(words))
    return sorted(set(t for t in terms if len(t) >= 4), key=len, reverse=True)


def _mentions(text: str, terms: list[str]) -> bool:
    low = " ".join(re.findall(r"[a-z0-9&']+", (text or "").lower()))
    return any(re.search(r"\b" + re.escape(" ".join(re.findall(r"[a-z0-9&']+", t))) + r"\b", low) for t in terms)


def _item(name: str, status: str | None, label: str = "", message: str = "", posted: datetime | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": name.strip()[:160], "status": status, "label": (label or "").strip()[:80],
            "message": (message or "").strip()[:300] or None, "posted_at": posted.isoformat() if posted else None, **extra}


# --------------------------------------------------------------------- adapters
# Each adapter: (url, html, tz, now) -> {"items": [...], "page_posted_at": iso|None, "note": str|None}

def parse_sportsconnect(url: str, html: str, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    items = []
    for block in re.split(r'(?i)<li[^>]*class="(?![^"]*skLocationItem)[^"]*\bskItem\b', html)[1:]:
        head = re.match(r'[^"]*"', block)
        classes = (head.group(0) if head else "").lower()
        org = re.search(r'(?is)<p[^>]*class="skName"[^>]*>(.*?)</p>', block)
        if not org:
            continue
        org_name = unescape(re.sub(r"<[^>]+>", " ", org.group(1))).strip()
        date = re.search(r'(?is)<span[^>]*class="skDate"[^>]*>(.*?)</span>', block)
        posted = parse_posted_time(unescape(re.sub(r"<[^>]+>", " ", date.group(1))) if date else "", tz, now)
        status = "field_closed" if "skclose" in classes else "on" if "skopen" in classes else None
        subs = re.findall(r'(?is)<li[^>]*class="([^"]*skLocationItem[^"]*)"[^>]*>.*?<span[^>]*class="skCurrent"[^>]*>(.*?)</span>', block)
        items.append(_item(org_name, status, "Closed" if status == "field_closed" else "Open" if status else "", posted=posted, group=True))
        for cls, name in subs:
            c = cls.lower()
            st = "field_closed" if "skclose" in c else "on" if "skopen" in c else None
            items.append(_item(f"{org_name} - {unescape(re.sub(r'<[^>]+>', ' ', name)).strip()}", st,
                               "Closed" if st == "field_closed" else "Open" if st else "", posted=posted, parent=org_name))
    if not items:
        raise Unparseable("SportsConnect field-status widget has no fields")
    return {"items": items}


def parse_ambl(url: str, html: str, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    """AMBL rainout.php is mostly a weather outlook. Only rainout codes (999) or explicit
    rainout/postponed words inside a dated game-day section count; the explanation text
    and navigation ("Rainout" menu, "rainout code 999 ..." help text) are ignored."""
    lines = html_to_lines(html)
    day_re = re.compile(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s+([a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)$", re.I)
    items, current, section = [], None, []

    def flush():
        if current is None:
            return
        body = " ¦ ".join(section)
        day = parse_posted_time(current, tz, now)
        if re.search(r"\bno games? scheduled\b", body, re.I):
            items.append(_item(current, None, "No games scheduled", day_date=day.date().isoformat() if day else None, no_games=True))
            return
        for chunk in re.split(r"(?=\b(?:anderson|downs|san gabriel)\b)", body, flags=re.I):
            st = None
            if re.search(r"\b999\b|\brained?[ -]?out\b|\brainout\b(?!\s+code)", chunk, re.I):
                st = "cancelled"
            elif re.search(r"\b777\b|\bpostponed\b", chunk, re.I):
                st = "cancelled"
            if st:
                items.append(_item(f"{current} {chunk[:80]}", st, "Rainout" if st else "", message=chunk[:200],
                                   day_date=day.date().isoformat() if day else None))
            elif chunk.strip():
                items.append(_item(f"{current} {chunk[:80]}", None, "No rainout posted", message=chunk[:200],
                                   day_date=day.date().isoformat() if day else None))

    for line in lines:
        if day_re.match(line):
            flush()
            current, section = line, []
        elif current is not None:
            if re.match(r"^(how the model works|prediction tracking)$", line, re.I):
                flush()
                current = None
                continue
            section.append(line)
    flush()
    if not items:
        raise Unparseable("No game-day sections found on the AMBL rainout page")
    return {"items": items, "per_day": True,
            "note": "AMBL's page is a weather outlook; only posted rainout codes count as official."}


_FIELD_STATUS = re.compile(
    r"^(?P<name>[^|]{2,140}?)\s*(?:[:\-\u2013\u2014|]\s*)+(?P<status>open(?: reserved)?|closed|cancel+ed|delayed|postponed|rained out)\b\.?(?P<rest>.*)$",
    re.I)
_FIELD_STATUS_NOSEP = re.compile(
    r"^(?P<name>[^|]{0,100}?\b(?:fields?|diamonds?|pitch(?:es)?|courts?)\b[\w\s,&#/-]{0,24}?)\s+(?P<status>open|closed|cancel+ed|delayed)\b\.?(?P<rest>.*)$",
    re.I)
_DAY_STATUS = re.compile(r"\b((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:\s*/\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)*)\s*:\s*(open(?: reserved)?|closed|cancel+ed|delayed)", re.I)
_PAGE_DATE = re.compile(r"(?i)\b(as of|updated(?: for| on)?|last updated(?: on)?|status\s*[-\u2013:]|field status\s*[-\u2013:]|status for)\b[^|]{0,80}")
_HEADING_HINT = re.compile(r"(?i)\b(park|complex|fields?|center|stadium|diamonds?|campus|school|sports)\b")


def parse_field_list(url: str, html: str, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    """City parks / league pages: "Brushy Creek Softball Fields: OPEN", "FIELD 1 - CLOSED" under a park
    heading, "Baseball 10 | Open | comment", or "Field 1: Fri: Closed | Sat: Open"."""
    lines = html_to_lines(html)
    items, heading = [], ""
    page_posted = None
    for line in lines[:400]:
        if page_posted is None and _PAGE_DATE.search(line):
            page_posted = parse_posted_time(line, tz, now)
    for line in lines:
        if len(line) > 220:
            continue
        days = _DAY_STATUS.findall(line)
        if days and ":" in line:
            name = line.split(":", 1)[0]
            per_day = {}
            for spec, st in days:
                for d in re.split(r"\s*/\s*", spec):
                    per_day[d[:3].lower()] = normalize_status(st)
            items.append(_item(f"{heading} / {name}" if heading else name, None, "by day", message=line, by_day=per_day))
            continue
        m = _FIELD_STATUS.match(line) or _FIELD_STATUS_NOSEP.match(line)
        if m and not re.search(r"(?i)\b(registration|sign ?ups?|hours|office|pool|library|applications?|enrollment)\b", m.group("name")):
            name = m.group("name").strip(" -")
            st = normalize_status(m.group("status"))
            full = f"{heading} / {name}" if heading and not _mentions(name, [heading.lower()]) else name
            items.append(_item(full, st, m.group("status").title(), message=m.group("rest").strip(" |-")))
            continue
        if len(line) <= 80 and "|" not in line and _HEADING_HINT.search(line) and not normalize_status(line) and not line.endswith("."):
            heading = line
    if not items:
        raise Unparseable("No 'field: status' lines found")
    return {"items": items, "page_posted_at": page_posted.isoformat() if page_posted else None}


_ANNOUNCE = re.compile(
    r"(?i)(?:^|[.!]\s+)(?:(?:all|tonight'?s?|today'?s?|this evening'?s?)\b[^.!|]{0,60}?)?\b(games?|fields?|diamonds?|activities|leagues?|practices?)\b"
    r"[^.!|]{0,40}?\b(?:are|is|have been|has been|will be)?\s*(cancel+ed|closed|postponed|rained out|delayed)\b")


def parse_announcement(url: str, html: str, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    items = []
    for line in html_to_lines(html):
        if len(line) > 300 or re.search(r"(?i)\b(if|when|whether|in the event|policy|refund|make-?up)\b", line):
            continue
        m = _ANNOUNCE.search(line)
        if m:
            posted = parse_posted_time(line, tz, now)
            if not posted and re.search(r"(?i)\b(today|tonight)\b", line):
                posted = now.astimezone(tz)
            items.append(_item(line[:160], normalize_status(m.group(2)), m.group(2).title(), message=line, posted=posted, page_wide=True))
    if not items:
        raise Unparseable("No explicit cancellation notice")
    return {"items": items}


def detect_adapter(url: str, html: str) -> tuple[str, Callable[..., dict[str, Any]]]:
    host = (urlsplit(url).hostname or "").lower()
    low = (html or "").lower()
    if "austinmetrobaseball.com" in host:
        return "ambl", parse_ambl
    if 'class="skfield"' in low or re.search(r"\bsk(open|close)\b", low):
        return "sportsconnect", parse_sportsconnect
    return "field_list", parse_field_list


def page_items(url: str, html: str, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    """Run the matching adapter; field lists fall back to page-wide announcements."""
    name, adapter = detect_adapter(url, html)
    try:
        return {"parser": name, **adapter(url, html, tz, now)}
    except Unparseable as exc:
        if name != "field_list":
            return {"parser": name, "items": [], "unparseable": str(exc)}
        try:
            return {"parser": "announcement", **parse_announcement(url, html, tz, now)}
        except Unparseable:
            return {"parser": "none", "items": [], "unparseable": "No field status list, status widget or explicit notice found"}


# --------------------------------------------------------------------- decide for one venue
def field_zone(field: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(field.get("timezone") or "America/Chicago")
    except Exception:  # pragma: no cover
        return ZoneInfo("America/Chicago")


def _fallback_text(field: dict[str, Any]) -> str:
    phone = (field.get("rainout_phone") or "").strip()
    url = field.get("official_status_source_url")
    if phone and phone.lower() not in {"not_published", "unknown", "none", "n/a"}:
        return f"Call the rainout line ({phone}) or check {url}."
    return f"Check {url} directly." if url else "Check with the league or venue."


def decide(page: dict[str, Any], field: dict[str, Any], game_time: datetime | None, now: datetime) -> dict[str, Any]:
    """Turn adapter items into this venue's official status plus an explanation."""
    tz = field_zone(field)
    parser = page.get("parser")
    fallback = _fallback_text(field)
    out: dict[str, Any] = {"parser": parser, "fallback": fallback}

    def unknown(outcome: str, message: str, **extra: Any) -> dict[str, Any]:
        return {"official_status": "unknown", **out, "outcome": outcome, "message": f"{message} {fallback}", **extra}

    if not page.get("items"):
        return unknown("unparseable", f"The official page couldn't be read automatically ({page.get('unparseable') or 'format not recognized'}).")
    terms = match_terms(field)
    items = [dict(i) for i in page["items"]]  # page dicts may be shared through the memory cache
    game_local = game_time.astimezone(tz) if game_time else None
    if page.get("per_day"):
        if not game_local:
            return unknown("needs_game_time", "This page posts status per game day; pass a game time.")
        day = game_local.date().isoformat()
        day_items = [i for i in items if i.get("day_date") == day]
        if not day_items:
            return unknown("not_posted", f"The official page has nothing posted for {day} yet.")
        if any(i.get("no_games") for i in day_items):
            return unknown("no_game_listed", f"The official page lists no games on {day}.")
        items = day_items
    scope_all = field.get("official_status_scope") == "all"
    matched = [i for i in items if scope_all or i.get("page_wide") and _mentions(i["name"], terms) or _mentions(i["name"], terms)
               or (i.get("parent") and _mentions(i["parent"], terms))]
    page_wide = [i for i in items if i.get("page_wide")]
    groups = [i for i in items if i.get("group")]
    if not matched and parser == "sportsconnect" and len(groups) == 1:
        matched = [groups[0]] + [i for i in items if i.get("parent") == groups[0]["name"]]
    if not matched and page_wide and field.get("official_status_scope") in {"all", "page"}:
        matched = page_wide
    if not matched:
        return unknown("no_match", f"The official page lists {len(items)} status entries, but none names this venue.")


    weekday = WEEKDAYS[game_local.weekday()] if game_local else None
    if weekday:
        by_day_hits = [i for i in matched if (i.get("by_day") or {}).get(weekday[:3])]
        for i in by_day_hits:
            i["status"] = i["by_day"][weekday[:3]]
            i["label"] = f"{weekday.title()}: {WORDS.get(i['status'], i['status'])}"
        named = [i for i in matched if re.search(r"\b" + weekday + r"s?\b", (i["name"] or "").lower())]
        if named:
            matched = named
        elif any(re.search(r"\b(" + "|".join(WEEKDAYS) + r")s?\b", (i["name"] or "").lower()) for i in matched):
            return unknown("no_match", f"The official page lists this venue for other days, not {weekday.title()}.")
    statuses = [i for i in matched if i.get("status") in STATUSES]
    if not statuses:
        notes = [i for i in matched if i.get("message") or i.get("label")]
        return unknown("no_status", "The official page mentions this venue but posts no open/closed status for it.",
                       matched=_public(notes[:5]))

    posted = [datetime.fromisoformat(i["posted_at"]) for i in statuses if i.get("posted_at")]
    page_posted = datetime.fromisoformat(page["page_posted_at"]) if page.get("page_posted_at") else None
    latest = max(posted) if posted else page_posted
    if latest and posted:  # keep only the most recent posting when entries have their own times
        statuses = [i for i in statuses if not i.get("posted_at") or datetime.fromisoformat(i["posted_at"]) >= latest - timedelta(hours=1)]
    values = {i["status"] for i in statuses}
    if values == {"on"}:
        value = "on"
    elif len(values) == 1:
        value = values.pop()
    elif "on" in values:
        value = "mixed"
    else:
        value = "cancelled" if "cancelled" in values else "field_closed" if "field_closed" in values else "delayed"
    extra = {"posted_status": value, "posted_at": latest.isoformat() if latest else None, "matched": _public(statuses[:8])}
    if page.get("per_day"):
        return {"official_status": value, **out, "outcome": "matched", **extra,
                "message": f"Official page shows {WORDS[value]} for this venue on {game_local.date().isoformat()}."}
    if not latest:
        return unknown("undated", f"The official page shows {WORDS[value]} but no posting date, so it may be old.", **extra)
    age = now - latest
    if age > timedelta(hours=FRESH_HOURS):
        return unknown("stale", f"The official page last posted {WORDS[value]} on {latest.strftime('%b %d %I:%M %p')}, more than {FRESH_HOURS} hours ago.", **extra)
    if game_time and game_time - now > timedelta(hours=POSTED_AHEAD_HOURS):
        return unknown("not_posted_yet", f"The official page shows {WORDS[value]} now; the status for a game more than {POSTED_AHEAD_HOURS} hours away isn't posted yet.", **extra)
    if value == "mixed":
        msg = "The official page shows some of this venue's fields open and some closed."
    else:
        msg = f"Official page shows {WORDS[value]} (posted {latest.strftime('%b %d %I:%M %p')})."
    return {"official_status": value, **out, "outcome": "matched", **extra, "message": msg}


WORDS = {"on": "open / games on", "cancelled": "cancelled", "field_closed": "fields closed", "delayed": "delayed",
         "mixed": "some fields open, some closed"}


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("name", "status", "label", "message", "posted_at")
    return [{k: i.get(k) for k in keys if i.get(k) is not None} for i in items]


# --------------------------------------------------------------------- fetch with cache
def _blocked(status: int | None, body: str) -> bool:
    low = (body or "")[:5000].lower()
    return status in {401, 403, 429, 503} or "attention required! | cloudflare" in low or "just a moment..." in low \
        or "you have been blocked" in low


def fetch_page(url: str, timeout: float) -> tuple[int | None, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PAGE_BYTES)
            return getattr(response, "status", 200), raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(20000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, body


def data_url(url: str) -> str:
    """Some services load status from a second URL; adapters may rewrite (see later adapters)."""
    return url


def load_page(url: str, timeout: float, now: datetime, tz: ZoneInfo,
              fetch: Callable[[str, float], tuple[int | None, str]] | None = None) -> dict[str, Any]:
    from rainout_agent.cache import get_cache

    key = "official:v1:" + hashlib.sha256(url.encode()).hexdigest()[:24]
    cache = get_cache()
    try:
        cached = cache.get(key)
    except Exception:  # pragma: no cover
        cached = None
    if isinstance(cached, dict):
        return {**cached, "cache": "hit"}
    fetch = fetch or fetch_page
    try:
        status, body = fetch(data_url(url), timeout)
    except Exception as exc:
        page = {"fetch_error": "unavailable", "detail": exc.__class__.__name__, "items": []}
        _store(cache, key, page, FAILURE_CACHE_TTL_S)
        return {**page, "cache": "miss"}
    if _blocked(status, body):
        page = {"fetch_error": "blocked", "detail": f"HTTP {status}", "items": []}
    elif status and status >= 400:
        page = {"fetch_error": "unavailable", "detail": f"HTTP {status}", "items": []}
    else:
        page = page_items(url, body, tz, now)
    _store(cache, key, page, FAILURE_CACHE_TTL_S if page.get("fetch_error") else PAGE_CACHE_TTL_S)
    return {**page, "cache": "miss"}


def _store(cache: Any, key: str, value: dict[str, Any], ttl: float) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception:  # pragma: no cover
        pass


def check_official_status(field: dict[str, Any], game_time: datetime | None = None, timeout: float = 20,
                          now: datetime | None = None,
                          fetch: Callable[[str, float], tuple[int | None, str]] | None = None) -> dict[str, Any]:
    """Full check for one venue: fetch (cached), parse, decide. Never raises."""
    now = now or datetime.now(timezone.utc)
    url = field["official_status_source_url"]
    tz = field_zone(field)
    page = load_page(url, timeout, now, tz, fetch)
    if page.get("fetch_error"):
        what = "blocks automated checks" if page["fetch_error"] == "blocked" else "couldn't be reached"
        return {"official_status": "unknown", "parser": None, "outcome": page["fetch_error"],
                "message": f"The official page {what} ({page.get('detail')}). {_fallback_text(field)}",
                "fallback": _fallback_text(field), "checked": False, "cache": page.get("cache")}
    try:
        result = decide(page, field, game_time, now)
    except Exception as exc:  # pragma: no cover - parsing must never break a status answer
        result = {"official_status": "unknown", "parser": page.get("parser"), "outcome": "unparseable",
                  "message": f"The official page couldn't be read automatically ({exc.__class__.__name__}). {_fallback_text(field)}",
                  "fallback": _fallback_text(field)}
    if page.get("note"):
        result["note"] = page["note"]
    return {**result, "checked": True, "cache": page.get("cache")}

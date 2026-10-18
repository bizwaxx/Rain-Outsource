# Automatic official status checks

For venues with `poll_official_status` (not `false`) and an `official_status_source_url`,
`/v1/status` (and the MCP tools built on it) read the official page on each request
(cached for 5 minutes) with `rainout_agent/official_sources.py`. It's free: a plain HTTP GET
of a public page, with no paid AI or API.

The rule is **never guess**. A status is used only when the page clearly shows one for this
venue and it's current. Otherwise `official_status` stays `unknown` and
`official_status_check` says why and what to do instead.

## Supported page formats

| Parser | Pages | How it reads them |
|---|---|---|
| `rainoutline` | Rainout Line organization pages (`rainoutline.com/search/dnis/<number>`), e.g. Austin Sports & Social Club (Havins), Frisco, Carrollton | Reads the extension list the public page loads (`dnis_refresh`). Each extension's label ("It's A Go!", "Open", "Cancelled", "Closed", "Delayed") and time; "Info"/"Notice" entries are shown but aren't a status. Extensions named for a weekday ("Sunday Softball at Havins Fields") only count for games on that day. Postings older than 4 days are stale, so last week's "Cancelled" never carries over |
| `rainedout` | RainedOut public pages (`rainedout.net/team_page.php?a=…`) | The newest dated alert message that names the venue (or any, with `official_status_scope: "page"`). "[No messages to show]" gives the outcome `no_alerts`: usually nothing was cancelled, but it isn't a confirmation, so the status stays unknown |
| `sportsconnect` | Blue Sombrero / SportsConnect league sites with a "Field Status" widget | Each organization and field with its Open/Closed class and "Updated" time. The widget shows the current state, so its time is the last change |
| `field_list` | City parks and league pages listing fields, e.g. Cedar Park "Brushy Creek Sports Park Softball Fields: OPEN", TeamSideline "Rain-Out Info" pages ("FIELD 1 - CLOSED" under a park heading), College Station style "Field 1: Fri/Sat: Open \| Sun: Closed", T&C style tables | Lines of *name : status*; park headings are kept as context; day-by-day entries use the game's weekday; the page's "as of / updated" date is the posting time |
| `ambl` | Austin Metro Baseball League `rainout.php` | Only rainout codes (`999`, `777`) or explicit "rained out"/"postponed" inside the game-day section for the venue. The page is mostly a weather outlook, so its menu and help text are ignored |
| `announcement` | Any page with an explicit notice such as "All games are cancelled tonight" | Used only when the notice names the venue, or the venue sets `official_status_scope: "page"`/`"all"`. Conditional text ("if games are cancelled …") and policy text are ignored |

Status words map to `on`, `cancelled` (also rained out, postponed), `field_closed`,
`delayed`, or `mixed` when this venue's entries disagree (some fields open, some closed;
the odds treat it like a delay, capped at 50%). "TBD", "check back" and "info" aren't a status.

## Matching the venue

Entries count for a venue when they name it. Names come from `official_status_match` (a
list of names as written on the status page) or, by default, from the venue name and
aliases minus generic words such as "park" or "fields". `official_status_scope: "all"` means
every entry on the page is this venue (a single-venue page). A SportsConnect widget with a
single organization also counts as the whole venue.

## When it can't tell: fallback outcomes

`official_status_check.outcome` is one of:

| Outcome | Meaning |
|---|---|
| `matched` | Status found and current; `official_status` is set from it |
| `no_match` | The page lists statuses, but none for this venue (or only for other weekdays) |
| `no_status` | The venue is mentioned but with no open/closed status |
| `unparseable` | No supported format on the page |
| `no_alerts` | A RainedOut page with no current messages |
| `stale` | Last posting is too old: more than 36 hours for daily posted pages, 14 days for status widgets |
| `undated` | A status is shown but with no posting date |
| `not_posted_yet` | The game is more than 36 hours away; today's posting isn't about it |
| `no_game_listed`, `not_posted`, `needs_game_time` | Per-game-day pages (AMBL) with no game or nothing posted for that day, or no game time given |
| `blocked` | The site blocks automated requests (e.g. a Cloudflare 403). Not worked around |
| `unavailable` | Network error or HTTP error |
| `not_polled`, `no_source` | Information page that isn't a status feed, or no official source on file |

In all of these, `official_status` is `unknown`, `message` is a sentence an agent can read
aloud (for example "The official page blocks automated checks (HTTP 403). Call the rainout
line (512-978-2680) or check https://…"), `fallback` says where to check, and
`posted_status`/`posted_at` show what the page said when it was too old or undated.

## Adding a status page to a venue

1. Set `official_status_source_url` to the page and remove `"poll_official_status": false`.
2. If the page lists other venues, add `official_status_match` with the venue's name as the
   page writes it. For a page about only this venue, add `"official_status_scope": "all"`.
3. Add a test with a small HTML sample in `tests/test_official_sources.py`.

New formats go in `official_sources.py` as an adapter that returns status items; the venue
matching, freshness rules and fallback are shared.

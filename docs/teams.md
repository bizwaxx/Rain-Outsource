# Teams: "I play on ATX Boys, Friday nights at Craig Fields"

Users tell their AI agent which team they're on. The agent finds the team's league schedule,
gets the game, says how likely it is to happen and why, and offers to add it to the calendar.
**Rainout Source never writes to calendars.** It returns a ready-made Google Calendar event, and
the agent must ask the user before adding it.

## Endpoints

### `GET /v1/teams/search`

| Param | Meaning |
|---|---|
| `q` | Team name as the user said it (`ATX Boys`) |
| `venue` | Venue name or alias (`Craig Fields` resolves to Krieg Field Softball Complex) |
| `sport` | `softball`, `baseball`, … |
| `day` | Weekday they usually play (`friday`, `fri`) |
| `schedule_url` | League schedule link from the user: an ICS/webcal feed or a schedule web page |

The response holds `candidates[]` (`team_id`, `name`, `league`, `division`, `season`, `sport`,
`day`, `venue`, `schedule_source_url`, `platform`, `confidence` from 0 to 1), plus `next_step`,
`message` and `searched_sources`.

- `next_step: confirm_with_user`: read the best candidate back to the user ("ATX Boys in the City
  of Austin Friday softball league, division 503, at Krieg?") before using it.
- `next_step: ask_user_for_schedule_url`: no public match was found. Ask the user for their
  league's schedule link and search again with `schedule_url`.

Confidence is name similarity, adjusted for matching or conflicting sport, day and venue.
Look-alike names below 0.7 similarity are dropped, so the agent asks for a link instead of guessing.

### `GET /v1/teams/{team_id}/games`

Pass `date=today|tomorrow|YYYY-MM-DD`, or `from=&to=`. The default is the next 14 days, and a
range can be at most 62 days. Each game has:

| Field | Meaning |
|---|---|
| `start`, `end`, `timezone`, `end_estimated` | ISO-8601 with offset; IANA zone |
| `opponent`, `home_away`, `venue` (`field_id`, `name`, `field` such as "Krieg 05", `address`, `rainout_phone`) | |
| `league_status` | `scheduled`, `final`, `cancelled`, `postponed`, `forfeit` (from the league schedule) |
| `play_probability` | 0–100. Same model as `/v1/status` (`docs/play-probability.md`): NWS forecast within ~7 days, NOAA history to 31 days, `null` beyond that. 100 = already played, 0 = league cancelled, postponed or forfeit |
| `reason` | One sentence an agent can say aloud |
| `status_source` | League schedule (name, url, status), official rainout source, weather source |
| `calendar_event` | Google Calendar API event resource: `summary`, `location`, `start`/`end` `{dateTime, timeZone}`, `description` (odds and reason), stable `iCalUID` |
| `ics` | The same game as single-event iCalendar text |
| `as_of` | When the odds were computed |

When the range has no game (for example "do I have a game today?" on an off night), `next_game`
holds the next scheduled game and `message` says so: *"ATX Boys has no games today. Next game:
tomorrow at 8:20 PM vs The Vatos."*

`iCalUID` is derived from the league's own game id, so adding the same game twice (or later with
updated odds) updates the event instead of duplicating it. Use `events.import` with the iCalUID, or
`events.insert`.

### `GET /v1/teams/{team_id}/calendar.ics`

A subscribable feed covering the last 7 days through the next 120 days, with odds in each
description. Cancelled games carry `STATUS:CANCELLED`.

## Sources

| Platform | Leagues | How it's read |
|---|---|---|
| `teamsideline` | **Capital City Softball**: City of Austin PARD adult softball, Mon–Fri at Krieg ("South") and Havins ("North"). Also any TeamSideline league via `schedule_url` | The public JSON API the league's own schedule pages use (`cms-api.teamsideline.com/api/v1/Schedules`, `/Teams`, `/Schedules/Games`) |
| `ambl` | **Austin Metro Baseball League** (Downs, San Gabriel, Anderson) | `schedule.php?season_key=&month=&team=` HTML. Times have no AM/PM, so 8–11 is morning and anything else is PM. `rain`/`ppd` cells mean the game was rained out or postponed |
| `generic` | Any `schedule_url` | SSRF-safe fetch (public http(s) only, size and time caps, redirects re-checked). Parsed as an ICS feed, then a linked `.ics` on the page, then schema.org `SportsEvent` JSON-LD, then a schedule table (Date/Time/Home/Away/Field columns) |
| optional LLM | Only when `RAINOUT_LLM_API_KEY` is set and deterministic parsing found nothing | OpenAI-compatible `chat/completions` (`RAINOUT_LLM_BASE_URL`, `RAINOUT_LLM_MODEL`, default `gpt-4o-mini`). It sends up to 20 kB of page text. Any failure counts as "no games found" |

League configs live in `data/leagues/*.json`. Venue names in the league data (for example
"Krieg 05", "Downs") map to venue ids there.

**Team ids** describe themselves, so no server state is needed: `tsl-<org>-<division>-<team>`,
`ambl-<year><season>-<team>`, and `url-<base64url({"u": schedule_url, "t": team})>`. The URL inside
a `url-` id is re-validated against the SSRF rules every time it's used.

**Caching** uses the same cache layer as the weather (`docs/weather-cache.md`): in-memory by
default, plus Upstash/Vercel KV or Supabase when their env vars are set. Team lists are cached for
6 hours and schedules for 10 minutes. HTTP `Cache-Control`: search `max-age=300`, games
`max-age=120`, calendar feed `max-age=900`, errors `no-store`.

## Real test teams (checked Oct 15, 2026)

**ATX Boys (softball, Craig Fields).** "Craig Fields" is how people say **Krieg Fields**: the City of
Austin's Krieg Athletic Complex, 515 S. Pleasant Valley Rd. (11 lighted softball fields). It was
already in the data as `austin-tx-krieg-field-softball-complex`. This PR adds the "Craig Fields"
alias, the operator and an official coordinate source. City of Austin adult softball (Capital City
Softball) plays Monday–Friday there. **ATX Boys** is public in **Friday Softball, division 503 Men 0HR
South** (`team_id tsl-340-744621-4306365`). Their next game is Fri Oct 16, 8:20 PM vs The Vatos on
Krieg 05. Other games are on Oct 23, Oct 30, and Nov 6, 13 and 20.

**The Generals (AMBL baseball, Downs Field).** Public in the AMBL Fall 2026 American Division
(`team_id ambl-2026fall-30`). Their next game is Sun Oct 18, 7:00 PM at Royals (Downs). Their Oct 4
game shows as rained out.

Neither team needed a schedule link. A Five Tool youth team called "ATX boys" (Buda, 12U baseball)
also exists, but it's a different team and isn't searched.

## Fallback

If the team isn't found or a league site is down, search returns `next_step:
ask_user_for_schedule_url` and games return `502 schedule_source_unavailable` with a
`spoken_answer` asking for the link. A pasted ICS feed or schedule page then goes through the
generic extractor. See `docs/agent-conversation.md`.

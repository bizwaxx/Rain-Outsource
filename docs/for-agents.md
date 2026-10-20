# Rainout Source for agents

**What it does.** Rainout Source tells an agent whether a weather-dependent plan will
happen, and what to do about it: an outdoor game (softball, baseball, soccer), a motocross race,
or outdoor work (concrete pour, roofing, painting). Every answer comes with a probability, a
one-sentence reason you can read aloud, and its sources. The sources are official league or
venue status, the NWS forecast (Open-Meteo outside the US and as a fallback), and 10 years of
NOAA rain history.

- REST: `https://rainout-agent-source.vercel.app` (JSON, CORS open). The spec is at `/openapi.yaml`.
- MCP (Streamable HTTP, stateless): `https://rainout-agent-source.vercel.app/mcp`
- No key and no signup are needed. Every endpoint below works anonymously.

## When to call it

| The user says… | Call (MCP tool / REST) |
|---|---|
| "Is my game tonight still on?" | `find_team`, then `check_game` (`/v1/teams/search`, `/v1/teams/{id}/games`) |
| "Will it rain on the game at Krieg at 7?" | `get_venue_status` (`/v1/status?field_id=&game_time=`) |
| "Should **we** cancel Saturday's games?" (organizer, coach, race director, foreman) | **`should_we_cancel`** (`/v1/should-we-cancel`) |
| "Go or no-go for the roof job / pour / race tomorrow?" | `go_no_go` (`/v1/go-no-go`) |
| "Which games on our schedule are at risk?" | `check_schedule` (`/v1/schedule/check`) |
| "Will the race at Swan MX happen Saturday?" | `find_track`, then `check_race` (`/v1/tracks/{id}/check`) |
| "Will it rain in London this afternoon?" | `get_rain_chance` (`/v1/rain`) |
| "Is rain about to start here?" (next 2 hours) | `get_precipitation_nowcast` (`/v1/nowcast`) |
| "Our game got rained out" | Offer `report_cancellation` (`POST /v1/reports`) |

**Players vs organizers.** `check_game` and `go_no_go` answer the player's question, "is it
on?". `should_we_cancel` answers the organizer's question, "what should I do, and when?".
It uses exactly the same odds and sources as `go_no_go`, and adds the timing.

**Don't** use it for life-safety calls (lightning, flooding, severe weather). It is not an
official NWS product, and the construction answers are weather screening, not engineering advice.

## should_we_cancel

Input: one of the following, plus a time. Optionally pass `activity` (the default is the team's
sport, `motocross` for a track, or `softball`) and `duration_hours`.
- `team_id` (from `find_team`) with a date or start time; the game is looked up.
- `venue` (name or field_id).
- `track_id`.
- `location` or `lat`/`lon`.

Output:

| Field | Meaning |
|---|---|
| `recommendation` | `play`, `delay`, `cancel` or `wait_until` |
| `play_probability_percent` | Chance the game, race or work goes ahead (from `go_no_go`) |
| `reason` | Plain words, ready to read aloud or forward |
| `check_again_at` | When to ask again; `null` once the call is final |
| `delay_to` / `wait_until` | Set when the recommendation is `delay` / `wait_until` |
| `event` | The game or event it decided on (team, opponent, start, place) |
| `based_on` | The `go_no_go` result it builds on: `decision`, `weather_decision`, `event_confirmed`, `routed_to`, `official_status` |
| `sources` | Where the facts came from |

How it decides:

| Situation | Recommendation |
|---|---|
| Already cancelled or postponed on the official page or league schedule | `cancel` |
| No race listed at that time (`no_event_found`) | `wait_until` (never `play`) |
| `go_no_go` says go | `play` |
| Bad (no_go), 6 hours or less to start | `delay` if a start within 3 hours looks like a go, otherwise `cancel` |
| Bad, within 24 hours, under 25% | `cancel` early, so nobody travels |
| Bad, further out | `wait_until` the recheck time (forecasts change) |
| Iffy (wait), 2 hours or less to start | `delay` if a later slot is a go; else `play` at 55% or more, `cancel` below |
| Iffy, further out | `wait_until` a check time, 2 hours before the start at the latest (12 hours for races and pours) |

What to do with the answer:
- `play`: tell them it's on. If `check_again_at` is set, offer to check again then.
- `delay`: suggest the new start (`delay_to`) and offer to draft the message to the team.
- `cancel`: say so plainly, with the reason. Don't hedge.
- `wait_until`: give the check time and offer a reminder. Never call it on or off early.

```bash
# Organizer, by venue and start time
curl -G https://rainout-agent-source.vercel.app/v1/should-we-cancel \
  --data-urlencode "venue=Krieg" --data-urlencode "time=2026-10-24 18:30"

# By team (game picked by date)
curl -G https://rainout-agent-source.vercel.app/v1/should-we-cancel \
  -d team_id=tsl-340-744621-4306365 -d time=2026-10-23

# Motocross race day
curl -G https://rainout-agent-source.vercel.app/v1/should-we-cancel -d track_id=swan -d time=2026-10-24
```

Real response from the preview, Oct 20, 2026, for `track_id=swan&time=2026-10-24` (no race listed that Saturday):

```json
{"recommendation": "wait_until", "play_probability_percent": 96,
 "reason": "I can't find this race listed, so I won't say it's on. Confirm with the organizer or track first. No race is listed then; weather alone looks fine (96% weather-only odds). …",
 "check_again_at": "2026-10-23T00:00:00-05:00", "delay_to": null, "wait_until": "2026-10-23T00:00:00-05:00",
 "based_on": {"tool": "go_no_go", "decision": "no_event_found", "weather_decision": "go", "event_confirmed": false,
              "routed_to": "check_race", "official_status": "unknown"},
 "detail": "basic", "full_detail": "A paid API key adds hour-by-hour start options, …"}
```

For `venue=Krieg&time=2026-10-24 18:30`, the same day returned `"recommendation": "play"` with 96%
and `check_again_at` set 24 hours before the start.

MCP (`tools/call`):

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "should_we_cancel", "arguments": {"venue": "Krieg", "time": "2026-10-24 18:30"}}}
```

`detail=full` (REST only, with a paid API key) also returns:
- `start_options`: hour by hour, from 2 hours before to 4 hours after the start
- `best_start`
- `decision_deadline`
- `notify_message`: a ready-to-send text for the team or crew

Without a paid key you get the basic answer and a `full_detail` note. You never get an error for asking.

## go_no_go

The same answer shape works for every activity.

```bash
curl -G https://rainout-agent-source.vercel.app/v1/go-no-go -d activity=roofing -d lat=30.27 -d lon=-97.74 -d time=now
```

It returns:
- `decision`: `go`, `wait`, `no_go` or `no_event_found`
- `probability_percent`, `reason`, `recheck_at` and `sources`
- `routed_to`: which model answered
- `event_confirmed` and `weather_decision`

`no_event_found` means nothing is listed at that time. Don't tell the user the event is on.

## Games and venues

```bash
curl "https://rainout-agent-source.vercel.app/v1/teams/search?q=ATX%20Boys&venue=Craig%20Fields&day=friday"
curl "https://rainout-agent-source.vercel.app/v1/teams/tsl-340-744621-4306365/games?date=today"
curl "https://rainout-agent-source.vercel.app/v1/fields?q=Zilker&state=TX"
curl "https://rainout-agent-source.vercel.app/v1/status?field_id=Krieg&game_time=2026-10-24T18:30:00-05:00"
```

- **Confirm the team match** with the user before you answer for "their" game.
- **Read `reason` aloud** with the odds.
- **Official status beats odds.** When `official_status` is `unknown`, read
  `official_status_check.message`.
- If `next_step` is `ask_user_for_schedule_url`, ask for the league's schedule link and pass
  it as `schedule_url`.
- **Calendars:** Rainout Source never writes to calendars. It returns `calendar_event` and ICS
  text. Always ask the user before adding anything.

## Rain anywhere

```bash
curl "https://rainout-agent-source.vercel.app/v1/rain?q=London&hours=12"
curl "https://rainout-agent-source.vercel.app/v1/nowcast?lat=30.27&lon=-97.74"
```

If `ambiguous` is true, ask the user which of the `matches` they mean.

## Errors

Errors are JSON: `{"error": "...", "message": "...", "next_step"?: "..."}`.

| Status | Meaning | What to do |
|---|---|---|
| `400` | Bad input | Fix the argument named in `message` |
| `404 place_not_found` | Place not found | Ask for a state or country, or for lat/lon |
| `502` | Weather source unavailable | Retry in a minute; tell the user to check the official source |

## Optional API keys (rolling out)

Anonymous access stays exactly as it is. Keys are optional and only add capability:
bigger `/v1/batch` checks, a usage dashboard at `/v1/account`, and `should_we_cancel` with
`detail=full`. If `GET /v1/plans` answers on the deployment:
- create a free key with `POST /v1/keys` (no card; email optional);
- send it as `X-API-Key: rk_…`.

Never put a key in a URL, and never share it with the user's contacts.

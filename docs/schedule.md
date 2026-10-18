# Schedule check: flag rained-out games before you drive

`/v1/schedule/check` takes a whole schedule — an ICS calendar (URL or upload) or a
simple JSON/CSV league schedule — matches every game to a supported venue, estimates
the game-time play probability (NWS forecast + NOAA history) and flags at-risk games.
Forecasts go through the shared weather cache; if NWS is down a game may be scored
with Open-Meteo instead (`basis: open_meteo_forecast…`, `weather_source_id: open_meteo`,
counted in `summary.with_fallback_forecast`). See `docs/weather-cache.md`.
It is stateless: nothing is stored.

## Endpoints

### `GET /v1/schedule/check?ics_url=...`

```text
https://rainout-agent-source.vercel.app/v1/schedule/check?ics_url=https://example.org/team.ics
https://rainout-agent-source.vercel.app/v1/schedule/check?ics_url=webcal://example.org/team.ics&venue=Krieg&format=ics
```

### `POST /v1/schedule/check`

Body is one of:

| Content-Type | Body |
| --- | --- |
| `application/json` | `{"games": [...]}`, a bare list of games, `{"ics": "BEGIN:VCALENDAR..."}`, `{"csv": "..."}` or `{"ics_url": "https://..."}` |
| `text/calendar` | a raw `.ics` file |
| `text/csv` | a CSV with a header row |

```bash
curl -X POST https://rainout-agent-source.vercel.app/v1/schedule/check \
  -H 'Content-Type: application/json' \
  -d '{"games": [{"venue": "Krieg", "start": "2026-05-09T19:00:00-05:00", "home": "Hawks", "away": "Bats"}]}'

curl -X POST 'https://rainout-agent-source.vercel.app/v1/schedule/check?format=ics' \
  -H 'Content-Type: text/calendar' --data-binary @team.ics -o team-with-rain-risk.ics
```

### Options (query string, or keys in the JSON body)

| Option | Default | Meaning |
| --- | --- | --- |
| `format` | `json` | `json` report, or `ics` for an annotated calendar feed |
| `risk_threshold` | `60` | Games below this play probability (%) are flagged |
| `days` | `14` | Look-ahead window (1–31 days); earlier/later games are skipped |
| `venue` | – | Venue id/alias for games whose location can't be matched (e.g. a team calendar without locations) |
| `timezone` | venue's zone | IANA zone for times without an offset when no venue matches |

## Game fields (JSON / CSV)

| Field | Also accepted as | Notes |
| --- | --- | --- |
| `venue` | `venue_id`, `field_id`, `field`, `location` | Venue id, name, alias, or free text containing one |
| `start` | `game_time`, `datetime`, `start_time`, or `date` + `time` | ISO-8601 (`2026-05-09T19:00:00-05:00`), `2026-05-09 19:00`, `05/09/2026 7:00 PM`; times without an offset use the venue's local time |
| `home`, `away` | `home_team`, `away_team`, `teams` (`"Hawks vs Bats"`) | |
| `title`, `id`, `end`, `timezone` | | optional |

For ICS, `DTSTART` (UTC, `TZID`, floating or all-day), `DTEND`, `SUMMARY`, `LOCATION`,
`UID` and `STATUS:CANCELLED` are used. The venue is matched from `LOCATION`, then
`SUMMARY`, then the `venue` option.

### Repeating events (RRULE)

Repeating events are expanded into one game per occurrence inside the checked window
(from 3 hours ago to `window_days` ahead). Supported:

- `FREQ` = `DAILY`, `WEEKLY`, `MONTHLY` or `YEARLY`, with `INTERVAL`, `COUNT`, `UNTIL` and `WKST`
- `BYDAY` (e.g. `TU,TH`; with ordinals such as `2SA` or `-1FR` for monthly and yearly rules),
  `BYMONTHDAY` (negative values count from the end of the month) and `BYMONTH`
- `EXDATE` removes occurrences, and an event with the same `UID` and a `RECURRENCE-ID`
  replaces the occurrence it points at (a moved or cancelled single game)

Occurrences keep the first event's local wall-clock time across DST changes and its
duration. Each one gets the id `<UID>#<local start>` and a `recurrence` object
(`rule`, `series_id`, `first_start`); `summary.repeating_occurrences` counts them.
Rules using anything else (`BYSETPOS`, `HOURLY`, `RDATE` and so on) aren't expanded:
only the first occurrence is checked, and the game note and `warnings` say so.

## How each game is scored

1. **Venue match**: exact id/alias first, then the longest venue name or alias found as
   whole words inside the location/title text.
2. **Forecast** (games within ~7 days): one NWS hourly forecast per venue per request,
   fetched in parallel; the period nearest the start time gives rain chance and storms.
3. **Play probability**: the same model as `/v1/status`, calibrated with the venue's NOAA
   history for that month. Official pages only describe today, so most games have no
   posted status (`official_status: unknown`). Those start from a **96% baseline**
   (normal no-shows and venue issues), and rain and storms lower it (see
   [play probability](play-probability.md)). The official source (polled only for games in
   the next 12 hours at venues with a status feed) or the calendar can mark a game
   cancelled (0%), and delayed or conflicting signals are capped at 50%.
4. **Beyond the forecast range**: a history-only outlook: 96% × the share of days in that
   month that did *not* reach rainout-level rain (`basis: history_only`).
5. **At risk** when any of: official/calendar cancellation, thunderstorms likely, rain
   chance 60%+, or play probability below `risk_threshold` (default 60). With unknown
   status, that means roughly 55%+ rain (after NOAA calibration) or any likely thunderstorms.
   `risk_reasons` lists why.

## Response (JSON)

```json
{
  "summary": {"games_in_input": 12, "games_in_window": 5, "evaluated": 5, "at_risk": 2,
              "unmatched_venue": 0, "with_nws_forecast": 3, "with_fallback_forecast": 0, "history_only": 2,
              "skipped_past": 7, "skipped_beyond_window": 0},
  "games": [{
    "uid": "g1@league", "title": "Hawks vs Bats", "start": "2026-05-09T19:00:00-05:00",
    "venue": {"field_id": "austin-tx-krieg-field-softball-complex", "name": "Krieg Field Softball Complex", "match_method": "alias_in_text"},
    "basis": "nws_forecast+history", "official_status": "unknown", "assumes_game_scheduled": true,
    "rain_chance_percent": 80, "thunderstorm_likely": false, "play_probability_percent": 47,
    "risk_level": "high", "at_risk": true, "risk_reasons": ["rain chance 80%", "play probability 47% is below 60%"],
    "recommendation": "check again before leaving", "rainout_phone": "512-978-2680",
    "status_url": "https://rainout-agent-source.vercel.app/v1/status?field_id=..."
  }],
  "warnings": [], "assumptions": ["..."]
}
```

With `format=ics` the response is a `text/calendar` feed: each event's summary is
prefixed with `[RAIN RISK: 47% play]`, `[85% play]`, `[85% play] (outlook)` or
`[venue unknown]`, and the description carries the reason, rain chance, rainout line and
official source. At-risk events also get `CATEGORIES:RAIN RISK`.

## Limits (Vercel serverless)

| Limit | Value |
| --- | --- |
| Request body / fetched ICS | 1 MB |
| Games read from input | 2,000 |
| Games evaluated (inside the window) | 300 |
| Occurrences per repeating event | 400 (from up to 5,000 periods after `DTSTART`) |
| Venues with live forecasts per request | 20 (others get history-only) |
| Time budget for all forecast/official fetches | 8 s total (6 s per NWS call, 4 s per official page) |
| ICS URL fetch | 8 s total, 3 redirects |

When a fetch fails or the budget runs out, affected games fall back to history and the
response lists a warning, so the function stays inside the default Vercel timeout.

## ICS URL safety (SSRF protection)

`ics_url` is fetched by `rainout_agent/safe_fetch.py`:

- only `http` and `https` (`webcal://` is treated as `https://`); no credentials in the URL; ports 80/443 only;
- `localhost`, `*.localhost`, `*.local`, `*.internal` and metadata hostnames are refused;
- every DNS answer must be a public address — private, loopback, link-local (incl.
  `169.254.169.254`), CGNAT, multicast, reserved and IPv4-mapped IPv6 forms are refused;
- the connection is pinned to the vetted IP (Host header and TLS SNI keep the name), so DNS
  rebinding cannot swap the target after the check;
- redirects are followed manually (max 3) and every hop is re-validated;
- 1 MB size cap (declared and streamed) and an 8 s overall deadline, including slow drips.

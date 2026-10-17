# Rain chance anywhere (`/v1/rain`, `get_rain_chance`, box CLI)

One shared module, `rainout_agent/rain.py`, answers "will it rain?" for any place on Earth.
The HTTP endpoint, the MCP tool, the box CLI, the motocross checker and the venue/team checks
all read the same cached requests, so they agree.

## Sources (free, no key, no signup)

| Need | API | Notes |
| --- | --- | --- |
| Hourly chance, amount, weather code; daily max chance and totals; local timezone | [Open-Meteo forecast](https://open-meteo.com/en/docs) | Worldwide, 16 days. One request per point (3 past days + 16 days) is cached 10 minutes and reused by every feature. Free for non-commercial use under 10,000 calls a day; attribution "Weather data by Open-Meteo.com" (CC BY 4.0). |
| Place names | [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api) | Cached 7 days. |
| US hourly chance (primary) | [NWS api.weather.gov](https://www.weather.gov/documentation/services-web-api) | Through `weather.get_hourly_forecast`: points lookup cached 7 days, forecast 10 minutes, stale NWS up to 2 h, then Open-Meteo. Sends a User-Agent naming the app and a contact URL, as NWS asks. |

NWS has no hourly amounts, so amounts always come from Open-Meteo. Hours past the NWS range
(about 6.5 days) use Open-Meteo. Each row says where its chance came from (`probability_source`).
US means the geocoded country is US, or for raw coordinates a box covering CONUS, Alaska, Hawaii,
Puerto Rico and Guam.

## HTTP

`GET /v1/rain?q=<place or lat,lon>` or `?lat=&lon=`, plus optional:

* `hours` (1-384): hourly rows from now (default 24 when `days` and `at` are absent)
* `days` (1-16): daily rows starting today (today covers the rest of the day)
* `at`: local time at the place (`2026-10-18 19:00`) or ISO with offset
* `pick` (1-5): choose a match from an ambiguous name

Response fields: `place` (label, coordinates, timezone, `in_nws_coverage`), `timezone`, `units`
(`in` in the US, `mm` elsewhere, used in `summary`; rows carry both), `source`, `hourly`, `daily`,
`at`, and `summary` (one sentence).

Ambiguous names: when the best match has a near rival (a second place with at least 25% of
its population, more than about 50 km away), the API returns `ambiguous: true`, `matches` and
`next_step: ask_user_which_place` and no forecast. "Austin" and "London" resolve to the big city;
"Springfield" lists Missouri, Illinois, Massachusetts and the others. Qualifiers narrow the match:
`Austin, TX`, `Austin TX`, `London, Canada`, `London, UK`.

Errors: 400 `invalid_request`, 404 `place_not_found` (with `suggestions`), 502 `weather_unavailable`
or `upstream_unavailable` (with `spoken_answer`).

## MCP

`get_rain_chance` takes `query`, or `lat` and `lon`, plus `hours`, `days`, `at` and `pick`. It returns the same JSON.

## Box CLI

`/workspace/shared/weather/rain` (symlinked to `/usr/local/bin/rain`). See
`/workspace/shared/weather/README.md`. It imports this module from a git worktree pinned to
`main` (`/workspace/shared/weather/repo`, refreshed with `rain --update`), calls the APIs
directly (no Vercel), and keeps a shared file cache in `/workspace/shared/weather/.cache`
(the `file` cache backend).

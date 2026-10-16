# Motocross race checker

Answers "what are the chances the race at <track> on <date> happens?" for the 10 supported
motocross tracks, and shows each track's rain history and documented rainouts.

## Endpoints and MCP tools

| HTTP | MCP tool | What it returns |
| --- | --- | --- |
| `GET /v1/tracks?q=&state=` | `find_track` | Candidate tracks with `track_id`, `short_id`, sources and confidence. Confirm the track with the user. |
| `GET /v1/tracks/{id}/events?date=\|from=&to=&include_practice=&events_url=` | `get_track_events` | Races (and optionally practice days) with `play_probability`, `reason`, `official_status`, per-day breakdown, `sources`, `calendar_event` and `ics`. |
| `GET /v1/tracks/{id}/check?date=\|event_id=&events_url=` | `check_race` | `answer` (one sentence), the event, `next_step` (`offer_calendar_add`, `tell_user_cancelled` or `ask_user_for_events_url`). |
| `GET /v1/tracks/{id}/history` | `get_track_history` | NOAA monthly rain table with climatology race odds, wettest months, recent inch-plus days, documented cancelled or moved events with sources, and coverage notes. |
| `GET /v1/tracks/{id}/calendar.ics` | | Subscribable feed of races (past 7 days to 180 days out) with odds in each description. |

`{id}` is the full id (`tyler-tx-swan-mx`) or the short id (`swan`). Dates accept `today`,
`tomorrow`, `saturday`, `sunday`, `this weekend` or `YYYY-MM-DD`. Rainout Source never writes to
calendars: agents must ask the user before inserting `calendar_event`.

Errors: 404 `unknown_track` (`next_step: find_track`), 400 `invalid_request`, 502
`track_source_unavailable` with `next_step` and `spoken_answer`.

## Event sources (public only, never invented)

Configured per track in `data/motocross/tracks.json`:

* **Track sites**: dated sections of the Freestone, Swan MX, 3 Palms, Dade City, Orlando MX Park and OMC
  schedule pages; Cycle Ranch's Shopify event collection.
* **MX Parks** per-track ICS feeds (`mxparks.com/events/feed.ics?track=...`) for all 10 tracks. MX Parks
  is an aggregator; its "cancelled" flag is honored.
* **Series**: AMA Texas State Championship Series (South and Central) and the Florida Motocross Series;
  only rounds at the requested track are kept.
* **User link**: `events_url` (ICS, JSON-LD or a dated web page), fetched with the SSRF-safe fetcher.

Listings of the same event from several sources are merged (overlapping dates, same kind) and
each source is cited. When nothing is found the answer says so and asks for a link.
Not readable: Facebook and Instagram (login required), Murphy's MX and 3 Palms Wix calendar widgets,
Mesa's Facebook calendar. These are listed in `sources_checked` / `unreadable_sources`.

## Odds model

Per race day:

1. **Official status** from the event listings and recent notices (RideMX "Latest update" blocks,
   Freestone's news feed, cancel/postpone wording on track schedule pages). Conservative: a notice
   cancels only when it is recent, about racing (not practice), names this event's date and says
   cancelled/rained out. Vaguer notices make the status `uncertain` (odds capped at 50%).
2. **Forecast** (within 16 days): rain chance and thunder over the race window from NWS hourly,
   falling back to Open-Meteo. Model thunder codes count only within 7 days and when at least two
   race hours show thunder with a 40%+ rain chance. The base comes from the shared play-probability
   model with the motocross NOAA calibration (1.0 inch heavy-rain threshold).
3. **Soaked track**: Open-Meteo hourly rain for the 72 h before the race window, weighted
   1 / 0.6 / 0.35 by day and divided by a soil factor (sand 1.4, loam-sand 1.15, clay 0.8).
   0.5 in costs nothing; 1 in about 8 points; 2 in 25; 3 in 45; 5 in 65.
4. **Race-day rain** amount during the window costs up to 45 points.
5. **Beyond 16 days**: climatology, `96 x (1 - 0.75 x P(heavy rain on race day or the day before))`
   using the track's NOAA station.

Thursday/Friday openers of 3+ day events are marked `arrival_practice`; the headline odds use
the race days. `reason` is a single plain sentence for agents to read aloud.

## History

`scripts/enrich_mx_history.py` attached NOAA observed rain (the date and the 3 days before) to each
documented event. Documented events were found on track news (Freestone's WordPress archive also
carries the FMF Texas series news), series results and schedule pages, MX Parks and RideMX
event pages and race reports. An empty list means nothing was publicly documented, not that rainouts
never happened; `coverage` says what could not be read.

Caveats: Open-Meteo past rain is model analysis, not a gauge; NOAA stations can be 10-30 km away;
event times are estimated when a source gives only dates (`time_estimated: true`, all-day calendar
event); round numbers can disagree between sources (Orlando: Florida Series says Round 12, MX Parks
says Round 11).

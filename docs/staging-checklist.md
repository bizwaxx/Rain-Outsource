# Pre-launch staging checklist

One command that answers "can we launch?" with **GREEN LIGHT** or a **punch list** (each item has an
owner and a fix). It runs black-box checks against a live deployment and a known-outcome backtest
against real past games, races and concrete pours.

```bash
python scripts/staging_checklist.py                         # production, sampled  (= make staging)
python scripts/staging_checklist.py --mode full             # every confirmed venue + every map venue offline (= make staging-full)
python scripts/staging_checklist.py --base https://<preview>.vercel.app --label "PR #37"
python scripts/staging_checklist.py --known-previews        # production + the open PR previews (= make staging-previews)
python scripts/staging_checklist.py --code-path ../rainout-construction   # backtest construction too (needs PR #28/#31 code)
```

`make` is optional (it isn't installed everywhere); every target is the plain command above.
Exit code: `0` GREEN LIGHT, `1` punch list. Output: a markdown report (`--out`, verdict first) and
optional raw JSON (`--json`). Free sources only, no keys, no signups.

**CI-ready:** it needs only Python 3.11+, the repo and network access, and its exit code is the
verdict, so a scheduled GitHub Actions job is one step (`python scripts/staging_checklist.py`).
The workflow file isn't in this PR because our token lacks the `workflow` scope; add it once that
scope exists.

## What it checks

| Area | Checks |
|---|---|
| Core | `/health`, `/openapi.yaml`, `/llms.txt`, `/`, `/v1/stats` return 200 |
| Every field | `/v1/fields` lists all confirmed venues; `/v1/status` for every confirmed venue (full) or one per official source and city (sample); random map (OSM) venues from every state file; full mode also checks every one of the ~64k map rows offline (ids, names, coordinates within 1,000 km of the state median, NOAA station) |
| Official sources and parsers | per source host: which parser ran and its outcome (matched / no game listed / unparseable / blocked / unavailable ...); coverage (feeds we read vs info pages we only link); readable rate |
| Freshness | `official_status_last_checked` <= 60 min, weather `last_checked` <= 180 min, forecast period within 90 min of game time |
| Forecast providers | NWS points API, Open-Meteo forecast and historical forecast, NOAA NCEI; `/v1/rain` US and non-US |
| Motocross, teams, schedule | `/v1/tracks` and events / check / history / calendar.ics for every track; team search, games and ICS; `/v1/schedule/check` turning an ICS feed into an annotated ICS |
| MCP | initialize, tools/list (core tools plus feature tools where deployed), tools/call for venue status and rain |
| Player reports | read endpoint, validation (400 on bad input); on previews a clearly marked test report (note says it's a test, expires in 24 h) is written and read back from several requests |
| go_no_go / should_we_cancel | when deployed (PR #31 / #35): nowcast, concrete pour, materials, go_no_go for sports and concrete, no listed race is never a bare "go", should_we_cancel for sports, concrete and a no-race motocross time |
| Alert paths | ICS feeds (tracks, teams, schedule check); switch page (PR #37): pages served, every `/v1/status` field `switch.js` reads is present, and `switch.js` is run under Node with stub DOM/fetch/Notification through 7 re-check scenarios (status change, odds crossing 50%, new report, no change, alerts off, request error); Stripe billing webhook (PR #27) rejects unsigned posts; **email, push and odds-change webhooks are reported as not built** |
| Admin | `/v1/admin/usage` is closed without the admin token |

Features that aren't deployed on a target (404) are skipped there, so the same command works on
production and every preview. The report labels each row with its target (production or the PR).

## Known-outcome backtest

`staging/fixtures/known_outcomes.json` holds real events whose outcome was publicly reported,
each with its source URL: college baseball on turf (UFCU Disch-Falk Field), softball dirt infields
(McCombs Field, a Mont Belvieu tournament), grass soccer (Q2 Stadium, NTX State Cup), a high
school football game, motocross races (Freestone, Swan, Murphy's, 3 Palms) and concrete pours
(Durban, Mankato). Nothing is inferred; assumptions (e.g. a start time the source doesn't give)
are listed per fixture, and events we couldn't verify are listed under `excluded`.

For each fixture it fetches that day's weather from the free Open-Meteo archives and runs the
production model code with the official status unknown (the model on its own):

* sports: `calculate_play_probability` at the forecast hour closest to the start (what /v1/status does);
* motocross: `motocross.race_odds` with the track's NOAA calibration, soil, prior-day soak and race-day rain;
* construction: `concrete.evaluate` (when the construction modules are importable).

Weather grade per fixture: `archived_forecast` (archived hourly rain probability, about spring 2024
onward; the fair test), `forecast_amount_proxy` (archived forecast rain amounts converted to a
chance) or `observed_proxy` (what actually fell, ERA5; hindsight). Results are reported by field
type and by weather grade, with every mismatch listed.

It also checks, per fixture, that an official cancellation always wins (0%), that player reports
move the odds the right way only while the official status is unknown and expire, and, for
motocross, feeds the real cancellation post (through the same WordPress feed production reads) to
the notice parser to see whether it would have flagged the race.

## Pass / fail thresholds

| Rule | Threshold |
|---|---|
| Endpoints | every checked endpoint 200 (or the documented 4xx for bad input / no auth) |
| /v1/status latency | p95 <= 10 s, max <= 25 s |
| Freshness | official check <= 60 min, weather <= 180 min, forecast period within 90 min of game time |
| Official sources | >= 90% of polled venues read and understood; any blocked/unparseable source is a punch item |
| Map venues | >= 98% of sampled OSM venues answer 200 with weather-only odds |
| Backtest | directional accuracy >= 70% and **zero confident wrong calls** (>= 80% on a rained-out event, <= 20% on a played one) |

Severity: `blocker` and `should_fix` items make the verdict PUNCH LIST; `info` items and
not-built features are listed but don't block. Thresholds live in `staging/checks.py`
(`THRESHOLDS`).

## Safety

Requests use the User-Agent `rainout-test-staging-checklist/1.0`, which the usage metrics (PR #32)
count as internal traffic, so runs never show up as outside users. Read-only except the marked
test report on previews (production only with
`--allow-prod-test-report`). Concurrency is capped (`--workers`, default 6) so upstream weather
services aren't hammered. No credentials are read or needed.

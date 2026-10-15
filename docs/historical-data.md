# Historical rain data (NOAA GHCN-Daily)

Rainout Source calibrates its play probability with real daily rain observations
from NOAA's Global Historical Climatology Network – Daily (GHCN-Daily). The data
is free and needs no API key.

## How it is built

`scripts/fetch_noaa_history.py` is the reproducible pipeline:

1. Loads every field definition in `data/` (anything with `coordinates`).
2. Reads NOAA's public station list and inventory
   (`https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt`,
   `ghcnd-inventory.txt`) and keeps stations that report precipitation
   (`PRCP`) for the whole study period, within 40 km of the field.
3. Downloads daily `PRCP` for the nearest candidates from NCEI's Access Data
   Service (`https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries`,
   with quality attributes; values that failed NOAA QC are treated as missing).
4. Keeps the **nearest station with at least 90% of days observed** (falls back
   to the best station above 75% if none qualifies; none needed the fallback in
   the current run).
5. Writes a `noaa_history` block into the field's
   `data/historical/*-historical.json`, keeping the hand-entered
   `historical_cancellations` list and the legacy keys.

Raw downloads are cached in `.cache/noaa/` (git-ignored).

```bash
python scripts/fetch_noaa_history.py                    # all fields, 2016-01-01..2025-12-31
python scripts/fetch_noaa_history.py --field krieg      # one field
python scripts/fetch_noaa_history.py --start 2016-01-01 --end 2025-12-31 --max-km 40 --min-coverage 0.9
```

Run it again whenever a field is added; it is idempotent for a fixed period.

## `noaa_history` schema

| Key | Meaning |
| --- | --- |
| `station` | GHCN station id, name, lat/lon, elevation, distance from the field (km) |
| `period` | Study period (`start`, `end`) |
| `days_observed`, `coverage_percent`, `coverage_ok` | Observation coverage over the period |
| `rain_to_cancel_threshold_in` | Daily rain total treated as rainout-level for this field |
| `annual` | Mean yearly rain, wet days (>= 0.01 in) per year, rainout-level days per year, share of wet days that reach the threshold |
| `monthly.MM` | Per month: days observed, mean monthly rain, `wet_day_frac`, `p_ge_0_10` … `p_ge_1_00` (share of days at or above each amount), `rainout_day_frac`, `cancel_given_wet` |
| `rainout_level_days` | Every day in the period at or above the threshold (`date`, `prcp_in`) |
| `recorded_cancellations_check` | For each hand-entered cancellation, what the station recorded that day and the day before |

### Rain-to-cancel threshold

Default by sport: baseball/softball (skinned dirt infields) **0.5 in**, soccer and
multipurpose grass fields **0.75 in**, motocross **1.0 in**. A field file can set
`rain_to_cancel_threshold_in` directly, and an optional `drainage` of
`poor` / `average` / `good` scales it by 0.75 / 1.0 / 1.5.

## How it changes play probability

The NWS rain chance is the chance of any measurable rain (>= 0.01 in). Most rainy
days do not reach rainout level, so when the official status is `on`:

```
severity = 0.5 + 0.5 * cancel_given_wet(field, game month)   # 1.0 if thunderstorms are likely
play_probability = 100 - round(rain_chance * severity)  (then the existing storm/lead-time adjustments)
```

- The 50% floor keeps history from discounting rain too much: wet grounds,
  lightning rules and crew calls cancel games below the daily threshold too.
- Thunderstorms count fully, because lightning rules stop play regardless of totals.
- Official `cancelled` / `field_closed` (0%) and `unknown` (50%) are unchanged.
- `risk_level` and `recommendation` still come from the raw rain chance, so a
  60%+ rain chance is still flagged high risk.
- Without NOAA history the original model runs unchanged.

`/v1/status` returns an additive `historical_calibration` object (station, month,
threshold, `rainy_day_share_reaching_threshold`, `rain_severity_applied`).

## Current coverage (2016-01-01 to 2025-12-31)

| Field | Station | km | Coverage | Mean rain/yr (in) | Rainout-level days/yr | Rainy days reaching threshold |
| --- | --- | --- | --- | --- | --- | --- |
| `austin-tx-anderson-high-school-baseball-field-ambl` | US1TXTV0087 (AUSTIN 3.9 NNE) | 3.4 | 96.5% | 36.0 | 23.0 | 24% |
| `austin-metro-northeast-metropolitan-park` | US1TXTV0267 (PFLUGERVILLE 2.2 ENE) | 4.6 | 92.9% | 35.3 | 22.4 | 25% |
| `austin-metro-southeast-metropolitan-park` | USW00013904 (AUSTIN BERGSTROM INTL AP) | 7.0 | 100.0% | 34.6 | 20.5 | 26% |
| `austin-tx-balcones-youth-sports` | USC00410433 (AUSTIN GREAT HILLS) | 5.7 | 100.0% | 33.0 | 21.1 | 26% |
| `cedar-park-tx-brushy-creek-sports-park-softball-fields` | US1TXWM0044 (JOLLYVILLE 1.2 WNW) | 5.9 | 90.6% | 35.9 | 22.3 | 22% |
| `cedar-park-tx-elizabeth-milburn-park-multipurpose-fields` | US1TXTV0021 (JONESTOWN 2.6 E) | 4.3 | 96.8% | 30.7 | 13.4 | 18% |
| `dallas-tx-dallas-sport-social-club-softball` | US1TXDA0053 (DALLAS 2.7 WNW) | 4.3 | 99.3% | 44.1 | 26.0 | 33% |
| `dallas-tx-dfw-adult-baseball-association` | US1TXDA0053 (DALLAS 2.7 WNW) | 4.3 | 99.3% | 44.1 | 26.0 | 33% |
| `austin-tx-downs-field-ambl` | US1TXTV0044 (AUSTIN 1.0 N(45TH&SHCRK)) | 6.3 | 91.4% | 36.4 | 24.4 | 27% |
| `dripping-springs-tx-ctx-field-of-dreams` | US1TXHYS017 (DRIPPING SPRINGS 8.4 W) | 3.2 | 99.6% | 32.5 | 19.9 | 23% |
| `dripping-springs-tx-dsysa-baseball-softball-sports-complex` | US1TXHYS063 (DRIPPING SPRINGS 1.7 NW) | 1.4 | 91.8% | 34.5 | 22.7 | 27% |
| `dripping-springs-tx-dsysa-sports-park-fields` | US1TXHYS063 (DRIPPING SPRINGS 1.7 NW) | 3.5 | 91.8% | 34.5 | 22.7 | 27% |
| `fort-worth-tx-fort-worth-university-little-league` | US1TXTN0102 (FORT WORTH 3.9 SW) | 6.8 | 93.1% | 38.7 | 25.2 | 31% |
| `fort-worth-tx-senior-softball-dfw` | US1TXTN0102 (FORT WORTH 3.9 SW) | 6.8 | 93.1% | 38.7 | 25.2 | 31% |
| `georgetown-tx-san-gabriel-park-gyba-field-8-ambl` | US1TXWM0202 (GEORGETOWN 1.4 SE) | 2.5 | 92.6% | 35.4 | 22.6 | 23% |
| `austin-tx-havins-softball-complex` | US1TXTV0087 (AUSTIN 3.9 NNE) | 6.3 | 96.5% | 36.0 | 23.0 | 24% |
| `helotes-tx-greater-helotes-little-league` | US1TXBXR310 (SAN ANTONIO 13.0 W) | 5.9 | 91.9% | 27.4 | 16.6 | 21% |
| `houston-tx-houston-sports-social-club-softball` | USC00414325 (HOUSTON-WESTBURY) | 8.8 | 98.6% | 55.3 | 33.0 | 29% |
| `houston-tx-houston-sportsplex` | USC00414325 (HOUSTON-WESTBURY) | 7.2 | 98.6% | 55.3 | 33.0 | 29% |
| `austin-tx-krieg-field-softball-complex` | USW00013904 (AUSTIN BERGSTROM INTL AP) | 7.8 | 100.0% | 34.6 | 20.5 | 26% |
| `manchaca-tx-manchaca-optimist-youth-sports-complex` | US1TXHYS028 (MANCHACA 2.1 ENE) | 1.6 | 99.9% | 36.8 | 22.6 | 24% |
| `austin-tx-northwest-little-league` | US1TXTV0044 (AUSTIN 1.0 N(45TH&SHCRK)) | 2.7 | 91.4% | 36.4 | 24.4 | 27% |
| `austin-tx-oak-hill-youth-sports-association` | US1TXTV0228 (AUSTIN 9.8 WSW) | 4.0 | 94.4% | 30.7 | 20.1 | 21% |
| `round-rock-tx-old-settlers-baseball-complex` | US1TXWM0148 (ROUND ROCK 3.0 NE) | 2.0 | 96.7% | 30.2 | 19.2 | 25% |
| `round-rock-tx-old-settlers-softball-complex` | US1TXWM0148 (ROUND ROCK 3.0 NE) | 2.0 | 96.7% | 30.2 | 19.2 | 25% |
| `san-antonio-tx-lady-bird-johnson-park-softball-fields` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 5.4 | 99.8% | 27.4 | 17.3 | 20% |
| `san-antonio-tx-mcallister-park-baseball-softball-fields` | US1TXBXR048 (HOLLYWOOD PARK 1.7 ESE) | 3.6 | 96.5% | 32.6 | 18.3 | 20% |
| `san-antonio-tx-normoyle-park-baseball-fields` | US1TXBXR015 (LACKLAND AFB 3.5 SSE) | 7.3 | 95.3% | 29.2 | 18.6 | 23% |
| `san-antonio-tx-northside-suburban-little-league` | US1TXBXR310 (SAN ANTONIO 13.0 W) | 8.3 | 91.9% | 27.4 | 16.6 | 21% |
| `san-antonio-tx-northwest-little-league` | US1TXBXR184 (SAN ANTONIO 8.0 NNW) | 9.2 | 98.0% | 29.2 | 19.5 | 26% |
| `san-antonio-tx-seniors-softball-league-normoyle-park` | US1TXBXR015 (LACKLAND AFB 3.5 SSE) | 7.5 | 95.3% | 29.2 | 18.6 | 23% |
| `san-antonio-tx-sports-social-club-softball-fields` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 9.0 | 99.8% | 27.4 | 17.3 | 20% |
| `san-antonio-tx-sportskind-softball` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 9.0 | 99.8% | 27.4 | 17.3 | 20% |
| `austin-tx-town-and-country-sports-complex` | US1TXWM0044 (JOLLYVILLE 1.2 WNW) | 1.1 | 90.6% | 35.9 | 22.3 | 22% |
| `austin-tx-western-hills-little-league` | USW00013958 (AUSTIN-CAMP MABRY) | 3.0 | 100.0% | 32.6 | 21.1 | 26% |

The motocross stubs (`floresville-tx-cycle-ranch-motocross-park`,
`red-rock-tx-murphys-mx`, `wortham-tx-freestone-raceway`) have no field
definition or coordinates yet, so they have no NOAA history.

## Caveats

- Most stations are CoCoRaHS volunteer gauges (`US1…` ids) read around 7 AM,
  so a day's total mostly covers the previous evening. Airport stations
  (`USW…`) use midnight-to-midnight days. Missing days in volunteer records may
  slightly understate totals (for example the San Antonio/Helotes gauges).
- The threshold and the 50% floor are informed defaults, not fitted to real
  cancellations; there are only a few hand-entered cancellation records so far.
  As official rainouts are logged in `historical_cancellations`, fit them.
- The sample cancellation records (Krieg 2025-05-15, Downs 2025-05-20,
  Havins 2025-06-01) do not match the station records (0.00 in that day and the
  day before); see `recorded_cancellations_check`. Krieg 2025-04-22 matches
  Camp Mabry (1.26 in) but not the nearer Bergstrom gauge.

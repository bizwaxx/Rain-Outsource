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
- Official `cancelled` / `field_closed` stay at 0%. `unknown` status uses a 96%
  baseline lowered by rain, with this history at half weight (see `play-probability.md`).
- `risk_level` and `recommendation` still come from the raw rain chance, so a
  60%+ rain chance is still flagged high risk.
- Without NOAA history the original model runs unchanged.

`/v1/status` returns an additive `historical_calibration` object (station, month,
threshold, `rainy_day_share_reaching_threshold`, `rain_severity_applied`).

## Current coverage (2016-01-01 to 2025-12-31)

All 68 venues have NOAA history (nearest qualifying station 1–25 km away; the
farthest is Freestone Raceway, 25.2 km from Corsicana Campbell Field).

| Field | Station | km | Coverage | Threshold (in) | Mean rain/yr (in) | Rainout-level days/yr | Rainy days reaching threshold |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `austin-metro-northeast-metropolitan-park` | US1TXTV0267 (PFLUGERVILLE 2.2 ENE) | 4.6 | 92.9% | 0.5 | 35.3 | 22.4 | 25% |
| `austin-metro-southeast-metropolitan-park` | USW00013904 (AUSTIN BERGSTROM INTL AP) | 7.0 | 100.0% | 0.5 | 34.6 | 20.5 | 26% |
| `austin-tx-anderson-high-school-baseball-field-ambl` | US1TXTV0087 (AUSTIN 3.9 NNE) | 3.4 | 96.5% | 0.5 | 36.0 | 23.0 | 24% |
| `austin-tx-balcones-youth-sports` | USC00410433 (AUSTIN GREAT HILLS) | 5.7 | 100.0% | 0.5 | 33.0 | 21.1 | 26% |
| `austin-tx-downs-field-ambl` | US1TXTV0044 (AUSTIN 1.0 N(45TH&SHCRK)) | 6.3 | 91.4% | 0.5 | 36.4 | 24.4 | 27% |
| `austin-tx-havins-softball-complex` | US1TXTV0087 (AUSTIN 3.9 NNE) | 6.3 | 96.5% | 0.5 | 36.0 | 23.0 | 24% |
| `austin-tx-krieg-field-softball-complex` | USW00013904 (AUSTIN BERGSTROM INTL AP) | 7.8 | 100.0% | 0.5 | 34.6 | 20.5 | 26% |
| `austin-tx-northwest-little-league` | US1TXTV0044 (AUSTIN 1.0 N(45TH&SHCRK)) | 2.7 | 91.4% | 0.5 | 36.4 | 24.4 | 27% |
| `austin-tx-oak-hill-youth-sports-association` | US1TXTV0228 (AUSTIN 9.8 WSW) | 4.0 | 94.4% | 0.5 | 30.7 | 20.1 | 21% |
| `austin-tx-onion-creek-soccer-complex` | US1TXTV0218 (ONION CREEK 3.2 ENE) | 1.9 | 97.1% | 0.75 | 36.4 | 13.9 | 18% |
| `austin-tx-town-and-country-sports-complex` | US1TXWM0044 (JOLLYVILLE 1.2 WNW) | 1.1 | 90.6% | 0.5 | 35.9 | 22.3 | 22% |
| `austin-tx-western-hills-little-league` | USW00013958 (AUSTIN-CAMP MABRY) | 3.0 | 100.0% | 0.5 | 32.6 | 21.1 | 26% |
| `baton-rouge-la-brec-burbank-soccer-complex` | US1LAEB0033 (LSU) | 10.6 | 92.5% | 0.75 | 62.2 | 28.3 | 25% |
| `baytown-tx-baytown-soccer-park` | USC00410586 (BAYTOWN) | 15.7 | 94.9% | 0.75 | 66.1 | 27.5 | 26% |
| `cedar-park-tx-brushy-creek-sports-park-softball-fields` | US1TXWM0044 (JOLLYVILLE 1.2 WNW) | 5.9 | 90.6% | 0.5 | 35.9 | 22.3 | 22% |
| `cedar-park-tx-elizabeth-milburn-park-multipurpose-fields` | US1TXTV0021 (JONESTOWN 2.6 E) | 4.3 | 96.8% | 0.75 | 30.7 | 13.4 | 18% |
| `conroe-tx-3-palms-action-sports-park` | US1TXMNG026 (THE WOODLANDS 2.8 N) | 6.0 | 97.3% | 1.0 | 51.0 | 16.7 | 18% |
| `dade-city-fl-dade-city-motocross` | US1FLPS0010 (DADE CITY 1.8 WSW) | 1.7 | 98.1% | 1.0 | 62.9 | 19.2 | 16% |
| `dallas-tx-dallas-sport-social-club-softball` | US1TXDA0053 (DALLAS 2.7 WNW) | 4.3 | 99.3% | 0.5 | 44.1 | 26.0 | 33% |
| `dallas-tx-dfw-adult-baseball-association` | US1TXDA0053 (DALLAS 2.7 WNW) | 4.3 | 99.3% | 0.5 | 44.1 | 26.0 | 33% |
| `dallas-tx-moneygram-soccer-park-at-elm-fork` | US1TXDA0003 (UNIVERSITY PARK 3.1 WNW) | 6.2 | 96.9% | 0.75 | 45.6 | 20.5 | 23% |
| `dripping-springs-tx-ctx-field-of-dreams` | US1TXHYS017 (DRIPPING SPRINGS 8.4 W) | 3.2 | 99.6% | 0.5 | 32.5 | 19.9 | 23% |
| `dripping-springs-tx-dsysa-baseball-softball-sports-complex` | US1TXHYS063 (DRIPPING SPRINGS 1.7 NW) | 1.4 | 91.8% | 0.5 | 34.5 | 22.7 | 27% |
| `dripping-springs-tx-dsysa-sports-park-fields` | US1TXHYS063 (DRIPPING SPRINGS 1.7 NW) | 3.5 | 91.8% | 0.5 | 34.5 | 22.7 | 27% |
| `edmond-ok-service-blake-soccer-complex` | US1OKLG0014 (EDMOND 6.2 NNW) | 10.3 | 97.3% | 0.75 | 37.7 | 18.0 | 23% |
| `farmers-branch-tx-ross-stewart-soccer-complex` | USW00003927 (DAL-FTW WSCMO AP) | 9.6 | 99.9% | 0.75 | 38.5 | 16.5 | 20% |
| `fellsmere-fl-mesa-motocross-park` | US1FLIR0024 (VERO BEACH 3.4 W) | 19.0 | 95.2% | 1.0 | 60.8 | 16.5 | 11% |
| `floresville-tx-cycle-ranch-motocross-park` | US1TXWO0005 (FLORESVILLE 8.1 NNW) | 14.5 | 99.6% | 1.0 | 29.9 | 8.7 | 9% |
| `fort-worth-tx-fort-worth-university-little-league` | US1TXTN0102 (FORT WORTH 3.9 SW) | 6.8 | 93.1% | 0.5 | 38.7 | 25.2 | 31% |
| `fort-worth-tx-senior-softball-dfw` | US1TXTN0102 (FORT WORTH 3.9 SW) | 6.8 | 93.1% | 0.5 | 38.7 | 25.2 | 31% |
| `georgetown-tx-san-gabriel-park-gyba-field-8-ambl` | US1TXWM0202 (GEORGETOWN 1.4 SE) | 2.5 | 92.6% | 0.5 | 35.4 | 22.6 | 23% |
| `helotes-tx-greater-helotes-little-league` | US1TXBXR310 (SAN ANTONIO 13.0 W) | 5.9 | 91.9% | 0.5 | 27.4 | 16.6 | 21% |
| `houston-tx-houston-sports-social-club-softball` | USC00414325 (HOUSTON-WESTBURY) | 8.8 | 98.6% | 0.5 | 55.3 | 33.0 | 29% |
| `houston-tx-houston-sportsplex` | USC00414325 (HOUSTON-WESTBURY) | 7.2 | 98.6% | 0.5 | 55.3 | 33.0 | 29% |
| `irving-tx-irving-soccer-complex` | USW00003927 (DAL-FTW WSCMO AP) | 2.9 | 99.9% | 0.75 | 38.5 | 16.5 | 20% |
| `jacksonville-fl-bob-hayes-sports-complex` | USW00013889 (JACKSONVILLE) | 10.0 | 100.0% | 0.75 | 54.8 | 23.7 | 20% |
| `lake-charles-la-lagrange-soccer-park` | USC00165076 (LAKE CHARLES PORT) | 3.7 | 91.3% | 0.75 | 68.0 | 29.9 | 26% |
| `manchaca-tx-manchaca-optimist-youth-sports-complex` | US1TXHYS028 (MANCHACA 2.1 ENE) | 1.6 | 99.9% | 0.5 | 36.8 | 22.6 | 24% |
| `mansfield-tx-philip-thompson-soccer-complex` | US1TXTN0061 (MANSFIELD 2.6 NNE) | 2.3 | 99.3% | 0.75 | 41.8 | 17.6 | 23% |
| `mckinney-tx-al-ruschhaupt-soccer-complex` | USW00053914 (MCKINNEY MUNI AP) | 5.5 | 99.5% | 0.75 | 39.0 | 17.6 | 21% |
| `mckinney-tx-mckinney-soccer-complex-at-craig-ranch` | US1TXCLL023 (FAIRVIEW 1.5 ESE) | 9.3 | 91.4% | 0.75 | 47.3 | 21.1 | 26% |
| `miami-fl-kendall-soccer-park` | US1FLMD0007 (SUNSET 2.4 SW) | 2.0 | 98.9% | 0.75 | 70.6 | 30.6 | 20% |
| `muskogee-ok-sweet-16-mx` | USW00093953 (MUSKOGEE DAVIS FLD) | 15.0 | 99.4% | 1.0 | 44.0 | 13.3 | 14% |
| `new-braunfels-tx-heb-soccer-complex` | US1TXCML003 (NEW BRAUNFELS 3.1 WNW) | 3.9 | 99.9% | 0.75 | 31.5 | 12.6 | 13% |
| `norman-ok-oklahoma-motorsports-complex` | US1OKCV0045 (NORMAN 2.7 SW) | 1.8 | 94.7% | 1.0 | 35.2 | 11.2 | 15% |
| `orlando-fl-econ-soccer-complex` | US1FLOR0019 (UNION PARK 1.1 E) | 5.6 | 98.4% | 0.75 | 60.8 | 25.5 | 20% |
| `orlando-fl-orlando-mx-park` | US1FLOR0019 (UNION PARK 1.1 E) | 12.8 | 98.4% | 1.0 | 60.8 | 17.3 | 13% |
| `pembroke-pines-fl-west-pines-soccer-park-preserve` | USW00012882 (MIAMI OPA LOCKA AP) | 16.5 | 99.0% | 0.75 | 58.6 | 23.9 | 17% |
| `red-rock-tx-murphys-mx` | US1TXBST090 (RED ROCK 1.2 N) | 4.7 | 98.1% | 1.0 | 33.9 | 9.8 | 12% |
| `round-rock-tx-old-settlers-baseball-complex` | US1TXWM0148 (ROUND ROCK 3.0 NE) | 2.0 | 96.7% | 0.5 | 30.2 | 19.2 | 25% |
| `round-rock-tx-old-settlers-softball-complex` | US1TXWM0148 (ROUND ROCK 3.0 NE) | 2.0 | 96.7% | 0.5 | 30.2 | 19.2 | 25% |
| `san-antonio-tx-gvtc-soccer-complex` | US1TXCML024 (BULVERDE 4.3 ESE) | 5.5 | 93.5% | 0.75 | 31.1 | 12.3 | 14% |
| `san-antonio-tx-lady-bird-johnson-park-softball-fields` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 5.4 | 99.8% | 0.5 | 27.4 | 17.3 | 20% |
| `san-antonio-tx-mcallister-park-baseball-softball-fields` | US1TXBXR048 (HOLLYWOOD PARK 1.7 ESE) | 3.6 | 96.5% | 0.5 | 32.6 | 18.3 | 20% |
| `san-antonio-tx-mission-city-soccer-complex` | US1TXBXR313 (ELMENDORF 0.8 NNE) | 13.5 | 96.6% | 0.75 | 29.0 | 12.2 | 13% |
| `san-antonio-tx-normoyle-park-baseball-fields` | US1TXBXR015 (LACKLAND AFB 3.5 SSE) | 7.3 | 95.3% | 0.5 | 29.2 | 18.6 | 23% |
| `san-antonio-tx-northside-suburban-little-league` | US1TXBXR310 (SAN ANTONIO 13.0 W) | 8.3 | 91.9% | 0.5 | 27.4 | 16.6 | 21% |
| `san-antonio-tx-northwest-little-league` | US1TXBXR184 (SAN ANTONIO 8.0 NNW) | 9.2 | 98.0% | 0.5 | 29.2 | 19.5 | 26% |
| `san-antonio-tx-seniors-softball-league-normoyle-park` | US1TXBXR015 (LACKLAND AFB 3.5 SSE) | 7.5 | 95.3% | 0.5 | 29.2 | 18.6 | 23% |
| `san-antonio-tx-sports-social-club-softball-fields` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 9.0 | 99.8% | 0.5 | 27.4 | 17.3 | 20% |
| `san-antonio-tx-sportskind-softball` | US1TXBXR135 (TERRELL HILLS 1.0 NE) | 9.0 | 99.8% | 0.5 | 27.4 | 17.3 | 20% |
| `schertz-tx-schertz-soccer-complex` | US1TXGP0029 (SCHERTZ 2.2 N) | 3.9 | 90.5% | 0.75 | 27.9 | 11.9 | 14% |
| `tampa-fl-shimberg-sports-complex` | USW00012842 (TAMPA INTL AP) | 5.8 | 100.0% | 0.75 | 53.6 | 21.6 | 19% |
| `tulsa-ok-alsuma-soccer-complex` | US1OKTL0014 (TULSA 5.4 SSE) | 3.9 | 99.6% | 0.75 | 50.3 | 22.7 | 23% |
| `tulsa-ok-bill-boevers-soccer-complex` | US1OKTL0042 (BROKEN ARROW 4.6 NNW) | 1.4 | 97.6% | 0.75 | 46.1 | 20.1 | 20% |
| `tulsa-ok-mohawk-sports-complex` | USW00013968 (TULSA INTL AP) | 6.3 | 100.0% | 0.75 | 42.7 | 19.3 | 21% |
| `tyler-tx-swan-mx` | USW00013972 (TYLER POUNDS FLD) | 9.4 | 99.9% | 1.0 | 42.6 | 13.3 | 14% |
| `wortham-tx-freestone-raceway` | USW00053912 (CORSICANA CAMPBELL FLD) | 25.2 | 99.0% | 1.0 | 37.1 | 12.1 | 15% |

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

# Construction layer (v2 beta)

Tools: `get_precipitation_nowcast` (`/v1/nowcast`), `check_concrete_pour` (`/v1/construction/concrete-pour`),
`check_materials_risk` (`/v1/construction/materials`) and `go_no_go` (`/v1/go-no-go`).

Rainout Source v2 adds rain-risk tools for outdoor work on top of the sports core. It reuses the same free
weather plumbing (NWS, Open-Meteo, the shared cache) and the same answer style: a decision, a probability,
one plain sentence of reason, and the sources.

> **Not engineering advice.** These tools read public weather forecasts and apply simple, documented rules
> of thumb. They do not replace your project specifications, the concrete supplier, the engineer of record,
> manufacturer instructions or local codes. Forecasts can be wrong, especially for pop-up thunderstorms.

Job sites have no venue database. Pass `lat` and `lon` for the exact site (best). A town name
(`q=Austin, TX`, or `location` in JSON) goes through the same free Open-Meteo geocoder as `/v1/rain` and
resolves to the **town center**; street addresses resolve only as far as their town, and the response says
so in `location.precision_note`.

## Two-hour nowcast: `GET /v1/nowcast`, MCP `get_precipitation_nowcast`

```
GET /v1/nowcast?lat=30.27&lon=-97.74
GET /v1/nowcast?q=Austin, TX
```

Returns 8 fifteen-minute `intervals` for the next 2 hours, each with `precip_mm`/`precip_in`,
`rate_mm_per_hr`, `intensity` and `probability_percent`, plus `rain_expected`, `rain_starts_at`,
`rain_ends_at`, `peak_intensity`, totals, `observed` (US) and a one-sentence `summary`.

| Piece | Source | Notes |
|---|---|---|
| 15-minute amounts | Open-Meteo `minutely_15` | NOAA HRRR at 15-minute steps over North America; interpolated hourly models elsewhere. Each value is the sum of the 15 minutes **before** its timestamp, so it is shifted onto the slot it covers. Cached 5 min. |
| Rain chance (US) | NWS gridpoint data (`/gridpoints/{wfo}/{x},{y}`) `probabilityOfPrecipitation` | Primary in the US, matching `/v1/rain`. Cached 10 min. |
| Fallback amounts (US) | NWS gridpoint `quantitativePrecipitation` | Multi-hour totals spread evenly over 15-minute steps; flagged in `notes` and `amount_source: nws_qpf_spread`. |
| Rain chance (elsewhere) | Open-Meteo `minutely_15` `precipitation_probability` | |
| Last hour observed (US) | NWS nearest station `/observations/latest` `precipitationLastHour` | Often null; `stale: true` when older than 90 min. |

Intensity uses the AMS Glossary of Meteorology rain-rate classes: light < 2.5 mm/h, moderate 2.5–7.6 mm/h,
heavy > 7.6 mm/h. A 15-minute step counts as rain at ≥ 0.1 mm (about 0.4 mm/h).

This is a **model nowcast, not radar extrapolation**. Thunderstorms can form between model runs; recheck
every 15–30 minutes (`recheck_after_minutes`) when it matters.

### Optional provider (off by default)

If the operator already has a [Precip](https://precip.ai/) API key, setting `RAINOUT_PRECIP_API_KEY`
makes the nowcast use Precip's observation-based 1-minute rates (`GET https://api.precip.ai/api/v1/minutely`,
`Authorization: Bearer ...`) for amounts, averaged into 15-minute steps. Precip is a paid service (also sold
per call through Orthogonal's x402 gateway). Rainout Source never signs up for it and makes no request to it
unless that variable is set.

### Inspiration (not integrated)

- **Precip (Orthogonal)**: ~1 km observation-based rainfall; paid, so optional only (above).
- **Buienradar MCP** (`get_precipitation_for`): 2-hour radar nowcast by lat/lon, but **Netherlands/Belgium
  only**, so not usable for US job sites.
- **NOAA MRMS** radar precipitation: free, but published as GRIB2 grids that need heavy decoding libraries,
  too large for this serverless function. A candidate for a future worker.

## Concrete pour: `GET|POST /v1/construction/concrete-pour`, MCP `check_concrete_pour`

```
GET /v1/construction/concrete-pour?lat=30.27&lon=-97.74&pour_start=2026-10-21 07:00
POST /v1/construction/concrete-pour  {"lat": 30.27, "lon": -97.74, "pour_start": "2026-10-21T07:00:00-05:00",
                                      "duration_hours": 4, "cure_hours": 24, "mix": "standard", "protection": false}
```

| Input | Default | Meaning |
|---|---|---|
| `lat`, `lon` (or `location`) | required | Job site. Town names resolve to the town center. |
| `pour_start` | required | Local time at the site (`2026-10-21 07:00`) or ISO 8601 with offset. |
| `duration_hours` | 4 | Length of the pour (0.5-16). |
| `cure_hours` | 24 | Cure window after the pour to check (2-72). |
| `critical_hours` | by mix | Early-set window right after the pour that is most sensitive to rain (1-8). |
| `mix` | `standard` | `standard` (4 h early set), `high_early` / `accelerated` (2 h), `retarded` / `slag_or_fly_ash` (6 h), `low_wc_or_silica_fume` (4 h, stricter evaporation limit 0.5 kg/m²/h). |
| `protection` | false | Covers/blankets/heat ready on site: heavy rain later in the cure becomes OK and freeze becomes `wait` instead of `no_go`. |
| `concrete_temp_f` | air temp | Concrete temperature at placement, for the evaporation estimate. |
| `search_days` | 3 | Days to scan (starts 5 AM-2 PM local) for a better window (0-5). |

Returns `decision` (`go` / `wait` / `no_go`), `probability_percent`, `rain_probability_percent`, a plain
`reason`, every `check` with its value and threshold, `conditions` for the pour / early set / rest of cure,
`best_alternative` (only when it beats the requested start), `best_window_per_day`, `recheck_at`, `sources`,
`thresholds` (with references) and `disclaimer`.

### Rules (data/construction/thresholds.json)

| Check | Wait | No-go | Basis |
|---|---|---|---|
| Thunderstorms during the pour | | any | NOAA/NWS and OSHA lightning safety: stop outdoor work |
| Rain during the pour | chance ≥ 30% or ≥ 0.5 mm (0.02 in) | chance ≥ 60%, or ≥ 2.5 mm (0.1 in) with chance ≥ 30% | field practice: don't place or finish in rain |
| Rain in the early-set window | chance ≥ 30% or ≥ 0.5 mm | chance ≥ 70%, or ≥ 2.5 mm with chance ≥ 30% | fresh surface vulnerable until final set (≈2-4 h at 70 °F, longer when cool or retarded) |
| Heavy rain later in the cure | ≥ 6.4 mm (0.25 in) total or ≥ 7.6 mm/h (AMS heavy), unless `protection` | | field practice; AMS rain-rate classes |
| Freeze (≤ 32 °F, pour through cure) | with `protection` | without `protection` | ACI 306: protect from freezing until ≈500 psi |
| Cold (< 40 °F) | needs a cold-weather plan | | ACI 306 definition of cold weather |
| Heat (> 90 °F during the pour) | needs a hot-weather plan or earlier start | | ACI 305; many specs cap concrete at 90 °F (ACI 305.1 default 95 °F) |
| Evaporation (pour + early set) | ≥ 1.0 kg/m²/h (0.2 lb/ft²/h), 0.5 for low w/cm or silica fume | | ACI 305 nomograph equation (Uno 1998) |
| Wind gusts during the pour | ≥ 35 mph | | conservative pump-boom screen; check the manufacturer limit |

The evaporation estimate uses the ACI 305 equation
`E = 5·((Tc+18)^2.5 − r·(Ta+18)^2.5)·(V+4)·10⁻⁶` (kg/m²/h; °C, relative humidity as a fraction, wind in km/h),
with concrete temperature = air temperature unless `concrete_temp_f` is given. It uses the forecast's 10 m wind,
which is higher than wind at the slab, so it errs on the cautious side.

`probability_percent` is a heuristic: 100 minus the highest hourly rain chance in the pour and early set, then
× 0.85 for each other `wait` flag and × 0.3 for a non-rain `no_go` flag. Rain chance comes from NWS in the US
(same as /v1/rain) and Open-Meteo elsewhere; temperature, humidity, wind and amounts come from Open-Meteo.

`recheck_at`: 24 h before far-off pours, the evening before (12 h) for tomorrow's pour, then run
`get_precipitation_nowcast` about 2 hours before the pour.

Override any threshold with `RAINOUT_CONSTRUCTION_THRESHOLDS=/path/to/override.json` (deep-merged).

## Materials damage risk: `GET|POST /v1/construction/materials`, MCP `check_materials_risk`

```
GET /v1/construction/materials?lat=30.27&lon=-97.74&materials=drywall,lumber,cement_bags&hours=24
POST /v1/construction/materials {"lat": 30.27, "lon": -97.74, "start": "2026-10-21 17:00", "end": "2026-10-22 07:00",
                                  "materials": [{"material": "drywall", "covered": true}, "lumber", "open_roof_deck"]}
```

The window (default the next 24 h, up to 7 days) gets a **rain level**:

| Level | Rule (data/construction/thresholds.json) |
|---|---|
| none | max hourly rain chance < 15% and total < 0.25 mm |
| heavy | chance ≥ 40% and (total ≥ 12.7 mm / 0.5 in, or an hour ≥ 7.6 mm, AMS "heavy") |
| likely | chance ≥ 50% or total ≥ 2.5 mm (0.1 in) |
| possible | anything else |

Each material maps the level to a risk. Covered materials drop one step, except when gusts reach 25 mph
(loose tarps lift), and moisture-sensitive materials go to at least `moderate` after 6+ hours at 90%+ humidity.

| Material | Label | Risk at none / possible / likely / heavy | Reference |
|---|---|---|---|
| `drywall` | drywall (gypsum board) | low / high / severe / severe | Gypsum Association GA-801, Handling and Storage of Gypsum Panel Products |
| `insulation` | insulation (batts, rolls, loose fill) | low / high / severe / severe | Common field practice (PCA/NRMCA guidance such as NRMCA CIP 11 'Curing In-Place Concrete' and CIP 12 'Hot Weather Concreting') |
| `lumber` | framing lumber | low / low / moderate / high | American Wood Council / WWPA lumber storage guidance |
| `osb` | OSB or plywood sheathing | low / low / moderate / high | APA Engineered Wood Construction Guide (Form E30) |
| `cement_bags` | bagged cement, mortar or grout | low / high / severe / severe | Portland Cement Association |
| `open_roof_deck` | open roof deck (no underlayment) | low / moderate / high / severe | Common field practice (PCA/NRMCA guidance such as NRMCA CIP 11 'Curing In-Place Concrete' and CIP 12 'Hot Weather Concreting') |
| `electrical` | electrical equipment and rough-in | low / high / severe / severe | NFPA 70 (NEC) 110.11 Deteriorating Agents |
| `flooring` | wood or laminate flooring | low / high / severe / severe | National Wood Flooring Association installation guidelines |
| `masonry_units` | brick or block | low / low / low / moderate | Brick Industry Association Technical Notes (e.g. 7A, 23A) |
| `windows_doors` | windows and doors not yet installed | low / low / moderate / high | Common field practice (PCA/NRMCA guidance such as NRMCA CIP 11 'Curing In-Place Concrete' and CIP 12 'Hot Weather Concreting') |
| `rebar` | rebar and structural steel | low / low / low / low | Common field practice (PCA/NRMCA guidance such as NRMCA CIP 11 'Curing In-Place Concrete' and CIP 12 'Hot Weather Concreting') |
| `open_excavation` | open excavation or trench | low / moderate / high / severe | EPA construction stormwater (SWPPP) guidance |

Aliases are accepted (plywood, sheathing → `osb`; cement, mortar, grout → `cement_bags`; brick, block → `masonry_units`;
roof deck → `open_roof_deck`; trench → `open_excavation`; plurals). Each result has `risk`, `reason`, `actions` and the
`reference` text. The response also has `overall_risk`, a spoken `summary`, `rain` (level, total, peak intensity,
wettest hour), `wind` (gusts, `covers_at_risk`), `humid_hours` and `recheck_at`.

## Unified go / no-go: `GET|POST /v1/go-no-go`, MCP `go_no_go`

```
GET /v1/go-no-go?activity=softball&venue=Krieg&time=2026-10-23 20:20
GET /v1/go-no-go?activity=motocross&track_id=swan&time=2026-10-24 09:00
GET /v1/go-no-go?activity=roofing&lat=30.27&lon=-97.74&time=2026-10-21 08:00
POST /v1/go-no-go {"activity": "concrete_pour", "lat": 30.27, "lon": -97.74, "time": "2026-10-21 07:00", "mix": "high_early"}
```

Every answer has the same core shape:

```json
{"activity": "roofing", "decision": "go | wait | no_go", "probability_percent": 88,
 "reason": "Go for roofing: 5% rain chance, 54-83°F, gusts to 12 mph.",
 "recheck_at": "2026-10-20T20:00:00-05:00", "sources": [...], "routed_to": "workability",
 "time": {...}, "location": {...}, "details": {...}}
```

| activity | Needs | Routed to | probability_percent means |
|---|---|---|---|
| softball, baseball, soccer | `venue` (name or field_id) | `get_venue_status` (official status + NWS + NOAA history) | chance the game is played |
| softball, baseball, soccer | only a location | `weather_only_play_odds` (same unknown-status model, no official status) | chance the game is played |
| motocross | `track_id` | `check_race` | chance the race runs |
| concrete_pour | lat/lon or town | `check_concrete_pour` | chance the pour goes as planned |
| roofing, painting, general_outdoor_work | lat/lon or town | `workability` | chance conditions stay workable |

Sports decisions: official cancellation → `no_go`; otherwise ≥ 70% → `go`, 40-69% → `wait`, < 40% → `no_go`.

Workability rules (data/construction/thresholds.json → `go_no_go.activities`):

| Rule | roofing | painting (exterior) | general_outdoor_work |
|---|---|---|---|
| Thunderstorms in the window | no-go | no-go | no-go |
| Rain chance wait / no-go | 30% / 60% | 20% / 50% | 40% / 70% |
| Rain after finishing (wait) | 2 h, ≥ 30% | 4 h dry time, ≥ 20% | — |
| Gusts wait / no-go | 25 / 35 mph | 20 mph | 30 / 45 mph |
| Temperature | wait below 40 °F (shingle sealing) | wait below 50 °F or above 90 °F | — |
| Humidity / dew point | — | wait above 85% RH or within 5 °F of dew point (SSPC-PA 1) | — |
| Heat index (NWS) | — | — | wait ≥ 103 °F, no-go ≥ 115 °F (OSHA-NIOSH bands) |

Inspired by Rain Check (Australia, field-service workability go/no-go) and WeatherNews (venue-specific event go/no-go
tied to schedules). Neither is integrated; both are paid/regional services.

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0; free tier is non-commercial, under
10,000 calls a day) and the U.S. National Weather Service.

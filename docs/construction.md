# Construction layer (v2 beta)

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

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0; free tier is non-commercial, under
10,000 calls a day) and the U.S. National Weather Service.

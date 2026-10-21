# Data sources: licenses, commercial use and the paid-answer switch

Short version: **free answers** keep using NWS + Open-Meteo. **Paid answers** (paid API-key
plans and x402 pay-per-call) must not use Open-Meteo's free API, because its terms say the
free API is for non-commercial use only. With the switch on, paid answers use **NWS/NOAA in
the US** and **MET Norway outside the US**. The switch ships **off**.

Pages checked on 2026-10-21 (quotes below come from these pages; nothing here is legal advice):

| Source | Pages checked |
| --- | --- |
| Open-Meteo | https://open-meteo.com/en/terms, https://open-meteo.com/en/licence |
| NWS API | https://www.weather.gov/documentation/services-web-api, https://www.weather.gov/disclaimer |
| MET Norway | https://api.met.no/doc/TermsOfService, https://api.met.no/doc/License, https://api.met.no/weatherapi/locationforecast/2.0/documentation, https://docs.api.met.no/doc/locationforecast/datamodel |

## The sources

### Open-Meteo (forecast API + geocoding) — free answers only

- **What we use it for:** hourly/daily rain chance and amounts worldwide, the NWS fallback,
  motocross prior-rain totals, the 15-minute nowcast, construction site weather, and
  place-name geocoding.
- **License:** data under CC BY 4.0 (licence page). Attribution: "You must include a link next
  to any location Open-Meteo data are displayed", e.g. "Weather data by Open-Meteo.com".
- **Commercial use:** free API is **not** allowed for commercial use. The terms list
  "Websites or apps that have subscriptions or display advertisements" as commercial. The
  comparison table marks commercial use ❌ for Free and ✅ for API Standard and up (paid).
- **Rate limits (free):** under 10,000 calls/day, 5,000/hour, 600/minute, and 300,000/month.
  The terms warn that on hosts like Vercel "you might be rate-limited earlier than expected".
- **User-Agent / caching:** the terms set no User-Agent rule. We send our own User-Agent and
  cache for 10 minutes (geocoding for 7 days).

### National Weather Service API (api.weather.gov) — US, free and paid

- **What we use it for:** primary hourly rain chance and forecast text in the US. We also use
  points lookups (grid + time zone), station observations (nowcast), and alerts/warnings
  (lightning PR).
- **License / commercial use:** "All of the information presented via the API is intended to be
  open data, free to use for any purpose." The disclaimer says NWS pages are "in the public
  domain, unless specifically noted otherwise, and may be used without charge for any lawful
  purpose". Its limits: don't claim it as your own, don't imply NOAA/NWS endorsement, and
  don't modify it and present it as official.
- **Attribution:** none required. We credit "National Weather Service (NOAA)". We may not use
  the NWS name or logo to suggest endorsement. 17 U.S.C. § 403 notice applies only to
  copyrighted works made mostly of NWS material.
- **Rate limits:** "not public information, but allows a generous amount for typical use". If
  it is exceeded, retry after it clears ("typically within 5 seconds"). The disclaimer asks us
  to limit retries to 1-minute intervals when a service is down.
- **User-Agent:** required: "A User Agent is required to identify your application ... If you
  include contact information (website or email), we can contact you". We send
  `RainoutSource/1.0 (JEEZ Labs; +https://rainout-agent-source.vercel.app; github.com/bizwaxx/Rain-Outsource)`.
- **Caching:** responses are cache-friendly. You may cache the points → grid mapping, but grid
  values "may occasionally change", so check back periodically. We cache points for 7 days,
  the hourly forecast for 10 minutes, and serve stale for up to 2 hours.

### NOAA NCEI (GHCN-Daily history), NOAA GOES GLM (lightning PR)

- Used for the NOAA history behind each venue's calibration and, in the lightning PR, for
  satellite flashes. Both are NOAA. We did **not** re-check NCEI's or the GLM data's own terms
  pages today (the NCEI docs page timed out). See "Still unknown".

### MET Norway Locationforecast 2.0 (api.met.no) — non-US paid answers

- **What we use it for:** hourly (then 6-hourly) precipitation amount, symbol (weather text),
  and, in the Nordic area only, rain chance and thunder chance. Endpoint:
  `/weatherapi/locationforecast/2.0/complete`. We use complete, not compact, because only
  complete carries `probability_of_precipitation`.
- **License:** "all data and products are licensed under the Norwegian Licence for Open
  Government Data (NLOD) 2.0 and Creative Commons 4.0 BY International licences". Both allow
  commercial use with attribution. The ToS has no non-commercial limit. Trademark rule: we may
  not make services that appear to be made by Yr, NRK or MET Norway, or use "Yr" in the name.
- **Attribution:** credit "The Norwegian Meteorological Institute, shortened MET Norway". They
  suggest «Data from MET Norway». CC BY 4.0 also asks for a license link and a note if changes
  were made. We send `attribution: "Data from MET Norway (api.met.no), CC BY 4.0"` with every
  MET answer. Our rain chance outside the Nordic area is our own estimate, and it is labelled.
- **Rate limits:** "Anything over 20 requests/second per application (total, not per client)
  requires special agreement." A 429 means we are throttled, so back off at once.
- **User-Agent:** must identify the app plus a company email or website link. Requests with a
  missing or banned UA get 403, and "fake or random strings" can earn a permanent ban. We send
  the same User-Agent as for NWS.
- **Caching / traffic rules (implemented in `weather.fetch_met_norway`):**
  - don't ask again before the `Expires` header (we also keep a 5-minute minimum);
  - revalidate with `If-Modified-Since` set to the previous `Last-Modified` (304 → reuse);
  - lat/lon at most 4 decimals ("requests with 5+ decimals will return a 403");
  - support gzip (`Accept-Encoding: gzip`) and redirects, HTTPS only;
  - HTTP 203 = deprecated product version → we log a warning;
  - spread traffic (no on-the-hour bursts). We fetch only on demand, so this already holds.
  - Privacy: MET logs IPs and coordinates. We call it server-side, so user IPs are never sent.
- **Data limits (datamodel page):**
  - `probability_of_precipitation` and `probability_of_thunder` exist **only in the Nordic
    region**. The global forecast has `precipitation_amount` only.
  - `next_1_hours` exists only in the short range (about 2.5 days). After that the data comes
    in 6-hour blocks.
  - The forecast runs to about 9 to 10 days, not 16 like Open-Meteo.

## The switch (`RAINOUT_COMMERCIAL_SOURCES`)

Code: `rainout_agent/source_policy.py`. No new Vercel function.

| Value | Effect |
| --- | --- |
| `off` (default, also any unknown value) | Same as today for everyone. |
| `paid` | Requests marked paid use only NWS (US) / MET Norway (non-US). Free requests unchanged. |
| `all` | Every request uses the commercial-safe sources. |

- **Which requests are paid:** `/v1/batch` with a key on a paid Stripe plan (`plans.PAID`) and
  `/v1/premium/batch` paid with x402. Anonymous and free-key requests are free. The mark is a
  context variable copied into every batch worker thread.
- **In paid mode:**
  - US points use NWS live or stale cache (up to 2 h). If NWS has nothing, the answer is
    "weather unavailable", not Open-Meteo.
  - Non-US points use MET Norway.
  - Every Open-Meteo URL (forecast, geocoding, archive, …) is refused at the HTTP layer
    (`SourceNotAllowed`), so a missed code path fails loudly instead of quietly using Open-Meteo.
  - Place-name lookups ask for lat,lon (geocoding is Open-Meteo).
- **What responses show:**
  - batch: `data_sources: "free" | "commercial"`;
  - status: `weather_source_policy` and `weather_attribution` when set;
  - rain: `source.policy` and `source.attribution`;
  - each MET hour: `probability_kind: "estimated_from_amount"` when the chance is our estimate.

### Rain chance outside the Nordic area (heuristic, uncalibrated)

MET gives no rain chance there, so we map the forecast amount for the period to a chance:

| Amount | < 0.05 mm | < 0.2 | < 0.5 | < 1 | < 2.5 | < 5 | ≥ 5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Chance | 5% | 25% | 40% | 55% | 70% | 80% | 90% |

This is a plain starting point, not a calibrated model. Before turning on `paid` for non-US
customers, backtest it against past games, the same way we test the US model.

## Switch plan

1. **Now (this PR, flag off):** code, tests with recorded NWS and MET Norway responses, and
   this doc. Nothing changes in production.
2. **Before the first paid customer:** set `RAINOUT_COMMERCIAL_SOURCES=paid` in Vercel (needs
   Jimmy's OK). Every current venue is in the US, so paid answers become NWS-only. Compare paid
   vs free answers on the staging checklist. The expected difference is small, because NWS is
   already primary in the US and Open-Meteo is only the fallback.
3. **Non-US paid answers:** backtest the amount → chance table, then allow them.
4. **Decide on `all`.** Open-Meteo's terms count "Websites or apps that have subscriptions" as
   commercial. Once paid plans exist, even our free answers may count as commercial use of the
   free API. Options:
   - (a) `all`: NWS + MET Norway for everyone, giving up Open-Meteo's 16-day range and amounts
     for the US;
   - (b) an Open-Meteo API Standard subscription (costs money, needs Jimmy's OK);
   - (c) ask Open-Meteo whether a free tier inside a paid product is OK.
   This needs a decision before paid plans go live.
5. **Gaps to close for `all` (the US loses some features without Open-Meteo):**
   - US hourly rain **amounts**: NWS hourly has none. The NWS gridpoint `quantitativePrecipitation`
     (6-hour QPF) could fill this. Not built yet.
   - Days 8–16: NWS covers about 7 days. After that, "no forecast yet".
   - 15-minute nowcast and construction site weather: today these are Open-Meteo (HRRR). A
     commercial path would be NOAA HRRR data directly, or MET Norway Nowcast (Nordic only).
   - Geocoding: needs a commercial-OK geocoder (or our own venue database, plus lat,lon).
   - Time zone for non-US lat/lon without a place: UTC for now. Needs an offline tz lookup.

## What's stubbed or not built

- US amounts in paid mode are reported as "unavailable".
- Paid place-name lookups return a plain error asking for lat,lon.
- Nowcast, construction and site weather have no commercial path. They refuse Open-Meteo in
  paid mode, but those endpoints are not paid today.
- No live MET Norway call happens in tests. The fixtures were recorded once on 2026-10-21
  (`tests/fixtures/sources/`, instant details trimmed).

## Still unknown (not invented here)

- Whether Open-Meteo treats a free tier inside a product with paid subscriptions as commercial.
  Their examples suggest yes. Ask them before launch.
- NCEI and GOES GLM terms pages were not re-checked today. We believe NOAA data is generally
  public domain, but confirm on the NCEI pages before relying on it for paid answers.
- Whether MET Norway's global (ECMWF-based) output carries any terms beyond NLOD 2.0 / CC BY
  4.0. Their licence page says "Unless specified otherwise, all data", and we found no
  exception, but the datamodel page names ECMWF as the global source.
- NWS's real rate limit (not public).
- Whether our amount → chance table is any good outside the US (needs a backtest).

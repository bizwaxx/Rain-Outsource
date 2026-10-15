# Weather caching and the Open-Meteo fallback

Rainout Source reads hourly forecasts from the National Weather Service (NWS).
Milestone 4 adds a cache in front of NWS and a free, no-key fallback source,
so repeated questions are fast and an NWS outage does not turn into an error.

## How a forecast is resolved

For each venue (coordinates rounded to 4 decimals, which is what NWS accepts):

| Step | What happens | `weather_source_id` | `weather_cache` |
|------|--------------|---------------------|-----------------|
| 1 | A fresh cached NWS hourly forecast, younger than `RAINOUT_FORECAST_TTL_S` (default 600 s, i.e. 10 min) | `nws` | `hit` |
| 2 | Live NWS. The `points` → gridpoint lookup is cached for **7 days**, so a cold forecast usually needs one NWS request instead of two | `nws` | `miss` |
| 3 | NWS errors or times out: a cached NWS forecast up to `RAINOUT_FORECAST_STALE_S` old (default 7200 s, i.e. 2 h) | `nws_stale` | `stale` |
| 4 | Still nothing: **Open-Meteo** (`api.open-meteo.com`, free, no key), cached `RAINOUT_FALLBACK_TTL_S` (default 600 s) | `open_meteo` | `miss` / `hit` |
| 5 | Everything failed: `/v1/status` answers `502 weather_source_unavailable`; the schedule check falls back to history-only for that venue with a warning | – | – |

The time budget is split: about two thirds of the request timeout goes to NWS
and the rest (at least 2 s) is kept for Open-Meteo. The NWS `forecastHourly`
URL from the points response is only followed if it is on `https://api.weather.gov/`.

Open-Meteo hourly data (`precipitation_probability`, `weather_code`, UTC unix
times) is converted to NWS-shaped hourly periods. WMO codes 95/96/99 become
"Thunderstorms" / "Thunderstorms With Hail", so thunderstorm detection works
the same way for both sources. Missing probabilities count as 0%.

Only the fields the estimator uses are cached (start/end time, precipitation
probability, short and detailed forecast), about 15 KB per venue.

## Source marking in responses

`/v1/status` adds:

```json
"weather_source": "Open-Meteo (fallback; NWS unavailable)",
"weather_source_id": "open_meteo",
"weather_cache": "miss",
"source": "Rainout phone: 512-978-2680; weather: Open-Meteo (National Weather Service unavailable)"
```

With NWS, `weather_source` stays `National Weather Service API` (as before) and
`weather_source_id` is `nws`. Served stale, it reads
`National Weather Service API (cached; NWS currently unavailable)`, and
`last_checked` is when that forecast was fetched.

`/v1/schedule/check` adds `weather_source` and `weather_source_id` to each
forecast-scored game. `basis` is `open_meteo_forecast[+history]` instead of
`nws_forecast[+history]` for fallback games, and `summary.with_fallback_forecast`
counts them.

## Cache backends

The default is an in-memory cache per serverless instance: thread-safe, LRU,
512 entries. It needs no configuration and no paid service. Vercel reuses warm
instances, so it helps most under steady traffic and is lost on cold starts.

A shared backend can be added through environment variables. It sits *behind*
the memory tier, uses a 1.5 s timeout, and after any error it is skipped for
60 s. A misconfigured or unavailable backend never fails a request.

`RAINOUT_CACHE_BACKEND` = `auto` (default) | `memory` | `upstash` | `supabase` | `none`

- **auto**: Upstash/Vercel KV if its variables are set, else Supabase if its
  variables are set, else memory.
- **upstash** (also `kv`, `vercel-kv`): needs `UPSTASH_REDIS_REST_URL` and
  `UPSTASH_REDIS_REST_TOKEN`, or the Vercel KV names `KV_REST_API_URL` and
  `KV_REST_API_TOKEN`. Uses the REST API (`SET key value EX ttl` / `GET key`).
  The Upstash free tier is enough.
- **supabase**: needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (or
  `SUPABASE_KEY`). The table is `RAINOUT_CACHE_TABLE`, default `rainout_cache`.
  Create it once:

  ```sql
  create table if not exists public.rainout_cache (
    key text primary key,
    value jsonb not null,
    expires_at timestamptz not null
  );
  create index if not exists rainout_cache_expires_at on public.rainout_cache (expires_at);
  alter table public.rainout_cache enable row level security;  -- service role bypasses RLS
  -- optional housekeeping (e.g. pg_cron): delete from public.rainout_cache where expires_at < now();
  ```

  Expired rows are never returned (`expires_at=gt.now`), but Postgres does not
  delete them by itself.
- **memory**: in-memory only. **none**: disables caching entirely.
- If a backend is requested but its variables are missing, or the value is
  unknown, the app logs a warning and uses memory.

Keys are prefixed with `rainout:v1:`. `/health` reports the active backend
(`weather_cache.backend`, e.g. `memory+upstash`), never URLs or tokens.

## HTTP caching (Cache-Control)

| Endpoint | Cache-Control |
|----------|---------------|
| `/v1/fields` | `public, max-age=300, s-maxage=3600, stale-while-revalidate=86400` |
| `/openapi.yaml` | `public, max-age=3600, s-maxage=86400` |
| `/` | `public, max-age=300, s-maxage=3600` |
| `/v1/status` (NWS) | `public, max-age=60, s-maxage=120, stale-while-revalidate=60` |
| `/v1/status` (Open-Meteo or stale NWS) | `public, max-age=30, s-maxage=30` |
| `GET /v1/schedule/check` (200) | `public, max-age=300, s-maxage=600` |
| POST, OPTIONS, any error, `/health`, `/v1/stats` | `no-store` |

`/v1/status` is kept short at the edge because the official status (for example
a cancellation) can change at any time. The Vercel edge cache keys on the full
URL, including `field_id` and `game_time`.

## Tuning

| Variable | Default | Meaning |
|----------|---------|---------|
| `RAINOUT_FORECAST_TTL_S` | 600 | How long a cached NWS forecast counts as fresh |
| `RAINOUT_FORECAST_STALE_S` | 7200 | How long it can be served when NWS is down |
| `RAINOUT_FALLBACK_TTL_S` | 600 | Cache time for Open-Meteo forecasts |

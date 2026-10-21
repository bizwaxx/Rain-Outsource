# Lightning (advisory)

> **Not a safety device. Follow the official call and your league's lightning policy. When thunder roars, go indoors.**

`GET /v1/lightning?field_id=Krieg` (or `?lat=&lon=`, `?track_id=`, optional `radius_mi`, 3–15, default 10). The MCP tool is `get_lightning`.

## Sources (free, public)
- **NOAA GOES-R GLM** (Geostationary Lightning Mapper) Level-2 LCFA flashes on AWS Open Data / NOAA NODD:
  `noaa-goes19` (GOES-East) for venues east of 106° W, and `noaa-goes18` (GOES-West) for the West. They're read anonymously over HTTPS, with no account.
- **NWS active alerts** for the point (`api.weather.gov/alerts/active?point=`): Severe Thunderstorm Warnings, Tornado Warnings, and Special Weather Statements or Significant Weather Advisories that mention lightning.
- Blitzortung and Xweather are not used, including their free tiers.

## Logic (30-minute rule, NWS / NFHS)
Flashes from the last 30 minutes are counted within 8, 10 and 30 mi of the venue. Every flash within `radius_mi` restarts the clock, so `all_clear_at` = the last such flash + 30 minutes.

| lightning_status | Meaning |
|---|---|
| `active` | A flash within `radius_mi` in the last 30 min, **or** an NWS Severe Thunderstorm or Tornado Warning covers the point |
| `nearby` | Flashes within 30 mi but not within the radius, or an NWS statement mentions lightning |
| `clear` | The **whole** 30-minute GLM window was read (no missing files, newest file under 4 min old), with no flashes within 30 mi and no warnings |
| `unknown` | GLM data is missing, late or partial and nothing above was seen. **It is never reported as clear.** |

The response also has `last_flash` and `nearest_flash` (time and distance), `flashes` counts, `glm` coverage (satellite, files read, data age), the `nws` alerts, `sources`, `caveats` and `advisory`.

Flash time is the file's **end** time (files are 20 s), so the all-clear is never early. GLM misses some flashes (it detects roughly 70–90%) and places them within about 5–10 km. Data lands about 20–60 s after the flash.

## Play answers
For an event that starts within 3 h or began in the last 4 h, `/v1/status`, `go_no_go` and `should_we_cancel` include a `lightning` block. While lightning is `active`, the answer changes only if the event is under way or starts before the hold ends:
- `/v1/status`: `recommendation` becomes `"lightning delay: stay off the field"`, plus `lightning_delay`. The reason and spoken answer lead with the delay and the advisory.
- `go_no_go`: `go` becomes `wait`, and `recheck_at` is set to the all-clear.
- `should_we_cancel`: `play` becomes `delay`, with `delay_to` set to the all-clear.

An official cancellation still wins, and the play odds aren't changed. Set `RAINOUT_LIGHTNING=off` to disable the lookups.

## Why this works on Vercel serverless
- GLM files are netCDF-4 (HDF5) at about 180–600 KB each, roughly 90 per satellite per 30 minutes (about 15–25 MB). h5py plus numpy would add about 40 MB to every function, so `rainout_agent/glm.py` is a **pure-Python HDF5 subset reader** (about 300 lines; stdlib `zlib` and `struct`). It reads only `flash_lat` and `flash_lon`, and was checked against h5py on real GOES-16, -18 and -19 files. A file in any other layout raises an error and the answer becomes `unknown`.
- Measured from a non-AWS box: a cold 30-minute window (87 files, 21 MB) took 2.9 s, with 0.1 s of that parsing; a warm request took 0.16 s. Vercel's iad1 region sits next to S3 us-east-1.
- Parsed North American flashes are stored in 5-minute buckets in the shared cache (Upstash), about 7 keys per request and 45-minute TTL. A cold request downloads only the missing files within a 7 s budget. Whatever isn't read counts as missing, so it can't be `clear`.
- `/v1/lightning` is served by the existing `api/rain.py` function. No new function is added, which matters because Vercel Hobby has a function limit.

### Keeping it warm (optional, nothing set up)
`GET|POST /v1/lightning/ingest` pre-reads both satellites into the shared cache, so user requests read the cache only. Auth is any one of:
- `X-Rainout-Internal: $RAINOUT_INTERNAL_TOKEN`
- `Authorization: Bearer $CRON_SECRET`
- `?key=$RAINOUT_DISPATCH_KEY`

It returns 503 until one is set. Call it every 1–5 minutes from a free pinger (cron-job.org or UptimeRobot, which Jimmy must sign up for). Vercel Hobby cron runs only once a day, which is too slow to help. Without a pinger, the first request after a quiet spell pays the cold cost.

## Tests
`tests/test_lightning.py` uses recorded fixtures:
- a real GOES-18 GLM file;
- real GOES-18 flashes around a severe storm near Yavapai County, AZ (2026-10-21, 14:16 UTC), for every file in the window;
- the real NWS alert responses for that point, for Austin, and for current Special Weather Statements;
- netCDF4-written multi-chunk and empty files.

There's no network in tests.

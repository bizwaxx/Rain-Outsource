# Community cancellation reports

People at the field often know before any official page does. `/v1/reports` (and the MCP
tool `report_cancellation`) let a user say what they see, and recent reports nudge the play
odds other people get from `/v1/status`.

## Submit

```bash
curl -X POST https://rainout-agent-source.vercel.app/v1/reports \
  -H 'Content-Type: application/json' \
  -d '{"field_id":"Krieg","status":"cancelled","game_time":"2026-06-05T19:00:00-05:00","note":"Umps called it at 6:40"}'
```

| Field | Required | Rules |
|---|---|---|
| `field_id` | yes | Any field_id or alias `/v1/status` accepts (confirmed or OpenStreetMap venues) |
| `status` | yes | `cancelled`, `delayed`, `field_closed` or `playing` (aliases such as `rained out`, `postponed`, `closed` and `on` are accepted) |
| `game_time` | no | ISO 8601 with UTC offset, from 12 hours ago to 36 hours ahead. Defaults to now |
| `note` | no | Up to 280 characters; control characters are stripped |

JSON or form-encoded bodies (up to 4 KB). Responses: `201` with the stored report and the
current summary; `400` for invalid input (`error` says which); `404` for an unknown venue;
`429` with `Retry-After` when rate limited.

`GET /v1/reports?field_id=Krieg&game_time=...` lists the last 24 hours of reports for that
venue (newest first, up to 20), optionally only for the game's local date.

## How reports change the odds

`/v1/status` (and the MCP `get_venue_status` / `check_game` tools) add a `community_reports`
block with the reports from the **last 12 hours for the same local game date**. While the
official status is `unknown` and the game is between 6 hours ago and 36 hours ahead:

- each `cancelled` or `field_closed` report counts 1 and each `delayed` report 0.5 against
  playing; each `playing` report counts 1 for playing; opposite reports cancel out;
- a net negative score of *n* multiplies the odds by `1 - 0.25 n` (at most an 80% cut);
- a net positive score of *n* closes `15% × n` of the gap to 100% (at most half of it).

So one cancellation report turns 80% into 60%, two turn it into 40%. The block shows
`applied`, `play_probability_before_reports`, `adjustment_percent_points` and a `reason`,
and the spoken answer mentions the reports. **A known official status always wins**:
reports are then shown but don't change the odds. Schedule and team game lists aren't
adjusted.

## Storage, privacy and abuse limits

- Reports live in the existing cache layer (`rainout_agent/cache.py`) for 24 hours, at most
  50 per venue. Nothing is paid or added: by default that's memory inside each serverless
  instance (a dedicated memory tier, so weather lookups don't evict reports). With the
  default memory backend, a report is only seen by the Vercel instance that received it,
  and is lost when the instance is recycled. `/v1/reports` is served by the same function
  as `/v1/status` to make that more likely; MCP reports live in the MCP function. For
  reports shared across instances, configure an optional shared backend
  (`UPSTASH_REDIS_REST_URL`/`KV_REST_API_URL` and so on, see `docs/weather-cache.md`).
- Reports are anonymous and unverified. No IP address is stored: the client address
  (`X-Forwarded-For`) is only used as a rate-limit key, after a keyed hash: HMAC-SHA256 under the
  daily salt from `RAINOUT_METRICS_SALT` (the same daily rotation as the usage counters). The key expires after 1 hour.
  If the salt is unset, each instance uses a random salt, so limits apply per instance.
- Rate limits per client: 3 reports per venue and 10 in total per clock hour. With the
  memory backend these are per instance, so they're a speed bump, not a guarantee.

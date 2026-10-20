# Usage counters

Rainout Source counts its own usage as **aggregate daily counters** in the shared Upstash
Redis (the same store as the weather cache). It does not store any request logs.

## What is counted (per America/Chicago day)

| Counter | Redis | Notes |
|---|---|---|
| Requests per endpoint, per caller class | `HINCRBY rainout:v1:metrics:<day> req\|<endpoint>\|<class>` | Endpoint labels are fixed names (`status`, `teams.games`, `mcp`, ...); ids never appear |
| MCP tool calls per tool, per class | `HINCRBY … tool\|<tool>\|<class>` | Counted inside the `/mcp` request's pipeline |
| Free keys signed up | `HINCRBY … event\|free_key_signup\|<class>` | Hooked in when the free-key PR lands |
| Unique clients per class | `PFADD rainout:v1:metrics:<day>:clients:<class> <hash>` | HyperLogLog, about 0.8% error; not tracked for internal traffic |

Every key has a TTL of **400 days**.

## Caller classes

- **internal**: our own tests and tools. They send `X-Rainout-Internal: <RAINOUT_INTERNAL_TOKEN>`,
  which is compared in constant time, or a User-Agent that starts with `rainout-smoke`,
  `rainout-cli`, `rainout-monitor`, `rainout-test` or `rainout-health`.
  `scripts/smoke_prod.py` sends both.
- **monitor**: known uptime and synthetic monitors, matched by User-Agent (UptimeRobot,
  Pingdom, Better Stack, StatusCake, Site24x7, Checkly, Datadog, Uptime Kuma, Vercel screenshot
  bots, …).
- **bot**: crawlers (User-Agent contains bot, crawler or spider).
- **external**: everyone else. These are outside users and agents.

Anyone can send a `rainout-smoke` User-Agent. That only moves their requests out of the
"outside" numbers, so it cannot inflate them. The header secret is what we rely on for
our own traffic.

## Privacy

- No raw IP address or User-Agent is ever stored.
- For unique counts, the client IP (the first `X-Forwarded-For` hop) is hashed as
  `HMAC-SHA256(HMAC-SHA256(RAINOUT_METRICS_SALT, date), ip)` and truncated to 96 bits. The salt
  rotates every day, so the same client cannot be linked across days. Even inside a day, the
  hash is only added to a HyperLogLog, which keeps register maxima and not the hashes.
- Without `RAINOUT_METRICS_SALT`, each instance uses a random salt. Counting still works,
  but unique counts will overcount clients that hit several instances.
- Each instance keeps a bounded in-memory set (20k entries) of the hashes it has already
  added today, so that it can skip duplicate PFADDs. That set is never persisted.

## Cost: Upstash commands

Each request makes one HTTP call to Upstash `/pipeline`. It runs in a background thread
inside `try/except`, so a slow or down Redis never delays or breaks a response. Upstash
bills every command inside a pipeline, so the counts per request are:

| Request | Commands |
|---|---|
| Typical request from a client this instance already saw today | 1 (`HINCRBY`) |
| First request from a client on this instance today | 2 (`+ PFADD`) |
| MCP `tools/call` | +1 per tool called |
| First write to a day key on each instance | +1 `EXPIRE` (a few per day in total) |
| Internal (our smoke tests) | 1 |

**Estimate: about 1,100–1,500 commands per 1,000 requests.** The low end is mostly repeat
callers; the high end assumes many one-off clients or a large share of MCP tool calls.
With the free plan's 500k commands per month, counting alone would allow roughly
330k–450k requests per month. The weather cache uses the same quota, so watch the Upstash
dashboard. Set `RAINOUT_METRICS=off` to stop counting at once without a deploy rollback.

## Reading the counters

```
GET /v1/admin/usage?days=7            # JSON (days 1-90, default 7)
GET /v1/admin/usage?days=1&format=text  # short plain-text daily summary
Authorization: Bearer <RAINOUT_ADMIN_TOKEN>      (or X-Admin-Token)
```

Responses: 503 if `RAINOUT_ADMIN_TOKEN` is not set (it must be 16+ characters), 401 for a
wrong token, and 502 if the store cannot be read. Each read costs `days × 4` commands
(one HGETALL and three PFCOUNTs per day).

Sample text output:

```
Rainout Source usage 2026-10-19 (America/Chicago)
Requests: 1,284 outside | 96 ours | 288 monitors | 41 bots
Unique outside clients: 57 (monitors 3, bots 9)
Top endpoints (outside): status 611, mcp 302, teams.games 190, rain 88, llms 51, openapi 42
MCP tools (outside): get_field_status 140, check_game 97, search_teams 61
Free keys signed up: 4
```

## Environment variables

| Name | Purpose |
|---|---|
| `RAINOUT_INTERNAL_TOKEN` | Secret that marks our own traffic (`X-Rainout-Internal`) |
| `RAINOUT_ADMIN_TOKEN` | Protects `/v1/admin/usage` (16+ characters) |
| `RAINOUT_METRICS_SALT` | Base salt for the daily-rotating client hash |
| `RAINOUT_METRICS` | Optional; `off` disables counting |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` (or `KV_REST_API_*`) | Already set by the Vercel Marketplace integration |

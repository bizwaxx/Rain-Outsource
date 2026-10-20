# Plans and pricing (proposal)

The free public API does not change: every existing endpoint (status, rain, fields,
teams, tracks, reports, MCP, OpenAPI, llms.txt) stays open with **no key and no new
limits**. Paid plans add capability on new endpoints and come with an API key.

| | Free | Coach $9/mo | League $29/mo | Organization $99/mo |
|---|---|---|---|---|
| Existing endpoints | Unlimited, no key (as today) | Same | Same | Same |
| `POST /v1/batch` per call | 3 games / 1 team | 25 games / 10 teams | 100 games / 60 teams (a whole league) | 250 / 250 |
| Rate limit (paid endpoints) | — | 60 req/min | 300 req/min | 1,200 req/min |
| Monthly units (1 game or team = 1 unit) | — | 20,000 | 150,000 | 1,000,000 |
| `should_we_cancel` | Basic recommendation | Hour-by-hour window, decision deadline, next check | Same | Same |
| Usage dashboard `GET /v1/account` | — | Yes | Yes | Yes |
| Odds-change webhook alerts (planned) | — | 10 watches | 100 watches | 1,000 watches |
| SLA | — | — | — | 99.5% monthly uptime target, service credits |
| Support | GitHub issues | Email, best effort | Next business day | Priority |

Pay per call instead of subscribing: the x402 lane (USDC on Base; testnet only for now)
sells single premium calls with no account. See docs/x402.md.

## Using a key

```
curl -X POST https://rainout-agent-source.vercel.app/v1/batch \
  -H 'X-API-Key: rk_test_...' -H 'Content-Type: application/json' \
  -d '{"items":[{"field_id":"Krieg","game_time":"2026-10-24T18:30:00-05:00"},{"team_id":"tsl-340-744621-4306365","date":"this week"}]}'
curl https://rainout-agent-source.vercel.app/v1/account -H 'X-API-Key: rk_test_...'
```

No key = free tier. A key sent to an existing endpoint is ignored. A bad key sent to a
paid endpoint gets a clear 401/403 telling you to drop the header or fix the key.

## How keys work

* Signed, self-contained keys (`rk_<test|live>_<payload>.<hmac>`) using
  `RAINOUT_API_KEY_SECRET` (32+ chars). No database is needed to check a key.
* Revocations, subscription standing and usage counters go through the existing cache
  layer (memory by default). With memory only, counts are per instance and approximate
  (generous to customers); revocations reach every instance within 10 minutes via the
  billing lookup. Setting the optional shared cache (Upstash/KV) makes them exact.
* Subscription keys follow the Stripe subscription: active/trialing work, past_due
  keeps working during Stripe's card retries (with a note), unpaid/canceled stop.

## Before charging money

* Vercel's Hobby plan is non-commercial only
  (https://vercel.com/docs/limits/fair-use-guidelines#commercial-usage); taking payments
  needs a Pro (or Enterprise) plan.
* The SLA should only be offered once hosting and a shared cache are on paid tiers.

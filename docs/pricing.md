# Plans and pricing (proposal)

The public API does not change: every existing endpoint (status, rain, fields,
teams, tracks, reports, MCP, OpenAPI, llms.txt) stays open with **no key and no new
limits**. Onboarding is **free signup first**: anyone can create a free key instantly
(`POST /v1/keys` or the `/v1/keys` page, no card, email optional), which adds a usage
dashboard and bigger batches. Paid plans are offered from the dashboard after that.

| | Anonymous (no key) | Free key | Coach $9/mo | League $29/mo | Organization $99/mo |
|---|---|---|---|---|---|
| Existing endpoints | Unlimited, as today | Same | Same | Same | Same |
| `POST /v1/batch` per call | 3 games / 1 team | 10 games / 3 teams | 25 / 10 | 100 / 60 (a whole league) | 250 / 250 |
| Rate limit (batch and other key endpoints) | — | 30 req/min | 60 req/min | 300 req/min | 1,200 req/min |
| Monthly units (1 game or team = 1 unit) | — | 3,000 | 20,000 | 150,000 | 1,000,000 |
| `should_we_cancel` | Basic recommendation | Basic | Hour-by-hour window, decision deadline, next check | Same | Same |
| Usage dashboard `GET /v1/account` | — | Yes | Yes | Yes | Yes |
| Odds-change webhook alerts (planned) | — | — | 10 watches | 100 watches | 1,000 watches |
| SLA | — | — | — | — | 99.5% monthly uptime target, service credits |
| Support | GitHub issues | GitHub issues | Email, best effort | Next business day | Priority |

A key that is over its rate limit or quota is served at the anonymous allowance (batches
of 3) instead of being refused, so holding a key is never worse than not having one.

## Free key signup and abuse limits

* `POST /v1/keys` with `{}` (or `{"email": "...", "label": "..."}`) returns the key once (201).
  `GET /v1/keys` is a small HTML form for people.
* Limits: 3 keys per client IP per day (IP hashed, never stored), 2 per email per day,
  500 new free keys per day service-wide, plus a hidden honeypot field. Exceeding one gives
  429 with `Retry-After`; the API still works without a key.

Pay per call instead of subscribing: the x402 lane (USDC on Base; testnet only for now)
sells single premium calls with no account. See docs/x402.md.

## Using a key

```
curl -X POST https://rainout-agent-source.vercel.app/v1/batch \
  -H 'X-API-Key: rk_test_...' -H 'Content-Type: application/json' \
  -d '{"items":[{"field_id":"Krieg","game_time":"2026-10-24T18:30:00-05:00"},{"team_id":"tsl-340-744621-4306365","from":"2026-10-23","to":"2026-10-25"}]}'
curl https://rainout-agent-source.vercel.app/v1/account -H 'X-API-Key: rk_test_...'
```

No key = free tier. A key sent to an existing endpoint is ignored. A bad key sent to a
paid endpoint gets a clear 401/403 telling you to drop the header or fix the key.

## How keys work

* Signed, self-contained keys (`rk_<test|live>_<payload>.<hmac>`) using
  `RAINOUT_API_KEY_SECRET` (32+ chars). No database is needed to check a key.
* Revocations, subscription standing, signup limits and usage counters go through the
  existing cache layer. Production and preview use the shared Upstash Redis (free tier),
  so counters use atomic `INCRBY` and are exact across instances; reads skip the
  per-instance memory tier. With memory only (local dev) they are per instance and approximate.
* Subscription keys follow the Stripe subscription: active/trialing work, past_due
  keeps working during Stripe's card retries (with a note), unpaid/canceled stop.

## Before charging money

* Vercel's Hobby plan is non-commercial only
  (https://vercel.com/docs/limits/fair-use-guidelines#commercial-usage); taking payments
  needs a Pro (or Enterprise) plan.
* The SLA should only be offered once hosting and the shared cache are on paid tiers.
* Upstash's free tier caps daily commands; each key-holder batch call uses about 6 commands.

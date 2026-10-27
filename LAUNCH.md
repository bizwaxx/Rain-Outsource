# LAUNCH.md: Rain-Outsource go-live, in order

Every merge and every step that only Jimmy can do, in the order to do them. Dev does everything not marked **Jimmy**.
Rule: nothing is posted, submitted or paid for until the step that says so, and each one needs Jimmy's OK.

Short version: 1) Jimmy logs into Vercel and checks the secrets. 2) Jimmy OKs the merges, group by group. 3) The staging checklist says GREEN LIGHT. 4) Jimmy does the listing logins. 5) The licence and paid-plan items are done before we charge anyone.

Each PR is green on its own branch. The launch group (steps 1–13 of the order below, with #47, #51 and #52) merges into main with one conflict: #51 vs #33, where #33's pages win. 577 tests pass, with 12 Vercel functions (the Hobby limit). With every PR merged, including #49 and #50 (the reference branch `integration/launch-stack-v2`, Oct 21), 726 tests pass, with 2 skipped and 11 functions. Without #49 the full stack is 13 functions, so #49 has to land with the construction group. `integration/launch-stack-v2` isn't a PR and won't be merged. The old `integration/launch-stack` is kept as is and is no longer the reference.

---

## Step 1. Vercel secrets (Jimmy: Vercel login, about 10 minutes)
Vercel → project `rainout-agent-source` → Settings → Environment Variables. Set each one for Production and Preview. Dev can give the exact values to paste, except where it says Jimmy.

| Secret | Needed for | Who |
|---|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL` / `_TOKEN`) | Shared rainout reports, weather cache, usage counter | Already added through the Upstash integration. **Jimmy:** confirm they're there. Upstash's usage page also asks for a 6-digit code, which only he can enter. |
| `RAINOUT_METRICS_SALT` | Usage counter (no raw IPs) | Random text, from Dev |
| `RAINOUT_ADMIN_TOKEN` | Reading the usage numbers at `/v1/admin/usage` | Random text, from Dev. Jimmy keeps it private. |
| `RAINOUT_API_KEY_SECRET` | Free API keys and the paid tier (#25) | Random text, 32+ characters, from Dev. Changing it later breaks every key. |
| Stripe test keys (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*`) | Paid tier test mode (#27) | **Jimmy:** needs a Stripe account in his name. Skip this until Step 5. |
| `X402_PAY_TO` | Crypto pay-per-call on testnet (#29) | **Jimmy:** his wallet address. Skip this until Step 5. |
| `ALLOW_LIVE` | Real charges | **Leave unset** until Step 5 is done |

## Step 2. Staging checklist on a preview first (Dev)
`python scripts/staging_checklist.py --base <preview URL>`. It prints GREEN LIGHT, or a punch list where each item has an owner. This tool is in PR #38. Fix the punch list before any merge to production that changes answers.

## Step 3. Merges, in this order (Jimmy: one OK per group)
Each PR is a draft. When a group gets his OK, Dev marks it ready, merges it, and checks the live site before starting the next group. Stacked PRs get pointed at `main` once the PR under them is merged.

| Group | PRs, in order | What it does | Notes |
|---|---|---|---|
| A. Checklist + small fixes | #38, #39, #40, #52 | Checklist tool; Krieg/Cedar Park link-only; motocross date reading; keyed daily HMAC for the report rate-limit hash (#51 and #33 are stacked on #52) | Safe first. Live answers only change for unreadable pages and motocross notices. |
| B. Front door | #51 or #33 → #37 → #34 | Landing page, icon, SEO, MCP server card, `server.json`, draft privacy and terms, the "switch free" page, README and llms.txt. #51 serves the privacy/terms/icon/server-card pages from the existing index function; #33 serves them from a new `api/site.py` | **Merge only one copy of the pages, #51's or #33's.** **Jimmy:** read the draft privacy and terms pages and the "free promise" wording first. After this group main has 12 functions, the Hobby limit. |
| C. Model v2 + "Was it on?" + alerts | #42 → #43 → #44 → #46 → #47 → #45 → #48 | Better odds (soak, storms, venue profile), outcome reports and scoreboard, 68 more past events with honest odds caps, the buttons, free alerts in test mode | #43 and #47 change live odds. Held out, #47 scores 84% with 2 confident-wrong calls (gate: 0), so they need Jimmy's launch decision. No new Vercel functions. |
| D. This doc | #41 | LAUNCH.md | Last, so it matches what merged. |
| E. Construction v2 + lightning (parked) | #26 → #28 → #30 → #31 → #35 → #36, plus #49 | Nowcast, concrete, materials, go/no-go, should-we-cancel, agent docs, advisory lightning | #26 adds a 13th function. #49 moves llms.txt and openapi.yaml into the site function, so it must land in the same OK, before this group deploys. |
| F. Paid tier (parked, test mode only) | #25 → #27 → #29, plus #50 | API keys, usage dashboard, Stripe test mode, crypto testnet, NWS/MET Norway-only data for paid answers | With no Stripe keys, billing answers "not set up". #50 ships off; turn it on before the first paying customer. |

The full list with one line per PR is in the comment on #41.

After each group: run `python scripts/staging_checklist.py` (production by default). It has to say **GREEN LIGHT** before the next group, and before Step 4.

## Step 4. Directory listings (Jimmy: his logins; only after GREEN LIGHT on production and his OK on the list)
Packages are in `/workspace/Rain-Outsource-plan/listings/`. Do them in this order:
1. **Official MCP Registry.** Jimmy runs `mcp-publisher login github` (as `bizwaxx`), then `mcp-publisher publish`, from the repo root. `server.json` has to match the live version.
2. **Glama.** Jimmy signs in and clicks "Claim ownership" (`glama.json` is in the repo after Group B).
3. **mcp.so** (free GitHub issue; the $39 fast review needs his OK) and **mcpservers.org** (free form).
4. **awesome-remote-mcp-servers**: a pull request from his GitHub. The punkpeye list comes after the Glama badge.
5. **Cline Marketplace**: after a test install in Cline and some GitHub stars.
6. **Smithery**: it has announced it will shut down accounts on Oct 31, 2026. Only if it still accepts servers. PulseMCP is paused and imports from step 1.
7. Later: the **Claude Connectors Directory** (his paid Claude plan) and the **ChatGPT app directory** (OpenAI identity verification). Both want a custom domain first (costs money, see Step 5) and the final privacy and terms pages.

Also on GitHub (Jimmy): add the repo topics `mcp`, `mcp-server`, `model-context-protocol`, `weather`, `sports`, `rainout`, `ai-agents`, `open-data`.
Posts and outreach (Show HN, Reddit, X, league emails) come after the listings. Each one goes out in his name, with his OK on the exact wording.

## Step 5. Before we charge anyone (Jimmy: these cost money or need his name)
| Item | Why | Cost |
|---|---|---|
| **Vercel Pro** | The Hobby plan is for non-commercial use only | $20/month |
| **Commercial weather data** | Open-Meteo's free API is non-commercial. #50 switches paid answers to NWS (US) and MET Norway (elsewhere), free with credit. Open question: Open-Meteo counts any site with subscriptions as commercial, so free answers may need the switch too (see `docs/data-sources.md` in #50). | $0 with the switch, or Open-Meteo's paid plan |
| **Final terms and privacy** | Replace the DRAFT pages. They're required by Stripe and the Claude and OpenAI directories. | $0 to write; a legal review is optional |
| **Business entity** (for example a Texas LLC) | Contracts, Stripe payouts, liability | $300 filing |
| **Stripe account** (test mode first, then live) | Monthly plans | Stripe fees per charge |
| **Custom domain** (optional but advised before the Claude and ChatGPT directories) | Listings outlive `*.vercel.app` | About $10–20/year |
| **Turn on live billing** | Set live Stripe keys and `ALLOW_LIVE`. x402 mainnet needs a production facilitator first. | His explicit OK |

## Done means
- The staging checklist says GREEN LIGHT on production.
- The Official Registry lists `io.github.bizwaxx/rain-outsource`.
- The usage counter shows the first outside requests (`/v1/admin/usage`).
- The free tier is unchanged: no key, no signup.

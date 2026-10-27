# Free alerts (test mode)

A league, coach or player subscribes to **one venue and game time**, or to a **team calendar (ICS link)**. They get:

| Trigger | When |
|---|---|
| `day_before` | About 6 PM local time the day before the start |
| `three_hours` | 3 hours before the start. If the day-before alert was missed (a late cron), this one replaces it. |
| `change` | The official status flips, the play odds cross 50%, or a new player report or "was it on?" outcome arrives |

Each (trigger, game) pair goes out once. Changes are compared with the last stored values. The odds come from the same schedule engine as `/v1/schedule/check`. The official league or venue call always wins.

## Channels

| Channel | What happens today |
|---|---|
| `email` | **Stub.** Double opt-in: the confirmation email and every alert are rendered, logged and stored in the outbox. **Nothing is sent.** The sender is pluggable (`alerts.set_email_sender(obj)` with a `send(message) -> dict` method). |
| `webhook` | HMAC-signed JSON POST. **Test mode by default:** the signed payload is stored in the outbox, not POSTed. With `RAINOUT_ALERTS_LIVE=1` it's really POSTed, with retries (3 tries; 5xx, 408, 429 and network errors are retried) and URL safety: https only, standard ports only, no credentials, no private or reserved IPs, a pinned IP (no DNS rebinding) and no redirects. |
| `calendar` | A per-subscription ICS feed at `/v1/alerts/{id}/calendar.ics?token=…`, recomputed on every fetch, with the odds in titles and descriptions (reuses `schedule.build_risk_ics`). It works fully today and needs nothing extra. |

### Webhook signature
Header `X-Rainout-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>`. The secret (`whsec_…`) is returned once at subscribe time. Reject timestamps older than 5 minutes. `rainout_agent.alerts.verify()` is a reference verifier.

## API

```text
POST   /v1/alerts/subscribe   {"channel":"webhook","webhook_url":"https://…","field_id":"Krieg","game_time":"2026-10-23T19:00:00-05:00"}
                              {"channel":"email","email":"coach@example.org","field_id":"…","game_time":"…"}
                              {"channel":"calendar","ics_url":"https://league.example.org/team.ics"}
       → id, token (shown once), unsubscribe_url, calendar_url, webhook_secret (webhook only), status, mode
GET    /v1/alerts/confirm?id=&code=          (link in the confirmation email)
GET    /v1/alerts/unsubscribe?id=&token=     shows a button; POST does it (link previewers can't unsubscribe anyone)
DELETE /v1/alerts/{id}?token=                unsubscribe from code
GET    /v1/alerts/{id}?token=                what we store about you
GET    /v1/alerts/{id}/calendar.ics?token=   live calendar feed
GET|POST /v1/alerts/dispatch                 run the dispatcher (protected, see below)
GET    /v1/alerts/outbox                     rendered messages in test mode (X-Rainout-Internal only)
```

Limits: 10 new subscriptions per hour per client. A venue game must be within 31 days. Unconfirmed email subscriptions expire after 2 days.

## Privacy
We store only: the email address **or** the webhook URL, the venue and game time **or** the ICS link, a hash of the manage token, an unsubscribe-only token, the webhook secret, and which alerts went out. There are no names and no IP addresses. The rate limit keeps a hashed address for 1 hour.
- Records expire one day after the game (venue subscriptions) or after 120 days (calendar links).
- Unsubscribing deletes the record.
- The test-mode outbox (internal only) keeps the last 200 rendered messages for 7 days.
- Every email and webhook carries an unsubscribe link.

## Scheduling the dispatcher (free options; none are set up)
The dispatcher is safe to call as often as every minute. Vercel Hobby crons run **once a day**, which only covers the day-before alert. For 3-hour and change alerts, an outside pinger calls `/v1/alerts/dispatch` every 5–15 minutes.

Auth, any one of:
- `X-Rainout-Internal: $RAINOUT_INTERNAL_TOKEN`
- `Authorization: Bearer $CRON_SECRET` (what Vercel Cron sends)
- `?key=$RAINOUT_DISPATCH_KEY`, a dispatch-only key for pingers that can't set headers. It can't read anything.

| Option | Cost | Frequency | Needs |
|---|---|---|---|
| Vercel Cron (Hobby) | Free | Once a day | `crons` entry in `vercel.json` (for example `{"path":"/v1/alerts/dispatch","schedule":"0 23 * * *"}`, which is 6 PM CDT and 5 PM CST) and `CRON_SECRET` set in Vercel. **Not added in this PR.** |
| cron-job.org | Free | Every minute or more | Jimmy signs up; supports custom headers |
| GitHub Actions `schedule` | Free for public repos | Every 5 min or more (best effort) | A workflow file. Our token lacks the `workflow` scope, so Jimmy adds it. |
| UptimeRobot (free monitors) | Free | Every 5 min | Jimmy signs up; GET with `?key=` |
| Cloudflare Workers cron triggers / Upstash QStash schedules | Free tiers | Minutes | Jimmy signs up |

Limits change, so check each provider's current free tier before relying on it.

## Email providers that would work (Jimmy must sign up; nothing is integrated)
Free tiers seen on their pricing pages (check before relying on them): **Resend** (about 3,000 a month, 100 a day), **Brevo** (about 300 a day), **Mailjet** (about 6,000 a month, 200 a day), **Postmark** (about 100 a month for developers). **Amazon SES** is cheap but not free after the trial. Any of them needs a sending domain set up with SPF/DKIM, so a custom domain helps. Adding one is a small `send()` class plus an API key in Vercel. Until then the stub stays the default.

## Env vars
`RAINOUT_ALERTS_LIVE=1` (real webhook POSTs; leave unset), `RAINOUT_INTERNAL_TOKEN`, `CRON_SECRET`, `RAINOUT_DISPATCH_KEY`, `RAINOUT_PUBLIC_URL` (links in messages). Subscriptions are stored in the shared cache (Upstash). Without it they live in one instance's memory and the dispatcher may not see them.

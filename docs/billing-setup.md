# Billing setup (Stripe, test mode)

Nothing here is live. The code refuses to run billing with a live key
(`sk_live_`/`rk_live_`) unless `ALLOW_LIVE=true` is also set. Until
`STRIPE_SECRET_KEY` is set, every `/v1/billing/*` path answers 503
`billing_not_configured` and the free API is unaffected.

## 1. What Jimmy creates (one time, in Stripe **test mode**)

1. A Stripe account (only Jimmy can do this), then switch the Dashboard to **Test mode**.
2. Products and prices (Product catalog → Add product). All recurring, monthly, USD:

| Product name | Price | Lookup key | Env var for the price id |
|---|---|---|---|
| Rainout Source Coach | $9.00 / month | `rainout_coach_monthly` | `STRIPE_PRICE_COACH` |
| Rainout Source League | $29.00 / month | `rainout_league_monthly` | `STRIPE_PRICE_LEAGUE` |
| Rainout Source Organization | $99.00 / month | `rainout_org_monthly` | `STRIPE_PRICE_ORG` |

   Optional later: yearly prices (e.g. $90 / $290 / $990) as extra prices on the same products.
3. Webhook endpoint (Developers → Webhooks → Add endpoint):
   `https://<deployment>/v1/billing/webhook`, events:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `invoice.paid`, `invoice.payment_failed`. Copy its signing secret (`whsec_...`).
4. Customer portal (Settings → Billing → Customer portal): allow updating payment
   methods, viewing invoices, canceling at period end, and switching between the three
   prices above. Save to activate the default configuration.
5. Failed payments (Settings → Billing → Revenue recovery):
   * **Smart Retries** on (Stripe's recommended 8 tries within 2 weeks).
   * After all retries fail: **mark the subscription as unpaid** (the API key then stops)
     or cancel it; both revoke the key.
   * Turn on the failed-payment and expiring-card customer emails.
   While Stripe retries, the subscription is `past_due` and the key keeps working with a
   note telling the customer to update their card.

## 2. Env vars (Vercel → Project → Settings → Environment Variables, Preview first)

| Var | Value |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_...` (or a restricted `rk_test_...` with Checkout Sessions, Customers, Subscriptions, Customer portal write) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` from step 3 |
| `STRIPE_PRICE_COACH` / `STRIPE_PRICE_LEAGUE` / `STRIPE_PRICE_ORG` | `price_...` ids from step 2 |
| `RAINOUT_API_KEY_SECRET` | 32+ random characters, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Rotating it invalidates every key. |
| `RAINOUT_PUBLIC_URL` | optional; the deployment URL used in success/return links |
| `ALLOW_LIVE` | leave unset until going live on purpose |

## 3. Flow

1. `GET /v1/billing/checkout`: plan picker page; each button POSTs `plan=` and is redirected
   (303) to Stripe Checkout (`mode=subscription`). JSON clients can
   `POST /v1/billing/checkout {"plan":"league"}` and get `checkout_url`.
2. Stripe sends the customer to `/v1/billing/success?session_id=cs_...`, which checks the
   session is complete and shows the API key (same key on reload).
3. The webhook keeps the key in step with the subscription:
   created/active/trialing → works; `invoice.payment_failed` → past_due grace;
   `invoice.paid` → active; unpaid/canceled/deleted → revoked. Upgrades and downgrades in
   the portal change the key's plan with no new key.
4. `POST /v1/billing/portal` with `X-API-Key` returns a Stripe customer-portal link.
5. Even with the in-memory cache, every instance re-checks a subscription with Stripe at
   least every 10 minutes, so a cancellation reaches all instances.

## 4. Testing without a Stripe account

* Unit tests use an in-process fake (`tests/test_billing.py`).
* `tests/test_billing_stripe_mock.py` runs against
  [stripe-mock](https://github.com/stripe/stripe-mock), which validates every request
  against Stripe's API spec:
  `./stripe-mock -http-port 12111 & STRIPE_MOCK_URL=http://localhost:12111 pytest tests/test_billing_stripe_mock.py`.
* With a test account: `stripe listen --forward-to localhost:8000/v1/billing/webhook`,
  pay with card `4242 4242 4242 4242`, and use `4000 0000 0000 0341` (attaches, then
  fails to charge) to exercise Smart Retries and the past_due grace.

## 5. Before going live

* Move the Vercel project to Pro (Hobby is non-commercial only).
* Configure the optional shared cache (Upstash/KV) so usage counts and revocations are
  global, not per instance.
* Add live products/prices, a live webhook and live keys, then set `ALLOW_LIVE=true`.

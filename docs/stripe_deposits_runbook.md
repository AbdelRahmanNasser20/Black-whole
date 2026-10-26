# Stripe Deposits — setup & go-live runbook

The feature ships **dark**: with `STRIPE_SECRET_KEY` unset there is no Reserve button and
`/reserve/*` + `/stripe/webhook` return 404. This runbook takes you from zero to live money.

## 0. One-time business setup (~40 min total)

1. **Stripe account** — stripe.com → sign up as **Individual / sole proprietor** (SSN +
   personal bank; no LLC needed). Business category: Retail — used/secondhand goods.
   Test mode works immediately while activation is pending.
2. In Stripe (test mode first): **Settings → Payment methods → enable ACH Direct Debit**
   (`us_bank_account`) alongside cards. ACH is the cheap rail: 0.8% capped at $5.
3. Recommended, not required: free **EIN** (irs.gov, ~10 min) + a separate checking
   account. Keeps your SSN off forms, makes bookkeeping clean, and rebuilds the business
   footprint (the Estes carrier account was rejected for "residential address +
   unverifiable company" — EIN + business bank + this live site is the fix).
4. Tax facts (verified Jul 2026): opening Stripe does **not** notify the IRS. A 1099-K is
   only filed above **$20,000 AND 200 transactions/yr** through one processor (post-OBBBA).
   Profit is Schedule-C taxable regardless of rail — get one CPA hour.

## 1. Apply the migrations (once, before or after deploy — order doesn't matter)

Supabase project `nihgzltpjriekyqqucbd` → SQL Editor, run each, then stamp the
"Applied … on:" header line in the file and commit:

- `scripts/sql/005_deposits.sql` — `deposits` ledger, `site_settings` (seeded 15% / $200),
  `inventory.deposit_pct_override`.
- `scripts/sql/006_freight_quotes_storefront.sql` — widens the existing `freight_quotes`
  table for storefront logging (`source`, `buyer_email`, nullable `thread_url`).

Until 005 runs, the reserve flow 500s on quote computation — keep Stripe keys unset until
it's applied. (Freight estimates work without 006; they just skip logging.)

## 2. Local test-mode loop

```bash
# .env (repo root):
STRIPE_SECRET_KEY=sk_test_...          # Developers → API keys (test mode)
STRIPE_WEBHOOK_SECRET=whsec_...        # printed by `stripe listen` below

brew install stripe/stripe-cli/stripe
stripe login
stripe listen --forward-to localhost:8765/stripe/webhook   # leave running
python -m automation.web                                    # http://127.0.0.1:8765
```

Test matrix (all in test mode):

| # | Do | Expect |
|---|---|---|
| 1 | Unset key, restart | No Reserve button; `/reserve/<lot>` 404; admin Deposits tab still loads |
| 2 | Card `4242 4242 4242 4242` | Success page "RESERVED"; row `paid`; ONE Telegram ping |
| 3 | ACH: test bank, acct `000123456789` / routing `110000000` | Success page "BANK TRANSFER INITIATED"; row `processing` → minutes later `paid` + second ping |
| 4 | ACH fail acct `000111111113` | `failed` + reason + ping |
| 5 | Refund the payment in the Stripe test dashboard | Row `refunded`, `refunded_at` stamped |
| 6 | `stripe checkout sessions expire cs_...` | Row `canceled` |
| 7 | `curl -X POST localhost:8765/stripe/webhook -d '{}'` | 400, no row change |
| 8 | `stripe events resend evt_...` | Row unchanged, NO duplicate Telegram |
| 9 | Admin tab: set 20% / $300, save | Reserve page + Checkout amount update; per-lot override wins |
| 10 | Qty > remaining; tiny lot below the floor | 400; deposit capped at subtotal |

## 3. Go-live checklist

- [ ] Stripe account fully **activated**; ACH enabled in **live** payment-method settings;
      statement descriptor set (shows on buyer bank statements — use BLACKWHOLE).
- [ ] Stripe dashboard (live mode) → Developers → Webhooks → add endpoint
      `https://black-whole.com/stripe/webhook` with exactly these events:
      `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
      `checkout.session.async_payment_failed`, `checkout.session.expired`,
      `charge.refunded`. Copy its `whsec_`.
- [ ] Render dashboard → service `black-whole-web` → **Environment → Environment Variables → Edit** (vars live on the service; no env group is linked — `blackwhole-secrets` from render.yaml is not in use) → set live `STRIPE_SECRET_KEY` +
      `STRIPE_WEBHOOK_SECRET`. Service restarts → feature lights up. No code change.
- [ ] **Cloudflare** (optional — verified 2026-09-15: Access gates `/api/*` only; `POST /stripe/webhook` already reaches the origin, 404 while dark): add a WAF skip/bypass rule for path `/stripe/webhook` — Bot Fight
      Mode / managed challenges can 403 Stripe's POSTs. After the first live event, check
      the webhook's delivery log in Stripe for non-200s.
- [ ] Smoke test: one hidden test lot priced at $0.50, pay by real card, watch the
      Telegram ping + admin row, then refund it in the dashboard and watch it sync.

## 4. Day-to-day operation

- **A deposit does NOT decrement inventory.** The Telegram ping and the paid card both
  remind you: adjust `quantity_remaining` on the Inventory tab yourself.
- Change the deposit rule any time: admin → `11 Deposits` → settings strip (% and MIN $).
  Per-lot override: `PATCH /api/inventory/{lot_id}` with `deposit_pct_override` (0–1).
- Refunds: always from the Stripe dashboard; the site syncs via webhook. Policy shown to
  buyers (checkout + /terms): refundable until freight is booked; after booking the
  deposit covers freight on cancel; otherwise it applies to the balance at delivery.
- ACH lag is normal: `processing` for 1–4 business days is not an error. Never book
  freight against a `processing` row — wait for `paid`.

## 5. Affirm financing link (operator-made, `kind='financed'`)

For a buyer who pays part in cash and finances the rest — e.g. $9,000 deal = $4,500 cash at
pickup + $4,500 through Affirm. Affirm pays us the full $4,500 up front; the buyer repays
Affirm. Ours to lose only if we release chairs before the row says `paid`.

**One-time:**
1. `scripts/sql/034_deposits_financed.sql` — APPLIED to prod 2026-10-26.
   Until then the route answers 409 with the apply command.
2. Stripe → Settings → Payment methods → **Affirm → On** (test mode first, then live).
   Optional: **Affirm → financing package → Enhanced** = 0% APR plans (3/6 mo) offered to
   the buyer, at a higher fee to us. Check the fee on that screen before switching.
3. Same keys + webhook as §3 — nothing new. Affirm settles at once, so
   `checkout.session.completed` already carries `paid`.

**Per deal:** admin → Sales › Deposits → **AFFIRM LINK** row: lot id, qty, FINANCE $
(4500), ORDER $ (9000; blank = qty × lot price), buyer name + email/phone → **make link**
→ copy → send to the buyer. The link expires in 24 h unpaid; make a new one if it does.
Telegram pings `💰 AFFIRM PAID` when approved. Release the chairs only when the row is
`paid` AND the cash is in hand. Inventory is still adjusted by hand.

Test (test mode): make a link, open it, pick Affirm, use SSN last-4 `0000` on the Affirm
sandbox → row `paid`, `payment_method=affirm`, one ping. Close the Affirm modal instead →
the row stays `pending` and the session expires to `canceled`.

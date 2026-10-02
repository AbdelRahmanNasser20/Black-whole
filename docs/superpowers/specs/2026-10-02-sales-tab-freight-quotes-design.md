# Sales Tab + Freight Quote Capture — Design Spec

**Date:** 2026-10-02 · **Repo:** `listing_automation` · **Branch:** `feat/sales-tab-freight-quotes` · **Status:** built 2026-10-02 (operator: "start building"). Section 9 lists where the build differs from this text.

**Summary**
- Every freight request is saved with ZIP, email and phone, and shows in one admin **Sales** tab with a status.
- Sales replaces three tabs (Deposits, Inquiries, Subscribers) and adds Quotes. The rail goes from 12 tabs to 10, with keyboard shortcuts.
- Each request gets real carrier prices from Warp in the background, shown to the operator only. The buyer sees the same price as today.

---

## 1. Decisions (operator, 2026-10-02)

1. **Rate source = Warp.** ParcelPath has no public API. It stays a by-hand price check only.
2. **One Sales tab** holds Quotes, Inquiries, Deposits and Subscribers. No new rail tab.
3. **Keyboard shortcuts** to move between tabs.
4. **The buyer gives ZIP, email and phone before the price shows.** All three are required. Nothing else is asked.
5. **The price shown to the buyer does not change.** It stays the in-house estimator range.
6. **Chair weight, frame and chairs-per-pallet are tracked per lot.** The Idaho (Boise) chair is the standard for every lot until the operator says otherwise.
7. **Telegram stays the alert channel.**

## 2. Verified facts (2026-10-02)

- **Requests are already stored.** `freight_quotes` is live in Supabase with 9 rows (5 storefront, 4 CRM). The 160-chair Atlanta request is row 9. No admin screen reads this table.
- **Only 1 of 5 storefront requests left an email.** Email is asked after the price, and only when the insert worked.
- **Ways a request is lost today** (`automation/web/app.py:1817-1906`, `automation/freight_log.py`, `static/site/site.js:124-230`):
  - A lane the estimator cannot quote writes no row and sends no alert.
  - A failed insert still shows a price, hides the email step, and logs only a warning.
  - The email endpoint ignores a failed update and still answers `ok`.
  - Telegram send errors are dropped without a log line.
  - A ZIP that is not exactly 5 digits never leaves the browser.
  - Quantity is not checked against stock. Row 9 asked for 160 on a lot with 100 left.
- **Warp API** (spec: `https://www.wearewarp.com/.well-known/openapi.json`, base `https://www.wearewarp.com/api/v1`):
  - `POST /ltl/market-options` returns 17–18 carriers per lane. It works without a key ("indicative" rates), takes 20–45 s, and answers `200` with an empty list when its source fails.
  - `POST /ltl/quote` swaps in a dedicated truck on lanes with no Warp LTL (`mode_substituted`). It returned $6,598 for 72 chairs Las Vegas → Ohio; the market was $846.
  - Pallet height limit is 85 in.
- **`WarpProvider` in `automation/freight_estimate.py:543-578` does not match that API.** It posts another body shape to another host and looks for other price keys. It has never run with a key. With a key set today it would fail on every estimate and add up to a 20 s wait before the fallback.
- **Estimator accuracy** (`direction/tasks/2026-10-02/ltl-accuracy-check.md` in the workspace repo): close on 1–4 pallets; 40–90 % low on 150–200 chairs when chairs pack about 40 to a pallet.
- **Pallet math from the real truck:** 1,250 chairs filled a 53 ft trailer with about 2 rows spare, stacked about 15 high.
  - Floor used: about 47 of 52.5 ft, which is about 23–24 pallet positions. About 26 chairs per linear foot.
  - An LTL pallet cannot be 15 high. At the 85 in limit a stack is about 10 high, so one pallet holds about **35 chairs**.
  - The code assumes up to 92 chairs per pallet (`handling_units()`, 1 pallet per 4 linear ft). That is about 2.6× too many.
  - These are estimates from memory, not measurements.
- **Admin today:** 12 rail tabs, no tab shortcuts (`static/admin/shell.js`). `inventory.weight_lb` exists and is empty on all 44 lots.

## 3. Design

### 3.1 Sales tab

- **Rail:** one `sales` tab replaces `inquiries`, `subscribers` and `deposits`. Tab numbers are renumbered 01–10.
- **Views inside it:** Quotes · Inquiries · Deposits · Subscribers, as a switch at the top of the pane. Quotes is the default.
- **URL:** `?tab=quotes|inquiries|deposits|subscribers`. Each view keeps its own `?tab=` value, so old links work unchanged. The Sales rail tab is active for all four.
- **Reuse:** `inquiries.js`, `subscribers.js` and `deposits.js` keep their `mount()`/`load()` and their markup. `shell.js` owns the group and the switch. A new `quotes.js` owns the Quotes view.
- **Badge:** the Sales rail tab shows the count of quotes and inquiries with status `new`.
- **Quotes view columns:** date · lot · chairs (flag when over stock) · ZIP → miles · email · phone · price shown · cheapest carrier + name · status · note.
  - Status: `new` → `answered` → `won` / `lost`, plus `junk`. Changed with one click.
  - Filter: status (default `new` + `answered`). Sort: newest first.
  - Row action: **Check carrier prices** (runs 3.3 again).
  - The 9 existing rows show on day one with status `new`.

### 3.2 Keyboard shortcuts

- `[` and `]` go to the previous / next rail tab.
- `1`–`9` and `0` jump to tab 1–10.
- In Sales: `q` `i` `d` `s` pick the view.
- Shortcuts do nothing while the cursor is in an input, textarea or select, or when Cmd/Ctrl/Alt is held.
- Lives in `shell.js`. `deals.js` and `tracking.js` already listen for keys; the build checks for clashes.

### 3.3 Quote capture (storefront)

- **Widget** (`listing_detail.html`, `site.js`): fields are quantity, ZIP, email, phone. One button. The price shows only after a successful save.
- **ZIP:** ZIP+4 is trimmed to 5 digits. Anything else is still sent, so the request is stored.
- **Endpoint:** `POST /freight-estimate` takes `{lot_id, dest_zip, quantity, email, phone}`.
  - Missing or bad email or phone → `400` with a field name. Phone must be a US 10-digit number after cleanup.
  - Order of work: validate → run the estimator (pure arithmetic, no I/O) → **insert one row for either outcome** → answer. A price is returned only when the insert gave back an id.
  - Lane cannot be quoted → the row stays, with `unquotable_reason` set. The buyer sees "we will quote this lane by hand" and no contact form hand-off, because the contact details are already saved.
  - Quantity over stock → quoted as asked, stored with the lot's `quantity_remaining` at that time, flagged in the tab. The buyer sees "N available on this lot".
  - Insert fails → answer `{ok: false, reason: "not_saved"}`, show no price, and send the Telegram alert with all contact details, so the lead still reaches the operator.
- **Alert:** one Telegram `leads` message per request with lot, chairs, ZIP, email, phone, and price or "unquotable". A failed send is logged at warning level.
- **`POST /freight-estimate/email`** stays for pages cached in a browser. It is removed in a later release.
- **Rate limit:** unchanged. A `429` is logged with the lot id.

### 3.4 Carrier prices (operator only)

- **New module `automation/warp_rates.py`** (stdlib `urllib`, no new dependency). One function: lane + pallets → list of `{carrier, price_usd, transit_days}`.
- **Call:** `POST /ltl/market-options` only. Never `/ltl/quote`. Uses `WARP_API_KEY` as a Bearer token when set; works without it.
- **Inputs:** pallets = `ceil(chairs / chairs_per_pallet)`; weight per pallet from chair weight + 40 lb; 48×40 in footprint; height from the lot data; liftgate + residential delivery, same as the estimator default.
- **When:** after the answer is sent to the buyer, in a worker thread (`asyncio.to_thread`), 60 s timeout. Not on the event loop.
- **Stored on the row:** cheapest price, its carrier, carrier count, the 5 cheapest options, fetch time. The full response is not stored (Supabase size rule).
- **Guards:**
  - An option that carries a mode substitution is dropped.
  - An empty list or an error stores "no carrier data" and never changes the price shown.
  - Over 12 pallets: no call; the row says "too big for LTL — quote by hand".
- **Price check:** when the cheapest carrier is outside the range shown to the buyer by more than 25 %, the row shows "site low" or "site high". This builds the accuracy record with every request.
- **Removed:** `WarpProvider` and its branch in `select_provider()`. The buyer-facing path is estimator only, so a key can never switch on the broken adapter.

### 3.5 Chair data per lot

- **New `inventory` columns:** `chair_weight_lb`, `chair_frame`, `chairs_per_pallet`, `pallet_height_in`. All nullable.
- **Admin Inventory tab:** four small editable fields per lot.
- **`inventory.upsert_from_run()` keeps these on a re-run**, like the other operator edits.
- **Use:** the endpoint builds the `LotCalibration` from the lot row and passes it in. `freight_estimate.py` stays stdlib-only with no DB access.
- **Default (the Idaho chair) when a lot has no values:** 13 lb per chair (not measured), 35 chairs per pallet (estimate), 80 in pallet height, 23 chairs per linear foot (unchanged).
- **`inventory.weight_lb` is left alone.** Its meaning is not defined in the code.

### 3.6 Database — `scripts/sql/021_sales_quotes.sql`

- `freight_quotes`:
  - add `buyer_phone TEXT`
  - add `status TEXT NOT NULL DEFAULT 'new'`, CHECK `new|answered|won|lost|junk`
  - add `status_changed_at TIMESTAMPTZ`, `note TEXT`
  - add `unquotable_reason TEXT`, `lot_quantity_remaining INTEGER`
  - add `carrier_status TEXT` (`ok|none|too_big|error`), `carrier_low NUMERIC(10,2)`, `carrier_name TEXT`, `carrier_count INTEGER`, `carrier_options JSONB` (at most 5 entries), `carrier_checked_at TIMESTAMPTZ`
  - drop NOT NULL on `mode` and `origin_zip` (unquotable rows have neither)
  - add index on `(status, quoted_at DESC)`
- `inventory`: add the four columns in 3.5.
- All changes are additive. The old code keeps working after the migration is applied.
- The DDL in the workspace `docs/claude-reference/data-model.md` is updated in the same change.

### 3.7 Admin API (under `/api/`, behind auth)

- `GET /api/freight-quotes?status=` — list, with `@readcache.cached()`.
- `PATCH /api/freight-quotes/{id}` — status and note.
- `POST /api/freight-quotes/{id}/carrier-check` — run 3.4 again.
- Handlers are plain `def`, DB through `automation/db.py`.

## 4. Testing

- `tests/web/test_freight_endpoint.py`:
  - email and phone are required (`400` without them)
  - the row exists before the estimate runs
  - an unquotable lane stores a row and sends one alert (replaces the test that pins "nothing logged")
  - a failed insert answers `not_saved`, shows no price, and the alert carries the contact details
  - over-stock quantity is flagged
- `tests/test_warp_rates.py`: request body, key and no-key headers, cheapest pick, substitution dropped, empty list, timeout. Canned responses only, no network.
- `tests/web/`: the three new admin routes; `test_event_loop_hygiene.py` stays green.
- `tests/test_freight_estimate.py`: lot values override the default; pinned lanes do not move.
- By hand: load `/admin`, check each Sales view, each shortcut, and one real quote from the public page.

## 5. Operator gates

1. **`021_sales_quotes.sql` — applied to prod 2026-10-02**, before the deploy. The 9 existing rows got status `new`.
2. **Warp key (optional).** The feed works without it at 60 calls per hour. With a key the rates are bookable. Put it in `.env` and Render `blackwhole-secrets` as `WARP_API_KEY` only after this change is live.
3. **RLS.** `freight_quotes` will hold buyer phones, and RLS is off on every table. This change does not fix that. It is the open workspace gate.

## 6. Out of scope

- Showing carrier prices to the buyer.
- Changing the estimator's price math. This waits for measured chair data and more price checks.
- Full-truck and box-truck quotes from Warp.
- Booking freight through an API.
- ParcelPath or GoShip integration.
- Email alerts, a daily unanswered-quotes digest, CRM-side quote rows.

## 7. Follow-up tickets

1. **Measure the Idaho chair:** weight, chairs per pallet, stack height at 80 in. Enter the values in the Inventory tab.
2. **Re-tune the estimator** when 10 or more requests have carrier prices beside them.
3. **Warp key** from the business inbox (onboarding email from Warp), or a new account under `abdel.nasser@black-whole.com`.
4. **Find the weight on the Boise → Atlanta bill of lading.** It gives a real per-chair weight for 1,250 chairs.

## 8. Done when

- A quote made on the public page shows in Sales → Quotes with ZIP, email and phone, and a carrier price appears beside it within about a minute.
- A lane the estimator cannot quote also shows there.
- The rail has 10 tabs and the shortcuts work.
- The full test suite is green.

## 9. Differences between this spec and the build

- **Migration number is 021.** 018–020 were taken on other branches.
- **The code runs before the migration is applied.** `freight_log.schema_ready()` checks for `buyer_phone`. Without it, a quote saves through the old columns (phone inside `raw_response`), an unquotable request is alerted but not stored, and follow-up writes answer `409` with the apply command.
- **One insert, not insert-then-update.** The estimator is pure arithmetic, so it runs first and the row is written once.
- **No `sales.js`.** The four views stay separate `?tab=` values; `shell.js` groups them under one rail tab.
- **`/freight-estimate/email` now answers `503` when nothing was saved** (it answered `ok` before).
- **Older rows compare the ask against the lot's current stock**, because they have no stock snapshot.
- **Checked on a local Postgres 16 copy of the prod table shapes** with a live keyless Warp call: 160 chairs Atlanta 30318 → Decatur 30033 = 5 pallets, cheapest carrier Averitt $617 of 17, site showed $570–$810.
- **PR #110 (`feat/quote-form-phone`, optional phone after the price) is superseded** by this change.

## 10. Changes after the independent review (2026-10-02)

- **A destination that is not exactly 5 digits is never priced.** The estimator zero-pads, so `3003` was quoted as 03003 (New Hampshire) once the browser check was relaxed. The widget also stops an all-digit ZIP of the wrong length.
- **Carrier checks have their own 2-thread pool**, a queue cap of 8, a 6-hour memo per lane, and an hourly budget (`WARP_RATES_PER_HOUR`, default 40). They no longer share the default executor with the DB calls.
- **No Warp call before migration 021.** There is nowhere to store the answer.
- **An unquotable request that was not written sends the buyer to the contact form** (`saved: false`). "We'll follow up" is said only when the row exists.
- **A database outage on the lot lookup is a paged lead**, not a 500. Contact details are validated first.
- **If the row was not written and the alert also failed, the log line carries the contact details.** With a row on file they stay out of the log.
- **`schema_ready()` keys on `carrier_checked_at`**, the last `freight_quotes` column the migration adds, so a half-applied migration cannot break inserts.
- **The badge counts storefront requests with status `new` only**, and is zero before the migration.
- **Shortcuts match the physical key (`e.code`)**, so they work with an Arabic layout active. They are off while the deal drawer is open.
- **Links on a quote card are built for their context:** the email is percent-encoded in `mailto:`, the phone is digits only, a thread link must be `https`.
- **Smaller:** Warp response capped at 512 KB; `true` is not a chair weight; a non-number in a number box no longer clears the value; chair fields before the migration answer 409 with the command; gray-zone rows compare against the cheaper range.
- **Not changed, by choice:** a lot page left open across the deploy gets a 400 and an error message (the attempt is logged; a reload fixes it). The rate limiter still trusts `cf-connecting-ip` as before.

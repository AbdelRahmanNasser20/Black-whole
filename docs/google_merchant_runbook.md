# Google Merchant Center — operator runbook

Feed URL: `https://black-whole.com/catalog/google.csv` (public, refreshes every 15 min from `inventory`).

## One-time setup (≈20 min, operator clicks)
1. merchants.google.com → Create account. Business name "Black Whole Liquidation", country US, address = business address on file. Use the business Google account once it exists; personal is fine to start (rehoming accounts is parked).
2. **Verify website**: Business info → Website → choose "HTML tag". Copy the `content="..."` value. Set `GOOGLE_SITE_VERIFICATION=<value>` in Render → blackwhole-secrets → redeploy. Return to Merchant Center → Verify. (The meta tag renders on every public page when the env is set.)
3. **Shipping**: Shipping & returns → Add shipping service → name "Local pickup / freight quote", country US, rate = flat $0, delivery time 1–14 business days. Google requires *a* shipping entry for listings to show; the site's freight widget quotes the real number. Do not enter a made-up freight price anywhere else.
4. **Add the feed**: Products → Feeds → Add primary feed → country US, language English → name `black-whole inventory` → method **Scheduled fetch** → URL `https://black-whole.com/catalog/google.csv`, fetch daily 06:00 America/Phoenix → Create → Fetch now.
5. **Free listings**: Growth → Manage programs → Free listings → enable. Shopping ads only if a budget is decided later.
6. Wait for "Diagnostics" to go green (up to 3 days). Common warnings and what they mean:
   - `Missing value [shipping]` → step 3 not done.
   - `Invalid value [availability]` → the feed sends `in_stock`; if Google shows this, the fetch hit an old deploy.
   - `Missing value [gtin]` → expected; `identifier_exists=no` suppresses it once processed.
   - `Image too small` → the lot's hero on R2 is under 100×100; redo photos via `/list-lot redo-photos`.

## How it ties into Channels
- The Channels tab `GOOGLE` switch only tells the sync loop to keep recording `google` as live/delisted; the feed itself is always served. Turning the switch OFF does not remove products from Google — pause the feed in Merchant Center for that.
- A lot leaves the feed when `status` leaves `CATALOG_FEED_STATUSES` or `quantity_remaining` hits 0, same as Facebook. Google re-fetches daily, so expect up to 24 h lag; "Fetch now" forces it.

## Checks
- `curl -s https://black-whole.com/catalog/google.csv | head -3`
- Row count should equal the `live` count in the GOOGLE column of `/admin?tab=channels`.

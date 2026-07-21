# Site fixes — Public Surplus, budget removal, sold-out social proof, folder reorg

**Date:** 2026-07-21
**Status:** approved, ready for implementation plan

Four independent tickets. A fifth (site-wide date formatting + timezone
unification) was scoped but **backlogged** by the operator — captured at the
end for later.

Implementation order is **1 → 2 → 3 → 4**. Public Surplus is first because it
is actively destroying good data on every cron run.

---

## Ticket 1 — Public Surplus: stop data loss, restore visibility

### Problem

Public Surplus scraping works fine — it wrote 283 fresh rows to Supabase
`auction_listings` 12h before this spec. The listings are invisible because of
a chain of four defects:

1. **Destructive upsert.** `scripts/transfer_listings_to_supabase.py` upserts
   `quantity = EXCLUDED.quantity` with no `COALESCE`. When an LLM run fails and
   sets `quantity = NULL`, the transfer overwrites yesterday's verified count
   with NULL. Same bug locally at `auction_extractors/listings_db.py:228`
   (`listing.get("quantity", existing["quantity"])` returns `None` because the
   key is always present and set to `None`). The upsert also has **no
   per-source scoping and no `last_seen_at` freshness guard** — this is the
   same unfixed clobber described in the `transfer-clobbers-fresh-govdeals-rows`
   memory note.
2. **Shared-quota collision.** `scripts/run_discovery.sh` runs GovDeals first
   (line 13), Public Surplus second (line 23). Both share one Groq key
   (`LLM_PROVIDER=groq`, `render.yaml:104`). GovDeals burns the quota mid-run
   (575 failed / 132 OK on 07-21), so PS — running second — gets HTTP 429 on
   every chunk. `quantity_llm.py:291-299` nulls the quantity by design on
   failure (`quantity_source = "llm_failed"`, BLACKWHOLE-4 AC4: never ship
   untrusted regex counts). Result: 100% of fresh PS rows are NULL.
3. **Invisible failure.** `automation/auctions_supabase.py:50` filters
   `WHERE quantity >= %s`. `NULL >= 50` is never true, so all NULL-quantity rows
   silently vanish. A total LLM outage is indistinguishable from "PS has no
   chairs." The Auctions tab header reports "2,325 PS lots cached · newest
   today" while the grid shows zero cards — that contradiction is the tell.
4. **Silent for two days.** `_alert_on_quantity_degradation`
   (`public_surplus_automation.py:694`) should have Telegrammed on 07-20 when PS
   hit 100% failure. It didn't fire (or wasn't seen) — likely because
   `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are not set in the
   `black-whole-discovery` cron env group.

GovDeals is 81% failing from the same cause (only 4 active cards survive the
`active_only` + quantity filters). This is not a PS-only outage — PS just hit
100% first.

### Fix

**1a. Stop the data destruction (highest priority — do first).**
- In `scripts/transfer_listings_to_supabase.py`, change the upsert to
  `COALESCE(EXCLUDED.quantity, auction_listings.quantity)` for the quantity
  column, and guard the whole row update with
  `WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at` (or per-column
  `GREATEST`/`COALESCE` where a monotonic guard doesn't fit). A failed LLM run
  must never erase a previously-verified count or push `last_seen_at` backward.
- Mirror the guard in `auction_extractors/listings_db.py:228`: only overwrite
  `quantity` when the incoming value is non-NULL.

**1b. Break the quota collision.**
- Run Public Surplus **before** GovDeals in `scripts/run_discovery.sh` so PS
  isn't structurally last in line for a rate-limited shared quota. (GovDeals,
  now running second, will inherit the degraded-visibility behaviour from 1c,
  so it degrades gracefully instead of disappearing.)
- Ensure a real LLM fallback chain exists: `quantity_llm.py:193-213` only adds
  gemini/openai if those keys are configured. Confirm `GEMINI_API_KEY` is set
  in the `black-whole-discovery` env group so `_provider_chain` has a fallback
  when Groq 429s, instead of being groq-only.

**1c. Make failure visible instead of hiding it.**
- In `automation/auctions_supabase.py`, stop letting `quantity >= 50` swallow
  NULLs. Surface NULL-quantity rows as a degraded card (label `qty unknown`)
  rather than dropping them, so an outage reads as "N lots, quantity pending"
  not "no chairs." Keep the `>= min_qty` filter for rows that *do* have a
  quantity.
- Reconcile the header count (`cache_stats`) with the grid so they can't
  disagree.

**1d. Alerting.**
- Verify `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are present in the
  `black-whole-discovery` cron env group so degradation alerts actually send.
  (Config change in Render dashboard — flag to operator, not code.)

### Verification
- After 1a + a re-run of discovery, previously-good PS quantities are no longer
  NULL in `auction_listings`.
- `GET /api/auctions?source=ps&active_only=1` returns > 0 items.
- Auctions tab header count and rendered card count are consistent.

### Out of scope
- Public Surplus on the deals tab / public site (deals is GovDeals-only v1).
- Rewriting the quantity LLM or switching providers wholesale.

---

## Ticket 2 — Remove the budget question

### Problem
`automation/web/templates/_subscribe_form.html:73-82` renders a
`BUDGET / CHAIR` `<select name="budget_per_chair">`. Nothing ever reads the
value — the alert matcher (`automation/alerts/matcher.py`) filters on chair
type, quantity, and geo/radius only. It's collect-and-display-only.

### Fix
- Delete the budget `<label>`/`<select>` block (`_subscribe_form.html:73-82`).
  It sits in a two-column `mf-row mf-row--two` (line 63) paired with the
  `timeline` select; after removal that row becomes single-column — collapse it
  cleanly (make `timeline` full-width or leave the grid to reflow).
- Drop `budget_per_chair` from the Telegram new-subscriber ping
  (`app.py:928`) since new rows will always be blank there.

### Keep (deliberately)
- The `subscribers.budget_per_chair` **column stays.** It holds real answers
  from prior signups; dropping it loses that history for no benefit. The admin
  Subscribers tab keeps rendering it (`app.js:1732,1767`) for old rows; new
  rows show `—`.
- `create_subscriber` (`inventory.py:589-647`) keeps accepting the kwarg for
  backward compat; the form simply stops sending it.

### Verification
- `/subscribe` POST from `/listings` and `/` succeeds with the budget field
  gone.
- Existing `tests/test_subscribers.py` still passes (the test at line 103 that
  asserts budget round-trips may need adjusting — keep it exercising the column
  via a direct `create_subscriber` call, not the form).

---

## Ticket 3 — Sold-out lots as social proof

### Goal
Show sold / no-longer-available lots on the public site — dimmed, red "SOLD
OUT" stamp, sorted last, capped and time-bounded so "recently" stays honest —
to signal traction. Internally distinguish lots we genuinely sold from lots we
lost at auction.

### Status model

| Internal status | Public treatment | Meaning |
|---|---|---|
| `sold_out` | red **SOLD OUT** stamp | genuinely sold |
| `lost_sold_out` (new) | red **SOLD OUT** stamp (identical) | we bid and lost the auction |
| `lost` | hidden (unchanged) | lost, not shown publicly |

The public label is a single constant `SOLD_PUBLIC_LABEL = "SOLD OUT"` so
wording is a one-line change. `lost_sold_out` exists purely for the operator's
own back-end bookkeeping; the public sees no difference from a real sale.

> Note recorded for the operator: stamping SOLD OUT on lots we lost (never
> owned) is a claim the business can't substantiate if a buyer asks. The
> operator has chosen this deliberately for the traction signal; the internal
> `lost_sold_out` status preserves the truth on the back end.

### Schema
Migration `ops/pending-db-updates-sold-social-proof.sql` (staged, guarded,
reviewed before apply — same convention as existing `ops/pending-db-updates-*`):
- `ALTER TABLE inventory ADD COLUMN sold_at timestamptz;`
- Extend the Supabase status CHECK constraint to include `lost_sold_out`.
  (The SQLite schema doesn't enforce the set; the Supabase mirror does.)

### Query — `automation/inventory.py`
- Add module constants: `SOLD_PUBLIC_STATUSES = ("sold_out", "lost_sold_out")`,
  `SOLD_PUBLIC_LABEL = "SOLD OUT"`, `SOLD_RECENT_LIMIT = 8`,
  `SOLD_RECENT_DAYS = 90`.
- `list_public(include_sold=False)`:
  - available lots unchanged (existing PUBLIC_STATUSES + `quantity_remaining`
    filter, existing ordering at lines 88-91).
  - when `include_sold=True`, append sold lots: status in
    `SOLD_PUBLIC_STATUSES`, `sold_at >= now() - 90 days`, ordered
    `sold_at DESC`, limited to `SOLD_RECENT_LIMIT`. Sold rows **skip** the
    `quantity_remaining > 0` filter (a sold lot legitimately has stock recorded,
    e.g. lot 7126 with 360).
  - each returned row carries an `is_sold` flag for the template.
- `stats()`: fix the existing inconsistency (lines 98-117) where `chairs` /
  `cities` apply only the status filter while `lots` applies the qty filter, so
  a qty=0 public-status row can't inflate the city count. Sold lots must not
  count toward "available" stats.

### Routes — `automation/web/app.py`
- `/` (line 477) and `/listings` (line 491): call
  `list_public(include_sold=True)`. Available cards render first, sold cards
  last (query already orders them).
- **Sitemap (line 868) and catalog feed keep `include_sold=False`** — no
  indexing dead lots, no advertising sold lots in a merchant feed.
- `/listings/{lot_id}` (lines 503-518): a `sold_out` / `lost_sold_out` lot must
  **not** 404 (it currently renders a normal page). It renders a **sold detail
  view**: JSON-LD availability flips from `InStock` to
  `schema.org/SoldOut` (fix at lines 404-408, which currently key off
  `quantity_remaining > 0` regardless of status), and the quote form is
  replaced by the subscribe block (*"This lot has sold. Get alerts when similar
  lots appear."*). This makes `source='site_detail'` reachable — it was
  declared (`001_subscribers.sql:29`) but had no emitting surface.

### Templates
- `listings.html` (card ~50-87), `landing.html` (card ~73-103): the stamp must
  branch on **status before quantity**. Today the stamp is quantity-only
  (`listings.html:62`, `landing.html:81`) so sold lot 7126 renders "AVAILABLE."
  New logic: if `item.is_sold` → `.lot-stamp--sold` with `SOLD_PUBLIC_LABEL`;
  else existing AVAILABLE/INQUIRE logic. Add `.lot-card--sold` modifier class on
  the card.
- `listing_detail.html` (big stamp ~28-30, form ~68-96): sold view per the route
  change above.

### Styles — `automation/web/static/public.css`
- Add `.lot-stamp--sold` (reuse `.lot-stamp` at 318-326; red bg/border instead
  of `--stamp-bg`/`--accent`) and `.lot-stamp--big` sold variant.
- Add `.lot-card--sold`: photo `filter: grayscale + opacity ~0.6`, price
  struck through. `.lot-card` is already `position: relative` (line 293) so no
  structural change needed.

### Ordering on the grid
Order is entirely SQL `ORDER BY` (`public.js` does no sorting, only
show/hide). Because `list_public` returns available-then-sold, the server order
already places sold cards last — no JS change required. Confirm the client
filter (`public.js:79-121`) doesn't reorder and that sold cards respect the
type/city/qty filters sensibly (a sold card should still be filterable but not
count as available stock).

### Data — staged `ops/` SQL (guarded, reviewed before apply)
- `5003` Burgundy Vinyl, Fresno CA → `lost_sold_out`, set `sold_at`.
- `28505` Saffron Stacking, Fort Sill OK → `lost_sold_out`, set `sold_at`.
- `334` Tan Banquet, Wilmington NC → `lost_sold_out`, set `sold_at`.
- `7126` Red & Gold, North Miami FL → resolve the qty/status mismatch (sold_out
  with 360 remaining): keep `sold_out`, set `sold_at`; decide whether the 360 is
  real stock or stale (operator confirms).
- The 3 stub rows (`Blue_Banquet_Silver_Frame_MD_189`, the trailing-space ATL
  stub, `Lot_of_68_Chairs_Lockport_68`) are untouched by this ticket.

### Verification
- `/listings` shows available lots first, then ≤8 sold lots from the last 90
  days with red SOLD OUT stamps and dimmed photos.
- Lot 7126 no longer shows "AVAILABLE".
- A sold lot's detail page returns 200, shows SoldOut structured data, and
  offers the subscribe block instead of a quote form.
- Sitemap and catalog feed contain no sold lots.

---

## Ticket 4 — Reorganize listing photo folders by status

### Problem
`~/Desktop/Banquet chiars Pictures/` is 40 flat items (37 lot folders + an
existing `Lost biddings/` folder + loose `.md`/html docs). Hard to scan; the
lost lots aren't grouped.

### Fix
`scripts/reorganize_listing_folders.py`, **dry-run by default, `--apply` to
commit.** Reads the inventory ledger, then moves each lot folder into a
status bucket and updates `inventory.folder_path` in the same transaction so
the ledger, backfill, and `/image/` serving keep working.

```
Banquet chiars Pictures/
├─ _active/    ← status listed, draft, owned, won_pickup
├─ _sold/      ← status sold_out
├─ _lost/      ← status lost_sold_out, lost  (merges existing "Lost biddings/")
├─ _archive/   ← status hidden + zero-qty stubs
└─ _docs/      ← the loose .md + html files (Listing_Automation_Playbook.md, etc.)
```

- Match folders to ledger rows by `folder_name` / `folder_path`.
- **Unmatched folders are reported, never moved blindly.**
  `Access_Denied_Unknown_NA`, `General listing`, and
  `Unknown_Unknown_Email_Auction_to_a_Friend_` have no ledger row — the script
  lists them and leaves them in place for the operator to place manually.
- Update `inventory.folder_path` to the new path for every moved folder, in the
  same DB transaction; if the transaction fails, don't move files (or move back).
- Idempotent: re-running after apply is a no-op (folders already bucketed).

### Verification
- Dry-run prints the full move plan + the unmatched list, changes nothing.
- After `--apply`: folders are bucketed, `inventory.folder_path` matches the new
  locations, `/image/<lot>/...` still serves, and the admin
  "Backfill-from-folders" still walks the tree.

---

## Backlogged — Ticket 5: site-wide date formatting + timezone unification

Deferred by operator on 2026-07-21. Captured so it isn't rediscovered from
scratch.

- Target format: `07/16/2026 6:19 PM` (US date + 12h clock), date-only where
  time carries no information. Timezone: **America/New_York** everywhere.
- The public site renders **no dates today** — all date display is in the admin
  dashboard (`index.html` + `app.js`) and `deal_listing.html`. (Ticket 3's sold
  date would be the public site's first rendered date — format it per the above
  when built.)
- Underlying bug to fix when this is picked up: the same auction's close time
  renders in **two different timezones on one page** — `deal_listing.html:57`
  hardcodes `%H:%M UTC`, the DealCard overlay (`deal_card.js:110`) uses
  browser-local `toLocaleString()`, and the Auctions grid (`app.js:987`) /
  listings-DB table (`app.js:1915`) render the raw scraper `end_date` which is
  **Eastern** (GovDeals is Eastern site-wide) next to UTC columns. These can
  disagree by up to 5h. The favorites strip is already correct because it routes
  through `favorites.py`'s Eastern→UTC normalization (`_EASTERN`, lines 46-96).
- Approach: one Jinja filter (`|dt`, `|dt_date`) registered on `templates.env`
  (nothing is registered today) + one JS helper pair (`fmtDate` /
  `fmtDateTime`) shared with `deal_card.js`. Collapse the three competing
  duration formatters (`_fmtAge`, `_fmtRemaining`, `dealEndsCell`) into one.
  Leave machine-facing ISO alone (sitemap `<lastmod>`, SQL params, all `/api/*`
  JSON, the `title=` raw-ISO tooltip).
- Drive-by bug spotted: `/api/compare` returns `"timestamp": row["id"]`
  (`app.py:1259`) — the row id, not the `ts` column — which `app.js:479`
  renders as a date. One-line fix when this ticket is picked up.

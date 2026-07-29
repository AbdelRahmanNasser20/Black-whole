# Facebook Marketplace listing playbook

**What this is.** The step-by-step process for creating one FB Marketplace
listing by hand, written down so it can be automated. Every step below was
walked in the live UI on **2026-07-29** against the operator's account, not
recalled from memory. Where `automation/facebook.py` has drifted from what the
UI actually does now, it's called out.

Companion docs: `fb_catalog_feed_runbook.md` (the sanctioned Business/catalog
path) and `fb_business_catalog_HOWTO.md`. This file is the *scraped personal
Marketplace* path — the one that actually produces our leads today.

---

## 1. Why the copy matters more than the pipeline

Before the mechanics, the evidence. Counting distinct buyer conversations in
`contacts` by the listing they came in on:

| Listing title | Buyers |
|---|---|
| Blue Stackable Banquet chairs | 166 |
| Banquet Chairs (Light Gray design W/ Gray Frame) | 151 |
| Beige Banquet chairs (READ DESCRIPTION() | 119 |
| ALL Kind Of Chairs For Sale Or Rent | 116 |
| **Blue Banquet Chairs (MD)** | **100** |
| Blue Banquet Chairs | 27 |
| Blue Banquet Chairs (READ DESCRIPTION)) | 14 |

Blue chairs across their four title variants pulled **307 buyers** — more than
any other product we've listed. That is the single strongest demand signal in
the CRM, and it is why blue gets relisted first into any new metro.

Things the top performers have in common:

- **Colour first, product second.** "Blue Stackable Banquet chairs" beats
  "Banquet chairs" 166 → 87. Buyers search by colour.
- **A location qualifier in parentheses.** `(MD)`, `(ORLANDO Pickup)`,
  `(ATL)`. It pre-filters out-of-range buyers before they message.
- **`READ DESCRIPTION` earns its keep** on lots with a catch (min order,
  pickup-only, condition). It doesn't suppress volume — the beige variant
  carrying it pulled 119.
- Repo convention (`templates.listing_title`) is `"<chair_type> (<city>, <STATE>)"`.
  That matches the winners; keep it.

---

## 2. The flow, as it exists today

`https://www.facebook.com/marketplace/create/item` → three steps, then Publish.

### Step 0 — Meta AI drafts the listing for you

**This is new and it changes the whole job.** A `Draft listings with Meta AI`
toggle sits above the form and is **ON by default**. The moment photos finish
uploading, Meta AI writes the Title, Price, Category, Condition and a full
Description from the images alone.

On the blue-chair upload it produced, unprompted:

- Title: `Blue Upholstered Stacking Banquet Chairs with Chrome Frames`
- Price: `360`
- Category: `Outdoor Chairs, Benches & Swings`
- Condition: `Used - Good`
- Description: a competent paragraph **plus** `Estimated (WxDxH): 18 x 21 x 33 in`

Read that as: photos are now the input, and the job is *correcting* an AI draft
rather than filling a blank form. Two consequences:

1. **Its price is not our price.** It guessed 360 for a lot we sell at 25/chair —
   it priced the stack, not the chair. Always overwrite.
2. **Its category is usually wrong** (it chose Outdoor for indoor banquet chairs).
   Always overwrite.
3. **Race condition for automation:** if you upload photos and then fill fields,
   Meta AI can land its draft *after* your writes and clobber them. Either turn
   the toggle off first, or fill fields after the AI draft settles and verify.

Its dimension estimate is genuinely useful and worth keeping — we have no
`dim_in` for most lots.

### Step 1 — Item details

| Field | What to put | Notes |
|---|---|---|
| Photos | 4–10, best wide shot first | 0/10 limit; 1 video allowed |
| Title | `<Colour> <Type> Banquet Chairs (<City>, <ST>)` | see §1 |
| Price | per-chair price, e.g. `25` | **currency trap — see §3** |
| Category | `Home & Garden ▸ Furniture` | tree picker, not free text |
| Condition | `Used - Good` | exact string in UI |
| Description | see §4 template | |
| SKU | `lot_id` | under `More details`; "Optional. Only visible to you" |
| Promote listing after publish | OFF | paid ads; leave off |
| Hide from friends | ON | keeps 3k personal friends out of the feed |

**Category is a tree, not a search box.** Typing free text filters
unpredictably — typing "Dining Chairs" landed on `Tools`. Click the field, let
the dropdown open (`Home & Garden` → `Tools / Furniture / Household / Garden /
Appliances`), then click `Furniture`.

### Step 2 — Delivery method

| Field | Value |
|---|---|
| **Location** | the city the chairs are in, e.g. `Los Angeles, California` |
| Delivery method | `Local pickup` |
| Meetup preferences | leave all three unchecked (warehouse pickup, not a driveway meetup) |

**The location autocomplete is a trap.** Typing "Los Angeles" offers, in order:
Los Angeles *Mexico*, Los Angeles *Coahuila, Mexico*, **Los Angeles, California
(City)**, Los Angeles *Brazil*, Downtown Los Angeles. The US city is *third*.
Pick the entry labelled `City`; confirm the green check appears and the preview
line reads `Listed … in Los Angeles`.

Note `automation/facebook.py` never sets Location at all — it inherits whatever
the account currently defaults to. That default follows wherever the operator
physically is (it read `Jacksonville` this session). **Every automated listing
to date has silently used the account default city.**

### Step 3 — List in more places

- `Marketplace` — checked, leave it.
- **`List in your groups` — up to 20 groups, one click.** This is the largest
  free reach lever in the whole flow and the pipeline does not touch it.
  It rendered empty this session (see §3), but on a US-region account this is
  where a listing goes from one metro to twenty buy/sell groups.
- Then `Publish`.

`Save draft` (top right, steps 1–2 only) parks it in
`marketplace/create` → Drafts without going public. Step 3 has no Save draft.

---

## 3. Open blocker: the account is on Egyptian pounds

The draft built this session saved as **`EGP25`**, not `$25`.

Facebook sets listing currency from the **account's Marketplace region**, not
from the listing's location. Setting the listing location to Los Angeles,
California did *not* change it — the preview and the saved draft both stayed in
`ج.م` / EGP. The account's Marketplace has followed the operator to Egypt (the
Marketplace home feed is serving Cairo listings in EGP).

Consequences while this is true:

- A published listing shows **EGP 25 ≈ $0.50** to a US buyer.
- The `List in your groups` panel came up empty — no US buy/sell groups offered.

This is an **account setting**, so it needs the operator to change it, not the
pipeline. Until it's fixed, do not publish new US listings. Existing active
listings (Boise, Fort Sill) still display `$25` and are unaffected.

---

## 4. Description template

Rendered by `templates.fb_description()`. Keep the shape; only the first
paragraph changes per lot.

```
<2–4 sentences: colour, frame, padding, stackable, weight rating, condition>

Location: <City, ST> (local pickup; delivery quotes on request)
Quantity available: <n>

Ideal for churches, banquet halls, community centers, schools, and event venues.

To get a quote, please reply with:
1. Quantity needed
2. Pickup or delivery
3. Your city / ZIP
```

The three-question close is doing real work: `contacts.summary` is full of
"asked if available, gave no quantity or location", and those are the threads
that stall. Asking for qty + pickup/delivery + ZIP up front is what lets the CRM
geo-route on the first reply.

Avoid in the body: prices other than per-chair, phone numbers, street addresses,
pickup times. The CRM redactor (`bot_drafter.redact`) strips those from replies;
putting them in the listing just routes buyers around the gate.

---

## 5. Photos

Source of truth is `automation/lot_images.py` — `resolve_lot(lot_id)` returns
durable R2 URLs first, local disk second. Never hand-roll
`DOWNLOAD_ROOT / folder_name`.

- 4 photos is the working minimum: **one wide stack shot** (proves volume),
  one mid rack/row shot, one single-chair three-quarter, one seat/fabric
  close-up (proves condition).
- The wide shot goes first — it's the thumbnail, and volume is what makes a
  bulk buyer click.
- Cleaned/dewatermarked files only. `_originals/` and `_screenshots/` are
  internal.
- A studio-render prompt for hero shots lives in the vault at
  `BLACKWHOLE/Listings Creation.md`.

Before listing any lot as `crm_offerable`, run:

```bash
./.venv/bin/python scripts/check_offerable_images.py --http
```

It exits non-zero on a lot with no usable photos — the exact silent failure that
cost five buyers on lot 31225.

---

## 6. What the automation does vs. what the UI does now

`automation/facebook.py::create_draft` — drift found while walking the flow:

| Code | Reality on 2026-07-29 |
|---|---|
| `FB_CONDITION = "Used (Good)"` | UI string is `Used - Good`. The click-by-text will miss. |
| `FB_CATEGORY = "Dining Chairs"`, typed as text | Category is a **tree**; typing lands on the wrong node. Correct leaf is `Furniture`. |
| Step 2 fills `Pounds` / `Ounces` | Step 2 is **Delivery method** (Location / Local pickup / Meetup). Weight fields only appear when shipping is enabled. |
| Never sets Location | Location lives on step 2 and silently defaults to the account's current city. |
| No handling of Meta AI autodraft | Toggle is ON by default and can overwrite fields written before it settles. |
| No group cross-posting | Step 3 offers up to 20 groups — the biggest free reach lever, unused. |
| `Hide from friends` walk-up-DOM to `[role=switch]` | Still correct; toggle still lives under `More details`. |
| SKU label probe (`SKU` / `Inventory ID` / `Product ID`) | Still correct; field is under `More details`. |

Fix order, highest payoff first: **(1)** set Location explicitly, **(2)** fix the
condition string, **(3)** category tree navigation, **(4)** group cross-posting,
**(5)** disable or await the Meta AI draft.

---

## 7. After publishing

1. Copy the listing URL into the ledger — `inventory.set_platform_url()`, which
   also promotes `status` `draft` → `listed`. If the URL doesn't flow back, the
   dedup check in `run.py` stops working and the next run re-lists the lot.
2. FB nudges `Renew your listing?` on active listings — renewing refreshes
   position in search. Cheap, do it when prompted.
3. When the lot empties, set `quantity_remaining = 0` on the admin Inventory
   tab; it auto-flips `status` to `sold_out` and the lot moves to the
   `ALREADY MOVED` archive on the storefront (social proof, and the CRM will no
   longer offer it).

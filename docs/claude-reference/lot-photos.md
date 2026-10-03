<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Lot photos — READ BEFORE WRITING ANY IMAGE-PATH CODE

**Cloudflare R2 is the canonical backend. Supabase Storage is dead.** The shared
Supabase project blew its egress quota and Storage is 402-restricted — every
`…supabase.co/storage/v1/object/public/listing-images/…` URL returns HTTP 402,
not the image. R2 (`R2_*` in `.env`, public base
`https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev`) serves the same key
contract with zero egress fees. `listing_images.upload_lot_images()` already
dispatches to `r2_images` whenever R2 is configured, so the *upload* path needs
no thought — but **never write a new Supabase Storage URL into `inventory`**,
and treat any row still carrying one as broken. `lot_images.storage_backend(url)`
answers which backend a URL belongs to. `deals/archive.py` (scraped auction
photos — the seller's, undisguised) uploads to the **PRIVATE** bucket
(`LOT_ARCHIVE_R2_BUCKET`, `put_private_object`) and stores the session-walled
proxy path `/api/deal-photos/<site>/<a>_<b>_<c>/<hash>.webp?v=…`, never a
public URL. The proxy only accepts that exact key shape, so it can't read the
raw archive or lot archives sitting in the same bucket. The 302 rows archived
before this still carry dead Supabase URLs.

**Resolving is centralized in `automation/lot_images.py`.** Don't hand-roll
`DOWNLOAD_ROOT / folder_name` again. The bug that motivated this: the CRM poller
resolved photos off `folder_path`, which only exists on the operator's laptop,
so on the server it found nothing and sent text-only replies — five buyers lost
on lot 31225 (~945 chairs). Rules the module encodes:
- `image_urls` (the gallery) is the photo **set**. `hero_image_url` is the
  **cover**. File 0 is uploaded under both keys, so unioning them double-attaches
  the first photo — `.urls` deliberately doesn't.
- Local disk is a fallback, never an answer to "can the bot show a buyer this
  lot" — `has_usable_images()` ignores disk on purpose.
- `_originals/` and `_screenshots/` are internal; only top-level files in a lot
  folder are listing photos.

**Getting photos onto a lot** — two scripts, by whether we physically have it:
```bash
# lots we own (reads the operator's Desktop folder)
./.venv/bin/python scripts/backfill_listing_images.py --lot 31225
./.venv/bin/python scripts/backfill_listing_images.py --missing

# lots we're offering but never picked up (active_bid) — mirrors the seller's
# own GovDeals photos into R2; finds asset/account from the row automatically
./.venv/bin/python scripts/import_deal_images.py --lot wa-steilacoom-50
```

**The guard.** A `crm_offerable` lot with no usable photos is the exact failure
that cost those buyers, and it's silent. `scripts/check_offerable_images.py`
exits non-zero on any such lot; `--http` also proves the URLs return 200 (which
is what catches a backend going dark, as Supabase did). Run it after flipping
any lot to `crm_offerable`.

## Watermark policy (2026-10-03) — `automation/photo_policy.py`

**The rule** (operator, replaces "every public photo is watermarked"):

| lot status | black-whole.com | every other channel (FB Marketplace/Page/catalog/ads, Google feed, eBay, Craigslist, CRM) |
|---|---|---|
| `active_bid` (still bidding) | **watermarked** | clean |
| anything else (owned, won, listed, sold, lost, hidden) | clean | clean |

"Clean" = **the actual photo**: GovDeals mark removed (`automation/dewatermark.py`,
API only, unchanged), web-optimised (`listing_images.clean_for_web`: EXIF
orientation baked in, long edge ≤ 1600, JPEG q82, metadata stripped — phone
GPS would leak the storage location). **No** mirror, **no** re-frame, **no**
watermark. The full disguise (mirror + re-frame + tiled watermark,
`image_disguise.disguise`) runs only for the site copy of an `active_bid` lot
and for favorites. One function decides: `photo_policy.wants_watermark(status,
channel)` = `channel == "site" and status == "active_bid"`, applied in
`listing_images.prepare_for_web(data, ext, key=, status=, channel=)`.

**R2 layout** (extends the opaque `p/<hmac>/` scheme):

```
p/<hmac>/h.o.jpg       hero, original (clean)                always
p/<hmac>/<tok>.o.jpg   gallery, original (clean)             always
p/<hmac>/h.jpg         hero, disguised + watermarked         only uploaded while active_bid
p/<hmac>/<tok>.jpg     gallery, disguised + watermarked      only uploaded while active_bid
p/<hmac>/h.c.jpg       2026-09 FB-catalog twin (mirrored, no watermark) — legacy, never served
```

`inventory.hero_image_url` / `image_urls` (and `auction_favorites.clean_*`)
store the **clean** URLs. Any reader that skips the policy gets a clean photo.
`photo_policy.clean_url()` / `watermarked_url()` swap between twins (legacy
lot-id keys are never rewritten — they never carried our watermark).

**Reading.** `lot_images.resolve(row, channel)` maps every URL to the variant
for `(row["status"], channel)`. `channel=None` (the default) is clean, so a
caller that forgets can't ship a watermark. `hero_src` / `gallery_srcs` are the
storefront helpers and default to `"site"`. Channel writers call
`lot_images.channel_photo_urls(row, channel)` (Craigslist adapter, PR #120,
should use this) or take files from `listing_images.public_copies()` (always
clean). Feeds: `catalog_feed` → `"fb_catalog"`, `google_feed` → `"google"`.
`scripts/post_fb_listing.py::fetch_photos` (and `fb_replace_photos.py` through
it) maps every plan URL through `clean_url` as a belt.

**Writing.** `listing_images.upload_lot_images(lot_id, paths, status=…)` →
`r2_images.upload_lot_images`: clean variant always, watermarked twin only when
`status == "active_bid"`. Returns clean URLs. Callers pass the row status
(`lot_channels.mirror_photos` looks it up; favorites pass `active_bid`).

**Automatic flip.** The variant is derived from `status` at read time, so a lot
leaving `active_bid` shows clean everywhere on the next request, with no
re-upload. `inventory.set_fields` (every status writer, incl. auction sync via
`lot_channels.remove_lot/restore_lot`) calls
`photo_sync.on_status_change(prior, new)`; a move across `active_bid` starts a
background `photo_sync.sync_lot` that fills R2 gaps (missing clean or
watermarked twin, our own folder photos for an owned lot) and stores clean
URLs. It never drops a photo. `PHOTO_POLICY_AUTOSYNC=0` turns it off
(`tests/conftest.py` does).

**Owned lots use our own photos.** For `owned / won_pickup / listed / draft /
sold_out`, when the lot folder (`lot_images.lot_folder`) holds a photo set that
differs from R2's, the folder is re-uploaded (clean only) and wins. GovDeals
scrape photos are only the fallback when we have nothing of our own.

**Backfill / report.** `scripts/apply_photo_policy.py` — dry-run default
(reads DB, HEADs R2), `--apply`, `--lot ID`, `--drop-missing`, `--rollback
<log>`. Sources for a missing variant: the lot folder (exact match on the
source-bytes token) → the pre-disguise R2 object named in a
`disguise-backfill-*.jsonl` log → for the hero, gallery photo 0's source.
Objects are never deleted.

**Deploy order.** Run `.venv/bin/python scripts/apply_photo_policy.py --apply`
on the laptop (folders + backfill logs are the sources) **before** this code
is deployed. The new code maps every stored URL to a `.o.jpg` object; until
the script has uploaded them and rewritten the DB, non-site images 404. Facebook Marketplace posts keep whatever photos they
were posted with — re-upload with `scripts/fb_replace_photos.py` (writes to FB,
operator's call).

## History: disguise vs Google Lens (2026-09-19) — a note, not a rule

The 2026-10-03 policy accepts that clean copies off the site can be
Lens-matched to GovDeals again. This is why the watermark existed:

**Why.** Most lot photos are the GovDeals seller's own. Google Lens exact-matched
our live copies to GovDeals, AllSurplus and GovDeals' Instagram (lots 31225,
125), so a buyer could find the auction and what we paid. The R2 keys also
spelled the GovDeals ids (`gd-239-31465/00.jpg`).

**What runs.** `listing_images.prepare_for_web()` → `image_disguise.disguise()`:
mirror → keystone → 1–3° tilt + crop-in → uneven crop → small colour grade →
resize → tiled `BLACKWHOLE` watermark → grain → metadata-free JPEG. Seeded by
HMAC(salt, lot, source bytes), so re-runs are byte-identical. Output must clear
pHash/dHash distance 12 (`image_hash.py`) or it is redrawn stronger. Unreadable
files are skipped, never uploaded raw. Stored under `p/<hmac>/h.jpg` (hero) and
`p/<hmac>/<hmac>.jpg` (gallery); the `p/` prefix is also the "done" marker.

**Lens results that set the recipe** (exact matches → GovDeals?):

| variant | 31225 | 125 |
|---|---|---|
| live photo, as uploaded before | match | match |
| crop/tilt/keystone/colour/grain, no mirror | match | match |
| same + mirror | match | match |
| 68% zoom crop, branded card, 5° warp (each) | match | – |
| watermark only | match | – |
| no mirror + watermark | match | – |
| **mirror + re-frame + watermark (shipped)** | **none** | **none** |

The AllSurplus copy of 125 still showed ~25th in "visual matches" (same chair,
same scene). No honest edit removes that; photos we take ourselves do.

**Knobs.** `IMAGE_DISGUISE=0` kill switch (legacy path + lot-id keys).
`IMAGE_DISGUISE_NO_MIRROR_LOTS=lot,lot` for photos whose text must read right
(tape measures) — those lots can be matched again. `IMAGE_DISGUISE_SALT`
(defaults to one derived from `R2_SECRET_ACCESS_KEY`; never rotate it casually —
new salt = new keys for every re-upload).

**FB catalog twin.** Meta rejects watermarked catalog images, so each disguised
hero got a watermark-free (still mirrored) copy at `…/h.c.jpg`. Since
2026-10-03 it is unused: `photo_policy.clean_url` maps it to the `.o.jpg`
original.

**Paths covered.** R2 uploads (`upload_lot_images`: lot_channels add/redo-photos,
import_deal_images, backfill_listing_images, favorites mirror), `run.py` FB/eBay
drafts (`public_copies`), and via R2 URLs: site, CRM, FB catalog feed,
post_fb_listing, fb_replace_photos. **Not covered:** photos already posted on
FB Marketplace — re-upload with `scripts/fb_replace_photos.py` (writes to FB).

**Live photos.** `scripts/disguise_live_images.py` was run 2026-10-02 (log
`disguise-backfill-20261002T152210Z.jsonl`, 40 inventory + 15 favorites rows).
It is superseded by `scripts/apply_photo_policy.py`; only `--rollback` still
runs, and its logs are read as the source map for the clean variants.

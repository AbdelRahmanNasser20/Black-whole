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
answers which backend a URL belongs to; `deals/archive.py` still uploads to
Supabase and is the one module that hasn't been moved over.

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

## Disguise — every public photo, before upload (2026-09-19)

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

**Paths covered.** R2 uploads (`upload_lot_images`: lot_channels add/redo-photos,
import_deal_images, backfill_listing_images, favorites mirror), `run.py` FB/eBay
drafts (`public_copies`), and via R2 URLs: site, CRM, FB catalog feed,
post_fb_listing, fb_replace_photos. **Not covered:** photos already posted on
FB Marketplace — re-upload with `scripts/fb_replace_photos.py` (writes to FB).

**Live photos.** `scripts/disguise_live_images.py` (dry-run default, `--apply`,
`--lot`, `--rollback <log>`). Re-publishes inventory + `auction_favorites.clean_*`
photos, keeps old objects, logs old→new under `~/.listing_automation/logs/`.

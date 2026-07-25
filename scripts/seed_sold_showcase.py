#!/usr/bin/env python3
"""BLACKWHOLE-29 — seed the public SOLD archive.

Two jobs, both idempotent (safe to re-run):

1. Create the **Blue Banquet Chairs w/ Silver Frame** showcase lot: 3,000 chairs
   that moved out of Maryland, Atlanta and Orlando. We hold none of these, so it
   exists purely as proof of volume — the storefront renders it stamped SOLD.
   Its photos are the operator's own (passed via --photos), uploaded to R2.
   The two half-imported legacy rows for the same chairs are set `hidden` so the
   archive shows one clean card instead of three fragments.

2. Flag the lots the operator marked as sold-showcase on 2026-07-25:
   saffron 250 (Fort Sill OK), maroon 299 (Flint MI), natural wood 180
   (Miamisburg OH). `lost_sold_out` = we never owned it but present it as sold;
   `fake_sold_out=true` is the flag the CRM already honors by refusing to offer
   the lot to a buyer.

NOT touched: `folder:ATL_Grey_blueish_chairs_399` — the greyish-blue chairs the
operator still has ~100 of. Those stay live and buyable.

Usage:
    ./.venv/bin/python scripts/seed_sold_showcase.py --photos <dir> [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import config, inventory, r2_images  # noqa: E402,F401  (config loads .env)

SHOWCASE_LOT = "blue-silver-frame-3000"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

SHOWCASE = dict(
    lot_id=SHOWCASE_LOT,
    title="Blue Banquet Chairs w/ Silver Frame",
    subtitle="Royal-blue padded seats on polished chrome frames — stackable.",
    quantity=3000,
    price_per_chair=25.0,
    chair_type="Banquet Chairs",
    city="Baltimore",
    state="MD",
    description=(
        "3,000 commercial-grade blue banquet chairs on polished silver/chrome "
        "frames, padded seat and back, stackable. Moved in bulk out of three "
        "warehouses — Maryland, Atlanta and Orlando — to churches, event "
        "rental companies and banquet halls. This lot is sold out; we source "
        "sets like it regularly."
    ),
    locations="Baltimore, MD x1200; Atlanta, GA x800; Orlando, FL x1000",
    status="sold_out",
)

# Legacy fragments of the same chairs — folder-imported stubs with no city or
# headcount. Hidden so the archive shows one card, not three.
SUPERSEDED = (
    "folder:Blue_Banquet_Silver_Frame_MD_189",
    "folder:ATL_Blue_banquet_silver_frame_189 ",
)

# lot_id → (status, fake_sold_out). Operator's call, 2026-07-25.
FLAG_AS_SOLD = {
    "28505": ("lost_sold_out", True),   # 250 saffron, Fort Sill OK
    "2807": ("lost_sold_out", True),    # 299 maroon, Flint MI
    "125": ("lost_sold_out", True),     # 180 natural wood, Miamisburg OH
}


def _photos(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMG_EXTS
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", type=Path,
                    help="directory of showcase photos (hero = first by name)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dry = args.dry_run

    # ── 1. the showcase lot ────────────────────────────────────────────────
    existing = inventory.get(SHOWCASE_LOT)
    if existing:
        print(f"· {SHOWCASE_LOT} already exists — updating fields")
        if not dry:
            inventory.set_fields(
                SHOWCASE_LOT,
                title=SHOWCASE["title"], subtitle=SHOWCASE["subtitle"],
                description=SHOWCASE["description"],
                quantity_original=SHOWCASE["quantity"], quantity_remaining=0,
                price_per_chair=SHOWCASE["price_per_chair"],
                chair_type=SHOWCASE["chair_type"],
                city=SHOWCASE["city"], state=SHOWCASE["state"],
                locations=SHOWCASE["locations"], status=SHOWCASE["status"],
            )
    else:
        print(f"+ creating {SHOWCASE_LOT}")
        if not dry:
            inventory.insert_manual(**SHOWCASE)

    if args.photos:
        paths = _photos(args.photos)
        if not paths:
            print(f"! no images in {args.photos}", file=sys.stderr)
        elif not r2_images.is_configured():
            print("! R2 not configured — skipping photo upload", file=sys.stderr)
        else:
            print(f"↑ uploading {len(paths)} photo(s) to R2")
            if not dry:
                result = r2_images.upload_lot_images(SHOWCASE_LOT, paths)
                if not result:
                    print("! upload returned nothing", file=sys.stderr)
                else:
                    inventory.set_images(
                        SHOWCASE_LOT, result["hero_image_url"], result["image_urls"]
                    )
                    print(f"  ✓ hero {result['hero_image_url']}")

    # ── 2. supersede the legacy fragments ──────────────────────────────────
    for lot_id in SUPERSEDED:
        row = inventory.get(lot_id)
        if not row:
            print(f"· {lot_id!r} not present — skipping")
            continue
        if row.get("status") == "hidden":
            print(f"· {lot_id!r} already hidden")
            continue
        print(f"→ hiding {lot_id!r} (folded into {SHOWCASE_LOT})")
        if not dry:
            inventory.set_fields(lot_id, status="hidden")

    # ── 3. flag the operator's sold-showcase lots ──────────────────────────
    for lot_id, (status, fake) in FLAG_AS_SOLD.items():
        row = inventory.get(lot_id)
        if not row:
            print(f"! lot {lot_id} not found — skipping", file=sys.stderr)
            continue
        if row.get("status") == status and row.get("fake_sold_out") == fake:
            print(f"· {lot_id} ({row.get('title')}) already {status}")
            continue
        print(f"→ {lot_id} ({row.get('title')}): "
              f"{row.get('status')} → {status}, fake_sold_out={fake}")
        if not dry:
            inventory.set_fields(lot_id, status=status, fake_sold_out=fake)

    if dry:
        print("\n(dry run — nothing written)")
        return 0

    shown = inventory.list_sold_showcase()
    total = sum(r.get("quantity_original") or 0 for r in shown)
    print(f"\nSOLD archive now: {len(shown)} lots / {total:,} chairs")
    for r in shown:
        print(f"  · {r['lot_id']:<32} {r.get('quantity_original'):>5}  {r.get('title')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

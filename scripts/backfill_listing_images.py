#!/usr/bin/env python
"""Backfill durable listing images into Supabase Storage (BLACKWHOLE-6).

Walks the inventory ledger, uploads each lot's local images (from its download
folder) to the shared `listing-images` bucket, and stamps `hero_image_url` +
`image_urls` on the row. Idempotent (upsert keyed on lot id), so it's safe to
re-run; rows with no local folder are skipped and left untouched.

Run locally (the laptop has the Desktop image folders); storage creds come from
.env via automation.config.

    ./.venv/bin/python scripts/backfill_listing_images.py            # all rows
    ./.venv/bin/python scripts/backfill_listing_images.py --missing  # only rows lacking image_urls
    ./.venv/bin/python scripts/backfill_listing_images.py --lot 31225
    ./.venv/bin/python scripts/backfill_listing_images.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, inventory, listing_images  # noqa: E402

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _lot_folder(row: dict) -> Path | None:
    """Resolve a row's local image folder (folder_path, else DOWNLOAD_ROOT/name)."""
    fp = row.get("folder_path")
    if fp and Path(fp).is_dir():
        return Path(fp)
    name = row.get("folder_name")
    if name:
        cand = config.DOWNLOAD_ROOT / name
        if cand.is_dir():
            return cand
    return None


def _ordered_images(folder: Path, hero_name: str | None) -> list[Path]:
    """Image files in the folder, hero first so it maps to the flat hero key."""
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in IMG_EXTS)
    if hero_name:
        hero = folder / hero_name
        if hero in files:
            files.remove(hero)
            files.insert(0, hero)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", action="store_true",
                    help="only rows that have no image_urls yet")
    ap.add_argument("--lot", help="backfill a single lot_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not listing_images.is_configured():
        print("ERROR: storage not configured — set SUPABASE_STORAGE_URL / "
              "SUPABASE_STORAGE_KEY in .env", file=sys.stderr)
        return 2

    if args.lot:
        row = inventory.get(args.lot)
        rows = [row] if row else []
    else:
        rows = inventory.list_all()

    uploaded = skipped = failed = 0
    for row in rows:
        lot_id = row.get("lot_id")
        if args.missing and row.get("image_urls"):
            continue
        folder = _lot_folder(row)
        if not folder:
            skipped += 1
            print(f"  skip {lot_id}: no local folder")
            continue
        imgs = _ordered_images(folder, row.get("hero_image"))
        if not imgs:
            skipped += 1
            print(f"  skip {lot_id}: folder has no images")
            continue
        if args.dry_run:
            print(f"  would upload {len(imgs)} imgs for {lot_id} (hero={imgs[0].name})")
            uploaded += 1
            continue
        result = listing_images.upload_lot_images(lot_id, imgs)
        if not result:
            failed += 1
            print(f"  FAIL {lot_id}: upload returned nothing")
            continue
        inventory.set_images(lot_id, result["hero_image_url"], result["image_urls"])
        uploaded += 1
        print(f"  ✓ {lot_id}: {len(result['image_urls'])} imgs → {result['hero_image_url']}")

    print(f"\nDone. uploaded={uploaded} skipped={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

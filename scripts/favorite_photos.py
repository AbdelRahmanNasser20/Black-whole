#!/usr/bin/env python
"""Clean photos for favorited auctions → R2 → auction_favorites.clean_*.

    .venv/bin/python scripts/favorite_photos.py --all            # favorites missing clean photos
    .venv/bin/python scripts/favorite_photos.py --all --force    # redo every GovDeals favorite
    .venv/bin/python scripts/favorite_photos.py --asset 9685/56
    .venv/bin/python scripts/favorite_photos.py --all --dry-run
    .venv/bin/python scripts/favorite_photos.py --all --max-lots 8

Budget: each favorite is its own dewatermark run, so MAX_API_CALLS_PER_RUN (50)
does NOT bound a sweep — only MAX_API_CALLS_PER_DAY (250) does, at up to 6 calls
per favorite. Use --max-lots to bound a sweep yourself. Exit 2 if every attempted
favorite failed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from automation import favorite_images, favorites  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--asset")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-lots", type=int, default=None,
                    help="stop after N favorites (bounds the daily API spend)")
    a = ap.parse_args()
    targets = [a.asset] if a.asset else [
        f.asset_id for f in favorites.list_all()
        if favorite_images.r2_key(f.asset_id) and (a.force or not f.clean_hero_url)]
    if a.max_lots is not None:
        targets = targets[:a.max_lots]
    print(f"{len(targets)} favorite(s) to process")
    mirrored = failed = 0
    photos = 0
    for asset_id in targets:
        if a.dry_run:
            print(f"  would mirror {asset_id}")
            continue
        result = favorite_images.mirror_favorite_photos(asset_id, force=a.force)
        if result:
            mirrored += 1
            photos += len(result["image_urls"])
        else:
            failed += 1
    if a.dry_run:
        return 0
    skipped = len(targets) - mirrored - failed
    print(f"mirrored {mirrored} · skipped {skipped} · failed {failed} "
          f"(≈ {len(targets) * favorite_images.FAVORITE_PHOTO_LIMIT} API calls max, "
          f"{photos} photos on R2)")
    return 2 if targets and not mirrored else 0


if __name__ == "__main__":
    sys.exit(main())

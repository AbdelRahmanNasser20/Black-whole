#!/usr/bin/env python3
"""Archive each lot's *original* (uncompressed) photos to R2.

The site serves the `optimize_for_web` JPEGs (small + fast). This uploads the
untouched source files alongside them under `<key>/original/NN.<ext>` so the
full-resolution originals live off-laptop too. R2 bills zero egress and the
free tier is 10 GB, so the archive is effectively free.

Originals are uploaded byte-for-byte — no resize, no re-encode, EXIF intact
(so viewers still rotate them correctly).

Usage:
    ./.venv/bin/python scripts/upload_originals_to_r2.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import config, inventory, listing_images, r2_images  # noqa: E402

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _lot_folder(row: dict) -> Path | None:
    fp = row.get("folder_path")
    if fp and Path(fp).is_dir():
        return Path(fp)
    name = row.get("folder_name")
    if name:
        cand = config.DOWNLOAD_ROOT / name
        if cand.is_dir():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not r2_images.is_configured():
        print("R2 is not configured (need R2_* env vars).", file=sys.stderr)
        return 2

    cfg = r2_images.env_config()
    s3 = None if args.dry_run else r2_images.client(cfg)
    up = skipped = failed = 0
    total_bytes = 0

    for row in inventory.list_all():
        lot_id = row["lot_id"]
        base_key = listing_images.key_base(lot_id)
        folder = _lot_folder(row)
        if not folder or not base_key:
            print(f"  skip {lot_id}: no local folder")
            skipped += 1
            continue
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in IMG_EXTS)
        if not files:
            skipped += 1
            continue

        n = 0
        for i, fp in enumerate(files):
            ext = fp.suffix.lstrip(".").lower()
            key = f"{base_key}/original/{i:02d}.{ext}"
            size = fp.stat().st_size
            if args.dry_run:
                n += 1
                total_bytes += size
                continue
            ct = listing_images._EXT_CT.get(ext, "application/octet-stream")
            if r2_images.put_object(s3, bucket=cfg["bucket"], path=key,
                                    data=fp.read_bytes(), content_type=ct):
                n += 1
                total_bytes += size
            else:
                failed += 1
        verb = "would upload" if args.dry_run else "✓"
        print(f"  {verb} {lot_id}: {n} originals")
        up += 1

    print(f"\nDone. lots={up} skipped={skipped} failed={failed} "
          f"bytes={total_bytes/1048576:.0f} MB")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

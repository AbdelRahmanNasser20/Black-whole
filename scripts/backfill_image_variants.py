#!/usr/bin/env python3
"""Make the small phone copies (320 px + 800 px) of every live lot photo (SITE-SPEED).

New uploads get them in `r2_images.upload_lot_images`. This does the photos
that were uploaded before. For each disguised `p/…/*.jpg` URL in
`inventory.hero_image_url` / `image_urls`:

  1. HEAD `<name>.w320.jpg` and `<name>.w800.jpg` on the public bucket.
  2. If one is missing, GET the PUBLIC photo (the disguised bytes buyers
     already see — never an original), shrink it, upload the copies next to it.

- Dry-run by default (read-only: DB SELECT + public HEADs). `--apply` uploads.
- No DB write. The site derives the copy URL from the full URL
  (`lot_images.sized`), and falls back to the full photo if a copy is missing.
- Only our own R2 URLs under `p/` are touched. Legacy lot-id keys are listed
  as "skipped (not disguised)" — run `scripts/disguise_live_images.py` first.
- Idempotent: a photo whose copies exist is skipped (`--force` redoes it).

    .venv/bin/python scripts/backfill_image_variants.py                 # plan
    .venv/bin/python scripts/backfill_image_variants.py --apply         # do it
    .venv/bin/python scripts/backfill_image_variants.py --lot 31225-atl --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import db, lot_images, r2_images  # noqa: E402


def _get(http: httpx.Client, url: str, attempts: int = 4) -> bytes | None:
    for n in range(1, attempts + 1):
        try:
            resp = http.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            if n == attempts:
                print(f"    ! download failed {attempts}x: {url} ({e})")
                return None
            time.sleep(2 * n)
    return None


def _exists(http: httpx.Client, url: str) -> bool:
    try:
        return http.head(url).status_code == 200
    except httpx.HTTPError:
        return False


def collect_urls(lot: str | None) -> list[tuple[str, str]]:
    """[(lot_id, url)] for every photo URL in inventory, deduped, in lot order."""
    where, params = ("WHERE lot_id = %s", (lot,)) if lot else ("", ())
    rows = db.fetch_all(f"SELECT lot_id, hero_image_url, image_urls FROM inventory {where} "
                        "ORDER BY lot_id", params)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        for u in [r.get("hero_image_url"), *list(r.get("image_urls") or [])]:
            u = str(u or "").strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                out.append((str(r["lot_id"]), u))
    return out


def run(args) -> int:
    cfg = r2_images.env_config()
    if not cfg:
        raise SystemExit("R2 is not configured (R2_* in .env) — nothing to upload to")
    s3 = r2_images.client(cfg) if args.apply else None
    base = cfg["public_base"] + "/"
    stats = {"done": 0, "present": 0, "would_make": 0, "not_disguised": 0, "other_host": 0,
             "failed": 0}
    with httpx.Client(timeout=90.0, follow_redirects=True) as http:
        for lot_id, url in collect_urls(args.lot):
            if not url.startswith(base):
                stats["other_host"] += 1
                continue
            if not lot_images.has_variants(url):
                stats["not_disguised"] += 1
                print(f"  skip (not disguised) {lot_id}: {url}")
                continue
            copies = [lot_images.sized(url, w) for w in lot_images.VARIANT_WIDTHS]
            if not args.force and all(_exists(http, c) for c in copies):
                stats["present"] += 1
                continue
            if not args.apply:
                stats["would_make"] += 1
                print(f"  would make copies  {lot_id}: {url}")
                continue
            data = _get(http, url)  # the public (disguised) bytes, never an original
            path = urlsplit(url).path.lstrip("/")
            n = r2_images.put_variants(s3, bucket=cfg["bucket"], path=path, data=data) if data else 0
            if n == len(lot_images.VARIANT_WIDTHS):
                stats["done"] += 1
                print(f"  made {n} copies     {lot_id}: {url}")
            else:
                stats["failed"] += 1
                print(f"  ! failed ({n} copies) {lot_id}: {url}")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{mode}: {stats}")
    if not args.apply and stats["would_make"]:
        print("Run again with --apply to upload the copies.")
    return 1 if stats["failed"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="upload (default: dry-run)")
    ap.add_argument("--lot", help="one lot_id only")
    ap.add_argument("--force", action="store_true", help="remake copies that already exist")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

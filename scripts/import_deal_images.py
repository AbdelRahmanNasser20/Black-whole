#!/usr/bin/env python
"""Pull a lot's photos off GovDeals into our own storage (BLACKWHOLE-31).

`backfill_listing_images.py` covers lots we physically have — it reads the
operator's Desktop folder. It can't help the other kind: a lot we're offering
but don't own yet (`active_bid`), which never got a folder because nothing was
ever picked up. Those rows sit in `inventory` with `crm_offerable = true` and no
photos at all, so the bot pitches a lot it can't show. This script closes that
gap by fetching the seller's own photos and mirroring them into our bucket.

Why not reuse `deals/archive.py`: that module uploads to Supabase Storage, which
is egress-restricted on this project and 402s on every public read. This goes
through `listing_images.upload_lot_images`, which routes to R2 — the backend
that's actually live — and writes the same hero/gallery key contract every other
lot uses, so nothing downstream has to special-case these rows.

Source photos are hot-linkable but not durable: GovDeals removes the asset once
the auction closes, and the CDN needs a browser `Referer` to serve at all.
Mirroring is the point.

    # explicit ids (from the row's description or the GovDeals URL)
    ./.venv/bin/python scripts/import_deal_images.py --lot wa-steilacoom-50 \
        --asset 13 --account 23849

    # or let it find the matching deal_lots row by title/city
    ./.venv/bin/python scripts/import_deal_images.py --lot wa-steilacoom-50
    ./.venv/bin/python scripts/import_deal_images.py --lot wa-steilacoom-50 --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, db, inventory, listing_images  # noqa: E402,F401

# The CDN 403s without a browser-shaped request — same reason
# `automation/downloader.py` sets these.
DOWNLOAD_HEADERS = {
    "Referer": "https://www.govdeals.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def find_ids(row: dict) -> tuple[int, int] | None:
    """Recover `(asset_id, account_id)` for an inventory row.

    Three places carry it, in descending reliability: the GovDeals URL, the
    "GovDeals asset N / acct N" note the operator leaves in `description`, and
    a title/city match against `deal_lots`.
    """
    url = row.get("govdeals_url") or ""
    m = re.search(r"/asset/(\d+)/(\d+)", url)
    if m:  # /en/asset/{asset_id}/{account_id} — asset first
        return int(m.group(1)), int(m.group(2))

    desc = row.get("description") or ""
    m = re.search(r"asset\s+(\d+)\s*/\s*acct\s+(\d+)", desc, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))

    city = (row.get("city") or "").strip()
    if city:
        hit = db.fetch_one(
            "SELECT asset_id, account_id FROM deal_lots "
            "WHERE city ILIKE %(city)s AND title ILIKE %(kw)s "
            "ORDER BY first_seen_at DESC LIMIT 1",
            {"city": city, "kw": "%chair%"},
        )
        if hit:
            return int(hit["asset_id"]), int(hit["account_id"])
    return None


def download(urls: list[str], dest: Path) -> list[Path]:
    """Fetch each URL into `dest`, numbered so upload order is deterministic."""
    import httpx

    out: list[Path] = []
    with httpx.Client(timeout=60.0, follow_redirects=True,
                      headers=DOWNLOAD_HEADERS) as client:
        for i, url in enumerate(urls):
            ext = listing_images.guess_ext(url.split("?")[0])
            target = dest / f"{i:02d}.{ext}"
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - one bad photo isn't fatal
                print(f"  ! download failed ({type(exc).__name__}): {url[:90]}")
                continue
            if not resp.content:
                continue
            target.write_bytes(resp.content)
            out.append(target)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lot", required=True, help="inventory.lot_id to stamp")
    ap.add_argument("--asset", type=int, help="GovDeals asset id")
    ap.add_argument("--account", type=int, help="GovDeals account id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    row = inventory.get(args.lot)
    if not row:
        print(f"ERROR: no inventory row for {args.lot!r}", file=sys.stderr)
        return 2

    if args.asset and args.account:
        asset_id, account_id = args.asset, args.account
    else:
        found = find_ids(row)
        if not found:
            print(f"ERROR: couldn't work out asset/account for {args.lot!r} — "
                  "pass --asset and --account", file=sys.stderr)
            return 2
        asset_id, account_id = found
    print(f"{args.lot}: GovDeals asset {asset_id} / account {account_id}")

    from deals.adapters.govdeals import GovDealsAdapter

    urls = GovDealsAdapter().fetch_gallery(asset_id, account_id)
    if not urls:
        print("ERROR: GovDeals returned no photos (asset may have closed)",
              file=sys.stderr)
        return 1
    print(f"  found {len(urls)} source photos")

    if args.dry_run:
        for url in urls:
            print(f"    {url}")
        return 0

    with tempfile.TemporaryDirectory(prefix="deal_images_") as tmp:
        files = download(urls, Path(tmp))
        if not files:
            print("ERROR: every download failed", file=sys.stderr)
            return 1
        result = listing_images.upload_lot_images(args.lot, files)

    if not result:
        print("ERROR: upload returned nothing — check R2/storage config",
              file=sys.stderr)
        return 1

    inventory.set_images(args.lot, result["hero_image_url"], result["image_urls"])
    print(f"  ✓ {len(result['image_urls'])} photos → {result['hero_image_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

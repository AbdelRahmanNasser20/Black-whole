#!/usr/bin/env python3
"""Re-publish every live lot photo through the disguise (automation/image_disguise.py).

Photos uploaded before the disguise shipped are the dewatermarked GovDeals
originals under lot-id keys (`…r2.dev/gd-239-31465/00.jpg`), and Google Lens
exact-matches some of them to GovDeals / AllSurplus. This downloads each live
R2 photo, disguises it, uploads it under an opaque `p/…` key and swaps the URL.

Covers `inventory.hero_image_url` / `image_urls` (site, CRM, FB catalog feed) and
`auction_favorites.clean_*` (the public /map — lots we are still bidding on).

- Dry-run by default; `--apply` uploads and writes.
- Idempotent: a URL already under `p/` is the marker and is skipped.
- Only our own R2 URLs are touched; anything else in a row is kept as-is.
- Old objects are NOT deleted. Every row changed is logged old→new to
  ~/.listing_automation/logs/disguise-backfill-<ts>.jsonl; `--rollback <log>`
  puts the old URLs back.
- Facebook Marketplace posts keep their old photos until re-uploaded
  (scripts/fb_replace_photos.py — writes to FB, operator's call).

    .venv/bin/python scripts/disguise_live_images.py                # plan
    .venv/bin/python scripts/disguise_live_images.py --apply        # do it
    .venv/bin/python scripts/disguise_live_images.py --lot 31225 --apply
    .venv/bin/python scripts/disguise_live_images.py --rollback <log.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import config, db, favorite_images, favorites, image_disguise, inventory  # noqa: E402
from automation import listing_images as li  # noqa: E402
from automation import r2_images  # noqa: E402


class Converter:
    """Legacy R2 URL → disguised object under an opaque key (memoised per run)."""

    def __init__(self, *, apply: bool):
        self.apply = apply
        self.cfg = r2_images.env_config()
        if not self.cfg:
            raise SystemExit("R2 is not configured — nothing to re-publish to")
        self.s3 = r2_images.client(self.cfg) if apply else None
        self.http = httpx.Client(timeout=90.0, follow_redirects=True)
        self.done: dict[tuple[str, str, bool], str] = {}
        self.stats = {"converted": 0, "kept": 0, "failed": 0}

    def ours(self, url: str | None) -> bool:
        return bool(url) and url.startswith(self.cfg["public_base"] + "/")

    def convert(self, url: str | None, lot_key: str, *, hero: bool) -> str | None:
        if not url or not self.ours(url) or li.is_disguised_url(url):
            if url:
                self.stats["kept"] += 1
            return url
        memo = (url, lot_key, hero)
        if memo in self.done:
            return self.done[memo]
        source = self.fetch(url)
        if source is None:
            self.stats["failed"] += 1
            return url
        result = image_disguise.disguise(source, key=li.key_base(lot_key))
        if result is None:
            print(f"    ! not an image, kept old URL: {url}")
            self.stats["failed"] += 1
            return url
        blob, _ext, ct = result
        path = li.opaque_hero_path(lot_key) if hero else li.opaque_gallery_path(lot_key, source)
        new = r2_images.public_url(path, public_base=self.cfg["public_base"],
                                   version=r2_images.content_version(blob))
        if self.apply and not r2_images.put_object(self.s3, bucket=self.cfg["bucket"],
                                                   path=path, data=blob, content_type=ct):
            self.stats["failed"] += 1
            return url
        if hero and self.apply:  # watermark-free twin for the FB catalog feed
            twin = image_disguise.disguise(source, key=li.key_base(lot_key), watermark=False)
            if twin:
                r2_images.put_object(self.s3, bucket=self.cfg["bucket"], path=li.catalog_path(path),
                                     data=twin[0], content_type=twin[2])
        self.stats["converted"] += 1
        self.done[memo] = new
        return new

    def fetch(self, url: str, attempts: int = 4) -> bytes | None:
        for n in range(1, attempts + 1):
            try:
                resp = self.http.get(url)
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError as e:
                if n == attempts:
                    print(f"    ! download failed {attempts}x, kept old URL: {url} ({e})")
                    return None
                time.sleep(2 * n)
        return None

    def row(self, lot_key: str, hero: str | None, gallery: list[str]) -> tuple[str | None, list[str]]:
        return (self.convert(hero, lot_key, hero=True),
                [self.convert(u, lot_key, hero=False) for u in gallery])


def _log_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(config.LOG_DIR) / f"disguise-backfill-{ts}.jsonl"


def run(args) -> int:
    conv = Converter(apply=args.apply)
    log_file = _log_path() if args.apply else None
    changed = 0

    where, params = "", ()
    if args.lot:
        where, params = "WHERE lot_id = %s", (args.lot,)
    rows = db.fetch_all(f"SELECT lot_id, status, hero_image_url, image_urls FROM inventory {where} "
                        "ORDER BY lot_id", params)
    jobs = [("inventory", r["lot_id"], r["lot_id"], r["hero_image_url"], list(r["image_urls"] or []))
            for r in rows]
    if not args.lot and favorite_images.clean_columns_available():
        for fav in favorites.list_all():
            key = favorite_images.r2_key(fav.asset_id)
            if key and (fav.clean_hero_url or fav.clean_image_urls):
                jobs.append(("auction_favorites", fav.asset_id, key,
                             fav.clean_hero_url, list(fav.clean_image_urls)))

    for table, ident, lot_key, hero, gallery in jobs:
        todo = [u for u in [hero, *gallery] if conv.ours(u) and not li.is_disguised_url(u)]
        if not todo:
            continue
        print(f"  {table}:{ident}  {len(todo)} photo(s)")
        new_hero, new_gallery = conv.row(lot_key, hero, gallery)
        if (new_hero, new_gallery) == (hero, gallery):
            continue
        changed += 1
        if not args.apply:
            continue
        entry = {"table": table, "id": ident,
                 "old": {"hero": hero, "urls": gallery},
                 "new": {"hero": new_hero, "urls": new_gallery}}
        with log_file.open("a") as fh:  # log before the write, so a crash is still reversible
            fh.write(json.dumps(entry) + "\n")
        if table == "inventory":
            inventory.set_images(ident, new_hero, new_gallery)
        else:
            favorites.set_clean_images(ident, new_hero, new_gallery)

    mode = "APPLIED" if args.apply else "dry-run (nothing uploaded or written)"
    print(f"\n{mode}: {changed} row(s) to change · photos {conv.stats}")
    if log_file and changed:
        print(f"rollback log: {log_file}")
    return 1 if conv.stats["failed"] else 0


def rollback(path: Path) -> int:
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for e in reversed(entries):
        old = e["old"]
        if e["table"] == "inventory":
            inventory.set_images(e["id"], old["hero"], old["urls"])
        else:
            favorites.set_clean_images(e["id"], old["hero"], old["urls"])
        print(f"  restored {e['table']}:{e['id']}")
    print(f"rolled back {len(entries)} row(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="upload to R2 and write the new URLs")
    ap.add_argument("--lot", help="only this inventory lot_id (skips favorites)")
    ap.add_argument("--rollback", type=Path, help="restore the old URLs from a backfill log")
    args = ap.parse_args()
    if args.rollback:
        return rollback(args.rollback)
    if not image_disguise.enabled():
        raise SystemExit("IMAGE_DISGUISE is off — refusing to re-publish undisguised photos")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

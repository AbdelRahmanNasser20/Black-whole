"""Give every inventory row a public URL slug (migration 023).

    .venv/bin/python scripts/backfill_slugs.py            # dry run: prints lot_id → slug
    .venv/bin/python scripts/backfill_slugs.py --apply    # writes rows that have no slug

Idempotent: rows that already carry a slug are left alone (URLs are promises).
Collisions get a 4-char suffix from the lot id. Safe to re-run after a bulk
import. Refuses to run when the column is missing (apply the migration first).
"""
from __future__ import annotations

import argparse
import hashlib

from automation import config  # noqa: F401  (loads .env)
from automation import inventory, lot_urls


def _suffix(lot_id: str) -> str:
    return hashlib.sha1(lot_id.encode()).hexdigest()[:4]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write slugs (default: dry run)")
    args = ap.parse_args()

    if not inventory.has_slug_column():
        print("inventory.slug is missing — apply scripts/sql/023_inventory_slug.sql first")
        return 2

    rows = inventory.list_all()
    taken = {r["slug"] for r in rows if r.get("slug")}
    todo = [r for r in rows if not r.get("slug")]
    print(f"{len(rows)} rows · {len(taken)} already slugged · {len(todo)} to do")
    written = 0
    for r in todo:
        base = lot_urls.make_slug(r)
        slug = base
        if slug in taken:
            slug = lot_urls.slugify(f"{base}-{_suffix(str(r['lot_id']))}")
        n = 2
        while slug in taken:
            slug = lot_urls.slugify(f"{base}-{_suffix(str(r['lot_id']))}-{n}")
            n += 1
        taken.add(slug)
        print(f"{r['lot_id']:<48} → {slug}")
        if args.apply:
            inventory.set_slug(str(r["lot_id"]), slug)
            written += 1
    print(f"{'wrote' if args.apply else 'would write'} {written if args.apply else len(todo)} slugs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

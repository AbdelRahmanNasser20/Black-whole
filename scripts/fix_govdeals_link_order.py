"""One-shot cleanup: repair GovDeals links built with swapped URL segments.

Why: ``_asset_to_card`` (the JSON-API fast path, PR #7, 2026-06-10) built
listing links as ``/en/asset/{accountId}/{assetId}``, but the live site's
format is ``/en/asset/{assetId}/{accountId}``. Swapped links land on
GovDeals' "Item not available" page even for live auctions, and — because
``asset_id`` (the row PK) is derived from the link — the same lot can exist
twice: once under the correct browser-scraped key and once under the swapped
API key. The scraper is fixed at the source; this repairs the rows that
already landed.

Detection is deterministic, no LLM: the CDN photo filename embeds both IDs
as ``photos/{accountId}/{accountId}_{assetId}_<uuid>.jpg``. A row whose
link's FIRST segment equals the photo directory is swapped; one whose SECOND
segment matches is already correct.

Per swapped row:
  - compute the correct link + asset_id key
  - if a row already exists under the correct key (browser-scraped twin),
    keep whichever was seen most recently and delete the other
  - otherwise rewrite link + asset_id in place

Dry-run by default; pass ``--apply`` to write.

    .venv/bin/python scripts/fix_govdeals_link_order.py [--apply]
    .venv/bin/python scripts/fix_govdeals_link_order.py --sqlite auction_extractors/state/listings.db [--apply]

Run it against Supabase AND every local listings.db cache (the transfer
script mirrors the whole SQLite table, so swapped rows left in a local cache
would resurrect in Supabase on the next sync).
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import automation.config  # noqa: F401  — loads .env (BLACKWHOLE_DB_URL)
from automation import db

LINK_RE = re.compile(r"/asset/(\d+)/(\d+)")
# photos/{accountId}/{accountId}_{assetId}_<rest>
PHOTO_RE = re.compile(r"/photos/(\d+)/\1_(\d+)_")


def classify(row: dict) -> tuple[str, str, str]:
    """Return (verdict, fixed_link, fixed_asset_id).

    verdict: 'ok' | 'swapped' | 'unknown' (no usable photo evidence).
    """
    lm = LINK_RE.search(row["link"] or "")
    pm = PHOTO_RE.search(row["image_url"] or "")
    if not lm or not pm:
        return ("unknown", "", "")
    a, b = lm.group(1), lm.group(2)
    account, asset = pm.group(1), pm.group(2)
    if a == asset and b == account:
        return ("ok", "", "")
    if a == account and b == asset:
        fixed_link = (row["link"] or "").replace(
            f"/asset/{a}/{b}", f"/asset/{asset}/{account}")
        return ("swapped", fixed_link, f"{asset}/{account}")
    return ("unknown", "", "")


def repair(rows: list[dict], *, apply: bool, fetch_twin, update_row, delete_row):
    fixed = merged = unknown = ok = 0
    for row in rows:
        verdict, fixed_link, fixed_key = classify(row)
        if verdict == "ok":
            ok += 1
            continue
        if verdict == "unknown":
            unknown += 1
            continue
        twin = fetch_twin(fixed_key)
        if twin is not None:
            # Same lot exists under the correct key. Keep the fresher row's
            # data; the correct-key slot survives either way.
            merged += 1
            keep_swapped = (row.get("last_seen_at") or "") > (twin.get("last_seen_at") or "")
            print(f"  merge {row['asset_id']} + {fixed_key} "
                  f"(keep {'api' if keep_swapped else 'browser'} data) | {row['title'][:50]}")
            if apply:
                if keep_swapped:
                    delete_row(fixed_key)
                    update_row(row["asset_id"], fixed_link, fixed_key)
                else:
                    delete_row(row["asset_id"])
        else:
            fixed += 1
            print(f"  fix   {row['asset_id']} → {fixed_key} | {row['title'][:50]}")
            if apply:
                update_row(row["asset_id"], fixed_link, fixed_key)
    print(f"\nok={ok} fixed={fixed} merged={merged} unknown={unknown} "
          f"({'applied' if apply else 'DRY RUN — pass --apply to write'})")


def run_supabase(apply: bool) -> None:
    print("== Supabase auction_listings ==")
    rows = db.fetch_all(
        "SELECT asset_id, link, title, image_url, last_seen_at::text AS last_seen_at "
        "FROM auction_listings WHERE link LIKE %s", ("%govdeals.com%",))
    print(f"{len(rows)} GovDeals rows")

    def fetch_twin(key):
        return db.fetch_one("SELECT asset_id, last_seen_at::text AS last_seen_at "
                            "FROM auction_listings WHERE asset_id = %s", (key,))

    def update_row(old_key, link, new_key):
        db.execute("UPDATE auction_listings SET asset_id = %s, link = %s "
                   "WHERE asset_id = %s", (new_key, link, old_key))

    def delete_row(key):
        db.execute("DELETE FROM auction_listings WHERE asset_id = %s", (key,))

    repair(rows, apply=apply, fetch_twin=fetch_twin,
           update_row=update_row, delete_row=delete_row)


def run_sqlite(path: Path, apply: bool) -> None:
    print(f"== SQLite {path} ==")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT asset_id, link, title, image_url, last_seen_at "
        "FROM listings WHERE link LIKE '%govdeals.com%'")]
    print(f"{len(rows)} GovDeals rows")

    def fetch_twin(key):
        r = conn.execute("SELECT asset_id, last_seen_at FROM listings "
                         "WHERE asset_id = ?", (key,)).fetchone()
        return dict(r) if r else None

    def update_row(old_key, link, new_key):
        conn.execute("UPDATE listings SET asset_id = ?, link = ? "
                     "WHERE asset_id = ?", (new_key, link, old_key))

    def delete_row(key):
        conn.execute("DELETE FROM listings WHERE asset_id = ?", (key,))

    repair(rows, apply=apply, fetch_twin=fetch_twin,
           update_row=update_row, delete_row=delete_row)
    if apply:
        conn.commit()
    conn.close()


def main() -> None:
    apply = "--apply" in sys.argv
    if "--sqlite" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--sqlite") + 1])
        if not path.exists():
            raise SystemExit(f"not found: {path}")
        run_sqlite(path, apply)
    else:
        run_supabase(apply)


if __name__ == "__main__":
    main()

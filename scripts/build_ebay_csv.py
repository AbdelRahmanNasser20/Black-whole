"""Build the eBay Seller Hub bulk-upload CSV from live inventory (BLACKWHOLE-9).

One row per sellable lot (everything `inventory.list_catalog_feed()` returns —
listed / owned / won_pickup with stock). Rows already live on eBay (a stored
`ebay_url`) become ``Action=Revise`` + ``ItemID``; the rest become
``Action=Add``. Photos are the durable Supabase URLs; the black-whole.com
backlink is on every listing. No LLM, no browser, read-only DB.

Usage:
    python scripts/build_ebay_csv.py [--out PATH] [--quantity N] [--all]

Defaults write `catalog/ebay_bulk_upload.csv`. Upload it in Seller Hub →
Listings → Upload (bulk). ``--all`` exports every inventory row instead of just
the sellable feed set (useful for reconciliation review).

⚠️ Before the CSV imports cleanly, set the operator env vars documented in
`automation/listing_csv.py` (category id + Business Policy names).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from automation import config  # noqa: F401  (loads .env)
from automation import inventory, listing_csv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "catalog" / "ebay_bulk_upload.csv")
    ap.add_argument("--quantity", type=int, default=listing_csv.DEFAULT_QUANTITY,
                    help="listing quantity per row (lead-gen default: 1)")
    ap.add_argument("--all", action="store_true",
                    help="export every inventory row, not just the sellable feed")
    a = ap.parse_args()

    rows = inventory.list_all() if a.all else inventory.list_catalog_feed()
    eligible = listing_csv.build_rows(rows, quantity=a.quantity)
    csv_text = listing_csv.rows_to_csv(rows, quantity=a.quantity)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(csv_text, encoding="utf-8")

    adds = sum(1 for r in eligible if r["Action"] == listing_csv.ACTION_ADD)
    revises = len(eligible) - adds
    dropped = len(rows) - len(eligible)
    print(f"[ebay-csv] {len(rows)} lots in → {len(eligible)} rows "
          f"({adds} Add, {revises} Revise), {dropped} dropped (no price/photo)")
    if not listing_csv.banquet_category_id():
        print("[ebay-csv] WARNING: EBAY_BANQUET_CATEGORY_ID unset — Category "
              "column is blank; eBay will reject Add rows until you set it.")
    print(f"[ebay-csv] wrote {a.out}")


if __name__ == "__main__":
    main()

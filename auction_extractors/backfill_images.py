#!/usr/bin/env python3
"""
One-shot: visit each GovDeals listing with an empty or invalid ``image_url``
and pull the real product photo URL. Does NOT re-fetch descriptions; only
updates ``image_url`` on rows we haven't already successfully captured.

Prior scrape runs populated ``image_url`` with branding logos instead of
product photos because the selectors didn't match. Running this once after
the fix in ``_fetch_govdeals_long_description`` retroactively corrects all
cached rows.

Usage: .venv/bin/python backfill_images.py [--min-qty 50] [--limit 0]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from playwright.sync_api import sync_playwright

from govdeals_chairs_extraction import _fetch_govdeals_long_description
from govdeals_playwright_helpers import (
    govdeals_browser_context,
    govdeals_chromium_launch_args,
)
from paths import STATE_DIR


def _needs_backfill(image_url: str) -> bool:
    if not image_url:
        return True
    low = image_url.lower()
    # The old run stored these — they're branding logos, not products.
    return "/brands/" in low or "logo-" in low or "/images/icons/" in low


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-qty", type=int, default=50,
                    help="only backfill rows with quantity >= this (default 50)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap rows processed this run (0 = all)")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(STATE_DIR / "listings.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT asset_id, link, image_url FROM listings
        WHERE link LIKE '%govdeals.com%' AND quantity >= ?
        ORDER BY quantity DESC
        """,
        (args.min_qty,),
    ).fetchall()
    targets = [r for r in rows if _needs_backfill(r["image_url"] or "")]
    if args.limit:
        targets = targets[: args.limit]

    print(f"{len(targets)}/{len(rows)} rows need backfill (quantity ≥ {args.min_qty})")
    if not targets:
        return 0

    fixed = err = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless, args=govdeals_chromium_launch_args()
        )
        ctx = govdeals_browser_context(browser)
        page = ctx.new_page()
        try:
            for i, row in enumerate(targets, 1):
                try:
                    _desc, img = _fetch_govdeals_long_description(page, row["link"])
                except Exception as e:
                    err += 1
                    print(f"  [{i}/{len(targets)}] {row['asset_id']}  ERROR: {e}")
                    continue
                if img:
                    conn.execute(
                        "UPDATE listings SET image_url = ? WHERE asset_id = ?",
                        (img, row["asset_id"]),
                    )
                    conn.commit()
                    fixed += 1
                    if i % 10 == 0 or i == 1:
                        print(f"  [{i}/{len(targets)}] {row['asset_id']}  OK")
                else:
                    print(f"  [{i}/{len(targets)}] {row['asset_id']}  no image found")
        finally:
            browser.close()
            conn.close()

    print(f"\nfixed {fixed}, errors {err}, total scanned {len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

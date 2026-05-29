#!/usr/bin/env python3
"""One-off backfill: re-fetch the top-N highest-quantity active GovDeals
listings from the cache and update their ``pickup_zip`` / ``contact_email`` /
``contact_phone`` columns.

Why a separate script: the full scraper (``govdeals_chairs_extraction.py``)
re-walks search results from scratch — way too slow for "just freshen these
20 rows." This script reads existing rows out of ``state/listings.db``,
opens each asset URL once in Playwright, and upserts the contact columns
back in. Description / image / quantity already in the cache are preserved.

Usage:
    cd auction_extractors
    ../.venv/bin/python refresh_top_contacts.py            # default top 20
    ../.venv/bin/python refresh_top_contacts.py --n 50     # top 50
    ../.venv/bin/python refresh_top_contacts.py --no-active-only  # include expired

Env: ``HEADLESS=0`` (default) — GovDeals blocks headless via Akamai.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

import listings_db
from govdeals_chairs_extraction import _fetch_govdeals_long_description
from govdeals_playwright_helpers import (
    govdeals_browser_context,
    govdeals_chromium_launch_args,
)


def _is_govdeals(link: str) -> bool:
    return "govdeals.com" in (link or "")


def _select_targets(n: int, active_only: bool, max_stale_days: int) -> list[dict]:
    """Top-N by quantity. Active filter mirrors top_chairs._is_active."""
    conn = listings_db.connect()
    try:
        rows = conn.execute(
            """
            SELECT asset_id, link, title, quantity, end_date, last_seen_at
            FROM listings
            WHERE quantity IS NOT NULL
              AND link LIKE '%govdeals.com%'
            ORDER BY quantity DESC
            LIMIT ?
            """,
            (max(1, n * 4),),  # over-pull so the active filter still lands ≥ n
        ).fetchall()
    finally:
        conn.close()

    items = [dict(r) for r in rows]
    if active_only:
        from dateutil import parser as _dp
        now = datetime.now(timezone.utc)
        kept = []
        for it in items:
            end = (it.get("end_date") or "").strip()
            if end:
                try:
                    dt = _dp.parse(end, fuzzy=True)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now:
                        continue
                except Exception:
                    pass
            last = (it.get("last_seen_at") or "").strip()
            if last:
                try:
                    ls = datetime.fromisoformat(last)
                    if ls.tzinfo is None:
                        ls = ls.replace(tzinfo=timezone.utc)
                    if (now - ls).days > max_stale_days:
                        continue
                except Exception:
                    pass
            kept.append(it)
        items = kept

    return items[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20, help="how many top-quantity rows to refresh")
    ap.add_argument("--max-stale-days", type=int, default=7,
                    help="active-only staleness window (default 7d)")
    ap.add_argument("--no-active-only", action="store_true",
                    help="include expired/stale rows too")
    ap.add_argument("--delay-sec", type=float, default=0.5,
                    help="sleep between page loads (be polite)")
    args = ap.parse_args()

    targets = _select_targets(
        n=args.n,
        active_only=not args.no_active_only,
        max_stale_days=args.max_stale_days,
    )
    if not targets:
        print("No matching rows in cache. Run the scraper first.")
        return 1

    print(f"Refreshing contacts for {len(targets)} top-quantity GovDeals lots:")
    for i, it in enumerate(targets, 1):
        print(f"  {i:>2}. qty={it['quantity']:>5}  {(it['title'] or '')[:60]}")

    headless = os.getenv("HEADLESS", "0") == "1"
    updated = 0
    failed: list[tuple[str, str]] = []
    conn = listings_db.connect()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=govdeals_chromium_launch_args(),
        )
        ctx = govdeals_browser_context(browser)
        page = ctx.new_page()
        try:
            for i, it in enumerate(targets, 1):
                link = it.get("link")
                if not _is_govdeals(link):
                    failed.append((it.get("asset_id", "?"), "not_govdeals"))
                    continue
                print(f"\n[{i}/{len(targets)}] {link}")
                try:
                    desc, img, zip_code, email, phone = _fetch_govdeals_long_description(page, link)
                except Exception as e:
                    print(f"   • fetch failed: {e}")
                    failed.append((it.get("asset_id", "?"), repr(e)))
                    continue

                # We don't want to clobber description/image with empties just
                # because the model rendered slowly — listings_db.upsert_listing
                # already preserves a non-empty cached description on update.
                listings_db.upsert_listing(
                    conn,
                    {
                        "link": link,
                        "title": it.get("title") or "",
                        "description": desc,
                        "quantity": it.get("quantity"),
                        "image_url": img,
                        "pickup_zip": zip_code,
                        "contact_email": email,
                        "contact_phone": phone,
                    },
                )
                updated += 1
                print(f"   zip={zip_code or '—'}  email={email or '—'}  phone={phone or '—'}")
                if args.delay_sec > 0:
                    time.sleep(args.delay_sec)
        finally:
            browser.close()
            conn.close()

    print(f"\nDone. Updated {updated}/{len(targets)}.")
    if failed:
        print(f"Failures ({len(failed)}):")
        for asset_id, reason in failed:
            print(f"  {asset_id}: {reason}")
    return 0 if updated else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read the seller's own Marketplace listings and map them back to plan SKUs.

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/fb_my_listings.py            # print what's live
      ./.venv/bin/python scripts/fb_my_listings.py --write    # + backfill the plan JSON

Why this exists: after `post_fb_listing.py --publish`, Facebook redirects to
`/marketplace/you/selling`, not to the new item, so the item id is not in the URL.
The listing IS live — it just has to be read back off the Selling page. Matching
is by title, which is unique across the plan.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import browser  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "fb_relist_plan_2026-08-24.json"
SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
PROFILE_URL = "https://www.facebook.com/marketplace/profile/{pid}/"


async def scrape_selling(page, pid: str) -> list[dict]:
    """-> [{item_id, text}] for every live listing on the seller's PUBLIC profile.

    Not the Selling page: that surface renders an ``?listing_id=`` edit link only
    for *drafts*. Active listings there carry Mark-as-sold / Boost / Share and no
    id at all. The public Marketplace profile is the one place every live item is
    a real ``/marketplace/item/<id>`` anchor.
    """
    await page.goto(PROFILE_URL.format(pid=pid), wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    # The profile list lazy-loads; 4 scrolls stopped at 33 items and silently
    # reported freshly-published listings as "not live yet".
    for _ in range(12):
        await page.mouse.wheel(0, 2200)
        await page.wait_for_timeout(1000)
    return await page.evaluate(
        """() => {
            // The Selling page renders no /marketplace/item/ anchors — each card's
            // only stable id is its "Edit" link, ?listing_id=<id>, and that id IS
            // the public item id. Fall back to item anchors for other surfaces.
            const seen = new Map();
            const add = (id, node) => {
                if (!id || seen.has(id)) return;
                let n = node, text = '';
                for (let i = 0; i < 8 && n; i++) {
                    text = (n.innerText || '').trim();
                    if (text.length > 20) break;
                    n = n.parentElement;
                }
                seen.set(id, {item_id: id, text});
            };
            for (const a of document.querySelectorAll('a[href*="listing_id="]')) {
                const m = a.getAttribute('href').match(/listing_id=(\\d+)/);
                if (m) add(m[1], a);
            }
            for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
                const m = a.getAttribute('href').match(/\\/marketplace\\/item\\/(\\d+)/);
                if (m) add(m[1], a);
            }
            return [...seen.values()];
        }"""
    )


def match(cards: list[dict], listings: list[dict]) -> dict[str, str]:
    """title -> item url. Title match is exact-substring, case-insensitive."""
    out = {}
    for l in listings:
        t = l["title"].strip().lower()
        for c in cards:
            if t and t in (c["text"] or "").lower():
                out[l["sku"]] = f"https://www.facebook.com/marketplace/item/{c['item_id']}/"
                break
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write fb_listing_url back into the plan")
    a = ap.parse_args()

    plan = json.loads(PLAN.read_text())
    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()
        cards = await scrape_selling(page, plan["account"]["profile_id"])

    print(f"{len(cards)} live items on profile {plan['account']['profile_id']}")
    for c in cards:
        first = (c["text"] or "").splitlines()
        print(f"  {c['item_id']}  {(first[0] if first else '')[:60]}")

    found = match(cards, plan["listings"])
    print(f"\nmatched {len(found)}/{len(plan['listings'])} plan listings")
    for sku, url in found.items():
        print(f"  {sku:45s} {url}")

    missing = [l["sku"] for l in plan["listings"] if l["sku"] not in found]
    if missing:
        print(f"\nnot live yet: {', '.join(missing)}")

    if a.write and found:
        for l in plan["listings"]:
            if l["sku"] in found:
                l["fb_listing_url"] = found[l["sku"]]
        PLAN.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        print(f"\nwrote {len(found)} URLs into {PLAN.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

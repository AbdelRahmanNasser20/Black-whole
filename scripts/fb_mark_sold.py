#!/usr/bin/env python3
"""Mark one of our live Marketplace listings as SOLD, from the plan JSON.

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/fb_mark_sold.py --sku bs-10118-metal            # find + screenshot
      ./.venv/bin/python scripts/fb_mark_sold.py --sku bs-10118-metal --confirm  # actually mark sold

Why "Mark as sold" and not delete: mass deletions and Delete-&-Relist are the
documented spam-filter triggers on Marketplace (that is how the main account got
its restriction). Marking sold is the action a real seller takes when stock moves,
hides the item from search, and keeps the thread history for the CRM.

One attempt. If Facebook's DOM has moved, it prints the manual path and exits 1.
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
from automation.config import CHROME_PROFILE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "fb_relist_plan_2026-08-24.json"
SELLING_URL = "https://www.facebook.com/marketplace/you/selling"


def load(sku: str) -> dict:
    plan = json.loads(PLAN.read_text())
    for l in plan["listings"]:
        if l["sku"] == sku:
            return l
    raise SystemExit(f"sku {sku!r} not in plan")


async def shoot(page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=False)
        print(f"  screenshot: {path}")
    except Exception:
        pass


async def _dismiss(page) -> None:
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            return


async def find_card_button(page, title: str, label: str):
    """The 'Mark as sold' control that belongs to the card whose text carries `title`.

    Cards on /you/selling have no ids; the only stable structure is "a container
    that holds BOTH the listing title and a Mark-as-sold control". Walk up from the
    title node until such a container is found, then take its control.
    """
    handle = await page.evaluate_handle(
        """([title, label]) => {
            const t = title.trim().toLowerCase();
            const l = label.toLowerCase();
            const isCtl = (n) => {
                const txt = (n.innerText || n.getAttribute('aria-label') || '').trim().toLowerCase();
                return txt === l || txt.startsWith(l);
            };
            const ctlIn = (root) => {
                const q = root.querySelectorAll('[role=button], button, a, div[aria-label]');
                for (const n of q) if (isCtl(n)) return n;
                return null;
            };
            const nodes = [...document.querySelectorAll('span, div, a')]
                .filter(n => n.children.length <= 2 && (n.innerText || '').trim().toLowerCase().includes(t));
            for (const start of nodes) {
                let n = start;
                for (let i = 0; i < 10 && n; i++) {
                    const c = ctlIn(n);
                    if (c) return c;
                    n = n.parentElement;
                }
            }
            return null;
        }""",
        [title, label],
    )
    el = handle.as_element()
    return el


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", required=True)
    ap.add_argument("--confirm", action="store_true", help="actually mark it sold (default: locate + screenshot)")
    a = ap.parse_args()

    spec = load(a.sku)
    title = spec["title"]
    item_url = spec.get("fb_listing_url") or ""
    print(f"sku={a.sku}  {title!r}\n  item: {item_url or '(none recorded)'}\n  profile: {CHROME_PROFILE}")
    shot = ROOT / f"_fb_sold_{a.sku.replace(':', '_')}.png"

    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()
        await page.goto(SELLING_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        body = (await page.inner_text("body")).lower()
        if "can't buy or sell" in body or "log in" in body[:2000]:
            print("BLOCKED or logged out — see screenshot")
            await shoot(page, shot)
            return 1
        # lazy list: scroll until the title is on the page (or give up)
        for _ in range(10):
            if title.lower() in (await page.inner_text("body")).lower():
                break
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(900)
        await _dismiss(page)

        btn = await find_card_button(page, title, "Mark as sold")
        if not btn:
            # already sold? the control flips to "Mark as available"
            if await find_card_button(page, title, "Mark as available"):
                print("  = already marked sold")
                return 0
            print(f"  ! could not find a 'Mark as sold' control for {title!r}")
            print(f"  manual path: {item_url or SELLING_URL} → ··· → Mark as sold")
            await shoot(page, shot)
            return 1
        await btn.scroll_into_view_if_needed()
        if not a.confirm:
            print("  found 'Mark as sold' for this card (dry run — pass --confirm)")
            await shoot(page, shot)
            return 0
        await btn.click()
        await page.wait_for_timeout(2500)

        # FB asks "Who bought this?" — no buyer chosen, just confirm.
        for label in ("Mark as sold", "Done", "Skip", "Confirm"):
            try:
                loc = page.locator(f"[role=dialog] [role=button]:has-text('{label}'), "
                                   f"[role=dialog] button:has-text('{label}')").first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        for _ in range(8):
            if title.lower() in (await page.inner_text("body")).lower():
                break
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(800)
        ok = await find_card_button(page, title, "Mark as available")
        await shoot(page, shot)
        if ok:
            print("  ✓ marked as sold")
            return 0
        print("  ? clicked, but the card does not show 'Mark as available' yet — check the screenshot")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

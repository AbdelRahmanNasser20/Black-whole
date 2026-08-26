#!/usr/bin/env python3
"""Swap the photos on an ALREADY-PUBLISHED Marketplace listing for the plan's current set.

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/fb_replace_photos.py --sku gd-32876-2          # dry run
      ./.venv/bin/python scripts/fb_replace_photos.py --sku gd-32876-2 --save

Why: two listings went up with the seller's watermarked photos before the
dewatermark step was wired in (2026-08-26). Editing in place keeps the item id,
the thread history and the listing's age — re-posting would be Delete & Relist,
the documented spam-filter trigger.

Title, price, description, location, category are not touched.
One attempt; if the edit form's photo controls have moved, print the manual path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from automation import browser  # noqa: E402
from post_fb_listing import fetch_photos  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "fb_relist_plan_2026-08-24.json"
EDIT_URL = "https://www.facebook.com/marketplace/edit/?listing_id={lid}"


def load(sku: str) -> dict:
    plan = json.loads(PLAN.read_text())
    for l in plan["listings"]:
        if l["sku"] == sku:
            if not l.get("fb_listing_url"):
                raise SystemExit(f"{sku} has no fb_listing_url — not published")
            return l
    raise SystemExit(f"sku {sku!r} not in plan")


async def _dismiss(page) -> None:
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            return


REMOVE_JS = """() => [...document.querySelectorAll('[role=button][aria-label], button[aria-label], div[aria-label]')]
        .filter(n => /remove|delete/i.test(n.getAttribute('aria-label') || '')
                  && /photo|image|picture/i.test(n.getAttribute('aria-label') || ''))"""


async def count_photos(page) -> int:
    """Thumbnails on the edit form each carry a remove (×) control; count those.
    Falls back to the "Photos · N/10" caption when the controls carry no label."""
    n = await page.evaluate(REMOVE_JS + ".length")
    if n:
        return n
    try:
        txt = await page.inner_text("body")
        m = re.search(r"Photos\s*[·•]\s*(\d+)\s*/\s*\d+", txt)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


async def remove_all_photos(page) -> int:
    removed = 0
    for _ in range(40):
        handle = await page.evaluate_handle(REMOVE_JS + "[0] || null")
        el = handle.as_element()
        if el is None:
            # unlabeled ×: the first [role=button] inside the photos grid, before "Add photo"
            handle = await page.evaluate_handle(
                """() => {
                    const add = [...document.querySelectorAll('div, span')].find(n => /^Add photo$/i.test((n.innerText||'').trim()));
                    if (!add) return null;
                    let grid = add; for (let i = 0; i < 6 && grid; i++) grid = grid.parentElement;
                    if (!grid) return null;
                    const btns = [...grid.querySelectorAll('[role=button]')].filter(b => !/add photo|add video/i.test(b.innerText||''));
                    return btns.find(b => b.getBoundingClientRect().width < 40) || null;
                }""")
            el = handle.as_element()
        if el is None:
            break
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.click(timeout=4000)
            removed += 1
            await page.wait_for_timeout(500)
        except Exception:
            break
    return removed


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", required=True)
    ap.add_argument("--save", action="store_true", help="actually replace + save (default: inspect)")
    a = ap.parse_args()

    spec = load(a.sku)
    lid = re.search(r"/item/(\d+)", spec["fb_listing_url"]).group(1)
    shot = ROOT / f"_fb_photos_{a.sku.replace(':', '_')}.png"
    print(f"sku={a.sku}  listing_id={lid}  {len(spec['photo_urls'])} plan photos")

    tmp = Path(tempfile.mkdtemp(prefix=f"fbphotos_{a.sku.replace(':', '_')}_"))
    photos = fetch_photos(spec["photo_urls"], tmp)
    print(f"  downloaded {len(photos)} clean photos -> {tmp}")

    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()
        await page.goto(EDIT_URL.format(lid=lid), wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await _dismiss(page)

        have = await count_photos(page)
        fi = await page.query_selector("input[type=file][accept*='image']")
        print(f"  live photos on form: {have}   add-photos input: {'yes' if fi else 'NO'}")
        if not a.save:
            await page.screenshot(path=str(shot))
            print(f"  screenshot: {shot}\nDRY RUN — re-run with --save to replace.")
            return 0 if (have and fi) else 1
        if not fi:
            print(f"  ! no photo input on the edit form — manual path: {spec['fb_listing_url']} → Edit → photos")
            await page.screenshot(path=str(shot))
            return 1

        removed = await remove_all_photos(page)
        print(f"  removed {removed} old photos")
        fi = await page.query_selector("input[type=file][accept*='image']")
        await fi.set_input_files([str(p) for p in photos])
        await page.wait_for_timeout(1500 * len(photos) + 3000)
        now = await count_photos(page)
        print(f"  uploaded {len(photos)} → form shows {now}")
        if now < len(photos):
            print("  ! fewer thumbnails than uploaded files — NOT saving; check the screenshot")
            await page.screenshot(path=str(shot))
            return 1

        await _dismiss(page)
        for name in ("Save", "Update", "Publish", "Next"):
            try:
                btn = page.get_by_role("button", name=name, exact=True).first
                await btn.scroll_into_view_if_needed(timeout=4000)
                await btn.click(timeout=6000)
                print(f"  clicked {name}")
                await page.wait_for_timeout(6000)
                break
            except Exception:
                continue
        else:
            print("  [!] no Save/Update button found — NOT saved")
            await page.screenshot(path=str(shot))
            return 1
        await page.screenshot(path=str(shot))
        print(f"SAVED: {spec['fb_listing_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

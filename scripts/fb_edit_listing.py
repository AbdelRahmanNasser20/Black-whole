#!/usr/bin/env python3
"""Push a plan listing's current copy onto an ALREADY-PUBLISHED FB listing.

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/fb_edit_listing.py --sku 31225            # dry run
      ./.venv/bin/python scripts/fb_edit_listing.py --sku 31225 --save

Why: the plan JSON is the source of truth for listing copy, and it changes
after a listing is already live (the 2026-08-25 pre-order removal is the
motivating case). Re-posting would duplicate the listing; this edits in place.

Only Title, Price and Description are touched. Photos, category, condition and
location are left exactly as published — they were verified at post time.
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
EDIT_URL = "https://www.facebook.com/marketplace/edit/?listing_id={lid}"


def load(sku: str) -> dict:
    plan = json.loads(PLAN.read_text())
    for l in plan["listings"]:
        if l["sku"] == sku:
            if not l.get("fb_listing_url"):
                raise SystemExit(f"{sku} has no fb_listing_url — it is not published yet")
            return l
    raise SystemExit(f"sku {sku!r} not in plan")


async def _dismiss(page) -> None:
    """A stray FB flyout swallows clicks on the edit dialog, same as on create."""
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
        except Exception:
            return


async def field(page, label: str):
    """Resolve a field STRUCTURALLY, not by label text.

    The edit form's inputs carry no aria-label, no placeholder and no `for=`
    association, so `label:has-text('Price') textarea` happily matched the
    DESCRIPTION textarea — every read came back crossed. The form's real shape
    is stable and simple: two text inputs (Title, then Price) and one textarea
    (Description), with Location the only aria-labelled field.
    """
    await page.locator("textarea").first.wait_for(state="visible", timeout=8000)
    if label == "Description":
        return page.locator("textarea").first
    inputs = page.locator("input[type=text]:not([aria-label])")
    idx = {"Title": 0, "Price": 1}.get(label)
    if idx is None or await inputs.count() <= idx:
        return None
    return inputs.nth(idx)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", required=True)
    ap.add_argument("--save", action="store_true", help="actually save (default: show the diff and stop)")
    a = ap.parse_args()

    spec = load(a.sku)
    lid = re.search(r"/item/(\d+)", spec["fb_listing_url"]).group(1)
    print(f"sku={spec['sku']}  listing_id={lid}")

    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()
        await page.goto(EDIT_URL.format(lid=lid), wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await _dismiss(page)

        changed = []
        for label, want in (("Title", spec["title"]),
                            ("Price", str(spec["price"])),
                            ("Description", spec["description"])):
            el = await field(page, label)
            if el is None:
                print(f"  [!] no {label} field on the edit form")
                continue
            got = (await el.input_value()) or ""
            # FB renders Price as "$25" while the plan stores 25 — compare the
            # number, not the formatting, or every run "changes" an unchanged price.
            norm = (lambda v: v.replace("$", "").replace(",", "").strip()) if label == "Price" \
                else (lambda v: v.strip())
            if norm(got) == norm(want):
                print(f"  {label}: already current")
                continue
            print(f"  {label}: CHANGES")
            print(f"    live -> {got[:90]!r}")
            print(f"    plan -> {want[:90]!r}")
            changed.append(label)
            if a.save:
                await el.click()
                await el.fill(want)
                await page.wait_for_timeout(500)

        if not changed:
            print("nothing to change.")
            return 0
        if not a.save:
            print(f"\nDRY RUN — would update: {', '.join(changed)}. Re-run with --save.")
            return 0

        await _dismiss(page)
        for name in ("Save", "Update", "Publish", "Next"):
            try:
                btn = page.get_by_role("button", name=name, exact=True).first
                await btn.scroll_into_view_if_needed(timeout=4000)
                await btn.click(timeout=6000)
                print(f"  clicked {name}")
                await page.wait_for_timeout(5000)
                break
            except Exception:
                continue
        else:
            print("  [!] no Save/Update button found — NOT saved")
            return 1
        print(f"SAVED: {spec['fb_listing_url']}")
        await page.wait_for_timeout(3000)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

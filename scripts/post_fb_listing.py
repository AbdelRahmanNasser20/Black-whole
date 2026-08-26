#!/usr/bin/env python3
"""Post one listing from the relist plan to Facebook Marketplace, verbatim.

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/post_fb_listing.py --sku 9006            # fill + stop
      ./.venv/bin/python scripts/post_fb_listing.py --sku 9006 --publish  # fill + publish

Why not `automation.facebook.create_draft`: that renders its own title/description
from templates. The plan's copy was hand-written and machine-verified (prices, cities,
quantities, pre-order language). This posts **exactly** the planned copy — the whole
point of the verifier is that what ships equals what was checked.

Default is fill-and-stop with a screenshot, so a human sees it before it is public.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from automation import browser  # noqa: E402
from automation.facebook import _ensure_hide_from_friends_on  # noqa: E402
from automation.config import CHROME_PROFILE  # noqa: E402
from fb_my_listings import scrape_selling, match  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "fb_relist_plan_2026-08-24.json"
CREATE_URL = "https://www.facebook.com/marketplace/create/item"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def load(sku: str) -> dict:
    plan = json.loads(PLAN.read_text())
    for l in plan["listings"]:
        if l["sku"] == sku:
            return l
    raise SystemExit(f"sku {sku!r} not in plan. Have: {[l['sku'] for l in plan['listings']]}")


def fetch_photos(urls: list[str], dest: Path) -> list[Path]:
    out = []
    for i, u in enumerate(urls):
        ext = ".jpg" if ".png" not in u.lower() else ".png"
        p = dest / f"{i:02d}{ext}"
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as r:
            data = r.read()
        if len(data) < 2000:
            raise SystemExit(f"photo too small, refusing to post: {u} ({len(data)}B)")
        p.write_bytes(data)
        out.append(p)
    return out


async def fill_label(page, label: str, value: str, timeout: int = 8000) -> bool:
    for sel in (f"label:has-text('{label}') input", f"label:has-text('{label}') textarea"):
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
            await el.fill(value)
            return True
        except Exception:
            continue
    print(f"  [!] could not fill {label!r}")
    return False


async def pick_combo(page, label: str, *candidates: str) -> bool:
    """FB's Category/Condition are custom dropdowns, not <select>.

    Click the field, wait for the real [role=listbox], click the first option whose
    text matches any candidate. Typing does NOT filter these, so never type into them.
    """
    try:
        field = page.get_by_role("combobox", name=re.compile(label, re.I)).first
        await field.click(timeout=6000)
    except Exception:
        try:
            field = page.locator(f"label:has-text('{label}')").first
            await field.click(timeout=6000)
        except Exception as e:
            print(f"  [!] {label}: could not open ({type(e).__name__})")
            return False
    await page.wait_for_timeout(1200)
    for want in candidates:
        try:
            opt = page.get_by_role("option", name=re.compile(re.escape(want), re.I)).first
            await opt.click(timeout=4000)
            await page.wait_for_timeout(700)
            print(f"  {label} -> {want}")
            return True
        except Exception:
            continue
    print(f"  [!] {label}: no option matched {candidates}")
    await page.keyboard.press("Escape")
    return False


async def pick_category(page, *path: str) -> bool:
    """Category is a DIALOG, not a dropdown.

    Clicking the Category combobox opens a [role=dialog] listing top-level
    buckets (Home & Garden / Tools / Furniture / Household / …). There is no
    role=option and no "Chairs" leaf — "Furniture" is the chair bucket and
    clicking it selects and closes the dialog in one step.
    """
    try:
        cb = page.get_by_role("combobox").filter(has_text="Category").first
        await cb.click(timeout=8000)
        await page.wait_for_timeout(1800)
    except Exception as e:
        print(f"  [!] Category: could not open ({type(e).__name__})")
        return False
    for step in path:
        try:
            dlg = page.locator("[role=dialog]").filter(has_text=step).first
            await dlg.get_by_text(step, exact=True).first.click(timeout=6000)
            await page.wait_for_timeout(1600)
        except Exception:
            continue
    try:
        got = (await page.get_by_role("combobox").filter(has_text="Category").first.inner_text()).replace("\xa0", " ").strip()
        got = got.replace("Category", "").strip()
    except Exception:
        got = ""
    if got:
        print(f"  Category -> {got}")
        return True
    print("  [!] Category: still empty after drilling")
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def set_location(page, city: str, state: str) -> bool:
    """Location is a typeahead: type the city, then pick the suggestion.

    Retried, because the suggestion list arrives over the network in batches and
    a single 2s wait only ever sees whichever batch landed first. "Arlington"
    returns NJ / CA / MA / FL / RI before it returns VA, and "Fort Myer" matched
    on one run and missed on the next — both aborted a fully-filled form for a
    place Facebook does in fact know. Each attempt waits longer, and when the
    name-filtered locator comes up empty it scans the rendered option texts
    itself (the accessible name is not always what the row visibly reads).
    """
    # The row reads "Nashville, Tennessee" for some places and "Fort Myer, VA"
    # for others — accept the code or the full name (2026-08-26).
    from automation.catalog_feed import _STATE_CODES
    code = state.strip().upper()
    names = {code} | {n for n, c in _STATE_CODES.items() if c == code} | {state.strip()}
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pat = re.compile(f"{re.escape(city)}.*(?:{alt})", re.I)
    for attempt in range(3):
        for name in ("Location", "Add location"):
            try:
                box = page.get_by_label(name, exact=False).first
                await box.click(timeout=5000)
                await box.fill("")
                await page.keyboard.type(city, delay=80)
                await page.wait_for_timeout(2500 + 2000 * attempt)
                try:
                    await page.get_by_role("option", name=pat).first.click(timeout=4000)
                except Exception:
                    opts = page.get_by_role("option")
                    hit = None
                    for i in range(await opts.count()):
                        txt = " ".join((await opts.nth(i).inner_text()).split())
                        if pat.search(txt):
                            hit = opts.nth(i)
                            break
                    if hit is None:
                        raise RuntimeError("no option matched")
                    await hit.scroll_into_view_if_needed(timeout=4000)
                    await hit.click(timeout=5000)
                await page.wait_for_timeout(700)
                print(f"  Location -> {city}, {state}")
                return True
            except Exception:
                continue
    print(f"  [!] Location: could not set {city}, {state}")
    return False


async def shoot(page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), timeout=10000, animations="disabled")
        print(f"  screenshot: {path}")
    except Exception as e:
        print(f"  (screenshot skipped: {type(e).__name__})")


async def _dismiss_overlays(page) -> None:
    """Close any FB flyout sitting on top of the audience step.

    The Atlanta lot failed with a live `Publish` button on the page: a 5-item
    notifications panel was open over it, so every click landed on the panel.
    Escape closes the flyout; the click then reaches the button.
    """
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
        except Exception:
            return


async def finish_publish(page) -> bool:
    """Create form -> Next -> the `?step=audience` screen -> Publish.

    Publish is NOT reliably on screen the moment the audience step renders, and
    it can sit below the fold. A single 8s `get_by_role().click()` silently gave
    up here and left a filled-but-unpublished listing. Retry, scroll, and dump
    the real button labels before giving up so the next failure is diagnosable.
    """
    for attempt in range(4):
        try:
            await page.get_by_role("button", name="Next", exact=True).first.click(timeout=5000)
            print("  clicked Next")
            await page.wait_for_timeout(3500)
        except Exception:
            pass
        await _dismiss_overlays(page)
        for name in ("Publish", "Post"):
            btn = page.get_by_role("button", name=name, exact=True).first
            for force in (False, True):
                try:
                    await btn.scroll_into_view_if_needed(timeout=4000)
                    await btn.click(timeout=6000, force=force)
                    print(f"  clicked {name}{' (forced)' if force else ''}")
                    await page.wait_for_timeout(6000)
                    return True
                except Exception:
                    continue
        await page.wait_for_timeout(3000)
        if attempt == 2:
            try:
                labels = await page.evaluate(
                    "()=>[...document.querySelectorAll('[role=button],button')]"
                    ".map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim())"
                    ".filter(t=>t&&t.length<40).slice(0,40)"
                )
                print(f"  [debug] buttons on {page.url}: {labels}")
            except Exception:
                pass
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", required=True)
    ap.add_argument("--publish", action="store_true", help="actually publish (default: fill and stop)")
    ap.add_argument("--keep-open", type=int, default=25, help="seconds to leave the browser up at the end")
    a = ap.parse_args()

    spec = load(a.sku)
    if "<PROFILE_ID>" in spec["description"]:
        raise SystemExit("description still has <PROFILE_ID> — fill it in the plan JSON first")

    print(f"sku={spec['sku']}  {spec['title']!r}")
    print(f"  ${spec['price']} · {spec['city']}, {spec['state']} · {len(spec['photo_urls'])} photos")
    print(f"  profile: {CHROME_PROFILE}")

    tmp = Path(tempfile.mkdtemp(prefix=f"fbpost_{spec['sku'].replace(':','_')}_"))
    photos = fetch_photos(spec["photo_urls"], tmp)
    print(f"  downloaded {len(photos)} photos -> {tmp}")

    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()
        await page.goto(CREATE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        body = (await page.inner_text("body")).lower()
        if "can't buy or sell" in body or "restricted" in body:
            print("BLOCKED: this account cannot list right now.")
            await shoot(page, ROOT / "_fb_blocked.png")
            return 1

        fi = await page.query_selector("input[type=file][accept*='image']")
        if not fi:
            print("no photo input on the form")
            await shoot(page, ROOT / "_fb_noform.png")
            return 1
        await fi.set_input_files([str(p) for p in photos])
        await page.wait_for_timeout(4000)
        print(f"  uploaded {len(photos)} photos")

        await _dismiss_overlays(page)
        await fill_label(page, "Title", spec["title"])
        await fill_label(page, "Price", str(spec["price"]))
        await pick_category(page, "Furniture")
        await pick_combo(page, "Condition", "Used - Good", "Used – Good", "Used")
        # `fb_city`/`fb_state` exist only because a ledger city is not always a
        # place Facebook knows. Lot 28065 sits at "Selfridge ANGB (Harrison Twp)",
        # which the typeahead has no entry for — with no location the Next button
        # never enables, so the run burned a full cycle and stopped on the form.
        # The ledger city/state stay untouched so verify_fb_listings.py still
        # checks the listing against reality.
        loc_city = spec.get("fb_city") or spec["city"]
        loc_state = spec.get("fb_state") or spec["state"]
        if not await set_location(page, loc_city, loc_state):
            print(f"  ABORT: Facebook has no location for {loc_city!r} — "
                  "add fb_city/fb_state for this sku in the plan JSON.")
            await shoot(page, ROOT / f"_fb_noloc_{spec['sku'].replace(':','_')}.png")
            return 1
        # Description LAST — earlier steps can steal focus and clobber it.
        # The SKU rides at the end of the description text (the plan puts it there);
        # a personal account has no SKU field, and trying to fill one overwrote
        # Description with the bare lot id on the first attempt.
        await fill_label(page, "Description", spec["description"])
        await page.wait_for_timeout(600)

        # Hide from friends must end up ON — reuses the repo's existing helper
        # (automation/facebook.py), which reads aria-checked and only clicks when OFF.
        try:
            await _ensure_hide_from_friends_on(page, timeout=8000)
        except Exception as e:
            print(f"  [!] hide-from-friends: {type(e).__name__}")

        # prove what actually landed in the form before anyone publishes it
        checks = {"Title": spec["title"], "Description": spec["description"][:40]}
        for label, want in checks.items():
            try:
                got = await page.locator(f"label:has-text('{label}') textarea, label:has-text('{label}') input").first.input_value()
            except Exception:
                got = ""
            ok = want.strip()[:40].lower() in (got or "").lower()
            print(f"  check {label}: {'ok' if ok else 'MISMATCH'}")
            if not ok:
                print(f"    wanted ~{want[:60]!r}\n    got    {(got or '')[:60]!r}")

        shot = ROOT / f"_fb_draft_{spec['sku'].replace(':','_')}.png"
        await shoot(page, shot)
        print(f"  form URL: {page.url}")

        if not a.publish:
            print("\nFILLED, NOT PUBLISHED. Review the screenshot, then re-run with --publish.")
            await page.wait_for_timeout(a.keep_open * 1000)
            return 0

        if not await finish_publish(page):
            print("  DID NOT PUBLISH — stopped at:", page.url)
            await shoot(page, ROOT / f"_fb_stuck_{spec['sku'].replace(':','_')}.png")
            return 1

        await page.wait_for_timeout(4000)
        url = page.url
        item = ""
        m = re.search(r"/marketplace/item/(\d+)", url)
        if m:
            item = f"https://www.facebook.com/marketplace/item/{m.group(1)}/"
        else:
            # FB redirects to /marketplace/you/selling, which carries no item id.
            # The public profile is the only surface where a live listing is a
            # real /marketplace/item/<id> anchor — read it back and match by title.
            print(f"  no item id in {url} — reading it back off the profile")
            plan_now = json.loads(PLAN.read_text())
            try:
                cards = await scrape_selling(page, plan_now["account"]["profile_id"])
                item = match(cards, [spec]).get(spec["sku"], "")
            except Exception as e:
                print(f"  [!] profile read-back failed: {type(e).__name__}")
        if item:
            print(f"PUBLISHED: {item}")
            plan = json.loads(PLAN.read_text())
            for l in plan["listings"]:
                if l["sku"] == spec["sku"]:
                    l["fb_listing_url"] = item
            PLAN.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
            print("  wrote fb_listing_url into the plan JSON")
        else:
            print("  PUBLISH UNCONFIRMED — run scripts/fb_my_listings.py --write")
        await shoot(page, ROOT / f"_fb_posted_{spec['sku'].replace(':','_')}.png")
        await page.wait_for_timeout(a.keep_open * 1000)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

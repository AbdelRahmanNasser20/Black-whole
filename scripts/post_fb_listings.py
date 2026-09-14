"""One-off: post selected chair lots to Facebook Marketplace under a chosen
real Chrome profile, driving it directly via Playwright (channel="chrome").

Usage:
    python scripts/post_fb_listings.py idaho                # one
    python scripts/post_fb_listings.py general idaho maroon # several
    PUBLISH=0 python scripts/post_fb_listings.py idaho      # fill, stop before Publish

Requires the target Chrome profile to NOT be open (no profile lock).
"""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import inventory  # noqa: E402
from automation.templates import listing_title, fb_description  # noqa: E402
from automation.config import FB_CATEGORY, FB_CONDITION  # noqa: E402

# Logged-in Facebook profile = the facebook-crm poller's sibling CfT profile
# (Chromium engine, so cookies decrypt under Playwright's bundled chromium —
# unlike a real-Chrome profile whose cookies are macOS-keychain encrypted).
PROFILE = os.getenv(
    "LISTING_CHROME_PROFILE",
    "/Users/abdelnasser/Projects/blackwhole/facebook_scraper_Claude/chrome_profile",
)
PUBLISH = os.getenv("PUBLISH", "1") not in ("0", "false", "no")
SHOT_DIR = Path("/tmp/fb_post"); SHOT_DIR.mkdir(exist_ok=True)
PIX_BASE = Path.home() / "Desktop" / "Banquet chiars Pictures"


def _photos(folder_name: str, limit: int = 10) -> list[str]:
    d = PIX_BASE / folder_name
    files = sorted(
        p for p in d.glob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.is_file()
    )
    return [str(p) for p in files[:limit]]


def build_listings(keys: list[str]) -> list[dict]:
    rows = {r["lot_id"]: r for r in inventory.list_all()}
    out = []
    for key in keys:
        if key == "idaho":
            r = rows["31225"]
            out.append(dict(
                key=key, lot_id="31225",
                title=listing_title(r["chair_type"], r["city"], r["state"], r["title"]),
                price=int(float(r["price_per_chair"])),
                description=fb_description(
                    "", r["chair_type"], str(r["quantity_remaining"]), "",
                    r["description"], r["city"], r["state"], r["zip_code"]),
                city=r["city"], state=r["state"],
                photos=_photos(r["folder_name"]),
            ))
        elif key == "maroon":
            r = rows["2807"]
            out.append(dict(
                key=key, lot_id="2807",
                title=listing_title(r["chair_type"], r["city"], r["state"], r["title"]),
                price=int(float(r["price_per_chair"])),
                description=fb_description(
                    "", r["chair_type"], str(r["quantity_remaining"]), "",
                    r["description"], r["city"], r["state"], r["zip_code"]),
                city=r["city"], state=r["state"],
                photos=_photos(r["folder_name"]),
            ))
        elif key == "general":
            r = rows["5003"]  # Fresno-sourced
            desc = (
                "Bulk banquet & event chairs available in multiple colors, "
                "styles, and quantities across several US locations. Stackable "
                "and folding options. Used, good condition with normal wear "
                "from prior service. Pricing from $20/chair depending on lot "
                "size and pickup location.\n\n"
                "\U0001F4CD Pickup locations include CA, ID, MI, FL, NC and "
                "more (delivery quotes on request)\n"
                "\U0001F4E6 Hundreds to thousands available per lot\n\n"
                "Ideal for churches, banquet halls, community centers, "
                "schools, and event venues.\n\n"
                "To get a quote, please reply with:\n"
                "  1. Quantity needed\n"
                "  2. Pickup or delivery\n"
                "  3. Your city / ZIP"
            )
            out.append(dict(
                key=key, lot_id=None,
                title="Bulk Banquet & Event Chairs — Multiple Colors & Quantities",
                price=20,
                description=desc,
                city=r["city"], state=r["state"],
                photos=_photos(r["folder_name"]),
            ))
        else:
            raise SystemExit(f"unknown listing key: {key}")
    return out


CONDITION = "Used - Good"   # exact FB option label (verified from live form)


async def _set_textfield(page, label, value):
    el = page.get_by_label(label, exact=False).first
    await el.click()
    await el.press("ControlOrMeta+a")
    await el.press("Delete")
    await el.type(str(value), delay=20)
    return (await el.input_value()) if (await el.get_attribute("value")) is not None else None


async def _set_combobox(page, label, want_substr, type_text=None):
    """Open a role=combobox by its label and click the matching option."""
    cb = page.get_by_role("combobox", name=label).first
    await cb.click()
    await page.wait_for_timeout(500)
    if type_text:
        await page.keyboard.type(type_text, delay=40)
        await page.wait_for_timeout(700)
    opts = page.get_by_role("option")
    n = await opts.count()
    texts = []
    chosen = None
    for i in range(n):
        t = (await opts.nth(i).inner_text()).strip()
        texts.append(t)
        if want_substr.lower() in t.lower() and chosen is None:
            chosen = i
    if chosen is not None:
        await opts.nth(chosen).click()
        await page.wait_for_timeout(400)
        return texts[chosen], texts
    # close dropdown if nothing matched
    await page.keyboard.press("Escape")
    return None, texts


async def post_one(ctx, lst: dict) -> str:
    page = await ctx.new_page()
    await page.goto("https://www.facebook.com/marketplace/create/item",
                    wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    # Photos
    fi = await page.query_selector("input[type=file][accept*='image']")
    if fi and lst["photos"]:
        await fi.set_input_files(lst["photos"])
        await page.wait_for_timeout(3000)

    title_v = await _set_textfield(page, "Title", lst["title"])
    price_v = await _set_textfield(page, "Price", lst["price"])
    print(f"  title={title_v!r} price={price_v!r}")

    cat, cat_opts = await _set_combobox(page, "Category", "Chair", type_text="Chair")
    print(f"  category -> {cat!r} (from {cat_opts[:6]})")

    cond, _ = await _set_combobox(page, "Condition", CONDITION)
    print(f"  condition -> {cond!r}")

    desc = page.get_by_label("Description", exact=False).first
    await desc.click()
    await desc.fill(lst["description"])
    await page.wait_for_timeout(800)
    await page.screenshot(path=str(SHOT_DIR / f"{lst['key']}_1_filled.png"))

    # details -> delivery
    await page.get_by_role("button", name="Next").first.click(timeout=10000)
    await page.wait_for_timeout(3000)
    await page.screenshot(path=str(SHOT_DIR / f"{lst['key']}_2_delivery.png"))

    # delivery -> publish step (Local pickup is the default selection)
    try:
        await page.get_by_role("button", name="Next").first.click(timeout=8000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        print(f"  [delivery-next note: {e}]")
    await page.screenshot(path=str(SHOT_DIR / f"{lst['key']}_3_prepublish.png"))

    if PUBLISH:
        try:
            await page.get_by_role("button", name="Publish").first.click(timeout=12000)
            await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"  [publish note: {e}]")
    await page.screenshot(path=str(SHOT_DIR / f"{lst['key']}_4_done.png"))
    url = page.url
    print(f"[{lst['key']}] final_url={url}")
    await page.close()
    return url


async def main(keys):
    listings = build_listings(keys)
    print(f"PUBLISH={PUBLISH} profile={PROFILE!r}")
    for l in listings:
        print(f"  - {l['key']}: {l['title']} | ${l['price']} | "
              f"{len(l['photos'])} photos | lot={l['lot_id']}")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=False,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        # quick login guard
        guard = await ctx.new_page()
        await guard.goto("https://www.facebook.com/marketplace/create/item",
                         wait_until="domcontentloaded")
        await guard.wait_for_timeout(6000)
        if await guard.query_selector("input[name='email']"):
            await guard.screenshot(path=str(SHOT_DIR / "GUARD_loggedout.png"))
            print("ABORT: profile is logged out of Facebook")
            await ctx.close()
            return
        await guard.close()

        for l in listings:
            url = await post_one(ctx, l)
            if PUBLISH and l["lot_id"] and "listing_id=" in url:
                try:
                    inventory.set_platform_url(l["lot_id"], "facebook", url)
                    print(f"  ledger updated: {l['lot_id']} -> {url}")
                except Exception as e:
                    print(f"  ledger update failed: {e}")
            await asyncio.sleep(8)  # human-ish pacing between posts
        await ctx.close()


if __name__ == "__main__":
    ks = sys.argv[1:] or ["idaho"]
    asyncio.run(main(ks))

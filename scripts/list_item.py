"""CLI behind the `list-item` skill: create/update a marketplace listing for a
lot on a platform, driving the logged-in Chrome profile.

    python scripts/list_item.py <lot_id> ebay              # save as draft
    python scripts/list_item.py <lot_id> ebay --publish    # list it live
    python scripts/list_item.py <lot_id> ebay --publish --quantity 50

Uses the established, logged-in Chrome-for-Testing profile (the facebook-crm
poller's sibling `chrome_profile`, which is signed into eBay/FB). A fresh
browser profile gets bot-blocked at marketplace login, so we reuse this one.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import inventory, listing_content, drivers  # noqa: E402

PROFILE = os.getenv(
    "LISTING_CHROME_PROFILE",
    "/Users/abdelnasser/Projects/blackwhole/facebook_scraper_Claude/chrome_profile",
)
SHOT_DIR = Path(os.getenv("LISTING_SHOT_DIR", "/tmp/fb_post"))


async def run(lot_ids: list[str], platform: str, publish: bool,
              quantity: int | None):
    driver = drivers.get(platform)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=False,
            viewport={"width": 1440, "height": 1100},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            for lot_id in lot_ids:
                try:
                    content = listing_content.from_lot(lot_id, quantity=quantity)
                except Exception as e:
                    print(f"[{lot_id}] SKIP build error: {e}")
                    results.append((lot_id, {"error": str(e)}))
                    continue
                print(f"[{platform}] {lot_id}: {content.title} | ${content.price} "
                      f"x{content.quantity} | {len(content.photos)} photos | "
                      f"{content.attributes.as_ebay_specifics()}")
                try:
                    result = await driver.create(ctx, content, publish=publish,
                                                 screenshot_dir=str(SHOT_DIR))
                except Exception as e:
                    print(f"[{lot_id}] driver error: {e}")
                    results.append((lot_id, {"error": str(e)}))
                    continue
                print(f"[{lot_id}] RESULT:", result)
                if result.get("published") and result.get("url"):
                    try:
                        inventory.set_platform_url(lot_id, platform, result["url"])
                        print(f"[{lot_id}] ledger -> {result['url']}")
                    except Exception as e:
                        print(f"[{lot_id}] ledger update failed: {e}")
                results.append((lot_id, result))
        finally:
            await ctx.close()
    print("\nSUMMARY:")
    for lot_id, r in results:
        print(f"  {lot_id}: {r}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lot_ids", nargs="+", help="one or more lot ids")
    ap.add_argument("--platform", choices=sorted(drivers.REGISTRY), default="ebay")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--quantity", type=int, default=None)
    a = ap.parse_args()
    asyncio.run(run(a.lot_ids, a.platform, a.publish, a.quantity))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Is this Chrome profile logged in, and can it sell on Marketplace?

    LISTING_CHROME_PROFILE=~/.listing_automation/chrome_profile_dad \
      ./.venv/bin/python scripts/fb_check_account.py

Answers three things before any listing work happens:
  1. Is the profile logged in at all, and as whom?
  2. Does Marketplace selling work, or is this account restricted like the other one?
  3. What is the numeric profile id (needed for the "all my listings" URL)?

Prints a verdict and exits non-zero if selling is blocked. Reads nothing secret.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import browser  # noqa: E402
from automation.config import CHROME_PROFILE  # noqa: E402

CREATE_URL = "https://www.facebook.com/marketplace/create/item"
SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
BLOCKED = [
    "can't buy or sell",
    "cannot buy or sell",
    "restricted from",
    "temporarily blocked",
    "you're temporarily",
]
SHOT = Path(__file__).resolve().parents[1] / "_fb_account_check.png"


async def shoot(page) -> None:
    """Best-effort screenshot. FB's font loading can hang the default path."""
    try:
        await page.screenshot(path=str(SHOT), timeout=8000, animations="disabled")
        print(f"screenshot: {SHOT}")
    except Exception as e:
        print(f"(screenshot skipped: {type(e).__name__})")


async def main() -> int:
    print(f"profile: {CHROME_PROFILE}")
    async with browser.persistent_context() as ctx:
        page = await ctx.new_page()

        # 1. logged in?
        await page.goto("https://www.facebook.com/me", wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        who = page.url
        if "login" in who or "checkpoint" in who:
            print(f"NOT LOGGED IN (or checkpoint): {who}")
            await shoot(page)
            return 2
        name = ""
        try:
            name = (await page.title()).replace(" | Facebook", "").strip()
        except Exception:
            pass
        print(f"logged in as: {name or '(unknown)'}  url={who}")

        # 2. numeric profile id
        pid = ""
        m = re.search(r"facebook\.com/profile\.php\?id=(\d+)", who)
        if m:
            pid = m.group(1)
        if not pid:
            try:
                html = await page.content()
                m = re.search(r'"(?:userID|USER_ID)"\s*:\s*"?(\d{6,})"?', html)
                if m:
                    pid = m.group(1)
            except Exception:
                pass
        print(f"profile id: {pid or '(not found — grab it from a listing URL later)'}")

        # 3. can it sell?
        await page.goto(SELLING_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        body = (await page.inner_text("body")).lower()
        blocked = next((p for p in BLOCKED if p in body), None)
        if blocked:
            print(f"SELLING BLOCKED — page says: …{blocked}…")
            await shoot(page)
            return 1

        await page.goto(CREATE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        body = (await page.inner_text("body")).lower()
        blocked = next((p for p in BLOCKED if p in body), None)
        await shoot(page)
        if blocked:
            print(f"CREATE FORM BLOCKED — page says: …{blocked}…")
            return 1

        has_form = await page.query_selector("input[type=file][accept*='image']")
        print(f"create form reachable: {bool(has_form)}  url={page.url}")
        print("OK — this account can list." if has_form else "UNCLEAR — no photo input found; look at the screenshot.")
        return 0 if has_form else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

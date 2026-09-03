"""Shared Patchright fetcher for sites that gate server-rendered HTML behind a
real browser (Public Surplus ps-v2 search, HiBid's Angular shell).

Kept tiny on purpose: one fresh Chromium per call, no profile (never the
FB/bidding `~/.listing_automation/chrome_profile`), logged-out, non-headless
(headless builds trip anti-bot on GovDeals — same rule here). Chromium is
absent from the Render image, so `available()` lets an adapter fail loud
("needs a browser; run on the Mac") instead of silently yielding nothing.
"""
from __future__ import annotations

import re
import time

_CHALLENGE_RE = re.compile(r"Access Denied|captcha|verify you are human", re.I)


def available() -> bool:
    """True when Patchright imports; False on Render / a bare clone."""
    try:
        import patchright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def fetch_rendered(url: str, *, wait_selector: str, delay_s: float = 3.0,
                   timeout_ms: int = 30_000) -> str:
    """Render `url` in a fresh logged-out Chromium and return `page.content()`.

    Sleeps `delay_s` first (the crawl delay), waits for `wait_selector`, and
    raises RuntimeError if the page is a challenge / Access Denied wall — a
    block is a STOP, never something to work around.
    """
    from patchright.sync_api import sync_playwright

    time.sleep(delay_s)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            page = browser.new_context().new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_selector(wait_selector, timeout=timeout_ms)
            html = page.content()
        finally:
            browser.close()
    if _CHALLENGE_RE.search(html):
        raise RuntimeError(f"challenge page at {url} — stopping, not bypassing")
    return html

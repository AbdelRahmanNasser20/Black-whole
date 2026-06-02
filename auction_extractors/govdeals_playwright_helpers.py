"""
Shared GovDeals Playwright navigation.

Default: load the homepage (https://www.govdeals.com/en) and fill the search box.
The direct browse URL (/en/browse/search?keyword=…) often gets the egress IP
flagged by Akamai, so we treat it as opt-in:

  GOVDEALS_USE_BROWSE_SEARCH_URL=1   # use direct browse/search URL
  GOVDEALS_USE_BROWSE_SEARCH_URL=0   # (default) homepage + fill search box
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Browser, BrowserContext, Locator, Page

_GO_BROWSE = int(os.getenv("GOVDEALS_USE_BROWSE_SEARCH_URL", "0"))

# GovDeals serves the app under /en; bare https://www.govdeals.com/ can redirect differently.
GOVDEALS_SITE_ORIGIN = (os.getenv("GOVDEALS_SITE_ORIGIN") or "https://www.govdeals.com/en").rstrip("/")

# Real Chrome UA — default Playwright UA is often flagged by Akamai.
_DEFAULT_GOVDEALS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def govdeals_chromium_launch_args() -> list[str]:
    base = [
        "--disable-blink-features=AutomationControlled",
        "--disable-http2",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    extra = (os.getenv("GOVDEALS_CHROMIUM_ARGS") or "").strip()
    if extra:
        base.extend(extra.split())
    return base


def govdeals_browser_context(browser: Browser) -> BrowserContext:
    """
    Context with a normal desktop Chrome fingerprint. Helps with GovDeals / Akamai.
    Call before navigation; use context.new_page().
    """
    ctx = browser.new_context(
        user_agent=os.getenv("GOVDEALS_USER_AGENT", _DEFAULT_GOVDEALS_UA),
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": 1366, "height": 768},
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        },
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return ctx


def dismiss_govdeals_overlays(page: Page) -> None:
    for sel in (
        "button:has-text('Accept All Cookies')",
        "text=Accept All Cookies",
        "text=Accept",
        "[data-testid='cookie-accept']",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=2500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def _goto_timeout_ms() -> int:
    return int(os.getenv("GOVDEALS_GOTO_TIMEOUT_MS", "90000"))


def _govdeals_blocked(page: Page) -> bool:
    """Akamai / WAF often returns a tiny HTML page with 'Access Denied'."""
    try:
        title = (page.title() or "").lower()
        if "access denied" in title:
            return True
    except Exception:
        pass
    try:
        h1 = page.locator("h1").first
        if h1.count():
            t = (h1.inner_text(timeout=3000) or "").lower()
            if "access denied" in t:
                return True
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=5000)
        b = body.lower()
        # Do not require "permission" — some Akamai pages only show the heading + reference id.
        if "access denied" in b:
            return True
        if "errors.edgesuite.net" in b:
            return True
        if "you don't have permission to access" in b:
            return True
    except Exception:
        pass
    return False


def _govdeals_search_urls(keyword_q: str) -> list[str]:
    """Search-results URLs for ``keyword_q`` (already %20-encoded).

    The keyword param is ``kWord`` — verified 2026-06-01 by driving the live
    site's search box and capturing the navigation. ``?keyword=`` / ``?q=`` are
    SILENTLY IGNORED by GovDeals' Angular app: it returns the entire ~25k-item
    catalog instead of filtering, which is why earlier runs walked 200+ pages of
    cars/heavy-equipment. ``numPerPage`` / ``sortBy`` are likewise ignored by the
    SPA (page size is fixed at 24 client-side), so we don't send them.

    Space must be ``%20`` (caller uses ``quote``): the Angular router reads ``+``
    literally, so ``?kWord=banquet+chairs`` would search the literal string
    "banquet+chairs" and miss.
    """
    base = f"{GOVDEALS_SITE_ORIGIN}/search"
    return [
        f"{base}?kWord={keyword_q}",
    ]


def _wait_for_govdeals_result_cards(
    page: Page,
    *,
    screenshot_on_fail: Path | None = None,
    timeout_ms: int = 90000,
) -> None:
    """
    Poll until listing cards exist. The search shell (#myTabContent) can appear before
    cards hydrate; do not rely on ul.pagination for readiness.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _govdeals_blocked(page):
            raise TimeoutError(
                "GovDeals blocked (Access Denied) while waiting for search results."
            )
        # /search no longer wraps results in #myTabContent; match cards directly.
        n = page.locator(".card-search").count()
        if n > 0:
            return
        page.wait_for_timeout(450)

    if screenshot_on_fail:
        try:
            screenshot_on_fail.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_on_fail))
        except Exception:
            pass
    raise TimeoutError(
        "GovDeals: search shell loaded but no listing cards appeared "
        "(blocked, slow SPA, or zero hits). See screenshot if configured."
    )


def open_govdeals_keyword_search_results(
    page: Page,
    keyword: str,
    *,
    script_dir: Path | None = None,
) -> None:
    """
    Navigate to GovDeals browse search for ``keyword`` and wait for results UI.

    Does not use the homepage search box (avoids hydration / visibility issues).
    """
    shot = (script_dir / "govdeals_search_timeout.png") if script_dir else None
    # quote (not quote_plus): GovDeals' kWord param needs %20 for spaces, not +.
    q = quote(keyword.strip() or "chairs", safe="")
    urls = _govdeals_search_urls(q)
    last_err: Exception | None = None

    for url in urls:
        try:
            page.goto(url, wait_until="load", timeout=_goto_timeout_ms())
            dismiss_govdeals_overlays(page)
            page.wait_for_timeout(600)
            if _govdeals_blocked(page):
                last_err = TimeoutError(f"GovDeals blocked (Access Denied) at {url}")
                if shot:
                    try:
                        shot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shot))
                    except Exception:
                        pass
                continue

            # Wait for the first result card to attach (the old #myTabContent
            # shell no longer exists on /search). Don't use ul.pagination: it can
            # exist before listing cards hydrate.
            page.wait_for_selector(".card-search", state="attached", timeout=90000)
            page.wait_for_timeout(800)
            try:
                page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass
            _wait_for_govdeals_result_cards(page, screenshot_on_fail=shot)
            return
        except Exception as e:
            last_err = e
            if shot:
                try:
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot))
                except Exception:
                    pass
            continue

    raise TimeoutError(
        f"GovDeals: could not open search results (tried {len(urls)} URLs). Last error: {last_err}"
    ) from last_err


def wait_for_govdeals_search_input(
    page: Page,
    *,
    screenshot_on_fail: Path | None = None,
    timeout_ms: int = 120000,
) -> Locator:
    """Wait until a homepage search field is usable. Returns a Locator for fill()."""
    last_err: Exception | None = None
    per_try = max(25000, timeout_ms // 10)

    # Role / ARIA often survives DOM refactors better than #ids.
    role_attempts: list[Locator] = [
        page.get_by_role("searchbox"),
        page.get_by_placeholder(re.compile(r"search|keyword|enter|find", re.I)),
        page.get_by_label(re.compile(r"search|keyword", re.I)),
    ]
    for loc in role_attempts:
        try:
            first = loc.first
            first.wait_for(state="visible", timeout=per_try)
            try:
                first.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            return first
        except Exception as e:
            last_err = e

    candidates = [
        "#textSearchInput",
        "input#textSearchInput",
        'input[name="textSearchInput"]',
        'input[placeholder*="Search" i]',
        'input[placeholder*="Keyword" i]',
        'input[aria-label*="Search" i]',
        "input[type=search]",
        'input[type="text"][class*="search" i]',
        "header input[type=text]",
        "nav input[type=text]",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=per_try)
            try:
                loc.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            loc.wait_for(state="visible", timeout=15000)
            return loc
        except Exception as e:
            last_err = e
    if screenshot_on_fail:
        try:
            screenshot_on_fail.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_on_fail))
        except Exception:
            pass
    raise TimeoutError(
        f"GovDeals: no search input matched (roles + {len(candidates)} CSS). Last error: {last_err}"
    ) from last_err


def goto_govdeals_home_and_focus_search(
    page: Page,
    *,
    script_dir: Path | None = None,
) -> Locator:
    """Legacy: homepage, dismiss overlays, wait for search box. Returns Locator for fill()."""
    shot = (script_dir / "govdeals_search_timeout.png") if script_dir else None

    page.goto(GOVDEALS_SITE_ORIGIN + "/", wait_until="load", timeout=_goto_timeout_ms())
    dismiss_govdeals_overlays(page)
    page.wait_for_timeout(1200)
    if _govdeals_blocked(page):
        raise TimeoutError("GovDeals homepage blocked (Access Denied). Try another network or HEADLESS=0.")

    page.goto(GOVDEALS_SITE_ORIGIN + "/", wait_until="load", timeout=_goto_timeout_ms())
    dismiss_govdeals_overlays(page)
    page.wait_for_timeout(1000)
    if _govdeals_blocked(page):
        raise TimeoutError("GovDeals homepage blocked (Access Denied). Try another network or HEADLESS=0.")

    return wait_for_govdeals_search_input(page, screenshot_on_fail=shot)


def prepare_govdeals_search_page(
    page: Page,
    keyword: str,
    *,
    script_dir: Path | None = None,
) -> None:
    """
    Navigate to search results for ``keyword``.

    Prefer direct browse URL; if ``GOVDEALS_USE_BROWSE_SEARCH_URL=0``, use homepage + fill.
    If the browse URL fails (layout change), fall back to homepage search once.
    """
    # Read the flag at call time, not import time: the entrypoint loads .env
    # (load_dotenv) *after* importing this module, so the module-level _GO_BROWSE
    # would otherwise freeze to its default before .env is applied.
    if int(os.getenv("GOVDEALS_USE_BROWSE_SEARCH_URL", str(_GO_BROWSE))):
        try:
            open_govdeals_keyword_search_results(page, keyword, script_dir=script_dir)
            return
        except Exception as e:
            print(f" → GovDeals browse/search URL failed ({e}); trying homepage search box…")

    search_loc = goto_govdeals_home_and_focus_search(page, script_dir=script_dir)
    search_loc.fill(keyword)
    try:
        page.click("button[aria-label='Search']", timeout=10000)
    except Exception:
        page.keyboard.press("Enter")
    shot = (script_dir / "govdeals_search_timeout.png") if script_dir else None
    try:
        page.wait_for_selector("#myTabContent", state="attached", timeout=60000)
    except Exception:
        pass

    # The homepage form lands us on /browse/search?keyword=… with no
    # numPerPage. Re-open the same URL with numPerPage appended so each page
    # returns 100 results instead of 24. (Same param the browse-URL path
    # already uses; this keeps both routes converged.)
    try:
        cur = page.url or ""
        num = os.getenv("GOVDEALS_NUM_PER_PAGE", "100")
        if cur and "numPerPage=" not in cur:
            joiner = "&" if ("?" in cur) else "?"
            page.goto(f"{cur}{joiner}numPerPage={num}", wait_until="load", timeout=_goto_timeout_ms())
            page.wait_for_timeout(800)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
    except Exception as e:
        print(f" → could not bump page size: {e}")

    _wait_for_govdeals_result_cards(page, screenshot_on_fail=shot)

"""One-shot probe: can we load a GovDeals lot page from this machine/IP?

Reuses the repo's working anti-Akamai recipe (govdeals_playwright_helpers.py),
runs a HEADED Chromium (under Xvfb inside the container), navigates to one lot
page, and prints a clear PASS/BLOCKED verdict.

Env:
  PROBE_URL              lot URL to load (default a known GovDeals asset)
  HEADLESS               "1" forces headless (Akamai usually blocks this); default headed
  PROXY_URL              optional scheme://user:pass@host:port residential proxy
  OUT_BUCKET             optional GCS bucket name to upload the screenshot to
  GOVDEALS_GOTO_TIMEOUT_MS   nav timeout (default 90000)

Exit code: 0 = PASS, 2 = BLOCKED, 1 = error.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from govdeals_playwright_helpers import (
    govdeals_chromium_launch_args,
    govdeals_browser_context,
    _govdeals_blocked,
)

# Homepage never 404s/expires, so a PASS/BLOCKED verdict reflects IP reputation,
# not a dead lot. Override with PROBE_URL to test a specific live asset page.
URL = os.getenv("PROBE_URL", "https://www.govdeals.com/en/")
SHOT = "/tmp/probe.png"


def proxy_cfg():
    """Parse PROXY_URL into Playwright's proxy dict (creds split from server)."""
    raw = (os.getenv("PROXY_URL") or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg


def main() -> int:
    p_cfg = proxy_cfg()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=os.getenv("HEADLESS", "0") == "1",  # headed by default
            args=govdeals_chromium_launch_args(),
            proxy=p_cfg,  # None when PROXY_URL unset
        )
        ctx = govdeals_browser_context(browser)
        page = ctx.new_page()
        page.goto(
            URL,
            wait_until="load",
            timeout=int(os.getenv("GOVDEALS_GOTO_TIMEOUT_MS", "90000")),
        )
        page.wait_for_timeout(1500)

        title = (page.title() or "").strip()
        blocked = _govdeals_blocked(page)
        try:
            page.screenshot(path=SHOT, full_page=True)
        except Exception as e:
            print(f"screenshot failed: {e}")

        print(f"URL={URL}")
        print(f"PROXY={'on' if p_cfg else 'off'}  TITLE={title!r}")
        verdict = "BLOCKED" if blocked else "PASS"
        print(f"VERDICT: {verdict}")

        bucket = os.getenv("OUT_BUCKET")
        if bucket:
            try:
                from google.cloud import storage  # lazy import

                key = f"probe/{os.getenv('CLOUD_RUN_EXECUTION', 'local')}.png"
                storage.Client().bucket(bucket).blob(key).upload_from_filename(SHOT)
                print(f"SCREENSHOT: gs://{bucket}/{key}")
            except Exception as e:
                print(f"screenshot upload failed: {e}")

        ctx.close()
        browser.close()
        return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        print("VERDICT: ERROR")
        sys.exit(1)

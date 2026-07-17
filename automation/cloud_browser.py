"""BLACKWHOLE-22 pilot scaffold — cloud browser / scrape behind an env flag.

EVAL ONLY, ships OFF. This is the thin proof-of-concept for the Firecrawl /
Browserbase pilot. It adds two seams, both hard no-ops unless BOTH an env flag
is set AND an API key is present — so it is safe to import, test, and merge with
no paid account and makes zero live calls in CI:

  • resolve_cloud_cdp_endpoint()
      When LISTING_CLOUD_BROWSER=browserbase and BROWSERBASE_API_KEY is set,
      creates a Browserbase session and returns its CDP connect URL (a wss://
      URL). automation/browser.py hands that straight to Playwright's
      connect_over_cdp — the SAME seam it already uses to attach to the local
      poller (see _try_connect_cdp). Flag off or no key => returns None => the
      existing local browser path runs, byte-for-byte unchanged. This is the
      "post to Facebook/eBay from a cloud browser when local Chrome is
      fingerprinted or contended" de-risk the ticket asks about.

  • firecrawl_scrape(url)
      Thin Firecrawl REST wrapper for the SOURCE-scrape side (GovDeals /
      PublicSurplus reading). Raises CloudBrowserNotConfigured when not
      configured — it never invents a key or calls out silently. Live calls burn
      credits, so it stays behind FIRECRAWL_ENABLED + FIRECRAWL_API_KEY.

Design notes
------------
* Env is read at CALL time (not import), mirroring public_surplus_automation's
  `_browser_fallback_enabled` so `mock.patch.dict(os.environ, ...)` flips the
  gate in tests without reimporting.
* `requests` (already a project dep) is the transport — no new dependency, no
  vendor SDK. The Browserbase/Firecrawl REST shapes are small and stable enough
  for a pilot; a production build (BLACKWHOLE-20) may swap in the official SDKs.
* Cloud resolution NEVER raises into the pipeline: any failure logs and returns
  None so the caller falls back to the local browser. Losing the cloud path must
  not lose the run.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

# REST endpoints (stable enough to hard-code for a pilot).
BROWSERBASE_SESSIONS_URL = "https://api.browserbase.com/v1/sessions"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

# Short create/connect timeout so a cloud hiccup fast-fails to the local path
# instead of stalling the pipeline.
CLOUD_SESSION_TIMEOUT_SEC = 15


class CloudBrowserNotConfigured(RuntimeError):
    """Raised when a cloud entry point is used without flag + key set."""


# ── gates (read env live so patch.dict flips them) ──────────────────────────

def _provider() -> str:
    return os.getenv("LISTING_CLOUD_BROWSER", "").strip().lower()


def cloud_browser_enabled() -> bool:
    """True only when Browserbase is selected AND a key is present."""
    return _provider() == "browserbase" and bool(os.getenv("BROWSERBASE_API_KEY"))


def firecrawl_enabled() -> bool:
    """True only when FIRECRAWL_ENABLED is truthy AND a key is present."""
    flag = os.getenv("FIRECRAWL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return flag and bool(os.getenv("FIRECRAWL_API_KEY"))


# ── Browserbase: session -> CDP connectUrl for Playwright.connect_over_cdp ───

def resolve_cloud_cdp_endpoint() -> Optional[str]:
    """Return a Browserbase CDP connect URL, or None to use the local browser.

    None (never an exception) is returned when the pilot is off, unconfigured,
    or the create call fails — the caller then falls back to the local path.
    """
    if not cloud_browser_enabled():
        return None

    api_key = os.getenv("BROWSERBASE_API_KEY", "")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "")
    if not project_id:
        print("[cloud_browser] LISTING_CLOUD_BROWSER=browserbase but "
              "BROWSERBASE_PROJECT_ID is unset — using local browser.")
        return None

    payload = {"projectId": project_id}
    # Stealth + proxies are what make FB/eBay tolerate a cloud IP. Opt in via env
    # so the pilot can A/B them against per-session cost.
    if os.getenv("BROWSERBASE_STEALTH", "").strip().lower() in ("1", "true", "yes", "on"):
        payload["browserSettings"] = {"fingerprint": {"httpVersion": 2}}
        payload["proxies"] = True

    try:
        resp = requests.post(
            BROWSERBASE_SESSIONS_URL,
            headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=CLOUD_SESSION_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network, auth, quota — never break the pipeline
        print(f"[cloud_browser] Browserbase session create failed ({exc!r}); "
              "falling back to local browser.")
        return None

    connect_url = data.get("connectUrl")
    if not connect_url:
        print("[cloud_browser] Browserbase response had no connectUrl; "
              "falling back to local browser.")
        return None
    print(f"[cloud_browser] Browserbase session {data.get('id', '?')} ready — "
          "attaching over CDP.")
    return connect_url


# ── Firecrawl: source-scrape helper ─────────────────────────────────────────

def firecrawl_scrape(url: str, *, formats: Optional[list[str]] = None) -> dict:
    """Scrape one URL via Firecrawl and return its JSON `data` block.

    Raises CloudBrowserNotConfigured when the pilot is off or unconfigured, so a
    caller can cheaply decide to use the existing requests/Playwright path.
    """
    if not firecrawl_enabled():
        raise CloudBrowserNotConfigured(
            "Firecrawl is off. Set FIRECRAWL_ENABLED=1 and FIRECRAWL_API_KEY to "
            "use firecrawl_scrape (live calls cost credits)."
        )

    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    body = {"url": url, "formats": formats or ["markdown"]}
    resp = requests.post(
        FIRECRAWL_SCRAPE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=CLOUD_SESSION_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    return resp.json().get("data", {})

"""IndexNow pings — tell Bing (and so ChatGPT search) a storefront URL changed.

Why: ChatGPT search reads Bing's index, and Bing re-crawls a young site
slowly. IndexNow is one POST that says "these URLs changed, come look".

Rules:
- Dark-ship gate: `INDEXNOW_KEY` unset (or malformed) → every call is a no-op
  with no network, and `/<key>.txt` 404s in the web app.
- The key is public by design: Bing proves ownership by fetching
  `{PUBLIC_BASE_URL}/{key}.txt` and comparing the body.
- `submit()` never raises and never retries — a ping is a hint, not a write.
  Callers in `lot_channels` run it in a background thread (`submit_async`).
"""

from __future__ import annotations

import os
import re
import threading
from urllib.parse import urlsplit

import httpx

from . import lot_urls
from .config import PUBLIC_BASE_URL

ENDPOINT = "https://api.indexnow.org/indexnow"
TIMEOUT_S = 10.0
MAX_URLS = 10_000
_KEY_RE = re.compile(r"[A-Za-z0-9-]{8,128}")


def key() -> str:
    """The configured key, or "" when unset or not a valid IndexNow key."""
    k = os.getenv("INDEXNOW_KEY", "").strip()
    return k if _KEY_RE.fullmatch(k) else ""


def key_location() -> str:
    return f"{PUBLIC_BASE_URL}/{key()}.txt"


def submit(urls) -> tuple[bool, str]:
    """POST `urls` (site-relative or absolute on our host) to IndexNow.
    Returns (ok, detail). Never raises."""
    k = key()
    if not k:
        return False, "disabled"
    try:
        host = urlsplit(PUBLIC_BASE_URL).netloc
        seen: set[str] = set()
        out: list[str] = []
        for u in urls or []:
            u = (u or "").strip()
            if not u:
                continue
            if u.startswith("/"):
                u = f"{PUBLIC_BASE_URL}{u}"
            if urlsplit(u).netloc != host or u in seen:
                continue
            seen.add(u)
            out.append(u)
        out = out[:MAX_URLS]
        if not out:
            return False, "no urls"
        resp = httpx.post(
            ENDPOINT,
            json={"host": host, "key": k, "keyLocation": key_location(), "urlList": out},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code in (200, 202):
            return True, str(resp.status_code)
        return False, f"HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001 — a ping must never break its caller
        return False, repr(e)


def submit_async(urls) -> threading.Thread | None:
    """Fire `submit(urls)` in a background thread. None when no key is set."""
    if not key():
        return None
    t = threading.Thread(target=submit, args=(urls,), daemon=False, name="indexnow")
    t.start()
    return t


def lot_ping_urls(row: dict) -> list[str]:
    """The pages a lot change touches: the lot page, /listings, its city page."""
    urls = [lot_urls.public_path(row), "/listings"]
    city, state = (row.get("city") or "").strip(), (row.get("state") or "").strip()
    if city and state:
        urls.append(lot_urls.city_path(city, state))
    return urls


def ping_lot(row: dict) -> bool:
    """Background ping for one lot. True when a ping was started."""
    return submit_async(lot_ping_urls(row)) is not None

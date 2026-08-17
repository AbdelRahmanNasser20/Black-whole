#!/usr/bin/env python3
"""
GovDeals bulk chair listings — scrape, filter, LLM-rank, notify.

1. Playwright scrapes GovDeals.com for chair-related search terms (stackable, banquet, etc.).
2. Keeps listings with quantity above MIN_CHAIR_QUANTITY (default 50).
3. Ranks via LLM (OpenAI, Ollama, or Groq) by quantity (and price tie-break).
4. Sends results to Telegram.

Setup: see README.md and .env.example in auction_extractors/.
"""

import os
import re
import time
import uuid
import requests
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from quantity_infer import explicit_title_quantity, infer_chair_quantity_from_title
from quantity_llm import refine_quantities_with_llm
from quantity_refine import refine_quantities_with_regex_fulltext
from paths import REPORTS_DIR
from govdeals_playwright_helpers import (
    govdeals_browser_context,
    govdeals_chromium_launch_args,
    prepare_govdeals_search_page,
    dismiss_govdeals_overlays,
    _goto_timeout_ms,
)
import listings_db

# Load root .env (project-level)
load_dotenv()
# Also load local .env next to this script (overrides root if set)
_SCRIPT_DIR = os.path.dirname(__file__)
_LOCAL_ENV = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_LOCAL_ENV):
    load_dotenv(_LOCAL_ENV, override=True)

# ─── CONFIG (set in .env) ────────────────────────────────────────────────────
# Telegram (optional; if both set, final output is sent to Telegram)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_TOPIC_DEALS  = os.getenv("TELEGRAM_TOPIC_DEALS")
TELEGRAM_TOPIC_HEALTH = os.getenv("TELEGRAM_TOPIC_HEALTH")


def _tg_thread(raw):
    """message_thread_id fragment for forum-topic routing (BLACKWHOLE-24).
    Empty dict when unset so behavior is unchanged."""
    try:
        return {"message_thread_id": int(raw)} if raw and str(raw).strip() else {}
    except (TypeError, ValueError):
        return {}

# LLM: "openai" | "ollama" | "groq" (default: ollama — run: curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2)
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()

# OpenAI (when LLM_PROVIDER=openai)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Ollama local (when LLM_PROVIDER=ollama)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

# Groq (when LLM_PROVIDER=groq) — free tier, fast
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Gemini (when (QUANTITY_)LLM_PROVIDER=gemini) — high free-tier daily limits
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# The quantity pass can use a different (cheaper / higher-quota) provider than
# ranking. Groq's free tier (100k tokens/day) blows up on a full scrape and
# silently falls back to regex — which is how "Lot of 2,100" becomes qty 2.
# Set QUANTITY_LLM_PROVIDER=gemini in prod so quantity never hits that cap.
QUANTITY_LLM_PROVIDER = (os.getenv("QUANTITY_LLM_PROVIDER") or LLM_PROVIDER).strip().lower()

# Ollama HTTP timeout (large JSON prompts can exceed 120s on CPU)
try:
    OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
except ValueError:
    OLLAMA_TIMEOUT = 600
# ────────────────────────────────────────────────────────────────────────────

# Search terms to run (each is run separately; results are aggregated and deduped).
# TEMPORARY: narrowed to a single broad term while we validate the new
# regex_fulltext + cache pipeline. Restore the full list once accuracy is
# confirmed on real GovDeals data.
SEARCH_TERMS = [
    "chairs", "banquet chairs", "stackable chairs", "church chairs",
    "event chairs", "conference chairs", "folding chairs",
    # medical vertical — single-unit lots (qty filter gated by category)
    "dental chair", "exam chair", "treatment chair", "phlebotomy chair",
    "procedure chair", "exam table",
]



def _govdeals_search_cards_locator(page):
    """Listing cards for search results; fall back if markup shifts slightly."""
    for sel in (
        "#myTabContent .card.card-search",
        "#myTabContent .card-search",
        "main .card.card-search",
        ".card.card-search",
    ):
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc
    return page.locator("#myTabContent .card.card-search")


def _scroll_results_into_view(page) -> None:
    """Force-render any virtualized cards by scrolling to the bottom.

    GovDeals' Angular Material grid sometimes only renders the cards that are
    in the viewport. Without this, ``_scrape_one_page`` saw ~12-24 cards even
    when the results page should have had 100. We scroll in steps until the
    card count stops growing or we've scrolled 25 times (whichever first).
    """
    locator = _govdeals_search_cards_locator(page)
    last_count = -1
    stable_iters = 0
    for _ in range(25):
        count = locator.count()
        if count == last_count:
            stable_iters += 1
            if stable_iters >= 2:
                break
        else:
            stable_iters = 0
        last_count = count
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break
        page.wait_for_timeout(350)
    # Scroll back to the top so subsequent locators don't get confused by
    # any sticky overlays at the bottom of the viewport.
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


def _scrape_one_page(page) -> list:
    """Scrape all card listings from the current search results page. Returns list of dicts."""
    _scroll_results_into_view(page)
    page_listings = []
    cards = _govdeals_search_cards_locator(page)
    count = cards.count()

    for i in range(count):
        card = cards.nth(i)
        try:
            title_element = card.locator('.card-title a')
            title = title_element.inner_text().strip()
            link = title_element.get_attribute('href')

            location_element = card.locator('p[name="pAssetLocation"]')
            location = location_element.inner_text().strip()

            price_element = card.locator('p[name="pAssetCurrentBid"]')
            price = price_element.inner_text().strip()

            lot_element = card.locator('p:has-text("Lot#:")')
            lot_text = lot_element.inner_text().strip()
            lot_number = lot_text.replace('Lot#:', '').strip()

            end_date = ""
            time_left = ""
            try:
                timer_p = card.locator("app-ux-timer p.timerAttribute")
                timer_text = timer_p.inner_text().strip()
                m = re.search(r"(\d+)\s*H.*?(\d+)\s*M", timer_text)
                if m:
                    time_left = f"{m.group(1)}h {m.group(2)}m"
                m2 = re.search(r"\(([^)]+)\)", timer_text)
                if m2:
                    end_date = m2.group(1).strip()
            except Exception:
                pass

            qty = infer_chair_quantity_from_title(title)

            page_listings.append({
                "title": title,
                "link": "https://www.govdeals.com" + link if link else "",
                "quantity": qty,
                # Track where the quantity came from so the description-pass
                # below (and the optional LLM step) can override it without
                # ambiguity, and the Telegram formatter can show provenance.
                "quantity_source": "regex_title",
                "quantity_confidence": "low" if qty == 1 else "medium",
                "location": location,
                "price": price,
                "lot_number": lot_number,
                "end_date": end_date,
                "time_left": time_left,
            })
        except Exception as e:
            print(f"   • Error scraping card {i+1}: {e}")
            continue

    return page_listings


def _fetch_govdeals_long_description(
    page, url: str,
) -> tuple[str, str, str, str, str]:
    """Load asset page, return ``(description, image_url, pickup_zip,
    contact_email, contact_phone)``.

    GovDeals renders the description via Angular (`app-read-more`). Before the data
    binds, the DOM literally contains the string "undefined", which used to leak
    into our LLM prompts and collapse every quantity to 1. We poll the selectors
    until real text appears (or the timeout elapses). The primary image and the
    pickup ZIP are grabbed on the same page load — zero extra Playwright round-trip.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    selectors = (
        "p.long-description app-read-more div",
        "app-read-more.long-description div",
        "p.long-description",
        ".long-description",
    )

    def _clean(txt: str) -> str:
        if not txt:
            return ""
        s = txt.strip()
        # Angular renders bound-but-empty values as the literal "undefined"/"null".
        if s.lower() in ("undefined", "null", "none"):
            return ""
        return s

    def _pluck_image() -> str:
        """Primary product image URL from the GovDeals asset page.

        Real product photos use the CSS class ``lg-image`` and live on the
        ``webassets.lqdt1.com`` CDN under ``/assets/photos/``. We match on
        BOTH to reject the site's brand/partner logos which also sit in
        ``/assets/images/brands/`` on the same domain.
        """
        img_selectors = (
            "img.lg-image",
            "img.lg-object",
            'img[src*="webassets.lqdt1.com/assets/photos"]',
            ".carousel-item.active img",
        )
        for sel in img_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                src = loc.get_attribute("src", timeout=1500) or ""
                if not src or src.startswith("data:"):
                    continue
                # Reject branding / partner logos explicitly.
                low = src.lower()
                if "/brands/" in low or "/images/icons/" in low or "logo" in low:
                    continue
                return src
            except Exception:
                continue
        return ""

    description = ""
    # Poll for ~6s waiting for real text to bind in.
    deadline_ms = 6000
    waited = 0
    step = 250
    while waited <= deadline_ms and not description:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                text = _clean(loc.inner_text(timeout=2000))
                if text:
                    description = text[:4000]
                    break
            except Exception:
                continue
        if not description:
            page.wait_for_timeout(step)
            waited += step

    if not description:
        # Last-ditch fallback: scan the whole page text.
        try:
            text = _clean(page.locator("body").inner_text(timeout=2000))
            if text:
                description = text[:4000]
        except Exception:
            pass

    pickup_zip = ""
    contact_email = ""
    contact_phone = ""
    try:
        body_text = page.locator("body").inner_text(timeout=2000) or ""
        idx = body_text.lower().find("pickup")
        if idx != -1:
            window = body_text[idx : idx + 600]
            m = re.search(r"\b(\d{5})(?:-(\d{4}))?\b", window)
            if m:
                pickup_zip = f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)

        # Contact info — scan around the "Contact Information" / "Seller" anchor.
        # If no anchor is found, fall back to the whole body but only pick the
        # first email/phone (avoids capturing an unrelated phone number from
        # boilerplate at the bottom of the page).
        contact_re_idx = re.search(
            r"Contact Information|Seller Information|Contact:", body_text, re.I,
        )
        scope = (
            body_text[contact_re_idx.start() : contact_re_idx.start() + 800]
            if contact_re_idx else body_text
        )
        em = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", scope)
        if em:
            contact_email = em.group(0)
        ph = re.search(r"\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", scope)
        if ph:
            contact_phone = ph.group(0).strip()
    except Exception:
        pass

    return description, _pluck_image(), pickup_zip, contact_email, contact_phone


def _llm_quantity_enabled() -> bool:
    """LLM quantity pass defaults ON (matches Public Surplus).

    The title-only regex misreads model/fleet numbers and thousands-separated
    counts; the LLM is the trustworthy signal and must run unless explicitly
    disabled with ``USE_LLM_QUANTITY=0``.
    """
    return os.getenv("USE_LLM_QUANTITY", "1") == "1"


def enrich_listings_with_govdeals_descriptions(listings: list) -> list:
    if os.getenv("FETCH_GOVDEALS_DESCRIPTION") != "1":
        return listings

    # First pass: fill descriptions from the SQLite cache. The cache key is
    # the asset_id parsed from the listing URL, so anything we've seen on a
    # previous run skips the slow Playwright page load entirely.
    hits, _, relists = listings_db.hydrate_from_cache(listings)
    # A relist is a row whose asset_id matches cache but whose end_date rolled
    # forward — we intentionally re-fetch those so the new auction round is
    # picked up (fresh quantity, description, price, etc.).
    needs_fetch = [x for x in listings if not (x.get("description") or "").strip()]
    if hits:
        print(f"[1b] Cache hit on {hits}/{len(listings)} listings (description reused).")
    if relists:
        print(f"[1b] {relists} relist(s) detected (end_date changed) — re-fetching fresh.")
    if not needs_fetch:
        print("[1b] All descriptions served from cache; no Playwright fetch needed.")
        return listings

    delay = float(os.getenv("FETCH_GOVDEALS_DELAY_SEC", "0.35"))
    print(f"[1b] Fetching long descriptions for {len(needs_fetch)} uncached listings…")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=os.getenv("HEADLESS", "0") == "1",
            args=govdeals_chromium_launch_args(),
        )
        context = govdeals_browser_context(browser)
        page = context.new_page()
        try:
            for i, item in enumerate(needs_fetch):
                link = item.get("link")
                if not link:
                    item["description"] = ""
                    item.setdefault("image_url", "")
                    item.setdefault("pickup_zip", "")
                    item.setdefault("contact_email", "")
                    item.setdefault("contact_phone", "")
                    continue
                try:
                    desc, img, zip_code, email, phone = _fetch_govdeals_long_description(page, link)
                    item["description"] = desc
                    item["image_url"] = img
                    item["pickup_zip"] = zip_code
                    item["contact_email"] = email
                    item["contact_phone"] = phone
                except Exception as e:
                    print(f"   • description: {e}")
                    item["description"] = ""
                    item.setdefault("image_url", "")
                    item.setdefault("pickup_zip", "")
                    item.setdefault("contact_email", "")
                    item.setdefault("contact_phone", "")
                if delay > 0:
                    page.wait_for_timeout(int(delay * 1000))
                if (i + 1) % 20 == 0:
                    print(f"   … {i + 1}/{len(needs_fetch)}")
        finally:
            browser.close()
    return listings


def _with_page_param(url: str, page_num: int) -> str:
    """Return ``url`` with the GovDeals page-number query param ``pn`` set to
    ``page_num`` (replacing any existing ``pn``). Pure/string-only so it's unit
    testable without a browser."""
    parts = urlsplit(url)
    q = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k != "pn"]
    q.append(("pn", str(page_num)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _go_to_next_page(page, next_page_num: int) -> bool:
    """
    Navigate to result page ``next_page_num`` via the ``?pn=N`` URL param.
    Returns True if the page loaded with at least one listing card, False once
    we've stepped one past the last page (zero cards).

    Why URL-driven, not click-driven: as of 2026-06 GovDeals' search route
    (/en/search?kWord=…) paginates by the ``pn`` query param — page size is
    hard-capped at 24 regardless of numPerPage/ps — and the old click pager
    (``ul.pagination li[data-type="nextPage"]``) no longer exists. The new
    "google-like" pager only renders a forward control when results span more
    than ~3 pages, so the previous click+aria-label fallback silently stopped
    at page 1 for every term with ≤72 results (and stopped early on big ones).
    Driving ``pn`` directly and stopping on the first empty page is robust to
    both. Past-the-end pages return zero cards (no clamp/duplication).
    """
    target = _with_page_param(page.url or "", next_page_num)
    try:
        page.goto(target, wait_until="load", timeout=_goto_timeout_ms())
    except Exception as e:
        print(f"   [pagination] goto pn={next_page_num} failed: {e}")
        return False

    dismiss_govdeals_overlays(page)
    # networkidle frequently times out on Angular SPAs that stream XHRs;
    # treat that as best-effort, not fatal.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # Wait for cards to hydrate; their absence means we're past the last page.
    try:
        page.wait_for_selector("#myTabContent .card.card-search", state="attached", timeout=12000)
    except Exception:
        print(f"   [pagination] pn={next_page_num}: no cards — last page reached")
        return False
    page.wait_for_timeout(800)
    return True


# ─── JSON API fast path ──────────────────────────────────────────────────────
# GovDeals' Angular UI is backed by an internal JSON search API. Calling it
# directly returns every field we used to scrape from the DOM — crucially the
# long description — in one request per page, with no browser and no headless
# Akamai block. This is the primary scrape path; the Playwright DOM scraper
# (``scrape_listings_via_browser``) stays as an automatic fallback. See GitHub
# issue #6.
MAESTRO_URL = os.getenv("GOVDEALS_MAESTRO_URL", "https://maestro.lqdt1.com")
MAESTRO_SEARCH_PATH = "/search/assets/advanced"
# Anonymous key embedded in the public main.*.js bundle. ``_resolve_maestro_key``
# scrapes it fresh each process (it can rotate); this is the fallback.
MAESTRO_KEY_FALLBACK = "af93060f-337e-428c-87b8-c74b5837d6cd"
GOVDEALS_API_ROWS = int(os.getenv("GOVDEALS_API_ROWS", "120"))
IMAGE_CDN_BASE = "https://webassets.lqdt1.com/assets/photos"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_maestro_key_cache = None


def _singularize_term(term: str) -> str:
    """De-pluralize a search term's trailing word for the JSON API.

    The maestro API matches search tokens **literally** (no stemming), while
    the GovDeals website stems plurals (``chairs`` → ``chair``). Sending the
    plural verbatim under-matches badly — measured: ``"chairs"`` returns 419
    lots vs ``"chair"`` 684, and the 684 is a strict superset (0 lost, 265
    gained). De-pluralizing the last word makes the API return the same
    superset the site shows; the pipeline's quantity + category filters trim
    any over-match. Conservative: only a simple trailing ``s`` (not ``ss``).
    """
    words = term.split()
    if not words:
        return term
    last = words[-1]
    if len(last) > 3 and last.lower().endswith("s") and not last.lower().endswith("ss"):
        words[-1] = last[:-1]
    return " ".join(words)


# The maestro API token-matches "chair" against "Wheelchair"/"Wheel Chair",
# so wheelchair-accessible vans (and other vehicles) come back as chair
# results. Detect them by title so the pipeline can drop them outright.
_VEHICLE_NOUN_RE = re.compile(
    r"\b(?:van|vans|truck|trucks|bus|buses|suv|suvs|sedan|sedans|pickup|"
    r"pickups|minivan|minivans|ambulance|ambulances|motorcycle|motorcycles|"
    r"coupe|hatchback|forklift|forklifts|motorhome|motorhomes|rv|rvs|"
    r"camper|campers)\b",
    re.IGNORECASE,
)
_VEHICLE_YEAR_MAKE_RE = re.compile(
    r"^\s*(?:19|20)\d{2}\s+(?:ford|chevrolet|chevy|dodge|gmc|toyota|honda|"
    r"nissan|freightliner|international|jeep|chrysler|buick|cadillac|hyundai|"
    r"kia|subaru|mercedes|bmw|volkswagen|vw|volvo|mack|peterbilt|kenworth|"
    r"isuzu|hino|ram|lincoln|mercury|pontiac|oldsmobile|gillig|blue\s?bird|"
    r"thomas)\b",
    re.IGNORECASE,
)
_WHEELCHAIR_RE = re.compile(r"\bwheel\s?chairs?\b", re.IGNORECASE)


def _is_vehicle_lot(title: str) -> bool:
    """True when a listing title describes a vehicle, not seating.

    Two signals: (a) "model year + automotive make" prefix ("2016 Ford
    Econoline"); (b) a vehicle noun, AFTER neutralizing "wheelchair" /
    "wheel chair" so the embedded "chair" doesn't read as seating
    ("... Wheel Chair Accesible Van (5244)"). A surviving standalone
    chair word vetoes (b) — a chair lot that merely mentions a van is
    still a chair lot.
    """
    t = title or ""
    if _VEHICLE_YEAR_MAKE_RE.search(t):
        return True
    t = _WHEELCHAIR_RE.sub(" ", t)
    if re.search(r"\bchairs?\b", t, re.IGNORECASE):
        return False
    return bool(_VEHICLE_NOUN_RE.search(t))


def _drop_vehicle_lots(listings: list) -> list:
    """Filter out vehicle lots, logging what was dropped (no silent trims)."""
    kept, dropped = [], []
    for item in listings:
        (dropped if _is_vehicle_lot(item.get("title") or "") else kept).append(item)
    if dropped:
        print(f" → Dropped {len(dropped)} vehicle lot(s):")
        for item in dropped:
            print(f"    ✗ {item.get('title')}")
    return kept


def _dedup_listings(listings: list) -> list:
    """Collapse listings to one per lot, keyed by link (then lot_number /
    title). Shared by both scrape paths — the API path aggregates across many
    overlapping search terms, so the same lot routinely appears more than once."""
    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    return list(dedup.values())


def _resolve_maestro_key() -> str:
    """Return the maestro API key, scraped fresh from the public JS bundle.

    The key lives in the Angular ``main.*.js`` bundle as ``maestroApiKey:"<uuid>"``.
    Scraping it per process guards against the key rotating; falls back to the
    last-known constant on any failure. Cached for the life of the process.
    """
    global _maestro_key_cache
    if _maestro_key_cache:
        return _maestro_key_cache
    key = MAESTRO_KEY_FALLBACK
    try:
        headers = {"User-Agent": _BROWSER_UA}
        html = requests.get(
            "https://www.govdeals.com/en/search?kWord=chairs",
            headers=headers, timeout=15,
        ).text
        m = re.search(r"(main\.[0-9a-f.]+\.js)", html)
        if m:
            bundle = requests.get(
                f"https://www.govdeals.com/{m.group(1)}",
                headers=headers, timeout=20,
            ).text
            km = re.search(r'maestroApiKey:"([0-9a-f-]{8,})"', bundle)
            if km:
                key = km.group(1)
    except Exception as e:
        print(f"   [api] key resolve failed ({e}); using fallback key.")
    _maestro_key_cache = key
    return key


def _search_via_api(term: str, page: int, rows: int = GOVDEALS_API_ROWS) -> list:
    """POST one search page to the maestro JSON API and return the raw
    ``assetSearchResults`` list. Raises on any non-200 so the caller can fall
    back to the browser scraper. ``page`` is 1-indexed (0 clamps to page 1)."""
    headers = {
        "x-api-key": _resolve_maestro_key(),
        "x-user-id": "-1",                          # -1 = anonymous
        "x-api-correlation-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Origin": "https://www.govdeals.com",
        "Referer": "https://www.govdeals.com/",
        "User-Agent": _BROWSER_UA,
    }
    body = {
        "categoryIds": "",          # MUST be a string, not [] — server 400s on an array
        "searchText": term,
        "isQAL": False,
        "page": page,
        "displayRows": rows,
        "sortField": "bestfit",
        "sortOrder": "asc",
        "requestType": "search",
        "responseStyle": "fullResponse",
        "facets": [],
        "facetsFilter": "",
    }
    resp = requests.post(
        f"{MAESTRO_URL}{MAESTRO_SEARCH_PATH}", json=body, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("assetSearchResults") or []


def _asset_to_card(asset: dict) -> dict:
    """Map one ``assetSearchResults`` item to the card dict the pipeline
    consumes (same shape as ``_scrape_one_page``), pre-filling the description /
    image / pickup-zip that the browser path only gets via a per-lot page load."""
    title = (asset.get("assetShortDescription") or "").strip()
    account_id = asset.get("accountId")
    asset_id = asset.get("assetId")
    # Site URL order is /en/asset/{assetId}/{accountId} — verified against the
    # search page's own hrefs 2026-06-12. The reverse order renders GovDeals'
    # "Item not available" page even for live auctions.
    link = (
        f"https://www.govdeals.com/en/asset/{asset_id}/{account_id}"
        if account_id is not None and asset_id is not None else ""
    )
    city = (asset.get("locationCity") or "").strip()
    state = (asset.get("locationState") or "").strip()
    location = ", ".join(p for p in (city, state) if p)

    bid = asset.get("currentBid")
    if bid is None:
        bid = asset.get("assetBidPrice")
    currency = (asset.get("currencyCode") or "USD").strip()
    price = f"{currency} {bid}" if bid is not None else ""

    photo = (asset.get("photo") or "").strip()
    image_url = (
        f"{IMAGE_CDN_BASE}/{account_id}/{photo}"
        if photo and account_id is not None else ""
    )

    description = (asset.get("assetLongDescription") or "").strip()[:4000]
    time_remaining = asset.get("timeRemaining")

    qty = infer_chair_quantity_from_title(title)

    return {
        "title": title,
        "link": link,
        "quantity": qty,
        "quantity_source": "regex_title",
        "quantity_confidence": "low" if qty == 1 else "medium",
        "location": location,
        "price": price,
        "lot_number": str(asset.get("lotNumber") or "").strip(),
        "end_date": (asset.get("assetAuctionEndDate") or "").strip(),
        "time_left": time_remaining.strip() if isinstance(time_remaining, str) else "",
        # Pre-filled enrichment — lets enrich_listings_with_govdeals_descriptions
        # skip the per-lot Playwright page load entirely.
        "description": description,
        "image_url": image_url,
        "pickup_zip": (asset.get("locationZip") or "").strip(),
        "contact_email": "",
        "contact_phone": "",
    }


def scrape_listings_via_api() -> list:
    """Primary scrape path: pull all SEARCH_TERMS from the JSON API. Returns the
    same list-of-card-dicts shape as the browser scraper. Raises if the API is
    unreachable so ``scrape_listings`` can fall back."""
    print("[1] scrape via JSON API (maestro)")
    listings = []
    max_pages = int(os.getenv("GOVDEALS_MAX_PAGES", "200"))
    delay = float(os.getenv("GOVDEALS_API_DELAY_SEC", "0.2"))
    for term in SEARCH_TERMS:
        # The API token-matches literally; the site stems plurals. Search the
        # singular so we get the same superset the website shows (see
        # _singularize_term). Cross-term dupes are collapsed by _dedup_listings.
        api_term = _singularize_term(term)
        term_listings = []
        label = term if api_term == term else f"{term}→{api_term}"
        print(f"\n → Filter: '{label}'")
        page = 1
        while page <= max_pages:
            assets = _search_via_api(api_term, page)
            if not assets:
                break
            term_listings.extend(_asset_to_card(a) for a in assets)
            print(f"   Page {page}: {len(assets)} listings")
            if len(assets) < GOVDEALS_API_ROWS:
                break          # last (partial) page — don't probe the next
            page += 1
            if delay > 0:
                time.sleep(delay)
        listings.extend(term_listings)
        print(f"   Filter '{label}' total: {len(term_listings)} listings")
    unique = _dedup_listings(listings)
    print(f"\n[1] Done scrape_listings_via_api(); {len(unique)} unique lots "
          f"(from {len(listings)} across {len(SEARCH_TERMS)} terms)")
    return unique


def scrape_listings() -> list:
    """Dispatch to the JSON API scraper (fast, no browser) by default, falling
    back to the Playwright DOM scraper on any failure or empty result. Set
    ``GOVDEALS_USE_API=0`` to force the browser path."""
    if os.getenv("GOVDEALS_USE_API", "1") != "0":
        try:
            listings = scrape_listings_via_api()
            if listings:
                return _drop_vehicle_lots(listings)
            print(" → JSON API returned 0 listings; falling back to browser scrape.")
        except Exception as e:
            print(f" → JSON API scrape failed ({e}); falling back to browser scrape.")
    return _drop_vehicle_lots(scrape_listings_via_browser())


def scrape_listings_via_browser():
    print("[1] Starting scrape_listings_via_browser()")
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=os.getenv("HEADLESS", "0") == "1",
            args=govdeals_chromium_launch_args(),
        )
        context = govdeals_browser_context(browser)
        page = context.new_page()

        try:
            for term in SEARCH_TERMS:
                term_listings = []
                print(f"\n → Filter: '{term}'")
                prepare_govdeals_search_page(page, term, script_dir=REPORTS_DIR)
                page_num = 1
                # Safety cap: 24 cards/page, so 200 pages = 4800 lots/term —
                # far above any real chair search. Guards against an unexpected
                # clamp-to-last-page making the pn loop never terminate.
                max_pages = int(os.getenv("GOVDEALS_MAX_PAGES", "200"))

                while page_num <= max_pages:
                    page_cards = _scrape_one_page(page)
                    if not page_cards:
                        if page_num == 1:
                            print(f"   No results for '{term}'.")
                        break
                    term_listings.extend(page_cards)
                    quantities = [c["quantity"] for c in page_cards]
                    print(f"   Page {page_num}: {len(page_cards)} listings, quantities: {quantities}")

                    if not _go_to_next_page(page, page_num + 1):
                        break
                    page_num += 1

                listings.extend(term_listings)
                qty_list = [c["quantity"] for c in term_listings]
                print(f"   Filter '{term}' total: {len(term_listings)} listings, quantities: {qty_list}")

        except Exception as e:
            print(f" → Error during scraping: {e}")
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            err_png = REPORTS_DIR / "govdeals_error.png"
            page.screenshot(path=str(err_png))
            print(f" → Screenshot saved as {err_png}")
        finally:
            browser.close()

    listings_unique = _dedup_listings(listings)
    print(f"\n[1] Done scrape_listings_via_browser(); total unique listings: {len(listings_unique)}")

    # Print every unique listing the scraper found, before any filtering or
    # description enrichment. Useful for debugging coverage and seeing how
    # many lots a single search term actually returns.
    print(f"\n── Unique listings ({len(listings_unique)}) ──")
    for i, item in enumerate(listings_unique, 1):
        qty = item.get("quantity", "?")
        title = (item.get("title") or "")[:70]
        link = item.get("link") or ""
        print(f"  {i:>3}. qty={qty:>4}  {title:<70}  {link}")

    return listings_unique

# Only include listings with quantity above this threshold (e.g. bulk lots)
MIN_CHAIR_QUANTITY = 50

def _rank_with_openai(listings, prompt):
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=2000,
    )
    return resp.choices[0].message.content

def _rank_with_groq(listings, prompt):
    from openai import OpenAI
    client = OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",  # current Groq production model
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=2000,
    )
    return resp.choices[0].message.content

def _rank_with_ollama(listings, prompt):
    url = f"{OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    r = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "")


def _rank_with_claude_max(listings, prompt):
    """Run the ranking prompt through the user's Claude Max subscription via
    the official ``claude-agent-sdk``. Counts against the user's 5-hour Max
    quota; no API key needed beyond an authenticated ``claude`` CLI install.

    Implementation note: ``query()`` is async + returns a stream, so we wrap
    it in ``asyncio.run`` and concatenate every assistant TextBlock into the
    same plain-text shape the other ``_rank_with_*`` helpers return. The
    downstream JSON parser + ``_validate_ranked`` then handle it identically.
    """
    import asyncio
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    model = os.getenv("CLAUDE_MAX_MODEL", "claude-sonnet-4-5")

    async def _run() -> str:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=(
                "You output ONLY raw JSON. No prose, no markdown, no code fences."
            ),
            # Strip every tool — we want a pure text completion, not an agent
            # that might try to read files or run shell commands.
            allowed_tools=[],
            permission_mode="default",
            max_turns=1,
        )
        chunks: list[str] = []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks)

    return asyncio.run(_run())

def rank_with_llm(listings):
    """Deterministic sort by quantity desc, price asc. Name kept for
    backwards-compat with callers; the actual LLM round-trip was pure
    overhead (LLM was only sorting, which is free)."""
    print("[2] Ranking listings (deterministic sort by quantity desc, price asc)…")
    return _fallback_rank(listings)

def _validate_ranked(ranked, *, expected: int) -> tuple[bool, str]:
    """Sanity-check the LLM ranking response.

    Reasons we reject:
      - not a list
      - empty
      - row count differs from input (LLM dropped or duplicated rows)
      - any row is not a dict
      - any row is missing 'rank', 'title', or 'link'
      - any 'rank' is not a positive integer

    Returns ``(ok, reason)``. On failure ``rank_with_llm`` falls back to the
    deterministic Python sort instead of poisoning ``_format_output`` with a
    KeyError 30 minutes into a run.
    """
    if not isinstance(ranked, list):
        return False, f"not a list (got {type(ranked).__name__})"
    if not ranked:
        return False, "empty list"
    if expected and len(ranked) != expected:
        return False, f"row count {len(ranked)} != input {expected}"
    for i, row in enumerate(ranked):
        if not isinstance(row, dict):
            return False, f"row {i} is {type(row).__name__}, not dict"
        for key in ("rank", "title", "link"):
            if key not in row:
                return False, f"row {i} missing key {key!r}"
        try:
            if int(row["rank"]) < 1:
                return False, f"row {i} has non-positive rank {row['rank']!r}"
        except (TypeError, ValueError):
            return False, f"row {i} has non-integer rank {row['rank']!r}"
    return True, "ok"


def _fallback_rank(listings):
    """Deterministic ranking by quantity desc, price asc tiebreaker.

    Used whenever the LLM ranking is missing/broken/validation-rejected.
    Defensive against non-dict rows so even a partially-corrupt input list
    still produces a usable alert.
    """
    def _price_for_sort(p):
        s = str(p or "")
        digits = "".join(c for c in s if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else float("inf")
        except ValueError:
            return float("inf")

    safe = [x for x in listings if isinstance(x, dict)]
    safe.sort(key=lambda x: (-int(x.get("quantity") or 0), _price_for_sort(x.get("price"))))
    for i, listing in enumerate(safe, 1):
        listing["rank"] = i
    return safe

def partition_for_alert(
    listings: list,
    *,
    include_medical: bool = False,
    min_quantity: int = MIN_CHAIR_QUANTITY,
) -> tuple[list, list]:
    """Split scraped rows into ``(alertable, held_back)``.

    ``alertable`` is the historical filter, unchanged: banquet/event chairs
    with an LLM-verified count over ``min_quantity``. The trust gate stays shut
    — a regex count still never becomes a trusted count (BLACKWHOLE-4).

    ``held_back`` is the part that was missing. A lot whose count the LLM could
    not verify used to be dropped with no trace, which is fine when it happens
    to one row and catastrophic when the quantity LLM's provider quota runs out
    mid-sweep and it happens to a third of them. Since 2026-07-20 that has been
    20-56% of every GovDeals run. Lot 420/9312 — "LOT: (199) BANQUET CHAIRS",
    199 chairs in Las Vegas — was scraped daily from 2026-08-05, dropped every
    single time, and its auction closed without the operator ever seeing it.

    So: when a row has no trusted count but its *title states* a bulk count
    outright (``quantity_infer.explicit_title_quantity``), it goes to
    ``held_back`` carrying ``claimed_quantity`` + ``claimed_by`` instead of
    disappearing. Those two keys are the row's only marking — ``quantity`` and
    ``quantity_source`` are left exactly as the LLM left them, so nothing
    downstream can mistake the claim for a verified count.
    """
    from top_chairs import _classify, _is_non_chair_lot, trusted_quantity

    kept: list = []
    held: list = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        cat, _ = _classify(title, item.get("description"))
        if cat == "medical":
            # Medical lots bypass the quantity gate entirely when enabled —
            # preserved from the original filter, where a `return
            # include_medical` short-circuited before the count was read.
            if include_medical:
                kept.append(item)
            continue
        if _is_non_chair_lot(title):
            continue
        q = trusted_quantity(item)
        if q is not None:
            if q > min_quantity:
                kept.append(item)
            continue
        claim = explicit_title_quantity(title)
        if claim and claim[0] > min_quantity:
            flagged = dict(item)
            flagged["claimed_quantity"] = claim[0]
            flagged["claimed_by"] = claim[1]
            held.append(flagged)

    held.sort(key=lambda x: -int(x.get("claimed_quantity") or 0))
    return kept, held


def _alert_on_quantity_degradation(listings: list, *, provider: str) -> None:
    """Telegram-alert when the LLM quantity pass failed for some rows.

    The failure is not a mis-count — it is a disappearance. ``_mark_chunk_failed``
    nulls the regex seed (BLACKWHOLE-4 AC4), and the trust gate then drops every
    such row from ranking and from the alert. This message used to say the
    pipeline was "falling back to regex, so counts may be wrong", which reads as
    a precision problem you can eyeball later; it is really a recall problem you
    cannot see at all. ``partition_for_alert`` now rescues the rows whose titles
    state a count, but a row with no count in its title is still lost, so the
    operator needs the real number here."""
    degraded = [
        it for it in listings
        if it.get("quantity_source") in ("llm_failed", "llm_missing")
    ]
    if not degraded:
        return
    reason = next((it.get("quantity_error") for it in degraded if it.get("quantity_error")), "unknown")
    _send_telegram_plain(
        f"⚠️ GovDeals scrape: quantity LLM ({provider}) FAILED on {len(degraded)}/{len(listings)} "
        f"lots — those lots have NO verified count and are dropped from the ranked alert "
        f"(not mis-counted — omitted). Ones whose title states a count are listed under "
        f"“held back”. First error: {str(reason)[:160]}"
    )


def _send_telegram_plain(text: str) -> None:
    """Short alert (errors, etc.); skips if Telegram not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3900
    if len(text) > max_len:
        text = text[: max_len - 40] + "\n…(truncated)"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True,
                  **_tg_thread(TELEGRAM_TOPIC_HEALTH)},
            timeout=15,
        )
        r.raise_for_status()
        print(" → Telegram LLM alert sent.")
    except Exception as ex:
        print(f" → Could not send Telegram LLM alert: {ex}")


_QTY_SOURCE_TAG = {
    "regex_title":    "🔡t",   # title-only regex (weakest)
    "regex_fulltext": "🔡f",   # title+description regex
    "llm":            "🤖",
    "llm_failed":     "⚠LLM_FAILED",
    "llm_missing":    "⚠LLM_MISSING",
}


def _format_output(listings):
    """Build Telegram message body: quantity, end date, time left.

    Defensive against incomplete rows: every field is read with ``.get()`` so
    a single malformed listing can never crash the entire alert after a long
    scrape. Missing rank → ``?`` (and we add a fallback enumeration so the
    output is still ordered). Missing title/link → ``(no title)`` / no arrow.
    The unit tests in ``tests/test_formatting.py`` exercise every branch.
    """
    lines = [f"🪑 GovDeals chairs (qty > {MIN_CHAIR_QUANTITY}), ranked by quantity\n"]
    failed = sum(
        1
        for x in listings
        if isinstance(x, dict) and x.get("quantity_source") in ("llm_failed", "llm_missing")
    )
    if failed:
        lines.append(
            f"⚠ {failed}/{len(listings)} listings had LLM quantity failures "
            f"(no verified count — omitted from the ranking, not mis-counted)\n"
        )
    for fallback_idx, item in enumerate(listings, 1):
        if not isinstance(item, dict):
            # Should never happen, but a stray non-dict in the list is no
            # reason to drop the rest of the alert.
            lines.append(f"#?: (malformed listing: {type(item).__name__})\n")
            continue
        qty = item.get("quantity", "?")
        end_date = item.get("end_date") or "N/A"
        time_left = item.get("time_left") or "N/A"
        conf = item.get("quantity_confidence")
        src = item.get("quantity_source") or ""
        rank = item.get("rank", fallback_idx)  # ← was item['rank']; that's the crash from prod
        title = item.get("title") or "(no title)"
        link = item.get("link") or ""
        price = item.get("price") or "N/A"
        qty_line = f"Qty: {qty}"
        if conf:
            qty_line += f" ({conf})"
        # Surface where the quantity came from, so a 🔡t row stands out from
        # a 🔡f / 🤖 row at a glance — and LLM failures are still loud.
        tag = _QTY_SOURCE_TAG.get(src)
        if tag:
            qty_line += f" {tag}"
        block = (
            f"#{rank}: {title}\n"
            f"{qty_line} · {price}\n"
            f"Ends: {end_date} · Time left: {time_left}\n"
        )
        if link:
            block += f"↗ {link}\n"
        lines.append(block)
    return "\n".join(lines)

def _format_held_back(held):
    """Render the lots with no verified count whose own title states one.

    These are exactly the lots the pipeline used to drop in silence. The
    quantity LLM failed on them, so ``trusted_quantity`` rightly refuses to
    speak — but the title says "LOT: (199) BANQUET CHAIRS" in plain English,
    and throwing that away cost us lot 420/9312 for eleven days.

    So we show the claim, label it unverified, and name the pattern that read
    it. The operator opens the link and decides in five seconds. Nothing here
    is ever fed back as a trusted quantity.
    """
    if not held:
        return ""
    lines = [
        f"❓ {len(held)} lot(s) with NO verified count whose title claims "
        f"> {MIN_CHAIR_QUANTITY} chairs — unverified, open the link:\n"
    ]
    for item in held:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "(no title)"
        claim = item.get("claimed_quantity", "?")
        why = item.get("claimed_by") or "title"
        price = item.get("price") or "N/A"
        end_date = item.get("end_date") or "N/A"
        time_left = item.get("time_left") or "N/A"
        link = item.get("link") or ""
        block = (
            f"• {title}\n"
            f"Title claims ~{claim} [{why}] · unverified · {price}\n"
            f"Ends: {end_date} · Time left: {time_left}\n"
        )
        if link:
            block += f"↗ {link}\n"
        lines.append(block)
    return "\n".join(lines)


def _compose_alert(listings, held_back=None, max_len=4000):
    """Join the ranked section and the held-back section under Telegram's cap.

    Held-back lots are protected from truncation. They are the ones we were
    already losing, and a message that silently cuts them off reproduces the
    original bug at a different layer — so the ranked section absorbs the
    trimming instead.
    """
    ranked_body = _format_output(listings) if listings else ""
    held_body = _format_held_back(held_back or [])
    if not held_body:
        if len(ranked_body) > max_len:
            ranked_body = ranked_body[: max_len - 20] + "\n…(truncated)"
        return ranked_body
    if len(held_body) > max_len:
        return held_body[: max_len - 20] + "\n…(truncated)"
    room = max_len - len(held_body) - 2
    if len(ranked_body) > room:
        ranked_body = ranked_body[: max(0, room - 20)] + "\n…(truncated)" if room > 20 else ""
    return "\n".join(p for p in (ranked_body, held_body) if p.strip())


def send_telegram(listings, held_back=None):
    """Send final ranked output to Telegram. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

    ``held_back`` carries lots with no verified quantity whose title states a
    bulk count — rendered in their own clearly-unverified section.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" → Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); skipping.")
        return
    print("[3a] Sending to Telegram…")
    # Telegram max message length 4096
    body = _compose_alert(listings, held_back, max_len=4000)
    # Guard against empty text (Telegram 400: message text is empty)
    if not body or not body.strip():
        body = "GovDeals automation: no listings, test message."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": body, "disable_web_page_preview": True,
                  **_tg_thread(TELEGRAM_TOPIC_DEALS)},
            timeout=10,
        )
        r.raise_for_status()
        print(" → Telegram message sent.")
    except Exception as e:
        print(f" → Telegram error: {e}")


MOCK_LISTINGS = [
    {"title": "Banquet Reception Chairs", "link": "https://www.govdeals.com/en/asset/3597/1407", "quantity": 1, "location": "Clarksville, Tennessee, USA", "price": "USD 15.00", "lot_number": "LOT#: 1407-3597"},
    {"title": "Lot of Banquet Stacking Chairs - approximately 39...", "link": "https://www.govdeals.com/en/asset/783/19142", "quantity": 39, "location": "Ardrossan, Alberta, CAN", "price": "CAD 3,950.00", "lot_number": "LOT#: 19142-783"},
    {"title": "Blue Conference/Banquet Chairs (370)", "link": "https://www.govdeals.com/en/asset/195/1405", "quantity": 370, "location": "Jefferson, Georgia, USA", "price": "USD 12.00", "lot_number": "LOT#: 1405-195"},
]

def _preflight_formatting_tests() -> bool:
    """Run tests/test_formatting.py BEFORE the slow scrape.

    Cost: ~10ms. Benefit: catches a broken edit to _format_output /
    _validate_ranked / _fallback_rank instantly, instead of after a 30-minute
    scrape. Set ``SKIP_PREFLIGHT_TESTS=1`` to bypass (e.g. cron in a recovery
    state). On failure we abort the run rather than risk crashing on the
    Telegram-send step at the very end.
    """
    if os.getenv("SKIP_PREFLIGHT_TESTS") == "1":
        return True
    test_path = os.path.join(_SCRIPT_DIR, "tests", "test_formatting.py")
    if not os.path.exists(test_path):
        print(f" → preflight: tests/test_formatting.py missing ({test_path}); continuing without it")
        return True
    print("[0] Pre-flight: running formatting tests…")
    from importlib import util as _imp_util
    spec = _imp_util.spec_from_file_location("test_formatting", test_path)
    if spec is None or spec.loader is None:
        print(" → preflight: could not load tests/test_formatting.py; aborting")
        return False
    mod = _imp_util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        rc = mod.main()
    except Exception as e:
        print(f" → preflight: tests/test_formatting.py raised {type(e).__name__}: {e}")
        return False
    if rc != 0:
        print(" → preflight: formatting tests FAILED — aborting before the scrape.")
        print("   Fix the failing tests, or set SKIP_PREFLIGHT_TESTS=1 to bypass.")
        return False
    return True


def main():
    import sys
    use_test_data = os.getenv("RUN_TEST") == "1" or "--test" in sys.argv
    print("=== GovDeals chairs extraction ===")
    if not _preflight_formatting_tests():
        return
    if use_test_data:
        print("(Test mode: using mock listings, no scrape)")
        listings = list(MOCK_LISTINGS)
    else:
        listings = scrape_listings()
    if not listings:
        print("No listings found. Exiting.")
        return

    listings = enrich_listings_with_govdeals_descriptions(listings)

    # Always run the fulltext regex pass when descriptions are available — it's
    # free, deterministic, and recovers most quantities the title-only regex
    # missed (Lot of (12), Qty 200, etc. usually live in the description).
    if any(item.get("description") for item in listings):
        print("[1c] Refining quantities with regex on title+description…")
        listings = refine_quantities_with_regex_fulltext(listings)
    else:
        print("[1c] Skipping regex(fulltext) — no descriptions on any listing.")
        print("     Set FETCH_GOVDEALS_DESCRIPTION=1 in .env to enable.")

    if _llm_quantity_enabled():
        print("[1d] Refining quantities with LLM (title + description when present)…")
        listings = refine_quantities_with_llm(
            listings,
            provider=QUANTITY_LLM_PROVIDER,
            ollama_base_url=OLLAMA_BASE_URL,
            ollama_model=OLLAMA_MODEL,
            ollama_timeout=OLLAMA_TIMEOUT,
            groq_api_key=GROQ_API_KEY,
            openai_api_key=OPENAI_API_KEY,
            gemini_api_key=GEMINI_API_KEY,
        )
        _alert_on_quantity_degradation(listings, provider=QUANTITY_LLM_PROVIDER)

    # Persist EVERY processed listing to the SQLite cache before the
    # quantity filter — including small lots we're about to drop, so future
    # runs don't waste a Playwright page load on them either.
    cache_counts = listings_db.store_listings(listings)
    if cache_counts.get("disabled"):
        print(f"[1e] Cache disabled (LISTINGS_DB_DISABLE=1); {cache_counts['disabled']} listings not stored.")
    else:
        print(
            f"[1e] Cache: +{cache_counts['insert']} new, "
            f"~{cache_counts['update']} updated, "
            f"{cache_counts['skip']} skipped (uncacheable URL)."
        )

    # Keep only banquet/event chairs over MIN_CHAIR_QUANTITY (after LLM fixups).
    # Drop non-chair lots that merely share a chair-ish word (scales, stools,
    # recliners, …). Medical exam/dental lots are gated behind INCLUDE_MEDICAL
    # (default off) for the future medical vertical. Cache keeps every row.
    include_medical = os.getenv("INCLUDE_MEDICAL") == "1"
    listings, held_back = partition_for_alert(
        listings, include_medical=include_medical, min_quantity=MIN_CHAIR_QUANTITY
    )
    if held_back:
        print(
            f" → {len(held_back)} lot(s) held back: no verified count, but the "
            f"title claims > {MIN_CHAIR_QUANTITY} chairs. Reported separately."
        )
    if not listings:
        print(f"No banquet chairs with quantity > {MIN_CHAIR_QUANTITY}.")
        if held_back:
            # Do NOT return here. An LLM outage can leave every lot unverified,
            # and returning early is exactly how lot 420/9312 ("LOT: (199)
            # BANQUET CHAIRS") went unreported for eleven days while its
            # auction ran and closed.
            send_telegram([], held_back=held_back)
        else:
            print("Exiting.")
        return
    print(f" → {len(listings)} banquet-chair listings kept (qty > {MIN_CHAIR_QUANTITY})")
    ranked = rank_with_llm(listings)
    if not ranked:
        print("Ranking failed or returned empty. Exiting.")
        return
    send_telegram(ranked, held_back=held_back)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("(Telegram not configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to receive output.)")
    print("=== Script complete ===")

if __name__ == "__main__":
    main()
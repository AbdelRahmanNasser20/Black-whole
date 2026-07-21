#!/usr/bin/env python3
"""
public_surplus_banquet_chairs_alert.py

1. Uses Playwright to scrape PublicSurplus.com for chair-related keywords (same terms as GovDeals)
2. Extracts title, link, quantity (from title text), price, location (state), time left
3. Sends that data to an LLM (OpenAI, Ollama local, or Groq) to rank by "best deal"
4. Sends the ranked list to Telegram

Setup (same .env as govdeals_chairs_extraction.py in auction_extractors/):
- Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- LLM: LLM_PROVIDER=openai|ollama|groq (+ corresponding API keys / Ollama URL)
"""

import html as html_lib
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

from paths import REPORTS_DIR
from quantity_infer import infer_chair_quantity_from_title
from quantity_llm import refine_quantities_with_llm
import listings_db

load_dotenv()
_SCRIPT_DIR = os.path.dirname(__file__)
_LOCAL_ENV = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_LOCAL_ENV):
    load_dotenv(_LOCAL_ENV, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_TOPIC_DEALS = os.getenv("TELEGRAM_TOPIC_DEALS")
TELEGRAM_TOPIC_HEALTH = os.getenv("TELEGRAM_TOPIC_HEALTH")


def _tg_thread(raw):
    """message_thread_id fragment for forum-topic routing (BLACKWHOLE-24).
    Empty dict when unset so behavior is unchanged."""
    try:
        return {"message_thread_id": int(raw)} if raw and str(raw).strip() else {}
    except (TypeError, ValueError):
        return {}

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Quantity pass can use a higher-quota provider than ranking (see govdeals note).
QUANTITY_LLM_PROVIDER = (os.getenv("QUANTITY_LLM_PROVIDER") or LLM_PROVIDER).strip().lower()

try:
    OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
except ValueError:
    OLLAMA_TIMEOUT = 600

BASE_URL = "https://www.publicsurplus.com"

# Same search terms as govdeals_chairs_extraction.py (this folder)
SEARCH_TERMS = [
    "stackable chairs",
    "banquet chairs",
    "chairs",
    "church chairs",
    "event chairs",
    "conference chairs",
    # medical vertical — single-unit lots (qty filter gated by category)
    "dental chair",
    "exam chair",
    "treatment chair",
    "phlebotomy chair",
    "procedure chair",
    "exam table",
]

MIN_CHAIR_QUANTITY = 50
MAX_SEARCH_PAGES = 40

# Public Surplus has no JSON API (legacy JSP site), but every search and
# detail page is fully server-rendered and served to plain ``requests`` with
# no bot block — so the fast path is direct HTTP + HTML parse, no browser.
# Same effect as the GovDeals maestro path (GitHub issue #10 / PR #7 pattern):
# the Playwright scraper below stays as an automatic fallback.
PS_PAGE_SIZE = 25  # server-fixed cards per search page


def _descriptions_enabled() -> bool:
    """Description fetch defaults ON — it's a plain HTTP GET per uncached lot
    now, and the LLM quantity pass is only trustworthy with descriptions."""
    return os.getenv("FETCH_PUBLIC_SURPLUS_DESCRIPTION", "1") == "1"


def _llm_quantity_enabled() -> bool:
    """LLM quantity pass defaults ON. The title-regex value seeded at parse
    time is untrusted (it reads lot numbers like "LOT #142" as counts) and
    ships only when the LLM call fails, tagged ``quantity_source=llm_failed``."""
    return os.getenv("USE_LLM_QUANTITY", "1") == "1"


def _use_http_fast_path() -> bool:
    """Plain-HTTP scrape now defaults OFF (browser is primary).

    PS migrated to the ps-v2 site and anti-bot-gates search results: a plain
    ``requests`` GET reliably returns only the empty shell (``noAuctionsFound``,
    0 cards), so the old fast path just wastes a round-trip before the browser
    fallback runs. Opt back in with ``PUBLICSURPLUS_USE_API=1`` if PS ever
    re-opens server-rendered results to non-browser clients."""
    return os.getenv("PUBLICSURPLUS_USE_API", "0") == "1"


def _browser_fallback_enabled() -> bool:
    """Whether we may fall back to Playwright when an HTTP fetch comes up empty.

    Defaults ON — on the operator's Mac the browser is the primary path. The
    cloud image ships no Chromium on purpose (see Dockerfile), so the discovery
    cron sets ``PUBLICSURPLUS_ALLOW_BROWSER=0``: there, a failed HTTP fetch must
    degrade to "skip it" rather than raise out of the run. Without this guard a
    single flaky detail fetch launches Chromium, throws, and costs us every PS
    row for the day."""
    return os.getenv("PUBLICSURPLUS_ALLOW_BROWSER", "1") == "1"


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HTTP_HEADERS = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

# Each result renders twice (grid + list view); we parse the grid copy. All
# field elements carry the auction id in their DOM id, so every extract below
# is anchored to the card's own id — no positional guessing.
_GRID_CARD_RE = re.compile(r'<div class="auction-item" id="(\d+)searchGrid">')


def _parse_search_cards(html: str) -> list:
    """Parse one server-rendered search page into the card dicts the pipeline
    consumes (same shape as ``_scrape_one_page``), pre-filling ``image_url``
    and ``end_date`` — both embedded in the search HTML, neither captured by
    the browser path."""
    matches = list(_GRID_CARD_RE.finditer(html))
    cards = []
    for i, m in enumerate(matches):
        auc_id = m.group(1)
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        seg = html[m.start():seg_end]

        tm = re.search(
            r'<a\s[^>]*href="(/sms/auction/view\?auc=%s)"[^>]*title="([^"]+)"'
            % auc_id, seg)
        if not tm:
            continue
        link = BASE_URL + tm.group(1)
        title = html_lib.unescape(tm.group(2)).strip()

        price = ""
        pm = re.search(r'id="val_%ssearchGrid"[^>]*>\s*([^<]+)' % auc_id, seg)
        if pm:
            price = pm.group(1).strip()

        time_left = ""
        tlm = re.search(
            r'id="timeLeftValue%ssearchGrid"[^>]*>\s*([^<]+)' % auc_id, seg)
        if tlm:
            time_left = tlm.group(1).strip()

        # The card's countdown script carries the auction end as epoch millis:
        # updateTimeLeftSpan(timeLeftInfoMap, <auc>, "<auc>searchGrid", <now>, <end>, …)
        end_date = ""
        em = re.search(
            r'updateTimeLeftSpan\(\s*timeLeftInfoMap,\s*%s,\s*"[^"]*",\s*\d+,\s*(\d+)'
            % auc_id, seg)
        if em:
            end_date = datetime.fromtimestamp(
                int(em.group(1)) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        location = ""
        lm = re.search(r'auction-item-state[^>]*>\s*([^<]*)', seg)
        if lm:
            location = lm.group(1).strip()

        image_url = ""
        im = re.search(r'<img[^>]+src="(https://[^"]+/sms/docviewer/[^"]+)"', seg)
        if im:
            # The grid embeds the 120x90 "thumb-b" rendition — blurry on any
            # card bigger than a postage stamp. The same path with "thumb-l"
            # serves 1280x960.
            image_url = im.group(1).replace("/thumb-b/", "/thumb-l/")

        qty = infer_chair_quantity_from_title(title)
        cards.append({
            "title": title,
            "link": link,
            "quantity": qty,
            "quantity_source": "regex_title",
            "quantity_confidence": "low" if qty == 1 else "medium",
            "location": location,
            "price": price,
            "lot_number": f"AUC#{auc_id}",
            "end_date": end_date,
            "time_left": time_left,
            "image_url": image_url,
        })
    return cards


def _parse_detail_page(html: str) -> tuple[str, str]:
    """Extract ``(description, image_url)`` from one auction detail page —
    the plain-HTTP equivalent of ``_fetch_public_surplus_description``."""
    image_url = ""
    im = re.search(r'https://[^"\']+/sms/docviewer/cdnaucdoc/[^"\']+', html)
    if im:
        # Same thumb-b → thumb-l upgrade as the search-grid parse: the page
        # embeds the 120x90 rendition; thumb-l is 1280x960.
        image_url = html_lib.unescape(im.group(0)).replace("/thumb-b/", "/thumb-l/")

    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html,
                  flags=re.S | re.I)
    text = html_lib.unescape(re.sub(r'<[^>]+>', '\n', text))
    text = re.sub(r'[ \t\r]+', ' ', text)
    text = re.sub(r'\n\s*', '\n', text).strip()
    description = ""
    dm = re.search(
        r"Description[:\s]+\s*(.+?)(?:\n\s*(?:Seller|Location|Shipping|Time Left|Quantity)\b|$)",
        text, flags=re.IGNORECASE | re.DOTALL)
    if dm:
        description = dm.group(1).strip()[:4000]
    return description, image_url


def _dedup(listings: list) -> list:
    """One card per lot, keyed by link (then lot_number / title) — shared by
    both scrape paths."""
    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    return list(dedup.values())


def scrape_listings_via_http() -> list:
    """Primary scrape path: plain HTTP GET per search page, stdlib HTML parse.
    Raises if the site is unreachable so ``scrape_listings`` can fall back."""
    print("[1] scrape via plain HTTP (server-rendered HTML, no browser)")
    listings = []
    delay = float(os.getenv("PUBLICSURPLUS_HTTP_DELAY_SEC", "0.2"))
    for term in SEARCH_TERMS:
        term_listings = []
        print(f"\n → Filter: '{term}'")
        page_num = 0
        while page_num < MAX_SEARCH_PAGES:
            resp = requests.get(
                _search_url(term, page_num), headers=_HTTP_HEADERS, timeout=30)
            resp.raise_for_status()
            page_cards = _parse_search_cards(resp.text)
            if not page_cards:
                if page_num == 0:
                    print(f"   No results for '{term}'.")
                break
            term_listings.extend(page_cards)
            print(f"   Page {page_num}: {len(page_cards)} listings")
            if len(page_cards) < PS_PAGE_SIZE:
                break
            page_num += 1
            if delay > 0:
                time.sleep(delay)
        listings.extend(term_listings)
        print(f"   Filter '{term}' total: {len(term_listings)} listings")
    unique = _dedup(listings)
    print(f"\n[1] Done scrape_listings_via_http(); {len(unique)} unique lots "
          f"(from {len(listings)} across {len(SEARCH_TERMS)} terms)")
    return unique


def _auction_id_from_card(card) -> str:
    try:
        div_id = card.get_attribute("id") or ""
        m = re.match(r"^(\d+)", div_id)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _scrape_one_page(page) -> list:
    page_listings = []
    cards = page.locator(".auction-item")
    count = cards.count()
    for i in range(count):
        card = cards.nth(i)
        try:
            title_el = card.locator("h6.ps-card-feat__body--title a, .auction-item-body h6.card-title a").first
            title = (title_el.get_attribute("title") or title_el.inner_text() or "").strip()
            if not title:
                continue
            href = title_el.get_attribute("href") or ""
            if href.startswith("/"):
                link = BASE_URL + href
            elif href.startswith("http"):
                link = href
            else:
                link = BASE_URL + "/" + href.lstrip("/")

            location = ""
            try:
                location = card.locator(".auction-item-state").first.inner_text().strip()
            except Exception:
                pass

            price = ""
            try:
                price = card.locator('b[id^="val_"]').first.inner_text().strip()
            except Exception:
                pass

            auc_id = _auction_id_from_card(card)
            lot_number = f"AUC#{auc_id}" if auc_id else ""

            time_left = ""
            end_date = ""
            try:
                time_left = card.locator('[id^="timeLeftValue"]').first.inner_text().strip()
            except Exception:
                pass

            qty = infer_chair_quantity_from_title(title)

            page_listings.append({
                "title": title,
                "link": link,
                "quantity": qty,
                # Tag provenance so the fulltext refine / LLM pass know which
                # rows to touch — mirrors GovDeals seeding.
                "quantity_source": "regex_title",
                "quantity_confidence": "low" if qty == 1 else "medium",
                "location": location,
                "price": price,
                "lot_number": lot_number,
                "end_date": end_date,
                "time_left": time_left,
            })
        except Exception as e:
            print(f"   • Error scraping card {i + 1}: {e}")
            continue
    return page_listings


def _fetch_public_surplus_description(page, url: str) -> tuple[str, str]:
    """Load a Public Surplus auction page, return ``(description, image_url)``.

    PS markup varies a lot (old templates vs. new). We try several known
    containers in order of specificity and fall back to a ``Description:``
    section anchor. Image selector is best-effort on the same page load.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)

    selectors = (
        "#auctionDescription",
        ".auction-description",
        ".description",
        'div[itemprop="description"]',
        "#description",
        ".auction-item-description",
    )
    description = ""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            text = (loc.inner_text(timeout=2500) or "").strip()
            if text and text.lower() not in ("description", "description:"):
                description = text[:4000]
                break
        except Exception:
            continue

    if not description:
        # Fallback: grab the text block that immediately follows a "Description:"
        # label. Works for the legacy PS template.
        try:
            body = page.locator("body").inner_text(timeout=4000) or ""
            m = re.search(
                r"Description[:\s]+\s*(.+?)(?:\n\s*(?:Seller|Location|Shipping|Time Left|Quantity)\b|$)",
                body,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m:
                description = m.group(1).strip()[:4000]
        except Exception:
            pass

    # Image: first non-placeholder src among the usual suspects.
    image_url = ""
    img_selectors = (
        ".auction-photo img",
        "img.ps-card-feat__image",
        ".carousel-item.active img",
        '.auction-item img[src*="auction" i]',
        "#auctionImage img",
        "img[itemprop='image']",
    )
    for sel in img_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            src = loc.get_attribute("src", timeout=1500) or ""
            if src and not src.startswith("data:") and "placeholder" not in src.lower():
                image_url = src
                break
        except Exception:
            continue

    return description, image_url


def enrich_listings_with_descriptions(listings: list) -> list:
    """Mirror of the GovDeals pipeline step: cache-first description fetch.

    - Hydrates from the listings_db cache first (keyed by ``ps:<aucId>``).
    - Relists (same auc, new end_date) are treated as cache misses so we
      re-fetch the fresh description.
    - Listings that still have no description after hydration get a live
      Playwright fetch.
    """
    if not _descriptions_enabled():
        return listings

    hits, _, relists = listings_db.hydrate_from_cache(listings)
    needs_fetch = [x for x in listings if not (x.get("description") or "").strip()]
    if hits:
        print(f"[1b] Cache hit on {hits}/{len(listings)} listings (description reused).")
    if relists:
        print(f"[1b] {relists} relist(s) detected (end_date changed) — re-fetching fresh.")
    if not needs_fetch:
        print("[1b] All descriptions served from cache; no live fetch needed.")
        return listings

    delay = float(os.getenv("FETCH_PUBLIC_SURPLUS_DELAY_SEC", "0.35"))
    print(f"[1b] Fetching descriptions for {len(needs_fetch)} uncached listings…")

    # HTTP first — detail pages are server-rendered too. Playwright only for
    # the listings whose HTTP fetch errored (network / non-200).
    if os.getenv("PUBLICSURPLUS_USE_API", "1") != "0":
        failed = []
        for i, item in enumerate(needs_fetch):
            link = item.get("link")
            if not link:
                item["description"] = ""
                item.setdefault("image_url", "")
                continue
            try:
                resp = requests.get(link, headers=_HTTP_HEADERS, timeout=30)
                resp.raise_for_status()
                desc, img = _parse_detail_page(resp.text)
                item["description"] = desc
                if img and not item.get("image_url"):
                    item["image_url"] = img
                item.setdefault("image_url", "")
            except Exception as e:
                print(f"   • description (http): {e}")
                failed.append(item)
            if delay > 0:
                time.sleep(delay)
            if (i + 1) % 20 == 0:
                print(f"   … {i + 1}/{len(needs_fetch)}")
        if not failed:
            return listings
        if not _browser_fallback_enabled():
            # Cloud image has no Chromium — keep the rows (quantity still comes
            # from the title regex + LLM) instead of raising and losing the run.
            print(f"[1b] {len(failed)} HTTP fetch failure(s); browser fallback "
                  "disabled — keeping those listings without descriptions.")
            for item in failed:
                item["description"] = ""
                item.setdefault("image_url", "")
            return listings
        print(f"[1b] {len(failed)} HTTP fetch failure(s) — Playwright fallback for those.")
        needs_fetch = failed

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=os.getenv("HEADLESS", "0") == "1",
            args=[
                "--disable-http2",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = browser.new_page()
        try:
            for i, item in enumerate(needs_fetch):
                link = item.get("link")
                if not link:
                    item["description"] = ""
                    item.setdefault("image_url", "")
                    continue
                try:
                    desc, img = _fetch_public_surplus_description(page, link)
                    item["description"] = desc
                    item["image_url"] = img
                except Exception as e:
                    print(f"   • description: {e}")
                    item["description"] = ""
                    item.setdefault("image_url", "")
                if delay > 0:
                    page.wait_for_timeout(int(delay * 1000))
                if (i + 1) % 20 == 0:
                    print(f"   … {i + 1}/{len(needs_fetch)}")
        finally:
            browser.close()
    return listings


def _search_url(term: str, page_idx: int) -> str:
    q = urllib.parse.urlencode({"posting": "y", "keyWord": term, "page": page_idx})
    return f"{BASE_URL}/sms/browse/search?{q}"


def scrape_listings() -> list:
    """Browser scrape is primary (PS anti-bot-gates server-rendered results to
    non-browser clients). The plain-HTTP fast path is opt-in via
    ``PUBLICSURPLUS_USE_API=1`` and still falls back to the browser on empty/
    error."""
    if _use_http_fast_path():
        try:
            listings = scrape_listings_via_http()
            if listings:
                return listings
            print(" → HTTP scrape returned 0 listings; falling back to browser scrape.")
        except Exception as e:
            print(f" → HTTP scrape failed ({e}); falling back to browser scrape.")
    if not _browser_fallback_enabled():
        print(" → Browser fallback disabled (PUBLICSURPLUS_ALLOW_BROWSER=0); "
              "no listings this run.")
        return []
    return scrape_listings_via_browser()


# Page states after a search navigation: result cards (.auction-item), the
# explicit empty marker (#noAuctionsFound, un-hidden by JS when a search truly
# has no hits), or — under anti-bot throttling — neither (the ps-v2 shell).
# Wait for either of the first two; on the third, back off and reload before
# giving up so a transient throttle isn't misread as "no results".
_RESULTS_OR_EMPTY = ".auction-item, #noAuctionsFound:not(.d-none)"


def _wait_for_search_results(page, attempts: int = 3, backoff_sec: float = 4.0) -> bool:
    """Return True if result cards rendered, False if confirmed empty/blocked.

    Reloads with linear backoff to ride out transient PS throttling (the shell
    response carries neither cards nor the un-hidden empty marker)."""
    for attempt in range(max(1, attempts)):
        try:
            page.wait_for_selector(_RESULTS_OR_EMPTY, timeout=15000)
            return page.locator(".auction-item").count() > 0
        except Exception:
            if attempt < attempts - 1:
                page.wait_for_timeout(int(backoff_sec * 1000 * (attempt + 1)))
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
    return False


def scrape_listings_via_browser():
    print("[1] Starting scrape_listings_via_browser() (Public Surplus)")
    listings = []
    # Polite pacing — PS throttles bursty automated access. Tunable via env.
    page_delay = float(os.getenv("PUBLICSURPLUS_PAGE_DELAY_SEC", "1.5"))
    term_delay = float(os.getenv("PUBLICSURPLUS_TERM_DELAY_SEC", "2.5"))
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=os.getenv("HEADLESS", "0") == "1",
            args=[
                "--disable-http2",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = browser.new_page()
        try:
            # Warm-up visit (cookies + session). Best-effort: a reset/timeout
            # here must not abort the whole scrape.
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.locator("text='Yes, I Accept Cookies'").click(timeout=4000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
            except Exception as e:
                print(f" → Warm-up visit failed ({e}); continuing to search.")

            for term in SEARCH_TERMS:
                term_listings = []
                print(f"\n → Filter: '{term}'")
                # Per-term isolation: a connection reset / timeout on one term
                # (PS throttling is bursty) must not kill the remaining terms.
                try:
                    page_num = 0
                    while page_num < MAX_SEARCH_PAGES:
                        url = _search_url(term, page_num)
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(600)
                        if not _wait_for_search_results(page):
                            if page_num == 0:
                                print(f"   No results (or blocked) for '{term}'.")
                            break

                        page_cards = _scrape_one_page(page)
                        if not page_cards:
                            if page_num == 0:
                                print(f"   No results for '{term}'.")
                            break
                        term_listings.extend(page_cards)
                        quantities = [c["quantity"] for c in page_cards]
                        print(f"   Page {page_num}: {len(page_cards)} listings, quantities: {quantities}")

                        if len(page_cards) < 25:
                            break
                        page_num += 1
                        page.wait_for_timeout(int(page_delay * 1000))
                except Exception as e:
                    print(f"   • '{term}' failed ({e}); skipping to next term.")

                if term_delay > 0:
                    time.sleep(term_delay)
                listings.extend(term_listings)
                qty_list = [c["quantity"] for c in term_listings]
                print(f"   Filter '{term}' total: {len(term_listings)} listings, quantities: {qty_list}")

        except Exception as e:
            print(f" → Error during scraping: {e}")
            try:
                REPORTS_DIR.mkdir(parents=True, exist_ok=True)
                err_png = REPORTS_DIR / "publicsurplus_error.png"
                page.screenshot(path=str(err_png))
                print(f" → Screenshot saved as {err_png}")
            except Exception:
                pass
        finally:
            browser.close()

    listings_unique = _dedup(listings)
    print(f"\n[1] Done scrape_listings_via_browser(); total unique listings: {len(listings_unique)}")
    return listings_unique


def rank_with_llm(listings):
    """Deterministic sort by quantity desc, price asc. Name kept for
    backwards-compat; was an LLM call (pure overhead — sort is free)."""
    print("[2] Ranking listings (deterministic sort by quantity desc, price asc)…")
    return _fallback_rank(listings)


def _fallback_rank(listings):
    """Sort by quantity desc, price asc tiebreaker, then assign rank=1..N."""
    def _price_for_sort(p):
        s = str(p or "")
        digits = "".join(c for c in s if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else float("inf")
        except ValueError:
            return float("inf")

    safe = [x for x in listings if isinstance(x, dict)]
    safe.sort(key=lambda x: (-int(x.get("quantity") or 0), _price_for_sort(x.get("price"))))
    for i, item in enumerate(safe, 1):
        item["rank"] = i
    return safe


def _format_output(listings):
    lines = ["🪑 Public Surplus chairs (bulk lots), ranked by quantity\n"]
    for item in listings:
        qty = item.get("quantity", "?")
        end_date = item.get("end_date") or "N/A"
        time_left = item.get("time_left") or "N/A"
        lines.append(
            f"#{item['rank']}: {item['title']}\n"
            f"Qty: {qty} · {item.get('price', 'N/A')}\n"
            f"Ends: {end_date} · Time left: {time_left}\n"
            f"↗ {item['link']}\n"
        )
    return "\n".join(lines)


def _alert_on_quantity_degradation(listings: list, *, provider: str) -> None:
    """Telegram-alert when the LLM quantity pass failed for some rows so the
    pipeline is back on the (unreliable) regex value — silent regex fallback is
    how big lots get the wrong count and disappear from results."""
    degraded = [
        it for it in listings
        if it.get("quantity_source") in ("llm_failed", "llm_missing")
    ]
    if not degraded or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    reason = next((it.get("quantity_error") for it in degraded if it.get("quantity_error")), "unknown")
    text = (
        f"⚠️ Public Surplus scrape: quantity LLM ({provider}) FAILED on "
        f"{len(degraded)}/{len(listings)} lots — falling back to regex, counts may be wrong. "
        f"First error: {str(reason)[:160]}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True,
                  **_tg_thread(TELEGRAM_TOPIC_HEALTH)},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f" → Telegram degradation-alert error: {e}")


def send_telegram(listings):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" → Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); skipping.")
        return
    print("[3a] Sending to Telegram…")
    body = _format_output(listings)
    if not body or not body.strip():
        body = "Public Surplus automation: no listings, test message."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 4000
    if len(body) > max_len:
        body = body[: max_len - 20] + "\n…(truncated)"
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
    {
        "title": "#12345 - Lot of approx 120 stackable banquet chairs",
        "link": f"{BASE_URL}/sms/auction/view?auc=12345",
        "quantity": 120,
        "location": "CA",
        "price": "$500.00",
        "lot_number": "AUC#12345",
        "end_date": "",
        "time_left": "3 days 10 hours",
    },
]


def main():
    import sys

    use_test_data = os.getenv("RUN_TEST") == "1" or "--test" in sys.argv
    print("=== Public Surplus Banquet Chair Alert Script ===")
    if use_test_data:
        print("(Test mode: using mock listings, no scrape)")
        listings = list(MOCK_LISTINGS)
    else:
        listings = scrape_listings()
    if not listings:
        print("No listings found. Exiting.")
        return

    listings = enrich_listings_with_descriptions(listings)

    # Quantity comes from the LLM over title + description. No regex refine
    # pass — title-regex misreads lot numbers ("LOT #142" → 142 when the lot
    # holds 6 chairs) and survives only as the tagged fallback when the LLM
    # call fails (quantity_source=llm_failed). See GitHub issue #10.
    if _llm_quantity_enabled():
        print("[1c] Inferring quantities with LLM (title + description when present)…")
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
    else:
        print("[1c] USE_LLM_QUANTITY=0 — shipping untrusted title-regex quantities.")

    # Persist every processed listing to the cache BEFORE the quantity filter
    # so even small lots are remembered (avoids re-fetching next run).
    cache_counts = listings_db.store_listings(listings)
    if cache_counts.get("disabled"):
        print(f"[1e] Cache disabled; {cache_counts['disabled']} listings not stored.")
    else:
        print(
            f"[1e] Cache: +{cache_counts['insert']} new, "
            f"~{cache_counts['update']} updated, "
            f"{cache_counts['skip']} skipped (uncacheable URL)."
        )

    # Keep banquet/event chairs over MIN_CHAIR_QUANTITY; drop non-chair lots
    # that merely share a chair-ish word (scales, stools, recliners, …).
    # Medical exam/dental gated behind INCLUDE_MEDICAL (default off). Cache
    # already has every row.
    from top_chairs import _classify, _is_non_chair_lot, trusted_quantity
    include_medical = os.getenv("INCLUDE_MEDICAL") == "1"
    def _keep(item: dict) -> bool:
        title = item.get("title") or ""
        cat, _ = _classify(title, item.get("description"))
        if cat == "medical":
            return include_medical
        if _is_non_chair_lot(title):
            return False
        # Only alert on an LLM-verified count (BLACKWHOLE-4). Untrusted regex
        # quantities / LLM failures return None and are dropped.
        q = trusted_quantity(item)
        return q is not None and q > MIN_CHAIR_QUANTITY
    listings = [item for item in listings if _keep(item)]
    if not listings:
        print(f"No banquet chairs with quantity > {MIN_CHAIR_QUANTITY}. Exiting.")
        return
    print(f" → {len(listings)} banquet-chair listings kept (qty > {MIN_CHAIR_QUANTITY})")
    ranked = rank_with_llm(listings)
    if not ranked:
        print("Ranking failed or returned empty. Exiting.")
        return
    send_telegram(ranked)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("(Telegram not configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to receive output.)")
    print("=== Script complete ===")


if __name__ == "__main__":
    main()

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

import os
import re
import urllib.parse
import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

from paths import REPORTS_DIR
from quantity_infer import infer_chair_quantity_from_title
from quantity_llm import refine_quantities_with_llm
from quantity_refine import refine_quantities_with_regex_fulltext
import listings_db

load_dotenv()
_SCRIPT_DIR = os.path.dirname(__file__)
_LOCAL_ENV = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_LOCAL_ENV):
    load_dotenv(_LOCAL_ENV, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

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
    if os.getenv("FETCH_PUBLIC_SURPLUS_DESCRIPTION") != "1":
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


def scrape_listings():
    print("[1] Starting scrape_listings() (Public Surplus)")
    listings = []
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
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.locator("text='Yes, I Accept Cookies'").click(timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(800)

            for term in SEARCH_TERMS:
                term_listings = []
                print(f"\n → Filter: '{term}'")
                page_num = 0
                while page_num < MAX_SEARCH_PAGES:
                    url = _search_url(term, page_num)
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(600)
                    try:
                        page.wait_for_selector(".auction-item", timeout=20000)
                    except Exception:
                        if page_num == 0:
                            print(f"   No results (or timeout) for '{term}'.")
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

                listings.extend(term_listings)
                qty_list = [c["quantity"] for c in term_listings]
                print(f"   Filter '{term}' total: {len(term_listings)} listings, quantities: {qty_list}")

        except Exception as e:
            print(f" → Error during scraping: {e}")
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            err_png = REPORTS_DIR / "publicsurplus_error.png"
            page.screenshot(path=str(err_png))
            print(f" → Screenshot saved as {err_png}")
        finally:
            browser.close()

    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    listings_unique = list(dedup.values())
    print(f"\n[1] Done scrape_listings(); total unique listings: {len(listings_unique)}")
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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": body, "disable_web_page_preview": True},
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

    if any(item.get("description") for item in listings):
        print("[1c] Refining quantities with regex on title+description…")
        listings = refine_quantities_with_regex_fulltext(listings)
    else:
        print("[1c] Skipping regex(fulltext) — no descriptions on any listing.")
        print("     Set FETCH_PUBLIC_SURPLUS_DESCRIPTION=1 in .env to enable.")

    if os.getenv("USE_LLM_QUANTITY") == "1":
        print("[1d] Refining quantities with LLM (title + description when present)…")
        listings = refine_quantities_with_llm(
            listings,
            provider=LLM_PROVIDER,
            ollama_base_url=OLLAMA_BASE_URL,
            ollama_model=OLLAMA_MODEL,
            ollama_timeout=OLLAMA_TIMEOUT,
            groq_api_key=GROQ_API_KEY,
            openai_api_key=OPENAI_API_KEY,
        )

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

    # Medical/dental lots sell as singles — gate the qty floor on category
    # so the alert path still surfaces them. Cache already has every row.
    from top_chairs import _classify
    def _keep(item: dict) -> bool:
        if item.get("quantity", 0) > MIN_CHAIR_QUANTITY:
            return True
        cat, _ = _classify(item.get("title"), item.get("description"))
        return cat == "medical"
    listings = [item for item in listings if _keep(item)]
    if not listings:
        print(f"No listings with quantity > {MIN_CHAIR_QUANTITY} (or medical). Exiting.")
        return
    print(f" → {len(listings)} listings kept (qty > {MIN_CHAIR_QUANTITY} or medical)")
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

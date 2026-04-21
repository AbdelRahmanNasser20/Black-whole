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
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from quantity_infer import infer_chair_quantity_from_title
from quantity_llm import refine_quantities_with_llm
from quantity_refine import refine_quantities_with_regex_fulltext
from paths import REPORTS_DIR
from govdeals_playwright_helpers import (
    govdeals_browser_context,
    govdeals_chromium_launch_args,
    prepare_govdeals_search_page,
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

# LLM: "openai" | "ollama" | "groq" (default: ollama — run: curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2)
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()

# OpenAI (when LLM_PROVIDER=openai)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Ollama local (when LLM_PROVIDER=ollama)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

# Groq (when LLM_PROVIDER=groq) — free tier, fast
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

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
SEARCH_TERMS = ["chairs"]
# SEARCH_TERMS = ["stackable chairs", "banquet chairs", "chairs", "church chairs", "event chairs", "conference chairs"]



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


def _fetch_govdeals_long_description(page, url: str) -> tuple[str, str]:
    """Load asset page, return ``(description, image_url)``.

    GovDeals renders the description via Angular (`app-read-more`). Before the data
    binds, the DOM literally contains the string "undefined", which used to leak
    into our LLM prompts and collapse every quantity to 1. We poll the selectors
    until real text appears (or the timeout elapses). The primary image is
    grabbed on the same page load — zero extra Playwright round-trip.
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

    return description, _pluck_image()


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
                    continue
                try:
                    desc, img = _fetch_govdeals_long_description(page, link)
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


def _go_to_next_page(page) -> bool:
    """
    Click the 'Next Page' link in pagination (ul.pagination li[data-type="nextPage"]).
    Returns True if we moved to the next page, False if we're on the last page or link is disabled.

    Logs *why* we exited so we can tell pagination-broken-selector apart from
    last-page-reached apart from networkidle-timeout. Previously every failure
    mode silently returned False after page 1, which is why a 600-result
    "chairs" search was only returning 24 listings.
    """
    next_li = page.locator('ul.pagination li[data-type="nextPage"]').first
    if next_li.count() == 0:
        # Try a few alternate selectors before declaring defeat — GovDeals'
        # Angular markup has shifted before.
        for alt in (
            'ul.pagination li.page-item.next',
            'ul.pagination li:has(a[aria-label*="Next" i])',
            'a[aria-label*="Next" i]',
            'button[aria-label*="Next" i]',
        ):
            cand = page.locator(alt).first
            if cand.count() > 0:
                print(f"   [pagination] primary selector missed; using fallback {alt!r}")
                next_li = cand
                break
        else:
            print("   [pagination] no nextPage element found — selector broken or last page")
            return False

    cls = next_li.get_attribute("class") or ""
    aria_disabled = next_li.get_attribute("aria-disabled") or ""
    if "disabled" in cls or aria_disabled.lower() == "true":
        print(f"   [pagination] next disabled (class={cls!r}, aria-disabled={aria_disabled!r}) — last page")
        return False

    try:
        # Some Angular pagination components only respond to clicking the
        # inner anchor, others respond to the <li>. Try the anchor first,
        # fall back to clicking the element itself.
        try:
            next_li.locator("a.page-link").click(timeout=5000)
        except Exception:
            next_li.click(timeout=5000)
    except Exception as e:
        print(f"   [pagination] click failed: {e}")
        return False

    # networkidle frequently times out on Angular SPAs that stream XHRs;
    # treat that as best-effort, not fatal.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"   [pagination] networkidle timeout (continuing anyway): {e}")
    page.wait_for_timeout(1500)
    return True


def scrape_listings():
    print("[1] Starting scrape_listings()")
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

                while True:
                    page_cards = _scrape_one_page(page)
                    if not page_cards:
                        if page_num == 1:
                            print(f"   No results for '{term}'.")
                        break
                    term_listings.extend(page_cards)
                    quantities = [c["quantity"] for c in page_cards]
                    print(f"   Page {page_num}: {len(page_cards)} listings, quantities: {quantities}")

                    if not _go_to_next_page(page):
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

    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    listings_unique = list(dedup.values())
    print(f"\n[1] Done scrape_listings(); total unique listings: {len(listings_unique)}")

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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
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
        lines.append(f"⚠ {failed}/{len(listings)} listings had LLM quantity failures (regex value used)\n")
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

def send_telegram(listings):
    """Send final ranked output to Telegram. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" → Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); skipping.")
        return
    print("[3a] Sending to Telegram…")
    body = _format_output(listings)
    # Guard against empty text (Telegram 400: message text is empty)
    if not body or not body.strip():
        body = "GovDeals automation: no listings, test message."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram max message length 4096
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

    # Keep only chairs with quantity over MIN_CHAIR_QUANTITY (after optional LLM fixups)
    listings = [item for item in listings if item.get("quantity", 0) > MIN_CHAIR_QUANTITY]
    if not listings:
        print(f"No listings with quantity > {MIN_CHAIR_QUANTITY}. Exiting.")
        return
    print(f" → {len(listings)} listings with quantity > {MIN_CHAIR_QUANTITY}")
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
#!/usr/bin/env python3
"""
bidspotter_automation.py — BidSpotter.com bulk chair scraper (source key "bs").

1. Plain HTTP (requests) against the server-rendered search pages — no browser.
   BidSpotter fronts with AWS WAF: a boring ``User-Agent: Mozilla/5.0`` passes,
   a full spoofed Chrome UA gets challenged, and any request may draw an
   HTTP 202 + ``x-amzn-waf-action`` challenge that a simple retry clears.
2. Quantity comes from BidSpotter's STRUCTURED per-lot quantity field when
   present (~80-85% of cards) → quantity_source="structured" (trusted).
   Cards without it get the regex-title seed + LLM pass, same as GD/PS.
3. Prices are not in the static HTML — one batched unauthenticated POST per
   page (reload-timed-bid-info) fills them, best-effort.
4. Same downstream pipeline as the other scrapers: cache-hydrate → LLM →
   archive-all → keep-filter → rank → Telegram.

Setup: same .env as govdeals_chairs_extraction.py (Telegram + LLM provider
vars); BidSpotter knobs are BIDSPOTTER_* (see .env.example).
"""

import html as html_lib
import json
import os
import re
import time
import urllib.parse

import requests
from dotenv import load_dotenv

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

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
QUANTITY_LLM_PROVIDER = (os.getenv("QUANTITY_LLM_PROVIDER") or LLM_PROVIDER).strip().lower()
try:
    OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
except ValueError:
    OLLAMA_TIMEOUT = 600

BASE_URL = "https://www.bidspotter.com"
# Confirmed from the page's own SearchBiddingInfoSettings.bidReloadInfoUrl.
BID_INFO_PATH = "/en-us/lot/reload-timed-bid-info?v=1.3.0.1&c=lotsearch"

SEARCH_TERMS = [
    t.strip()
    for t in (os.getenv("BIDSPOTTER_SEARCH_TERMS") or "chairs").split(",")
    if t.strip()
]

MIN_CHAIR_QUANTITY = 50
PAGE_SIZE = int(os.getenv("BIDSPOTTER_PAGE_SIZE", "120"))
MAX_PAGES = int(os.getenv("BIDSPOTTER_MAX_PAGES", "20"))
WAF_RETRIES = int(os.getenv("BIDSPOTTER_WAF_RETRIES", "4"))
WAF_BACKOFF_SEC = float(os.getenv("BIDSPOTTER_WAF_BACKOFF_SEC", "2"))
HTTP_DELAY_SEC = float(os.getenv("BIDSPOTTER_HTTP_DELAY_SEC", "0.5"))

# Boring UA on purpose — a full spoofed Chrome UA trips the WAF.
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}

_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Class-prefix match: real cards are `panel item ` (trailing space) or
# `panel item featured` — an exact-class match finds zero cards.
_CARD_SPLIT_RE = re.compile(r'<article class="panel item[^"]*">')
# Match the ASSIGNMENT, not the earlier `forItem: null` object member.
_FOR_ITEM_RE = re.compile(
    r"window\.gapAmplitudeConfig\.forItem\s*=\s*(\{.*?\});", re.S)
_PAGES_RE = re.compile(r'class="pagination-content"[^>]*data-pages="(\d+)"')


def _text(fragment: str) -> str:
    """Tag-strip + entity-unescape an HTML fragment to flat text."""
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _parse_total_pages(html: str) -> int:
    m = _PAGES_RE.search(html)
    return int(m.group(1)) if m else 1


def _parse_for_item(html: str) -> dict:
    """Per-lot metadata map keyed by lot GUID from the page's embedded
    amplitude config. Values are all-string dicts carrying "Lot Quantity",
    "Lot End Time UTC" (ISO UTC), "Auction House Name", etc.

    ⚠ "Lot Quantity" is "1" BOTH for genuine single-item lots AND when the
    seller left the structured field empty — never use it as the trusted
    quantity signal. The DOM ``bulk-quantity-value`` element is the
    discriminator (see _parse_search_cards).
    """
    m = _FOR_ITEM_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_search_cards(html: str) -> list:
    """Parse one search-results page into the standard card dicts.

    Card shape matches GD/PS (title, link, quantity, quantity_source,
    quantity_confidence, location, price, lot_number, end_date, time_left,
    image_url, description) plus BidSpotter extras (lot_guid, auction_type,
    currency, auction_house) consumed in-run only — listings_db ignores them.
    """
    for_item = _parse_for_item(html)
    cards = []
    for seg in _CARD_SPLIT_RE.split(html)[1:]:
        gm = re.search(r'id="lot-(%s)"' % _GUID, seg)
        if not gm:
            continue
        guid = gm.group(1)
        tm = re.search(
            r'<h3>\s*<a href="(/en-us/auction-catalogues/[^"]+)"[^>]*>([^<]+)</a>',
            seg,
        )
        if not tm:
            continue
        link = BASE_URL + tm.group(1)
        title = html_lib.unescape(tm.group(2)).strip()

        meta = for_item.get(guid) or {}

        qty = None
        qm = re.search(r"bulk-quantity-value[^>]*>\s*([\d,]+)", seg)
        if qm:
            try:
                qty = int(qm.group(1).replace(",", ""))
            except ValueError:
                qty = None

        location = ""
        lm = re.search(
            r'class="lotlocation">\s*Location:\s*<strong>([^<]+)</strong>', seg)
        if lm:
            location = html_lib.unescape(lm.group(1)).strip()

        image_url = ""
        im = re.search(r'<img id="i%s"[^>]*data-src="([^"]+)"' % guid, seg)
        if im:
            # ?h=175 is a CDN downsize — strip the query for full-res.
            image_url = im.group(1).split("?")[0]

        lot_no = ""
        nm = re.search(r'<div class="number"><span>Lot</span>\s*([^<]+)</div>', seg)
        if not nm:
            nm = re.search(r'<span class="lot-number">([^<]+)</span>', seg)
        if nm:
            lot_no = nm.group(1).strip()
        am = re.search(r'data-auction-ref="([^"]*)"', seg)
        auction_ref = am.group(1) if am else ""
        lot_number = f"{auction_ref}#{lot_no}" if (auction_ref or lot_no) else ""

        description = ""
        dm = re.search(r'<div class="description">\s*<p>(.*?)</p>', seg, re.S)
        if dm:
            description = _text(dm.group(1))[:4000]

        auction_type = (meta.get("Auction Type") or "").strip().lower()
        if not auction_type:
            atm = re.search(r'data-auction-type="([^"]*)"', seg)
            auction_type = ((atm.group(1) if atm else "") or "").lower()
        currency = (meta.get("Auction Currency") or "").strip()
        if not currency:
            cm = re.search(r'data-currency="([^"]*)"', seg)
            currency = ((cm.group(1) if cm else "") or "").strip()

        end_date = (
            meta.get("Lot End Time UTC") or meta.get("Auction End Time UTC") or ""
        ).strip()

        if qty is not None:
            quantity, q_src, q_conf = qty, "structured", "high"
        else:
            seed = infer_chair_quantity_from_title(title)
            quantity, q_src = seed, "regex_title"
            q_conf = "low" if seed == 1 else "medium"

        cards.append({
            "title": title,
            "link": link,
            "quantity": quantity,
            "quantity_source": q_src,
            "quantity_confidence": q_conf,
            "location": location,
            "price": "",
            "lot_number": lot_number,
            "end_date": end_date,
            "time_left": "",
            "image_url": image_url,
            "description": description,
            # In-run extras (not persisted by listings_db):
            "lot_guid": guid,
            "auction_type": auction_type,
            "currency": currency or "USD",
            "auction_house": (meta.get("Auction House Name") or "").strip(),
        })
    return cards


def _parse_bid_info(data) -> dict:
    """Response rows → {lot_guid: model_dict}. Rows arrive wrapped as
    ``{"Model": {…}}`` (verified live + in the site's own updateSuccess
    handler); bare rows are accepted too, mirroring the site's parse()."""
    out = {}
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        model = row.get("Model") if isinstance(row.get("Model"), dict) else row
        lot_id = str(model.get("LotId") or "").lower()
        if lot_id:
            out[lot_id] = model
    return out


def _format_time_left(seconds) -> str:
    """SecondsRemaining → compact countdown ("34d 16h" / "1h 6m"), "" if unusable."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {rem // 60}m"


def _apply_bid_info(cards: list, info: dict) -> None:
    """Fill price / time_left / bid_count in place. Missing lots untouched.

    Price rule: LeadingBid when TotalBids > 0, else StartPrice (the opening
    price — a 0-bid lot's LeadingBid is not meaningful). GovDeals-style
    format: "USD 7.00"."""
    for card in cards:
        model = info.get((card.get("lot_guid") or "").lower())
        if not model:
            continue
        total_bids = model.get("TotalBids") or 0
        amount = model.get("LeadingBid") if total_bids else model.get("StartPrice")
        if amount is None:
            amount = model.get("StartPrice")
        currency = (model.get("Currency") or card.get("currency") or "USD").strip()
        if amount is not None:
            try:
                card["price"] = f"{currency} {float(amount):.2f}"
            except (TypeError, ValueError):
                pass
        try:
            card["bid_count"] = int(total_bids)
        except (TypeError, ValueError):
            card["bid_count"] = 0
        tl = _format_time_left(model.get("SecondsRemaining"))
        if tl:
            card["time_left"] = tl


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HTTP_HEADERS)
    # Page size is cookie-driven; the pageSize URL param is ignored.
    s.cookies.set("user_preference_pagesize", str(PAGE_SIZE),
                  domain="www.bidspotter.com")
    return s


def _is_waf_challenge(resp) -> bool:
    return resp.status_code == 202 and "x-amzn-waf-action" in resp.headers


def _fetch(session, method: str, url: str, *, retries: int | None = None, **kwargs):
    """HTTP request with AWS-WAF-challenge retry.

    The WAF challenge is probabilistic: the same request can 202 once and 200
    on the next try. Retry with linear backoff (WAF_BACKOFF_SEC * attempt);
    when the challenge persists past ``retries`` attempts, raise RuntimeError
    so the per-term error isolation catches it. Any other non-2xx raises via
    raise_for_status."""
    attempts = (WAF_RETRIES if retries is None else retries) + 1
    for attempt in range(attempts):
        resp = session.request(method, url, timeout=30, **kwargs)
        if _is_waf_challenge(resp):
            if attempt < attempts - 1 and WAF_BACKOFF_SEC > 0:
                time.sleep(WAF_BACKOFF_SEC * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"AWS WAF challenge persisted after {attempts} attempts: {url}")


def _search_url(term: str, page: int) -> str:
    q = urllib.parse.urlencode({"searchTerm": term, "page": page})
    return f"{BASE_URL}/en-us/search-results?{q}"


def _fetch_search_page(term: str, page: int, session=None) -> str:
    """WAF-retry GET of one search-results page → HTML text.

    Importable by the dashboard's /api/test-scrape (pass a shared session to
    reuse the pagesize cookie across pages)."""
    if session is None:
        session = _session()
    return _fetch(session, "get", _search_url(term, page)).text


def _fetch_bid_info(guids: list, session=None) -> dict:
    """One batched POST for a page's worth of lot GUIDs → {lotGuid: model}.
    Raises on HTTP failure — the caller treats prices as best-effort."""
    if not guids:
        return {}
    if session is None:
        session = _session()
    body = [{"LotId": g, "BidderHasBids": False} for g in guids]
    resp = _fetch(session, "post", BASE_URL + BID_INFO_PATH, json=body)
    return _parse_bid_info(resp.json())


def _dedup(listings: list) -> list:
    """One card per lot, keyed by link (then lot_number / title) — same as
    the GD/PS dedup helpers."""
    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    return list(dedup.values())


def scrape_listings() -> list:
    print("[1] scrape via plain HTTP (BidSpotter search pages, no browser)")
    listings = []
    session = _session()
    for term in SEARCH_TERMS:
        term_listings = []
        print(f"\n → Filter: '{term}'")
        # Per-term isolation: a persistent WAF block on one term must not
        # kill the remaining terms (mirrors the PS scraper).
        try:
            page = 1
            total_pages = 1
            while page <= min(total_pages, MAX_PAGES):
                page_html = _fetch_search_page(term, page, session=session)
                if page == 1:
                    total_pages = _parse_total_pages(page_html)
                page_cards = _parse_search_cards(page_html)
                if not page_cards:
                    if page == 1:
                        print(f"   No results for '{term}'.")
                    break
                # Prices are skeleton-loaded client-side — one batched POST
                # per page fills them. Best-effort: a failure keeps the cards
                # (they archive fine with price="").
                try:
                    info = _fetch_bid_info(
                        [c["lot_guid"] for c in page_cards], session=session)
                    _apply_bid_info(page_cards, info)
                except Exception as e:  # noqa: BLE001
                    print(f"   • bid-info failed on page {page}: {e} "
                          "(prices left empty)")
                term_listings.extend(page_cards)
                print(f"   Page {page}: {len(page_cards)} listings "
                      f"(of {total_pages} page(s))")
                page += 1
                if HTTP_DELAY_SEC > 0:
                    time.sleep(HTTP_DELAY_SEC)
        except Exception as e:  # noqa: BLE001
            print(f"   • '{term}' failed ({e}); skipping to next term.")
        listings.extend(term_listings)
        print(f"   Filter '{term}' total: {len(term_listings)} listings")
    unique = _dedup(listings)
    print(f"\n[1] Done scrape_listings(); {len(unique)} unique lots "
          f"(from {len(listings)} across {len(SEARCH_TERMS)} terms)")
    return unique


def rank_with_llm(listings):
    """Deterministic sort by quantity desc, price asc — name kept for parity
    with the GD/PS scrapers (the LLM never ranked; sorting is free)."""
    print("[2] Ranking listings (deterministic sort by quantity desc, price asc)…")
    return _fallback_rank(listings)


def _fallback_rank(listings):
    def _price_for_sort(p):
        s = str(p or "")
        digits = "".join(c for c in s if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else float("inf")
        except ValueError:
            return float("inf")

    safe = [x for x in listings if isinstance(x, dict)]
    safe.sort(key=lambda x: (-int(x.get("quantity") or 0),
                             _price_for_sort(x.get("price"))))
    for i, item in enumerate(safe, 1):
        item["rank"] = i
    return safe


def _format_output(listings):
    lines = ["🪑 BidSpotter chairs (bulk lots), ranked by quantity\n"]
    for item in listings:
        qty = item.get("quantity", "?")
        src = item.get("quantity_source") or ""
        tag = {"structured": "📋", "llm": "🤖"}.get(src, "")
        end_date = item.get("end_date") or "N/A"
        time_left = item.get("time_left") or "N/A"
        lines.append(
            f"#{item['rank']}: {item['title']}\n"
            f"Qty: {qty} {tag} · {item.get('price') or 'N/A'}\n"
            f"Ends: {end_date} · Time left: {time_left}\n"
            f"↗ {item['link']}\n"
        )
    return "\n".join(lines)


def _alert_on_quantity_degradation(listings: list, *, provider: str) -> None:
    """Telegram-alert when the LLM quantity pass failed for some rows —
    silent regex fallback is how big lots get the wrong count. Structured
    rows never route through the LLM, so they can't appear here."""
    degraded = [
        it for it in listings
        if it.get("quantity_source") in ("llm_failed", "llm_missing")
    ]
    if not degraded or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    reason = next(
        (it.get("quantity_error") for it in degraded if it.get("quantity_error")),
        "unknown")
    text = (
        f"⚠️ BidSpotter scrape: quantity LLM ({provider}) FAILED on "
        f"{len(degraded)}/{len(listings)} lots — falling back to regex, "
        f"counts may be wrong. First error: {str(reason)[:160]}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f" → Telegram degradation-alert error: {e}")


def send_telegram(listings):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" → Telegram not configured (set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID); skipping.")
        return
    print("[3a] Sending to Telegram…")
    body = _format_output(listings)
    if not body or not body.strip():
        body = "BidSpotter automation: no listings, test message."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 4000
    if len(body) > max_len:
        body = body[: max_len - 20] + "\n…(truncated)"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": body,
                  "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        print(" → Telegram message sent.")
    except Exception as e:  # noqa: BLE001
        print(f" → Telegram error: {e}")


def _llm_quantity_enabled() -> bool:
    return os.getenv("USE_LLM_QUANTITY", "1") == "1"


def _include_live() -> bool:
    """Whether `live`-auction lots may reach ranking/alerts. They are always
    ARCHIVED either way; their prices can't be polled, so default-include is
    harmless but tunable."""
    return os.getenv("BIDSPOTTER_INCLUDE_LIVE", "1") == "1"


MOCK_LISTINGS = [
    {
        "title": "Lot of stackable banquet chairs",
        "link": (f"{BASE_URL}/en-us/auction-catalogues/mockhouse/"
                 "catalogue-id-mock-10001/lot-00000000-0000-4000-8000-000000000001"),
        "quantity": 120, "quantity_source": "structured",
        "quantity_confidence": "high",
        "location": "Grand Rapids, Michigan", "price": "USD 50.00",
        "lot_number": "mock-10001#12",
        "end_date": "2027-01-01T16:40:00Z", "time_left": "34d 16h",
        "image_url": "", "description": "120 stackable banquet chairs",
        "lot_guid": "00000000-0000-4000-8000-000000000001",
        "auction_type": "timed", "currency": "USD", "auction_house": "Mock House",
    },
    {
        "title": "Banquet chairs, various",
        "link": (f"{BASE_URL}/en-us/auction-catalogues/mockhouse/"
                 "catalogue-id-mock-10001/lot-00000000-0000-4000-8000-000000000002"),
        "quantity": 1, "quantity_source": "regex_title",
        "quantity_confidence": "low",
        "location": "", "price": "", "lot_number": "mock-10001#13",
        "end_date": "2027-01-01T16:45:00Z", "time_left": "",
        "image_url": "", "description": "Approximately 75 padded banquet chairs.",
        "lot_guid": "00000000-0000-4000-8000-000000000002",
        "auction_type": "timed", "currency": "USD", "auction_house": "Mock House",
    },
]


def main() -> int:
    import sys

    use_test_data = os.getenv("RUN_TEST") == "1" or "--test" in sys.argv
    print("=== BidSpotter chairs extraction ===")
    if use_test_data:
        print("(Test mode: using mock listings, no scrape)")
        listings = [dict(x) for x in MOCK_LISTINGS]
    else:
        listings = scrape_listings()
    if not listings:
        print("No listings found. Exiting.")
        return 0

    # [1b] Cache hydration — ONLY rows without a structured quantity. A fresh
    # structured count must never be clobbered by a stale cached one (sellers
    # edit quantities); hydration exists to spare the LLM pass on lots a
    # previous run already resolved.
    unstructured = [x for x in listings
                    if x.get("quantity_source") != "structured"]
    if unstructured:
        hits, _, relists = listings_db.hydrate_from_cache(unstructured)
        print(f"[1b] Cache hydration: {hits} hit(s) on {len(unstructured)} "
              f"unstructured lot(s), {relists} relist(s).")
    else:
        print("[1b] Every lot carries a structured quantity; no hydration needed.")

    # [1d] LLM pass — only rows still lacking a trusted quantity.
    # refine_quantities_with_llm returns COPIES, so stitch back by index.
    if _llm_quantity_enabled():
        idx = [i for i, x in enumerate(listings)
               if x.get("quantity_source") not in ("structured", "llm")]
        if idx:
            print(f"[1d] Inferring quantities with LLM for {len(idx)} lot(s) "
                  "missing a structured count…")
            refined = refine_quantities_with_llm(
                [listings[i] for i in idx],
                provider=QUANTITY_LLM_PROVIDER,
                ollama_base_url=OLLAMA_BASE_URL,
                ollama_model=OLLAMA_MODEL,
                ollama_timeout=OLLAMA_TIMEOUT,
                groq_api_key=GROQ_API_KEY,
                openai_api_key=OPENAI_API_KEY,
                gemini_api_key=GEMINI_API_KEY,
            )
            for i, row in zip(idx, refined):
                listings[i] = row
            _alert_on_quantity_degradation(listings, provider=QUANTITY_LLM_PROVIDER)
        else:
            print("[1d] Nothing to refine — quantities all structured or cached-LLM.")
    else:
        print("[1d] USE_LLM_QUANTITY=0 — unstructured lots keep untrusted regex seeds.")

    # [1e] Archive EVERY processed listing before the quantity filter, so even
    # small lots are remembered (mirrors GD/PS).
    cache_counts = listings_db.store_listings(listings)
    if cache_counts.get("disabled"):
        print(f"[1e] Cache disabled; {cache_counts['disabled']} listings not stored.")
    else:
        print(
            f"[1e] Cache: +{cache_counts['insert']} new, "
            f"~{cache_counts['update']} updated, "
            f"{cache_counts['skip']} skipped (uncacheable URL)."
        )

    # Keep-filter: banquet/event chairs over MIN_CHAIR_QUANTITY with a TRUSTED
    # count; medical gated by INCLUDE_MEDICAL; live auctions gated by
    # BIDSPOTTER_INCLUDE_LIVE. Cache already has every row.
    from top_chairs import _classify, _is_non_chair_lot, trusted_quantity
    include_medical = os.getenv("INCLUDE_MEDICAL") == "1"
    include_live = _include_live()

    def _keep(item: dict) -> bool:
        if not include_live and (item.get("auction_type") or "") == "live":
            return False
        title = item.get("title") or ""
        cat, _ = _classify(title, item.get("description"))
        if cat == "medical":
            return include_medical
        if _is_non_chair_lot(title):
            return False
        q = trusted_quantity(item)
        return q is not None and q > MIN_CHAIR_QUANTITY

    listings = [item for item in listings if _keep(item)]
    if not listings:
        print(f"No banquet chairs with quantity > {MIN_CHAIR_QUANTITY}. Exiting.")
        return 0
    print(f" → {len(listings)} banquet-chair listings kept (qty > {MIN_CHAIR_QUANTITY})")
    ranked = rank_with_llm(listings)
    if not ranked:
        print("Ranking failed or returned empty. Exiting.")
        return 0
    send_telegram(ranked)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("(Telegram not configured; set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID to receive output.)")
    print("=== Script complete ===")
    return 0


if __name__ == "__main__":
    main()

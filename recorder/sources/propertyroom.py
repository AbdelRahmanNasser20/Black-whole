"""PropertyRoom (`propertyroom.com`) source adapter — police/government
seized-goods auctions, server-rendered ASP.NET.

LIVE RECON (verified 2026-10-16, via the Pi SOCKS proxy):

- **Geo-blocked outside the US**: from the operator's Egypt IP every request
  answers HTTP 403 `"Not available in your country"` — in the same minute the
  site works through the Pi's Phoenix tunnel. `propertyroom.com` is therefore
  listed in the `RECORDER_PROXY_HOSTS` default (`scripts/recorder_local.sh`),
  like publicsurplus.com and mibid.michigan.gov. Render's US IP stays direct
  (both proxy vars unset there). A short/bare User-Agent also 403s — the
  honest desktop-Chrome UA `polite_get` already sends is required.
- `robots.txt` (dated 2026-10-07) **disallows `/api/`** — so the adapter never
  touches the JSON endpoints the frontend's JS uses and reads the
  server-rendered pages instead (same stance as TXAuction's "never /api/").
  `/s/<term>` and `/l/<slug>/<id>` are allowed; of the query params only
  `sort=`/`formats=`/`seller=`/`perPage=`/`location=`/`deals=`/`pickup=` are
  disallowed — `?page=N` (the only one we use) is fine.
- Search: `GET /s/<url-quoted term>?page=N` (a `?q=` form 302s there).
  40 cards/page (buttons offer 20/40/80 but changing it needs the disallowed
  `perPage=`), `page` 1-indexed. A marker span carries the totals:
  `<span id="search-viewed" hidden data-term=".." data-results="4639"
  data-page="2" data-cards="40">`. Search is FUZZY ("chairs" matched coin
  banks and suggested "Did you mean charms?"), so every card is re-filtered
  by `FURNITURE_TERMS` against its title — 0 title matches is a clean,
  common result.
- Card (one `<a class="listing-card">` per lot, all server-rendered):
  `data-listing-id` (numeric, the `source_lot_id`), `data-slug`,
  `data-format` (`English` = auction, `FixedPrice` = buy-now, no bids),
  `data-photos` (JSON list of full image URLs), `.listing-title`,
  `.listing-time[data-end]` (**ISO-8601 UTC with `Z`** — e.g.
  `2026-10-19T13:15:00.0000000Z`, no tz conversion needed; Python ≥3.11
  parses the 7-digit fraction), `.listing-price` (`$300.00`),
  `.listing-bids` (`0 bids`; absent on FixedPrice cards).
- Detail: `GET /l/<any slug>/<id>` — the slug is decorative, any value 302s
  to the canonical URL, so `poll()` fetches `/l/i/<id>`. Server-rendered:
  `<h1>` title, `Listing ID: N`, breadcrumb category (`/c/...` links),
  `window.listingEndDate = "..Z"` / `window.listingId = N` script globals,
  `.price-value` ("US $179.00" — the CURRENT price while live, the FINAL
  price after close), the stats sidebar (`Active Bidder(s)` / `Watching` /
  `Total Bid(s)` — all real server-side numbers, verified 20 bids on
  18986916), `Bid History (N)` tab with the full bid table, and
  `.listing-timer countdown ended` + "Ended <date> (Eastern)" once closed.
- **A closed lot keeps its final price and bid count on the page
  indefinitely** (18901000: ended 2026-08-20, still serving $130.00 / 5 bids
  two months later) — so `poll()` reads a real final, `api_final`-grade.
  A no-bid close keeps the start price with `Total Bid(s) 0`
  (`sold_comps`'s `bid_count > 0` gate drops it, correctly).
- An unknown / purged id answers a clean HTTP 404 → `gone` once past
  `end_date`.
- No public completed-listings search was found (closed lots are reachable
  by id only), so `sold_sweep()` returns `[]` like gsa/public_surplus —
  the close is captured by `poll()` instead.
- Currency USD. No city/state on a lot — PropertyRoom ships from its own
  warehouses (`city`/`state` are None in raw).

raw keys (stable, read by the web layer): `title`, `city`, `state`,
`category`, `image_url`, `url`, `currency`, plus everything parsed from the
card under `propertyroom` (and from the detail page when `poll()` built the
observation).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_get

SOURCE = "propertyroom"

BASE_URL = "https://www.propertyroom.com"
SEARCH_PATH = "/s/"
ITEM_PATH = "/l/i/"  # the slug segment is decorative; the site 302s to the canonical URL
MAX_PAGES_PER_TERM = 5  # 40 cards/page; a furniture term never legitimately needs more
DESCRIPTION_MAX_CHARS = 2000

_END_DATE_RE = re.compile(r'window\.listingEndDate\s*=\s*"([^"]*)"')
_LISTING_ID_RE = re.compile(r"window\.listingId\s*=\s*(\d+)")
_BID_HISTORY_RE = re.compile(r"Bid History \((\d+)\)")
_STAT_RE = re.compile(
    r'stat-number">\s*([\d,]+)\s*</div>\s*</div>\s*<div class="stat-label">\s*([^<]+?)\s*</div>'
)
_RESULTS_MARKER_RE = re.compile(
    r'id="search-viewed"[^>]*data-results="(\d+)"[^>]*data-page="(\d+)"[^>]*data-cards="(\d+)"'
)


def _money(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.search(r"\d[\d,]*", text)
    return int(m.group(0).replace(",", "")) if m else None


def _parse_end(value: str | None) -> datetime | None:
    """`2026-10-19T13:15:00.0000000Z` → aware UTC datetime (already UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_furniture_text(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in FURNITURE_TERMS)


def _jsonable(obj):
    """raw goes to a JSON column — Decimals/datetimes become strings."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# --- search page -----------------------------------------------------------

def parse_search(html: str) -> tuple[list[dict], dict | None]:
    """(cards, totals) — totals from the `search-viewed` marker
    (`{"results", "page", "cards"}`) or None when the marker is missing."""
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict] = []
    for a in soup.find_all("a", class_="listing-card"):
        lot_id = a.get("data-listing-id")
        if not lot_id or not str(lot_id).isdigit():
            continue
        title_div = a.find("div", class_="listing-title")
        time_span = a.find("span", class_="listing-time")
        price_span = a.find("span", class_="listing-price")
        bids_span = a.find("span", class_="listing-bids")
        img = a.find("img")
        photos: list[str] = []
        try:
            parsed_photos = json.loads(a.get("data-photos") or "[]")
            if isinstance(parsed_photos, list):
                photos = [p for p in parsed_photos if isinstance(p, str)]
        except ValueError:
            pass
        cards.append({
            "lot_id": str(lot_id),
            "slug": a.get("data-slug"),
            "format": a.get("data-format"),
            "title": unescape(title_div.get_text(" ", strip=True)) if title_div else None,
            "url": f"{BASE_URL}{a.get('href')}" if a.get("href") else None,
            "image_url": (img.get("src") if img else None) or (photos[0] if photos else None),
            "photos": photos,
            "current_bid": _money(price_span.get_text(strip=True) if price_span else None),
            "bid_count": _int(bids_span.get_text(strip=True) if bids_span else None),
            "end_date": _parse_end(time_span.get("data-end") if time_span else None),
        })
    totals = None
    m = _RESULTS_MARKER_RE.search(html)
    if m:
        totals = {"results": int(m.group(1)), "page": int(m.group(2)), "cards": int(m.group(3))}
    return cards, totals


def _fetch_search(term: str, page: int) -> str | None:
    """None on ANY fetch failure (transport, non-200, unrecognised shape);
    the HTML otherwise. ConnectionError propagates — the sweep stops on a
    dead host/proxy instead of burning one timeout per term."""
    url = f"{BASE_URL}{SEARCH_PATH}{quote(term)}"
    params = {"page": page} if page > 1 else None
    try:
        resp = polite_get(url, params=params)
    except requests.exceptions.ConnectionError:
        raise
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: search {term!r} page {page} request failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"[{SOURCE}] RECORDER ERROR: search {term!r} page {page} HTTP {resp.status_code}")
        return None
    if "listing-card" not in resp.text and 'id="search-viewed"' not in resp.text:
        print(f"[{SOURCE}] RECORDER ERROR: search {term!r} page {page} — unrecognised page shape")
        return None
    return resp.text


def _sweep_term(term: str) -> list[dict] | None:
    """Every card the term's result pages serve (unfiltered), or None when
    page 1 failed. A later page failing returns the partial set with a
    warning."""
    cards: list[dict] = []
    for page in range(1, MAX_PAGES_PER_TERM + 1):
        html = _fetch_search(term, page)
        if html is None:
            if page == 1:
                return None
            print(f"[{SOURCE}] WARNING: search {term!r} page {page} failed — "
                  f"returning {len(cards)} partial cards")
            break
        page_cards, totals = parse_search(html)
        cards.extend(page_cards)
        if not page_cards:
            break
        if totals is not None and (totals["cards"] < 40 or len(cards) >= totals["results"]):
            break
    return cards


# --- item page -------------------------------------------------------------

def parse_item(html: str) -> dict | None:
    """Everything the detail page says about one lot, or None when the page
    is not a lot page (no `window.listingId`)."""
    m_id = _LISTING_ID_RE.search(html)
    if not m_id:
        return None
    lot_id = m_id.group(1)
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = unescape(h1.get_text(" ", strip=True)) if h1 else None

    category = None
    crumb = soup.find("div", class_="listing-breadcrumb")
    if crumb:
        cats = crumb.find_all("a", href=re.compile(r"^/c/"))
        if cats:
            category = cats[-1].get_text(" ", strip=True)

    timer = soup.find("div", class_="listing-timer")
    closed = bool(timer and "ended" in (timer.get("class") or []))
    end_div = soup.find("div", class_="end-date")
    end_text = end_div.get_text(" ", strip=True) if end_div else None
    if end_text and end_text.startswith("Ended"):
        closed = True

    m_end = _END_DATE_RE.search(html)
    end_date = _parse_end(m_end.group(1)) if m_end else None

    price = soup.find("span", class_="price-value")
    current_bid = _money(price.get_text(strip=True)) if price else None

    # Bid count: the stats sidebar's "Total Bid(s)" (server-rendered, verified
    # live), falling back to the "Bid History (N)" tab label.
    bid_count = None
    for num, label in _STAT_RE.findall(html):
        if label.strip().startswith("Total Bid"):
            bid_count = _int(num)
            break
    if bid_count is None:
        m_bh = _BID_HISTORY_RE.search(html)
        if m_bh:
            bid_count = int(m_bh.group(1))

    item_number = None
    m_item = re.search(r"Item #:\s*([^<]+)<", html)
    if m_item:
        item_number = unescape(m_item.group(1)).strip()

    main_img = soup.find("img", id="main-image")
    image_url = main_img.get("src") if main_img else None

    description_text = None
    desc = soup.find("div", id="tab-description")
    if desc:
        description_text = desc.get_text(" ", strip=True)[:DESCRIPTION_MAX_CHARS]

    m_slug = re.search(r'window\.listingSlug\s*=\s*"([^"]*)"', html)
    slug = m_slug.group(1) if m_slug else None
    url = f"{BASE_URL}/l/{slug}/{lot_id}" if slug else f"{BASE_URL}{ITEM_PATH}{lot_id}"

    return {
        "lot_id": lot_id,
        "slug": slug,
        "title": title,
        "category": category,
        "item_number": item_number,
        "closed": closed,
        "end_text": end_text,
        "end_date": end_date,
        "current_bid": current_bid,
        "bid_count": bid_count,
        "image_url": image_url,
        "description_text": description_text,
        "url": url,
    }


def _fetch_item(lot_id: str) -> tuple[int | None, dict | None]:
    """(http_status, parsed) — status None on a transport error; parsed None
    on non-200 or an unrecognised page."""
    try:
        resp = polite_get(f"{BASE_URL}{ITEM_PATH}{lot_id}")
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: item {lot_id} request failed: {e}")
        return None, None
    if resp.status_code != 200:
        return resp.status_code, None
    parsed = parse_item(resp.text)
    if parsed is None:
        print(f"[{SOURCE}] RECORDER ERROR: item {lot_id} — unrecognised page shape")
    return 200, parsed


# --- observations ----------------------------------------------------------

def _card_observation(card: dict, term: str) -> Observation:
    raw = {
        "title": card.get("title"),
        "city": None,
        "state": None,
        "category": None,
        "image_url": card.get("image_url"),
        "url": card.get("url") or f"{BASE_URL}{ITEM_PATH}{card['lot_id']}",
        "currency": "USD",
        "search_term": term,
        "propertyroom": {k: v for k, v in card.items() if k != "lot_id"},
    }
    return Observation(
        source=SOURCE,
        source_lot_id=card["lot_id"],
        status="active",
        raw=_jsonable(raw),
        current_bid=card.get("current_bid"),
        bid_count=card.get("bid_count"),
        end_date=card.get("end_date"),
    )


def _item_observation(item: dict) -> Observation:
    raw = {
        "title": item.get("title"),
        "city": None,
        "state": None,
        "category": item.get("category"),
        "image_url": item.get("image_url"),
        "url": item["url"],
        "currency": "USD",
        "propertyroom": {k: v for k, v in item.items() if k != "lot_id"},
    }
    return Observation(
        source=SOURCE,
        source_lot_id=str(item["lot_id"]),
        status="closed" if item.get("closed") else "active",
        raw=_jsonable(raw),
        current_bid=item.get("current_bid"),
        bid_count=item.get("bid_count"),
        end_date=item.get("end_date"),
    )


class PropertyRoomSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        """One `/s/<term>` sweep per FURNITURE_TERMS entry, re-filtered on the
        card title (the site search is fuzzy). Raises SourceFetchFailed only
        when EVERY term's fetch failed; a clean sweep that matches nothing
        returns [] (a breaker success)."""
        by_id: dict[str, tuple[dict, str]] = {}
        any_ok = False
        fetched_cards = 0
        for term in FURNITURE_TERMS:
            try:
                cards = _sweep_term(term)
            except requests.exceptions.ConnectionError as e:
                # Host (or the proxy it is routed through) is down — one
                # loud failure, not one timeout per remaining term.
                print(f"[{SOURCE}] RECORDER ERROR: search {term!r} connection failed: {e} — "
                      "stopping the term sweep (host down)")
                break
            if cards is None:
                continue
            any_ok = True
            fetched_cards += len(cards)
            for card in cards:
                if card["lot_id"] in by_id:
                    continue
                if not _is_furniture_text(card.get("title") or ""):
                    continue
                by_id[card["lot_id"]] = (card, term)
        if not any_ok:
            print(f"[{SOURCE}] RECORDER ERROR: discover() aborted — all "
                  f"{len(FURNITURE_TERMS)} FURNITURE_TERMS fetches failed, 0 observations")
            raise SourceFetchFailed(
                f"discover() aborted — all {len(FURNITURE_TERMS)} FURNITURE_TERMS fetches failed",
                url=BASE_URL)
        out = [_card_observation(card, term) for card, term in by_id.values()]
        if not out:
            print(f"[{SOURCE}] WARNING: discover() found 0 furniture-titled cards out of "
                  f"{fetched_cards} fetched — the search is fuzzy, 0 is common; "
                  "check FURNITURE_TERMS for drift if it persists")
        return out

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        budget = PollBudget()
        observations: list[Observation] = []
        for lot in lots:
            if budget.exhausted:
                print(f"[{SOURCE}] RECORDER ERROR: poll() aborted after "
                      f"{budget.consecutive} consecutive failures — {budget.stats(len(lots))}")
                break
            lot_id = str(lot["source_lot_id"])
            status, item = _fetch_item(lot_id)
            if item is not None:
                budget.record(True)
                observations.append(_item_observation(item))
                continue
            if status == 404:
                budget.record(True)
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE, source_lot_id=lot_id, status="gone",
                        raw={"recorder_probe": {"result": "not_found", "http_status": 404,
                                                "url": f"{BASE_URL}{ITEM_PATH}{lot_id}"}},
                    ))
                continue
            # transport error, 403 (geo-block / UA wall), 5xx, bad shape
            budget.record(False)
        return observations

    def sold_sweep(self) -> list[Observation]:
        """No public completed-listings search exists — a closed lot is only
        reachable by id, and `poll()` reads its final price + bid count from
        the detail page (kept indefinitely). Nothing to sweep."""
        return []

"""Public Surplus adapter — the legacy scraper ported under the site contract.

Source ported (read-only, nothing imported from it — it drags in Telegram, the
LLM quantity pass and listings.db): `auction_extractors/public_surplus_automation.py`
  - SEARCH_TERMS                       lines 69-83
  - _GRID_CARD_RE / _parse_search_cards lines 141-213  → parse_search_cards()
  - _parse_detail_page                 lines 216-237  → parse_detail_page()
  - _dedup                             lines 240-248  → the `seen` set in discover()
  - _search_url                        lines 529-531  → search_url()
  - _use_http_fast_path / _browser_fallback_enabled docstrings (ps-v2 note):
    plain `requests` may get the empty `noAuctionsFound` shell on search; the
    browser is then the primary lane on the Mac, and Render has no Chromium.

Deliberately NOT ported: the title-regex quantity guess, the LLM quantity pass,
the description fetch per lot at discover time, listings.db / Telegram output —
deals/ has its own classify + quantity pass and its own store.

Ids: Public Surplus lots have one stable numeric `auc` id; the legacy trio is
synthesized via `models.synth_ids("publicsurplus", auc, ordinal=2)` so
`account_id == -2` for every row. `refetch()` therefore needs the auc id back
from a synthesized key — the adapter remembers every key it discovered in this
process and re-sweeps search for keys it has not seen.
"""
from __future__ import annotations

import html as html_lib
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

import requests

from deals.adapters import _browser
from deals.models import Lot, Snapshot, lot_key, synth_ids

SITE = "publicsurplus"
ORDINAL = 2                     # permanent — feeds synth_ids account_id = -2
BASE_URL = "https://www.publicsurplus.com"
PS_PAGE_SIZE = 25               # server-fixed cards per search page
DELAY_S = 5.0                   # robots.txt Crawl-delay: 5 (named bots) — honored for everyone
UA = "Mozilla/5.0 (BLACKWHOLE deal tracker; contact: abdel@black-whole.com)"
_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
_SEARCH_WAIT = ".auction-item, #noAuctionsFound:not(.d-none)"
_DETAIL_WAIT = "#noOfBids"

# Same terms as the legacy scraper (and govdeals_chairs_extraction.py).
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

# Each result renders twice (grid + list view); we parse the grid copy. Every
# field element carries the auction id in its DOM id, so each extract is
# anchored to the card's own id — no positional guessing.
_GRID_CARD_RE = re.compile(r'<div class="auction-item" id="(\d+)searchGrid">')
_TITLE_PREFIX_RE = re.compile(r"^#\d+\s*-\s*")


def search_url(term: str, page_idx: int) -> str:
    q = urllib.parse.urlencode({"posting": "y", "keyWord": term, "page": page_idx})
    return f"{BASE_URL}/sms/browse/search?{q}"


def lot_url(auc_id: str) -> str:
    return f"{BASE_URL}/sms/auction/view?auc={auc_id}"


def parse_price(text: str, *, auc_id: str) -> float:
    """'$1,234.50' → 1234.5. Missing/garbled raises — never a silent 0.0."""
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        raise ValueError(f"publicsurplus {auc_id}: price missing")
    try:
        return float(cleaned)
    except ValueError:
        raise ValueError(f"publicsurplus {auc_id}: garbled price {text!r}") from None


def parse_search_cards(html: str) -> list[dict]:
    """One server-rendered search page → card dicts (the `raw` of each Lot).

    Fields: auc_id, title, link, price (raw text), time_left, end_epoch_ms
    (int | None, from the card's countdown script), state, image_url (thumb-l).
    """
    matches = list(_GRID_CARD_RE.finditer(html))
    cards: list[dict] = []
    for i, m in enumerate(matches):
        auc_id = m.group(1)
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        seg = html[m.start():seg_end]

        tm = re.search(r'<a\s[^>]*href="(/sms/auction/view\?auc=%s)"[^>]*title="([^"]+)"'
                       % auc_id, seg)
        if not tm:
            continue
        title = html_lib.unescape(tm.group(2)).strip()

        pm = re.search(r'id="val_%ssearchGrid"[^>]*>\s*([^<]+)' % auc_id, seg)
        price = pm.group(1).strip() if pm else ""

        tlm = re.search(r'id="timeLeftValue%ssearchGrid"[^>]*>\s*([^<]+)' % auc_id, seg)
        time_left = tlm.group(1).strip() if tlm else ""

        # updateTimeLeftSpan(timeLeftInfoMap, <auc>, "<auc>searchGrid", <now_ms>, <end_ms>, …)
        em = re.search(r'updateTimeLeftSpan\(\s*timeLeftInfoMap,\s*%s,\s*"[^"]*",\s*\d+,\s*(\d+)'
                       % auc_id, seg)
        end_epoch_ms = int(em.group(1)) if em else None

        lm = re.search(r'auction-item-state[^>]*>\s*([^<]*)', seg)
        state = lm.group(1).strip() if lm else ""

        im = re.search(r'<img[^>]+src="(https://[^"]+/sms/docviewer/[^"]+)"', seg)
        # The grid embeds the 120x90 "thumb-b" rendition; "thumb-l" is 1280x960.
        image_url = im.group(1).replace("/thumb-b/", "/thumb-l/") if im else ""

        cards.append({
            "auc_id": auc_id, "title": title, "link": BASE_URL + tm.group(1),
            "price": price, "time_left": time_left, "end_epoch_ms": end_epoch_ms,
            "state": state, "image_url": image_url,
        })
    return cards


def _text_of(html: str) -> str:
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    text = html_lib.unescape(re.sub(r'<[^>]+>', '\n', text))
    text = re.sub(r'[ \t\r]+', ' ', text)
    return re.sub(r'\n\s*', '\n', text).strip()


def parse_detail_page(html: str) -> dict:
    """One lot page → dict: auc_id, price (text), bid_count, end_epoch_ms,
    seller, city, state, zip, description, image_url. Used by refetch (bid
    count lives only here — the search card has none)."""
    vm = re.search(r'<strong\s+id="val_(\d+)"[^>]*>\s*([^<]+)', html)
    auc_id = vm.group(1) if vm else ""
    price = vm.group(2).strip() if vm else ""

    bid_count = 0
    bm = re.search(r'id="noOfBids"[^>]*>(.*?)</span>\s*</span>', html, flags=re.S)
    if bm:
        digits = re.search(r'\d+', re.sub(r'<[^>]+>', ' ', bm.group(1)))
        bid_count = int(digits.group(0)) if digits else 0

    em = re.search(r'updateTimeLeftSpan\(\s*timeLeftInfoMap,\s*(\d+),\s*"[^"]*",\s*\d+,\s*(\d+)', html)
    end_epoch_ms = int(em.group(2)) if em else None
    if not auc_id and em:
        auc_id = em.group(1)

    text = _text_of(html)
    sm = re.search(r'View (.+?) Auctions', text)
    seller = sm.group(1).strip() if sm else ""
    city = state = zip_ = ""
    lm = re.search(r'\n([A-Za-z .\'-]+),\n([A-Z]{2})\n(\d{5})', text)
    if lm:
        city, state, zip_ = lm.group(1).strip(), lm.group(2), lm.group(3)
    if not state:
        rm = re.search(r'Region:\n([A-Z]{2})\n', text)
        state = rm.group(1) if rm else ""

    description = ""
    dm = re.search(r'\nDescription\s*\n(.+?)(?:\n(?:Pictures|Computer Translation|Disclaimer)\b|$)',
                   text, flags=re.S)
    if dm:
        description = dm.group(1).strip()[:4000]

    image_url = ""
    im = re.search(r'https://[^"\']+/sms/docviewer/cdnaucdoc/[^"\']+', html)
    if im:
        image_url = html_lib.unescape(im.group(0)).replace("/thumb-b/", "/thumb-l/")

    return {"auc_id": auc_id, "price": price, "bid_count": bid_count,
            "end_epoch_ms": end_epoch_ms, "seller": seller, "city": city,
            "state": state, "zip": zip_, "description": description,
            "image_url": image_url}


def _end_utc(end_epoch_ms, *, auc_id: str) -> datetime:
    if not end_epoch_ms:
        raise ValueError(f"publicsurplus {auc_id}: no end epoch — a lot without an end can't be tracked")
    return datetime.fromtimestamp(int(end_epoch_ms) / 1000, tz=timezone.utc)


def _status(end_utc: datetime) -> str:
    # "STA" = live, same token deals/tracking.py uses for GovDeals (LIVE_STATUS).
    return "STA" if end_utc > datetime.now(timezone.utc) else "CLO"


def card_to_lot(card: dict) -> Lot:
    auc_id = str(card["auc_id"])
    price = parse_price(card.get("price", ""), auc_id=auc_id)
    end_utc = _end_utc(card.get("end_epoch_ms"), auc_id=auc_id)
    asset_id, account_id, auction_id = synth_ids(SITE, auc_id, ordinal=ORDINAL)
    title = _TITLE_PREFIX_RE.sub("", card.get("title", "")).strip() or card.get("title", "")
    state = (card.get("state") or "").strip().upper()
    return Lot(
        asset_id=asset_id, account_id=account_id, auction_id=auction_id,
        title=title, description="",
        native_category_id="", native_category_name="",
        canonical_category="other",          # → deals' own classify pass runs
        end_utc=end_utc,
        bid_count=0,                          # not on the card; parse_detail_page has it
        opening_bid=price, current_bid=price, currency_code="USD",
        high_bidder=0, has_reserve=False, reserve_not_met=False, reserve_price=None,
        is_free=False,
        seller="", city="", state=state if len(state) == 2 else "", zip="",
        lat=None, lng=None,
        hero_image_url=card.get("image_url") or "", status=_status(end_utc), is_sold=False,
        raw=dict(card), site=SITE, native_id=auc_id,
    )


class PublicSurplusAdapter:
    site = SITE

    def __init__(self, terms: list[str] | None = None, *, delay_s: float = DELAY_S):
        self.terms = list(terms) if terms else list(SEARCH_TERMS)
        self.delay_s = delay_s
        self._lane: str | None = None            # "requests" | "browser", picked on first search
        self._last_request = 0.0
        self._native_by_key: dict[str, str] = {}  # lot_key → auc id, filled by discover()

    # -- transport ------------------------------------------------------------
    def _pace(self):
        wait = self.delay_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> str:
        self._pace()
        r = requests.get(url, headers=_HEADERS, timeout=30)
        if r.status_code in (403, 429):
            raise RuntimeError(f"publicsurplus: HTTP {r.status_code} at {url} — stopping, not bypassing")
        r.raise_for_status()
        return r.text

    def _rendered(self, url: str, wait_selector: str) -> str:
        self._pace()
        return _browser.fetch_rendered(url, wait_selector=wait_selector, delay_s=0)

    def _search_html(self, term: str, page_idx: int) -> str:
        """Lane pick at runtime: plain GET first; if it yields no cards and a
        browser is available, render it; if neither yields cards, fail loud —
        an empty iterator is never an answer."""
        url = search_url(term, page_idx)
        if self._lane == "browser":
            return self._rendered(url, _SEARCH_WAIT)
        html = self._get(url)
        if self._lane == "requests" or parse_search_cards(html):
            self._lane = self._lane or "requests"
            return html
        if not _browser.available():
            raise RuntimeError("publicsurplus: search gated (0 cards for %r) and no browser "
                               "— run on the Mac, not Render" % term)
        html = self._rendered(url, _SEARCH_WAIT)
        if not parse_search_cards(html):
            raise RuntimeError("publicsurplus: 0 cards for %r from both requests and browser" % term)
        self._lane = "browser"
        return html

    def _detail_html(self, auc_id: str) -> str:
        url = lot_url(auc_id)
        return self._rendered(url, _DETAIL_WAIT) if self._lane == "browser" else self._get(url)

    # -- contract -------------------------------------------------------------
    def discover(self, *, category_ids: str = "", search_text: str = "",
                 max_pages: int = 60, end_before: datetime | None = None, **kw) -> Iterator[Lot]:
        terms = [search_text] if search_text else self.terms
        seen: set[str] = set()
        for term in terms:
            for page_idx in range(max_pages):
                cards = parse_search_cards(self._search_html(term, page_idx))
                if not cards:
                    break
                for card in cards:
                    if card["auc_id"] in seen:
                        continue
                    seen.add(card["auc_id"])
                    try:
                        lot = card_to_lot(card)
                    except ValueError as e:
                        print(f"[discover] skipping bad card {card['auc_id']}: {e}", file=sys.stderr)
                        continue
                    self._native_by_key[lot_key(lot.asset_id, lot.account_id, lot.auction_id)] = lot.native_id
                    if end_before is not None and lot.end_utc >= end_before:
                        continue
                    yield lot
                if len(cards) < PS_PAGE_SIZE:
                    break

    def refetch(self, keys: list[tuple[int, int, int]]) -> dict[str, Snapshot]:
        wanted = {lot_key(*k) for k in keys}
        if not wanted <= self._native_by_key.keys():
            for _ in self.discover():          # cold start: re-sweep to learn auc ids
                if wanted <= self._native_by_key.keys():
                    break
        found: dict[str, Snapshot] = {}
        for k in wanted:
            auc_id = self._native_by_key.get(k)
            if not auc_id:
                continue
            try:
                d = parse_detail_page(self._detail_html(auc_id))
                price = parse_price(d["price"], auc_id=auc_id)
                end_utc = _end_utc(d["end_epoch_ms"], auc_id=auc_id)
            except ValueError as e:
                print(f"[refetch] skipping {auc_id}: {e}", file=sys.stderr)
                continue
            ids = synth_ids(SITE, auc_id, ordinal=ORDINAL)
            found[k] = Snapshot(*ids, datetime.now().astimezone(),
                                d["bid_count"], price, end_utc, _status(end_utc))
        return found

    def fetch_gallery(self, asset_id: int, account_id: int) -> list[str]:
        """Public Surplus needs the auc id, not the synthesized pair; only
        answerable for lots this process discovered."""
        auc_id = self._native_by_key.get(lot_key(asset_id, account_id, 0))
        if not auc_id:
            return []
        try:
            d = parse_detail_page(self._detail_html(auc_id))
        except Exception as e:
            print(f"[gallery] fetch failed for {auc_id}: {e}", file=sys.stderr)
            return []
        return [d["image_url"]] if d["image_url"] else []

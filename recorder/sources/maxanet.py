"""Maxanet platform adapter — shared by Wisconsin Surplus (`bid.wisconsinsurplus.com`)
and USGovBid (`bid.usgovbid.com`). ASP.NET MVC, server-rendered partials, no JSON.

LIVE RECON (verified 2026-10-09 on both tenants):

- Session first. Every `/Public/Auction/*` and `/Public/GlobalSearch/*`
  partial answers `302 → /Public/Error/NotFound` without the session cookies
  the landing page `GET /Public` sets (`SERVERID`, `languageType`). So each
  adapter instance opens one `requests.Session`, warms it with `/Public`, and
  routes every call through `polite_get(..., session=)`.
- Auction list: `GET /Public/Auction/GetAuctions?pageNumber=1&filter=Current
  &auctionTypeFilter=&pageSize=150&viewType=List` (XHR) returns auction
  CARDS (title "#26-1410 - Wisconsin Dept. of Military Affairs - Camp
  Douglas, WI", Starts/Ends `data-auc-date`, "N Items", encrypted
  `AuctionDetails?AuctionId=`). `filter=` values: `""`=All (WI: 21,265
  entries, the whole archive), `Current` (WI 96 / USGov 3 auctions at probe
  time), `Future`, `Past`. 150 cards ≈ 1.3 MB and the server is slow
  (~30 s+), so this adapter does NOT walk auctions.
- Items, not auctions, are the Observation. The per-auction item partial
  (`GetAuctionItems?aucId=<enc>&pageSize=<enc "100">…`) exposes a numeric
  `AuctionItemId`, `TotalBids`, "Current Bid : 5.00", per-item staggered
  `hdn_item_N_EndDate`, lot number, title, SKU, description, image — one
  request per auction though (96 × ~1.3 MB on WI).
- So discover() uses the site-wide ITEM search instead:
  `GET /Public/GlobalSearch/GetGlobalSearchResults?pageNumber=1&pagesize=100
  &filter=Current&sortBy=enddate_asc&search=<term>` (XHR; plain params —
  the `/GlobalSearch/GetEncryptUrl` step the UI takes first is not needed).
  Returns item cards grouped under "Auction : <auction title>": numeric item
  id (`carouselExampleControls_<id>`), `AuctionItemDetail` href with the two
  encrypted tokens, "Lot - 45662", title ("Lot of 60 Metal Folding Chairs
  with Cart"), "SKU# : 583206", "Current Bid : 165.00", Starts/Ends
  `data-auc-date="10/12/2026 10:16:00"`, thumbnail. `Pager_TotalPages` +
  "Showing 1 - 44 of 44 entries" for paging. No bid count on these cards.
  `filter=Past&sortBy=enddate_desc` gives closed items with "Final Bid : 5.00"
  and a "Sold" label (no dates on the card) → `sold_sweep()`.
- Detail: `GET /Public/Auction/AuctionItemDetail?pageNumber=<enc "0">
  &pageSize=<enc "0">&AuctionItemId=<enc>&AuctionId=<enc>` — ALL FOUR params
  are required (dropping the page tokens 302s even with a Referer). Has
  `TotalBids`, "Current Bid : 165.00", `data-enddate="2026-10-12 10:16:00 AM"`,
  `hdn_Description`, title. Used by `poll()`.
- Encrypted ids are deterministic (the same plaintext always gives the same
  token: "0" = `WddRnDis30ojx01x46RicQ==`, "1" = `pf6Q+hJtdeleDd9FfYpy9w==`,
  "100" = `O5OaPaZE1XrTjGtTQItkaw==` on both tenants) and never change for a
  lot. `source_lot_id` = `"<AuctionId token>|<AuctionItemId token>"` —
  exactly what the detail URL needs and nothing a poll row lacks (poll rows
  carry no `raw`). `|` is not a base64 character. The numeric item id is
  kept in `raw["maxanet"]["item_id"]`.
- Timezone: every date the platform renders is America/Chicago — the
  platform's own `/bundles/publicscripts` does
  `moment.tz(inputdate, "MM-DD-YYYY HH:mm:ss", "America/Chicago")` for both
  tenants, and both servers' `data-nowdate` read 11:47 AM while UTC was
  16:47. So USGovBid is Central too, not Eastern (the probe brief's guess).
- robots.txt: neither `bid.*` host serves one (404). `www.usgovbid.com`'s
  (WordPress) says `Crawl-delay: 30`; we honour that for `bid.usgovbid.com`
  as well via `register_host_interval` (every request to that host waits
  ≥ 30 s). `www.wisconsinsurplus.com` has no crawl-delay (1 s floor).
- Money: USD, "165.00" / "1,234.00".

raw keys (stable, read by the web layer): `title`, `city`, `state`,
`category` (the auction's type when known, else None), `image_url`, `url`,
`currency`, plus `maxanet` (item_id, lot_number, sku, auction_title,
auction_token, item_token, start/end local strings, price_label, …).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_get

SITE_TZ = ZoneInfo("America/Chicago")
PAGE_ZERO_TOKEN = "WddRnDis30ojx01x46RicQ=="
PAGE_SIZE = 100
MAX_SEARCH_PAGES = 5       # per term, discover (Current)
SOLD_SWEEP_PAGES = 1       # per term, sold_sweep (Past, newest first)
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
LOT_ID_SEP = "|"

_MONEY_RE = re.compile(r"(Current|Final|High|Winning)\s*Bid\s*:\s*([\d,]+\.?\d*)", re.I)
_ITEM_ID_RE = re.compile(r"carouselExampleControls_(\d+)")
_SHOWING_RE = re.compile(r"Showing\s*(?:&nbsp;)*\s*[\d,]+\s*-\s*[\d,]+\s*of\s*([\d,]+)")
_TOTAL_PAGES_RE = re.compile(r'id="Pager_TotalPages"[^>]*value="(\d+)"')
_STATE_NAMES = {
    "wisconsin": "WI", "illinois": "IL", "minnesota": "MN", "iowa": "IA", "michigan": "MI",
    "new jersey": "NJ", "new york": "NY", "pennsylvania": "PA", "indiana": "IN", "ohio": "OH",
}


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


def _local_to_utc(text: str | None, fmt: str) -> datetime | None:
    if not text:
        return None
    try:
        local = datetime.strptime(text.strip(), fmt)
    except ValueError:
        return None
    return local.replace(tzinfo=SITE_TZ).astimezone(timezone.utc)


def parse_auc_date(text: str | None) -> datetime | None:
    """`data-auc-date="10/12/2026 10:16:00"` (America/Chicago) → UTC."""
    return _local_to_utc(text, "%m/%d/%Y %H:%M:%S")


def parse_enddate(text: str | None) -> datetime | None:
    """`data-enddate="2026-10-12 10:16:00 AM"` (America/Chicago) → UTC."""
    return _local_to_utc(text, "%Y-%m-%d %I:%M:%S %p")


def _city_state(auction_title: str | None) -> tuple[str | None, str | None]:
    """'#26-1410 - Wisconsin Dept. of Military Affairs - Camp Douglas, WI'
    → ('Camp Douglas', 'WI'). Best effort: the last ' - ' segment, when it
    looks like 'City, ST' or 'City, Statename'."""
    if not auction_title:
        return None, None
    tail = auction_title.split(" - ")[-1].strip()
    m = re.match(r"^(.+?),\s*([A-Za-z .]+)$", tail)
    if not m:
        return None, None
    city, st = m.group(1).strip(), m.group(2).strip()
    if len(st) == 2 and st.isupper():
        return city, st
    code = _STATE_NAMES.get(st.lower())
    return (city, code) if code else (None, None)


def _tokens(href: str) -> tuple[str | None, str | None]:
    qs = parse_qs(urlparse(unescape(href)).query)
    auction = qs.get("AuctionId", [None])[0]
    item = qs.get("AuctionItemId", [None])[0]
    return (unquote(auction) if auction else None), (unquote(item) if item else None)


def lot_id_for(auction_token: str, item_token: str) -> str:
    return f"{auction_token}{LOT_ID_SEP}{item_token}"


def split_lot_id(lot_id: str) -> tuple[str, str] | None:
    if LOT_ID_SEP not in lot_id:
        return None
    a, _, b = lot_id.partition(LOT_ID_SEP)
    return (a, b) if a and b else None


def _redirected_to_not_found(resp) -> bool:
    """requests follows Maxanet's 302 → /Public/Error/NotFound silently;
    spot it from the final URL or the redirect history."""
    final_url = str(getattr(resp, "url", "") or "")
    if "/Error/NotFound" in final_url:
        return True
    return any(getattr(r, "status_code", None) in (301, 302) and
               "/Error/NotFound" in str(getattr(r, "headers", {}).get("Location", ""))
               for r in (getattr(resp, "history", None) or []))


def _is_furniture_text(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in FURNITURE_TERMS)


def _full_image(src: str | None) -> str | None:
    if not src:
        return None
    return re.sub(r"-\d+x\d+(\.[a-z]+)$", r"\1", src)


# --- global search partial ----------------------------------------------

def parse_search(html: str) -> tuple[list[dict], int, int | None]:
    """(item cards, total_pages, total_entries) from GetGlobalSearchResults."""
    soup = BeautifulSoup(html, "html.parser")
    m_pages = _TOTAL_PAGES_RE.search(html)
    total_pages = int(m_pages.group(1)) if m_pages else 1
    m_show = _SHOWING_RE.search(html)
    total_entries = int(m_show.group(1).replace(",", "")) if m_show else None
    items: list[dict] = []
    for group in soup.find_all("div", class_="search-listitem"):
        h4 = group.find("h4")
        auction_title = None
        if h4:
            auction_title = unescape(h4.get_text(" ", strip=True))
            auction_title = re.sub(r"^Auction\s*:\s*", "", auction_title)
        for card in group.find_all("div", class_="bg-white", recursive=False):
            item = _parse_card(card, auction_title)
            if item:
                items.append(item)
        # cards are sometimes wrapped one level deeper
        if not group.find_all("div", class_="bg-white", recursive=False):
            for card in group.select("div.bg-white.border"):
                item = _parse_card(card, auction_title)
                if item:
                    items.append(item)
    return items, total_pages, total_entries


def _parse_card(card, auction_title: str | None) -> dict | None:
    carousel = card.find("div", id=_ITEM_ID_RE)
    item_id = _ITEM_ID_RE.search(carousel["id"]).group(1) if carousel else None
    link = card.find("a", href=re.compile(r"AuctionItemDetail"))
    if link is None:
        return None
    auction_token, item_token = _tokens(link["href"])
    if not auction_token or not item_token:
        return None
    lot_el = card.select_one("span.linkbuttons")
    title_el = card.select_one(".auction-Itemlist-Title a")
    sku_el = card.select_one("p.category-info")
    text = card.get_text(" ", strip=True)
    m_money = _MONEY_RE.search(text)
    starts = ends = None
    for p in card.select("p.bid-content-date"):
        span = p.find("span", class_="local-date-time")
        if not span or not span.has_attr("data-auc-date"):
            continue
        label = p.get_text(" ", strip=True).lower()
        if "end" in label:
            ends = span["data-auc-date"]
        elif "start" in label:
            starts = span["data-auc-date"]
    img = card.find("img")
    sold = bool(card.find(string=re.compile(r"^\s*Sold\s*$")))
    return {
        "item_id": item_id,
        "auction_token": auction_token,
        "item_token": item_token,
        "lot_number": unescape(lot_el.get_text(" ", strip=True)) if lot_el else None,
        "title": unescape(title_el.get_text(" ", strip=True)) if title_el else None,
        "sku": unescape(sku_el.get_text(" ", strip=True)) if sku_el else None,
        "auction_title": auction_title,
        "price_label": m_money.group(1).title() if m_money else None,
        "price": _money(m_money.group(2)) if m_money else None,
        "starts_local": starts,
        "ends_local": ends,
        "sold_label": sold,
        "image_url": _full_image(img["src"]) if img and img.has_attr("src") else None,
        "detail_href": unescape(link["href"]),
    }


# --- item detail page ----------------------------------------------------

def parse_detail(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    bids = soup.find("input", id="TotalBids")
    if bids is None:
        return None
    title_el = soup.select_one(".auction-Itemlist-Title a") or soup.select_one(".auction-Itemlist-Title")
    lot_el = soup.select_one(".Itemlist-Lottitle")
    if lot_el is None:
        # detail breadcrumb: <a href="…AuctionItems?AuctionId=…">#26-1410 …</a> / <span>Lot - 45662</span>
        crumb = soup.find("a", href=re.compile(r"AuctionItems\?AuctionId="))
        lot_el = crumb.find_next("span", class_="font-weight-600") if crumb else None
    end_el = soup.find(attrs={"data-enddate": True})
    now_el = soup.find("input", id="nowDate")
    bid_block = soup.find("span", id=re.compile(r"^CurrentBidAmount"))
    m_money = _MONEY_RE.search(bid_block.get_text(" ", strip=True)) if bid_block else None
    if m_money is None:
        m_money = _MONEY_RE.search(soup.get_text(" ", strip=True))
    desc = soup.find("input", id="hdn_Description")
    img = soup.select_one(".carousel-item img[src]")
    auction_title_el = (soup.find("a", href=re.compile(r"AuctionItems\?AuctionId="))
                        or soup.select_one(".auctionDetail-title a"))
    sold = bool(soup.find(string=re.compile(r"^\s*Sold\s*$")))
    return {
        "title": unescape(title_el.get_text(" ", strip=True)) if title_el else None,
        "lot_number": unescape(lot_el.get_text(" ", strip=True)) if lot_el else None,
        "auction_title": unescape(auction_title_el.get_text(" ", strip=True)) if auction_title_el else None,
        "bid_count": int(bids["value"]) if str(bids.get("value", "")).isdigit() else None,
        "price_label": m_money.group(1).title() if m_money else None,
        "price": _money(m_money.group(2)) if m_money else None,
        "end_local": end_el["data-enddate"] if end_el else None,
        "now_local": now_el["data-nowdate"] if now_el and now_el.has_attr("data-nowdate") else None,
        "description_html": unescape(desc["value"])[:2000] if desc and desc.has_attr("value") else None,
        "image_url": _full_image(img["src"]) if img else None,
        "sold_label": sold,
    }


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class MaxanetSource:
    """Parameterised by HOST + SOURCE; subclasses set both (and a crawl delay)."""

    HOST: str = ""
    SOURCE: str = ""
    SITE_NAME: str = ""

    def __init__(self):
        self._session: requests.Session | None = None

    # -- transport -------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.HOST}{path}"

    def session(self) -> requests.Session | None:
        """Warm session (cookies from /Public). None when the landing page
        itself cannot be fetched — the caller treats that as a fetch failure."""
        if self._session is not None:
            return self._session
        sess = requests.Session()
        try:
            resp = polite_get(self._url("/Public"), session=sess)
        except requests.exceptions.RequestException as e:
            print(f"[{self.SOURCE}] RECORDER ERROR: landing page request failed: {e}")
            return None
        if resp.status_code != 200:
            print(f"[{self.SOURCE}] RECORDER ERROR: landing page HTTP {resp.status_code}")
            return None
        self._session = sess
        return sess

    def _get(self, path: str, params: dict | None = None, *, xhr: bool = True,
             referer: str = "/Public") -> requests.Response | None:
        sess = self.session()
        if sess is None:
            return None
        headers = {"Referer": self._url(referer)}
        if xhr:
            headers.update(XHR_HEADERS)
        try:
            return polite_get(self._url(path), params=params, headers=headers, session=sess,
                              timeout=(10, 120))
        except requests.exceptions.RequestException as e:
            print(f"[{self.SOURCE}] RECORDER ERROR: {path} request failed: {e}")
            return None

    def _search_page(self, term: str, filter_: str, sort_by: str, page: int) -> tuple[list[dict], int] | None:
        resp = self._get("/Public/GlobalSearch/GetGlobalSearchResults", {
            "pageNumber": page, "pagesize": PAGE_SIZE, "filter": filter_,
            "sortBy": sort_by, "search": term,
        }, referer="/Public/GlobalSearch")
        if resp is None:
            return None
        if resp.status_code != 200:
            print(f"[{self.SOURCE}] RECORDER ERROR: search {term!r} {filter_} p{page} HTTP {resp.status_code}")
            return None
        if "search-listitem" not in resp.text and "No Data Available" not in resp.text:
            print(f"[{self.SOURCE}] RECORDER ERROR: search {term!r} — unrecognised partial shape")
            return None
        items, total_pages, _ = parse_search(resp.text)
        return items, total_pages

    def _sweep(self, filter_: str, sort_by: str, max_pages: int) -> tuple[dict[str, dict], bool]:
        """Dedup'd items across every FURNITURE_TERMS query; any_ok tells
        whether at least one query fetched."""
        found: dict[str, dict] = {}
        any_ok = False
        for term in FURNITURE_TERMS:
            page = 1
            while page <= max_pages:
                got = self._search_page(term, filter_, sort_by, page)
                if got is None:
                    break
                any_ok = True
                items, total_pages = got
                for it in items:
                    lid = lot_id_for(it["auction_token"], it["item_token"])
                    found.setdefault(lid, it)
                if page >= total_pages or not items:
                    break
                page += 1
        return found, any_ok

    # -- observations ----------------------------------------------------

    def _observation_from_card(self, item: dict, *, status: str | None = None) -> Observation:
        end_date = parse_auc_date(item.get("ends_local"))
        if status is None:
            now = datetime.now(timezone.utc)
            status = "closed" if (item.get("sold_label") or (end_date is not None and end_date <= now)) else "active"
        city, state = _city_state(item.get("auction_title"))
        raw = {
            "title": item.get("title"),
            "city": city,
            "state": state,
            "category": None,
            "image_url": item.get("image_url"),
            "url": self._url(item["detail_href"]) if item.get("detail_href") else None,
            "currency": "USD",
            "site": self.SITE_NAME,
            "maxanet": item,
        }
        return Observation(
            source=self.SOURCE,
            source_lot_id=lot_id_for(item["auction_token"], item["item_token"]),
            status=status,
            raw=_jsonable(raw),
            current_bid=item.get("price"),
            bid_count=None,
            end_date=end_date,
        )

    def discover(self) -> list[Observation]:
        found, any_ok = self._sweep("Current", "enddate_asc", MAX_SEARCH_PAGES)
        if not any_ok:
            raise SourceFetchFailed(
                f"discover() aborted — all {len(FURNITURE_TERMS)} FURNITURE_TERMS searches failed",
                url=self.HOST)
        out = [
            self._observation_from_card(it)
            for it in found.values()
            if _is_furniture_text(f"{it.get('title') or ''} {it.get('lot_number') or ''}")
        ]
        if not out:
            print(f"[{self.SOURCE}] WARNING: discover() matched 0 furniture items "
                  f"(site search returned {len(found)} candidates)")
        return out

    def detail_url(self, lot_id: str) -> str | None:
        parts = split_lot_id(lot_id)
        if parts is None:
            return None
        auction_token, item_token = parts
        from urllib.parse import quote
        return self._url(
            "/Public/Auction/AuctionItemDetail"
            f"?pageNumber={quote(PAGE_ZERO_TOKEN, safe='')}&pageSize={quote(PAGE_ZERO_TOKEN, safe='')}"
            f"&AuctionItemId={quote(item_token, safe='')}&AuctionId={quote(auction_token, safe='')}"
        )

    def _fetch_detail(self, lot_id: str) -> tuple[int | None, dict | None]:
        url = self.detail_url(lot_id)
        if url is None:
            print(f"[{self.SOURCE}] RECORDER ERROR: malformed source_lot_id {lot_id!r} (expected 'auction|item')")
            return None, None
        resp = None
        for attempt in (1, 2):
            sess = self.session()
            if sess is None:
                return None, None
            try:
                resp = polite_get(url, session=sess, timeout=(10, 120),
                                  headers={"Referer": self._url("/Public")})
            except requests.exceptions.RequestException as e:
                print(f"[{self.SOURCE}] RECORDER ERROR: detail request failed: {e}")
                return None, None
            if not _redirected_to_not_found(resp):
                break
            # A 302 → /Error/NotFound means "unknown item" OR "session cookie
            # lost". Re-warm the session once; a second NotFound is the item.
            if attempt == 1:
                self._session = None
                continue
            return 302, None
        if resp.status_code != 200:
            return resp.status_code, None
        parsed = parse_detail(resp.text)
        if parsed is None:
            print(f"[{self.SOURCE}] RECORDER ERROR: detail for {lot_id!r} — unrecognised page shape")
        return 200, parsed

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        budget = PollBudget()
        observations: list[Observation] = []
        for lot in lots:
            if budget.exhausted:
                print(f"[{self.SOURCE}] RECORDER ERROR: poll() aborted after "
                      f"{budget.consecutive} consecutive failures — {budget.stats(len(lots))}")
                break
            lot_id = str(lot["source_lot_id"])
            status_code, detail = self._fetch_detail(lot_id)
            if detail is not None:
                budget.record(True)
                end_date = parse_enddate(detail.get("end_local"))
                closed = detail.get("sold_label") or (detail.get("price_label") == "Final") \
                    or (end_date is not None and end_date <= now)
                city, state = _city_state(detail.get("auction_title"))
                raw = {
                    "title": detail.get("title"), "city": city, "state": state, "category": None,
                    "image_url": detail.get("image_url"), "url": self.detail_url(lot_id),
                    "currency": "USD", "site": self.SITE_NAME, "maxanet": detail,
                }
                observations.append(Observation(
                    source=self.SOURCE, source_lot_id=lot_id,
                    status="closed" if closed else "active",
                    raw=_jsonable(raw), current_bid=detail.get("price"),
                    bid_count=detail.get("bid_count"), end_date=end_date,
                ))
                continue
            if status_code in (302, 404):
                # Maxanet answers 302 → /Error/NotFound for an unknown item
                budget.record(True)
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=self.SOURCE, source_lot_id=lot_id, status="gone",
                        raw={"recorder_probe": {"result": "not_found", "http_status": status_code,
                                                "url": self.detail_url(lot_id)}},
                    ))
                continue
            budget.record(False)
        return observations

    def sold_sweep(self) -> list[Observation]:
        """Past-filter search, newest close first, first page per term. Cards
        carry 'Final Bid : X' + a Sold label but NO dates — `end_date` stays
        None here; `poll()` on a tracked lot records the exact close."""
        found, any_ok = self._sweep("Past", "enddate_desc", SOLD_SWEEP_PAGES)
        if not any_ok:
            print(f"[{self.SOURCE}] RECORDER ERROR: sold_sweep() — every search failed, 0 observations")
            return []
        return [
            self._observation_from_card(it, status="closed")
            for it in found.values()
            if _is_furniture_text(f"{it.get('title') or ''} {it.get('lot_number') or ''}")
        ]

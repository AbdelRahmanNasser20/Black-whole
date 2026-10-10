"""TXAuction (Gaston & Sheehan Auctioneers, txauction.com) adapter.

The site is a React SSR app (AuctioneerSoftware). Every page ships its whole
GraphQL cache inline as ``window.__APOLLO_STATE__ = {...}``: a flat map of
normalised entities (``AuctionLot.<id>``, ``Auction.<id>``) plus a
``ROOT_QUERY`` whose keys are the queries the page ran. We parse that blob and
nothing else.

robots.txt disallows ``/api/``, ``/admin/`` and ``/asset/`` — so this adapter
NEVER calls the GraphQL endpoint and never fetches ``/asset/image/...`` URLs;
images are referenced by their CloudFront URLs only. Logged-out, plain
``requests``, one request per ``DELAY_S`` seconds; a 403/429 stops the run
(never bypassed).

Pages used:
  * ``/search?search=<term>&page=<n>`` — live lots only (the query filter is
    ``auction_lot_status=100, auction_status=[200]``), 25 per page.
    ``ROOT_QUERY['lots({…})'] = {total, lots:[{__ref}]}``.
  * ``/auctions`` (``?page=n``) — live/upcoming auctions with
    ``auction_location`` — the only place a lot's city/state/zip is reliable
    (search cards carry ``lot_location: null``).
  * ``/auctions/<auction_id>/lot/<auction_lot_id>`` — one lot, with
    ``images[]``, ``description_plain``, ``front_lot_location`` and the
    masked winner. Still served after the lot closes, so ``refetch`` reads the
    real final price off it.

Ids: native_id = ``"<auction_id>/<auction_lot_id>"``; the legacy trio is
``models.synth_ids("txauction", native_id, ordinal=10)`` → account_id = -10.

Lot ``title`` on this site is just "Lot 11"; the real text is the
description. ``derive_title`` takes its first sentence. Quantity follows the
"(500) MTS Seating …" prefix convention (see auction_extractors.quantity_infer).
"""
from __future__ import annotations

import html as html_lib
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

import requests

from deals.models import Lot, Snapshot, lot_key, synth_ids

SITE = "txauction"
ORDINAL = 10                    # permanent — feeds synth_ids account_id = -10
BASE_URL = "https://www.txauction.com"
SEARCH_PAGE_SIZE = 25           # server-fixed
AUCTIONS_PAGE_SIZE = 50         # server-fixed on /auctions
# robots.txt: Crawl-delay 10 is declared for SemrushBot/PetalBot only; nothing
# for `*`. We stay well above the 2 s floor of deals/adapters/ONBOARDING.md.
DELAY_S = 3.0
UA = "Mozilla/5.0 (BLACKWHOLE deal tracker; contact: abdel@black-whole.com)"
_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Live-lot search is full-text over the description, so broad terms are cheap
# (the site has a few hundred live lots at a time). Non-chair hits (artwork
# that mentions a chair) are dropped later by the research profile.
SEARCH_TERMS = ["chair", "chairs", "banquet", "stackable", "stacking", "seating"]

LIVE_LOT = 100                  # auction_lot_status: 100 live, 200 ended
RAW_MAX_BYTES = 2048            # deal_lots.raw stays small (DB-size rule)
TITLE_MAX = 140

_STATE_RE = re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


# ── URLs ─────────────────────────────────────────────────────────────────────

def search_url(term: str, page: int) -> str:
    return f"{BASE_URL}/search?{urllib.parse.urlencode({'search': term, 'page': page})}"


def auctions_url(page: int) -> str:
    return f"{BASE_URL}/auctions" + (f"?page={page}" if page > 1 else "")


def lot_url(native_id: str) -> str:
    """native_id "<auction_id>/<auction_lot_id>" → the public lot page."""
    auction_id, _, lot_id = str(native_id).partition("/")
    if not (auction_id and lot_id):
        raise ValueError(f"txauction: bad native_id {native_id!r}")
    return f"{BASE_URL}/auctions/{auction_id}/lot/{lot_id}"


# ── pure parsing ─────────────────────────────────────────────────────────────

def extract_apollo_state(html: str) -> dict:
    """The page's inline Apollo cache. Absent/garbled → ValueError (a page
    without it is a block page or a redesign — never an empty result)."""
    m = _STATE_RE.search(html or "")
    if not m:
        raise ValueError("txauction: no __APOLLO_STATE__ on page")
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"txauction: __APOLLO_STATE__ is not JSON: {e}") from None
    if not isinstance(state, dict) or "ROOT_QUERY" not in state:
        raise ValueError("txauction: __APOLLO_STATE__ has no ROOT_QUERY")
    return state


def _deref(state: dict, v):
    if isinstance(v, dict) and "__ref" in v:
        return state.get(v["__ref"]) or {}
    return v


def _resolved_lot(state: dict, ref) -> dict:
    lot = dict(_deref(state, ref) or {})
    if "auction" in lot:
        lot["auction"] = _deref(state, lot["auction"]) or {}
    return lot


def _query_value(container: dict, prefix: str):
    for k, v in (container or {}).items():
        if k.startswith(prefix):
            return v
    return None


def parse_search_page(html: str) -> tuple[int, list[dict]]:
    """/search page → (total live hits, lot cards on this page)."""
    state = extract_apollo_state(html)
    res = _query_value(state["ROOT_QUERY"], "lots(")
    if not isinstance(res, dict):
        raise ValueError("txauction: search page has no lots() query")
    cards = [_resolved_lot(state, r) for r in res.get("lots") or []]
    return int(res.get("total") or 0), [c for c in cards if c.get("auction_lot_id")]


def parse_catalog_page(html: str) -> list[dict]:
    """/auctions/<id> catalog → the lots on this page (the Auction entity's
    ``lots({…})`` field)."""
    state = extract_apollo_state(html)
    out: list[dict] = []
    for k, v in state.items():
        if not k.startswith("Auction."):
            continue
        res = _query_value(v, "lots(")
        if isinstance(res, dict):
            loc = _location(v.get("auction_location"))
            for r in res.get("lots") or []:
                lot = _resolved_lot(state, r)
                lot.setdefault("_location", loc)
                out.append(lot)
    return [c for c in out if c.get("auction_lot_id")]


def state_code(name: str | None) -> str:
    """'Texas' → 'TX'; already-2-letter passes; unknown → ''."""
    s = (name or "").strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return US_STATES.get(s.lower(), "")


def _location(loc: dict | None) -> dict:
    loc = loc or {}
    st = loc.get("state")
    abbr = st.get("abbreviation") if isinstance(st, dict) else ""
    return {"city": (loc.get("city") or "").strip(),
            "state": state_code(abbr or loc.get("state_name")),
            "zip": (loc.get("zip_code") or "").strip()[:10]}


def parse_auction_index(html: str) -> dict[str, dict]:
    """/auctions page → {auction_id: {title, city, state, zip, status, end_time}}."""
    state = extract_apollo_state(html)
    out: dict[str, dict] = {}
    for k, v in state.items():
        if not k.startswith("Auction.") or not v.get("auction_id"):
            continue
        out[str(v["auction_id"])] = {
            "title": (v.get("title") or "").strip(),
            **_location(v.get("auction_location")),
            "status": v.get("auction_status"),
            "end_time": v.get("end_time"),
        }
    return out


def auction_index_total(html: str) -> int:
    state = extract_apollo_state(html)
    res = _query_value(state["ROOT_QUERY"], "auctions(")
    return int((res or {}).get("total") or 0)


def _gallery(lot: dict) -> list[str]:
    urls: list[str] = []
    for img in lot.get("images") or []:
        variants = {a.get("variant"): a.get("url") for a in (img or {}).get("cached_assets") or []}
        u = variants.get("large") or variants.get("medium")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        pi = lot.get("primary_image") or {}
        u = pi.get("large") or pi.get("medium") or pi.get("url")
        if u:
            urls.append(u)
    return urls


def parse_lot_page(html: str) -> dict:
    """/auctions/<a>/lot/<l> → the resolved AuctionLot dict plus
    ``_gallery`` (large CloudFront URLs, fallback medium) and ``_location``
    (front_lot_location, else the auction's location)."""
    state = extract_apollo_state(html)
    ref = _query_value(state["ROOT_QUERY"], "lot(")
    if ref is None:
        lots = [k for k in state if k.startswith("AuctionLot.")]
        if len(lots) != 1:
            raise ValueError("txauction: lot page has no lot() query")
        ref = {"__ref": lots[0]}
    lot = _resolved_lot(state, ref)
    if not lot.get("auction_lot_id"):
        raise ValueError("txauction: lot page has no AuctionLot entity")
    auction = state.get(f"Auction.{lot.get('auction_id')}") or lot.get("auction") or {}
    loc = _location(lot.get("front_lot_location"))
    if not loc["state"]:
        loc = _location(auction.get("auction_location"))
    lot["_gallery"] = _gallery(lot)
    lot["_location"] = loc
    return lot


def plain_text(lot: dict) -> str:
    raw = lot.get("description_plain")
    if not raw:
        raw = _TAG_RE.sub(" ", lot.get("description") or "")
    text = html_lib.unescape(raw).replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def derive_title(description: str | None, lot_number) -> str:
    """First sentence of the description, ≤140 chars, never empty."""
    text = re.sub(r"\s+", " ", html_lib.unescape(_TAG_RE.sub(" ", description or ""))).strip()
    if text:
        first = _SENTENCE_END.split(text, maxsplit=1)[0].rstrip(" .;:,")
        if len(first) > TITLE_MAX:
            cut = first[:TITLE_MAX - 1]
            first = (cut.rsplit(" ", 1)[0] if " " in cut[40:] else cut).rstrip(" ,;:") + "…"
        if first:
            return first
    return f"Lot {lot_number}" if lot_number not in (None, "") else "TXAuction lot"


def _money(v, *, what: str, lot_id) -> float:
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"txauction {lot_id}: {what} missing")
    try:
        f = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise ValueError(f"txauction {lot_id}: garbled {what} {v!r}") from None
    if f < 0:
        raise ValueError(f"txauction {lot_id}: negative {what} {v!r}")
    return f


def parse_price(lot: dict) -> float:
    """Winning bid when the lot has bids, else the starting bid. Missing or
    garbled raises — never a silent 0 (deals/ HARD RULE)."""
    lot_id = lot.get("auction_lot_id")
    if int(lot.get("bid_count") or 0) > 0:
        return _money(lot.get("winning_bid_amount"), what="winning_bid_amount", lot_id=lot_id)
    return _money(lot.get("starting_bid"), what="starting_bid", lot_id=lot_id)


def _end_utc(lot: dict) -> datetime:
    raw = (lot.get("end_time") or "").strip()
    if not raw:
        raise ValueError(f"txauction {lot.get('auction_lot_id')}: no end_time — "
                         "a lot without an end can't be tracked")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"txauction {lot.get('auction_lot_id')}: garbled end_time {raw!r}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _status(lot: dict) -> str:
    """'STA' = live (the token deals/tracking.py uses), 'CLO' = ended,
    'RNM' = ended with no sale (reserve not met / passed)."""
    if lot.get("auction_lot_status") == LIVE_LOT and not lot.get("is_past_end_time"):
        return "STA"
    if lot.get("is_no_sale") or lot.get("is_passed"):
        return "RNM"
    return "CLO"


def _seller(lot: dict) -> str:
    for f in lot.get("dynamic_fields") or []:
        if (f or {}).get("label") == "Seller":
            v = ((f.get("data") or {}).get("value") or "").strip()
            if v:
                return v[:200]
    c = lot.get("basic_consignor") or {}
    name = " ".join(x.strip() for x in (c.get("first_name") or "", c.get("last_name") or "") if x.strip())
    return (name or (c.get("company") or "")).strip()[:200]


def _raw(lot: dict, description: str) -> dict:
    auction = lot.get("auction") if isinstance(lot.get("auction"), dict) else {}
    cat = lot.get("category") or {}
    raw = {
        "auction_lot_id": lot.get("auction_lot_id"), "auction_id": lot.get("auction_id"),
        "lot_number": lot.get("lot_number"), "auction_title": (auction or {}).get("title"),
        "end_time": lot.get("end_time"), "original_end_time": lot.get("original_end_time"),
        "auction_lot_status": lot.get("auction_lot_status"),
        "is_past_end_time": lot.get("is_past_end_time"),
        "bid_count": lot.get("bid_count"), "winning_bid_amount": lot.get("winning_bid_amount"),
        "starting_bid": lot.get("starting_bid"), "required_bid": lot.get("required_bid"),
        "has_reserve": lot.get("has_reserve"), "reserve_met": lot.get("reserve_met"),
        "is_no_sale": lot.get("is_no_sale"), "image_count": lot.get("image_count"),
        "category": cat.get("name"), "description": description,
    }
    while len(json.dumps(raw, default=str).encode()) > RAW_MAX_BYTES and raw["description"]:
        raw["description"] = raw["description"][: max(0, len(raw["description"]) - 200)]
    return raw


def card_to_lot(card: dict, locations: dict[str, dict] | None = None) -> Lot:
    """One AuctionLot entity (search/catalog/lot page) → Lot. ValueError on a
    missing price or end time."""
    lot_id = str(card.get("auction_lot_id") or "")
    auction_id = str(card.get("auction_id") or "")
    if not lot_id or not auction_id:
        raise ValueError(f"txauction: card without ids {lot_id!r}/{auction_id!r}")
    native_id = f"{auction_id}/{lot_id}"
    price = parse_price(card)
    end_utc = _end_utc(card)
    description = plain_text(card)
    title = derive_title(description, card.get("lot_number"))
    loc = card.get("_location") or (locations or {}).get(auction_id) \
        or _location(card.get("front_lot_location") or card.get("lot_location"))
    cat = card.get("category") or {}
    pi = card.get("primary_image") or {}
    has_reserve = bool(card.get("has_reserve"))
    bids = int(card.get("bid_count") or 0)
    status = _status(card)
    asset_id, account_id, auction_synth = synth_ids(SITE, native_id, ordinal=ORDINAL)
    return Lot(
        asset_id=asset_id, account_id=account_id, auction_id=auction_synth,
        title=title, description=description[:4000],
        native_category_id=str(cat.get("category_id") or ""),
        native_category_name=(cat.get("name") or "").strip(),
        canonical_category="other",           # → deals' own classify pass runs
        end_utc=end_utc,
        bid_count=bids,
        opening_bid=_money(card.get("starting_bid"), what="starting_bid", lot_id=lot_id)
        if card.get("starting_bid") is not None else price,
        current_bid=price, currency_code="USD",
        high_bidder=0, has_reserve=has_reserve,
        reserve_not_met=has_reserve and card.get("reserve_met") is False,
        reserve_price=None, is_free=False,
        seller=_seller(card), city=loc.get("city", ""), state=loc.get("state", ""),
        zip=loc.get("zip", ""), lat=None, lng=None,
        hero_image_url=pi.get("large") or pi.get("medium") or pi.get("url") or "",
        status=status, is_sold=(status == "CLO" and bids > 0),
        raw=_raw(card, description), site=SITE, native_id=native_id,
    )


def lot_to_snapshot(lot: dict, now: datetime | None = None) -> Snapshot:
    """A parsed lot page → Snapshot (refetch). ValueError on missing price/end."""
    native_id = f"{lot.get('auction_id')}/{lot.get('auction_lot_id')}"
    ids = synth_ids(SITE, native_id, ordinal=ORDINAL)
    return Snapshot(*ids, now or datetime.now().astimezone(),
                    int(lot.get("bid_count") or 0), parse_price(lot), _end_utc(lot), _status(lot))


# ── adapter ──────────────────────────────────────────────────────────────────

class TXAuctionAdapter:
    site = SITE

    def __init__(self, terms: list[str] | None = None, *, delay_s: float = DELAY_S,
                 session=None):
        self.terms = list(terms) if terms else list(SEARCH_TERMS)
        self.delay_s = delay_s
        self._http = session or requests
        self._last_request = 0.0
        self._locations: dict[str, dict] | None = None
        self._native_by_key: dict[str, str] = {}   # lot_key → native_id

    # -- transport ------------------------------------------------------------
    def _pace(self):
        wait = self.delay_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> str:
        self._pace()
        r = self._http.get(url, headers=_HEADERS, timeout=30)
        if r.status_code in (403, 429):
            raise RuntimeError(f"txauction: HTTP {r.status_code} at {url} — stopping, not bypassing")
        r.raise_for_status()
        return r.text

    def locations(self) -> dict[str, dict]:
        """auction_id → location, read once per process from /auctions."""
        if self._locations is None:
            out: dict[str, dict] = {}
            page = 1
            while True:
                html = self._get(auctions_url(page))
                got = parse_auction_index(html)
                new = {k: v for k, v in got.items() if k not in out}
                out.update(new)
                if not new or len(out) >= auction_index_total(html) or len(got) < AUCTIONS_PAGE_SIZE:
                    break
                page += 1
            self._locations = out
        return self._locations

    def remember(self, lots) -> None:
        """Teach refetch the native ids of stored lots (watch-once runs in a
        fresh process; ids are synthesized, so they can't be reversed)."""
        for l in lots:
            if getattr(l, "site", SITE) == SITE and l.native_id:
                self._native_by_key[lot_key(l.asset_id, l.account_id, l.auction_id)] = l.native_id

    # -- contract -------------------------------------------------------------
    def discover(self, *, category_ids: str = "", search_text: str = "",
                 max_pages: int = 60, end_before: datetime | None = None, **kw) -> Iterator[Lot]:
        terms = [search_text] if search_text else self.terms
        locations = self.locations()
        seen: set[str] = set()
        for term in terms:
            for page in range(1, max_pages + 1):
                total, cards = parse_search_page(self._get(search_url(term, page)))
                for card in cards:
                    lid = str(card["auction_lot_id"])
                    if lid in seen:
                        continue
                    seen.add(lid)
                    try:
                        lot = card_to_lot(card, locations)
                    except ValueError as e:
                        print(f"[discover] skipping bad txauction lot {lid}: {e}", file=sys.stderr)
                        continue
                    self._native_by_key[lot_key(lot.asset_id, lot.account_id, lot.auction_id)] = lot.native_id
                    if end_before is not None and lot.end_utc >= end_before:
                        continue
                    yield lot
                if len(cards) < SEARCH_PAGE_SIZE or page * SEARCH_PAGE_SIZE >= total:
                    break

    def fetch_lot(self, native_id: str) -> dict:
        return parse_lot_page(self._get(lot_url(native_id)))

    def refetch(self, keys: list[tuple[int, int, int]]) -> dict[str, Snapshot]:
        found: dict[str, Snapshot] = {}
        for k in {lot_key(*k) for k in keys}:
            native_id = self._native_by_key.get(k)
            if not native_id:
                continue
            try:
                found[k] = lot_to_snapshot(self.fetch_lot(native_id))
            except ValueError as e:
                print(f"[refetch] skipping txauction {native_id}: {e}", file=sys.stderr)
            except requests.HTTPError as e:   # 404 = lot pulled; leave it to the watcher
                print(f"[refetch] txauction {native_id}: {e}", file=sys.stderr)
        return found

    def fetch_gallery(self, asset_id: int, account_id: int) -> list[str]:
        native_id = self._native_by_key.get(lot_key(asset_id, account_id, 0))
        if not native_id:
            return []
        try:
            return self.fetch_lot(native_id)["_gallery"]
        except RuntimeError:
            raise                                  # 403/429: stop the run, never bypass
        except Exception as e:
            print(f"[gallery] txauction fetch failed for {native_id}: {e}", file=sys.stderr)
            return []

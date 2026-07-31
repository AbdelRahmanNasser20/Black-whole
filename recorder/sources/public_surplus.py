"""Public Surplus source adapter — plain-HTTP scrape of the server-rendered
`www.publicsurplus.com` search + auction-detail pages (a legacy JSP site with
no JSON API). Independent re-implementation, not an import of
`auction_extractors/public_surplus_automation.py` — see "Why re-implemented,
not imported" below.

LIVE RECON (verified 2026-07-31, this task):

- Search: `GET https://www.publicsurplus.com/sms/browse/search?posting=y&
  keyWord=<term>&page=<n>` (0-indexed `page`) — confirmed live with this
  repo's own `polite_get` User-Agent: HTTP 200, real server-rendered HTML,
  no bot-block, no login wall. `auction_extractors/public_surplus_
  automation.py`'s own `_use_http_fast_path()` docstring claims PS
  "anti-bot-gates search results" against plain `requests` post-migration
  to "ps-v2" — NOT reproduced in this recon: `keyWord=chairs&page=0`
  returned 25 real `.auction-item` cards on the first try. That comment may
  describe a different UA/header combination or a since-resolved change;
  either way, this module's own `_fetch_search_page()` is defensive
  (403/429 backs off loudly, never retry-hammers) regardless.
  `PS_PAGE_SIZE = 25` (server-fixed cards/page, verified: pages 0-9 for
  "chairs" all returned exactly 25, page 10 returned 17 — the natural last
  page).
- Each result card: `<div class="auction-item" id="{auc_id}searchGrid">
  ...</div>` — no closing-tag balance in the markup (a legacy JSP quirk), so
  cards are sliced between consecutive `id="{n}searchGrid"` div-open
  matches (same "slice to the next match's start" technique
  `recorder/sources/municibid.py` uses for its own un-nested markup).
  Verified fields, all anchored to the card's own `auc_id` (no positional
  guessing):
  - title + link: `<a href="/sms/auction/view?auc={id}" title="{title}">`
  - price: `<b id="val_{id}searchGrid">$20.00</b>` — ALWAYS a `$` amount,
    even for a zero-bid lot (verified: a confirmed-zero-bid lot's grid
    price is its opening price, e.g. "$20.00" — matches the detail page's
    own "Opening Price: $20.00" for the same lot). No "No Bids" text ever
    appears on the grid card itself.
  - end date: `<script>updateTimeLeftSpan(timeLeftInfoMap, {id},
    "{id}searchGrid", <now_ms>, <end_ms>, ...)` — `<end_ms>` is Unix epoch
    MILLISECONDS (not seconds), confirmed by cross-checking against the
    same field's plain-English `time_left` text on the same card (e.g.
    "8 hours 49 mins" matched `end_ms - now_ms` within a few seconds).
  - state: `<span class="auction-item-state">CO</span>` (2-letter USPS
    code).
  - **No bid-count field exists on the search grid at all** — verified by
    inspecting the full card markup. `bid_count` stays `None` on every
    `discover()` observation; it's only knowable via the detail page
    (`poll()`'s job).
- Detail page: `GET https://www.publicsurplus.com/sms/auction/view?auc=
  <id>`. An ACTIVE lot: HTTP 200, contains `Time Left:` (label text) plus
  the SAME `updateTimeLeftSpan(timeLeftInfoMap, {id}, "{id}", <now_ms>,
  <end_ms>, ...)` script (id suffix `"{id}"` this time, not `"{id}
  searchGrid"`), `<strong id="val_{id}">$10.50</strong>` (labeled "Opening
  Price:" when zero bids, "Current Price:" when >0 — same element id
  either way, so one regex covers both), and `<span class="fw-bold"
  id="noOfBids">2</span>` (bid count) OR `<span class="fw-bold"
  id="noOfBids"><span class="text-danger">No Bids</span></span>` (zero
  bids) — both verified live on real lots (4053470: price "$10.50", 2
  bids; 4054005: price "$20.00", "No Bids").
- **Closed/removed lots: HTTP 401, NOT 404, NOT a distinguishable "closed"
  page.** This is the one place this module's behavior diverges from the
  brief's assumption ("ended-with-bid → 'closed' (PS shows the closed page
  for a while)"). Live-verified extensively in this recon: every auction id
  OUTSIDE the currently-active id range returned HTTP 401 with a generic
  `<title>Public Surplus: Login</title>` body (`loginLoad()` redirect to
  `/sms/login/login?dst=...`) — including ids adjacent (within single
  digits) to still-active ids from the exact same page-0 sweep, confirming
  this is PS closing off ANONYMOUS access the moment an auction ends, not
  an archival cutoff for old ids. No amount of probing (checked 12+ ids
  across a range of "ages") ever produced a 200 response with recognizable
  "closed"/"sold"/"final price" markup for a non-active auction — so
  `_fetch_detail()` treats BOTH 401 and 404 (in case that shape exists too,
  untested) as the "not_found" signal (matches the shared contract's `gone`
  probe shape), and does not attempt to distinguish a "closed-with-price"
  state — PS simply never shows one to an unauthenticated request. A 200
  response that lacks the `Time Left:` marker (a shape never observed live)
  is treated as an UNRECOGNIZED page shape → fetch failure, per the task
  brief's explicit instruction ("unrecognized page shape → fetch-failure,
  not 'gone'") — never guessed at.
- Money: `$10.50` / `$20.00` style — `_parse_money` strips everything but
  digits and `.` before `Decimal(...)`.
- Dates: epoch milliseconds, tz-aware UTC via `datetime.fromtimestamp(ms /
  1000, tz=timezone.utc)`.

Why re-implemented, not imported from `auction_extractors/public_surplus_
automation.py`: that module imports `playwright.sync_api` (a full browser
dependency), `quantity_llm`/`quantity_infer` (title/description LLM
quantity-inference machinery), and `listings_db` (a separate SQLite cache)
at MODULE SCOPE — importing it here would pull in all of that just to
parse a search card. The brief explicitly allows this: "reuse its
importable helpers if clean, else re-implement the minimal search-page
parse ... parsing only title/bid/end/id, NOT the LLM quantity machinery."
This module reimplements the (verified-identical-live) regexes for exactly
that minimal slice — same technique `recorder/sources/municibid.py`
already uses for its own from-scratch HTML parsing, so this stays
consistent with the rest of `recorder/sources/`.

Fixtures captured live 2026-07-31 under `tests/recorder/fixtures/
public_surplus/`: `search_chairs_page0.html` (6 real cards trimmed from a
`keyWord=chairs&page=0` response), `detail_active_no_bids_4054005.html` +
`detail_active_with_bids_4053470.html` (real active detail-page fragments,
one zero-bid one with bids — both trimmed to the Time-Left/price/bid-count
blocks), `detail_login_wall_401.html` (the real, generic 401 login-wall
body — see "Closed/removed lots" above; content is identical regardless of
which non-active auction id triggers it).
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, polite_get

SOURCE = "public_surplus"

BASE_URL = "https://www.publicsurplus.com"
SEARCH_URL = f"{BASE_URL}/sms/browse/search"
DETAIL_URL_TMPL = f"{BASE_URL}/sms/auction/view?auc={{id}}"

# Server-fixed cards per search page — verified live 2026-07-31 (see module
# docstring). A page returning fewer than this is the natural end of the
# result set.
PS_PAGE_SIZE = 25

# Hard cap on pages fetched per FURNITURE_TERMS query — "chairs" alone ran
# 11 pages (267 items) live; this leaves headroom without an unbounded
# request burst against a single host (>=1s/request via polite_get already
# throttles the wall-clock cost).
MAX_SEARCH_PAGES = 20

_GRID_CARD_RE = re.compile(r'<div class="auction-item" id="(\d+)searchGrid">')
_LOCATION_RE = re.compile(r'auction-item-state[^>]*>\s*([^<]*)')
_DETAIL_BIDCOUNT_RE = re.compile(r'id="noOfBids">(.*?)</span>', re.S)


def _title_link_re(auc_id: str) -> re.Pattern:
    return re.compile(r'<a\s[^>]*href="(/sms/auction/view\?auc=%s)"[^>]*title="([^"]+)"' % re.escape(auc_id))


def _search_price_re(auc_id: str) -> re.Pattern:
    return re.compile(r'id="val_%ssearchGrid"[^>]*>\s*([^<]+)' % re.escape(auc_id))


def _end_epoch_re(auc_id: str) -> re.Pattern:
    """Matches `updateTimeLeftSpan(timeLeftInfoMap, {id}, "<anything
    quoted>", <now_ms>, <end_ms>, ...)` — identical on both the search-grid
    card (`"{id}searchGrid"`) and the detail page (`"{id}"`); the quoted
    2nd arg is a wildcard so one pattern covers both call sites."""
    return re.compile(
        r'updateTimeLeftSpan\(\s*timeLeftInfoMap,\s*%s,\s*"[^"]*",\s*\d+,\s*(\d+)' % re.escape(auc_id)
    )


def _detail_price_re(auc_id: str) -> re.Pattern:
    return re.compile(r'id="val_%s"[^>]*>\s*([^<]+)' % re.escape(auc_id))


def _parse_money(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_epoch_ms(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_bid_count(raw_inner: str | None) -> tuple[int | None, str | None]:
    """Parse `noOfBids`'s inner HTML: either a bare digit string or a
    nested `<span class="text-danger">No Bids</span>`. Returns
    `(bid_count, raw_text)` — `raw_text` is the literal matched substring,
    kept in `raw` per the shared contract."""
    if raw_inner is None:
        return None, None
    text = raw_inner.strip()
    if "no bids" in text.lower():
        return 0, text
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None, text
    return int(digits), text


def _parse_search_cards(html: str, page_url: str) -> list[dict]:
    """Parse one search-results page into card dicts. `raw` per observation
    is the parsed card dict itself (title/link/location/price_raw/
    end_epoch_ms_raw/page_url) — the literal matched substrings, per the
    shared contract's "raw must carry ... the raw matched substrings"
    instruction for HTML sources."""
    matches = list(_GRID_CARD_RE.finditer(html))
    cards: list[dict] = []
    for i, m in enumerate(matches):
        auc_id = m.group(1)
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        seg = html[m.start():seg_end]

        tm = _title_link_re(auc_id).search(seg)
        if not tm:
            print(f"[public_surplus] skipping card {auc_id} with no title/link match")
            continue
        link = BASE_URL + tm.group(1)
        title = html_lib.unescape(tm.group(2)).strip()

        pm = _search_price_re(auc_id).search(seg)
        price_raw = pm.group(1).strip() if pm else None

        em = _end_epoch_re(auc_id).search(seg)
        end_epoch_ms_raw = em.group(1) if em else None

        lm = _LOCATION_RE.search(seg)
        location = lm.group(1).strip() if lm else ""

        cards.append({
            "auc_id": auc_id,
            "title": title,
            "link": link,
            "location": location,
            "price_raw": price_raw,
            "end_epoch_ms_raw": end_epoch_ms_raw,
            "page_url": page_url,
        })
    return cards


def _to_observation(card: dict, *, status: str = "active") -> Observation:
    return Observation(
        source=SOURCE,
        source_lot_id=card["auc_id"],
        status=status,
        raw=card,
        current_bid=_parse_money(card.get("price_raw")),
        bid_count=None,  # never present on the search grid — see module docstring
        end_date=_parse_epoch_ms(card.get("end_epoch_ms_raw")),
    )


def _fetch_search_page(term: str, page: int) -> list[dict] | None:
    """One page fetch. Returns `None` on ANY fetch failure (network
    exception, blocked, non-200) — never an empty list, so callers can tell
    "fetch failed" apart from "fetch succeeded, genuinely nothing there.\""""
    try:
        resp = polite_get(SEARCH_URL, params={"posting": "y", "keyWord": term, "page": page})
    except requests.exceptions.RequestException as e:
        print(f"[public_surplus] RECORDER ERROR: request failed ({term!r}, page={page}): {e}")
        return None
    if resp.status_code in (403, 429):
        print(
            f"[public_surplus] RECORDER ERROR: blocked HTTP {resp.status_code} on {resp.url} "
            "— backing off, no data this round"
        )
        return None
    if resp.status_code != 200:
        print(f"[public_surplus] RECORDER ERROR: unexpected HTTP {resp.status_code} on {resp.url}")
        return None
    return _parse_search_cards(resp.text, resp.url)


def _sweep_term(term: str, max_pages: int = MAX_SEARCH_PAGES) -> tuple[list[dict], bool]:
    """Paginate one FURNITURE_TERMS query until a short page (< PS_PAGE_SIZE
    — the natural end of PS's result set, verified live) or `max_pages` is
    hit (loud WARNING if the cap truncates a still-full page). Returns
    `(cards, ok)`; `ok=False` only if the FIRST page's fetch failed outright
    — a LATER page failing just stops pagination with whatever was already
    collected (mirrors `recorder/sources/municibid.py`'s per-term partial-
    failure model — this source is multi-request-by-term by construction).
    """
    cards: list[dict] = []
    page = 0
    while page < max_pages:
        batch = _fetch_search_page(term, page)
        if batch is None:
            if page == 0:
                return [], False
            print(
                f"[public_surplus] WARNING: pagination for {term!r} stopped early "
                f"(page {page} fetch failed) — {len(cards)} card(s) collected"
            )
            return cards, True
        cards.extend(batch)
        if len(batch) < PS_PAGE_SIZE:
            return cards, True
        page += 1
    print(
        f"[public_surplus] WARNING: pagination for {term!r} hit the {max_pages}-page "
        "cap — result set may be larger than what was collected"
    )
    return cards, True


def _sweep_all_terms() -> tuple[dict[str, dict], bool]:
    """Sweep FURNITURE_TERMS, merging/deduping by auc_id across terms (a lot
    matching two terms is fetched twice, stored once). Returns
    `(cards_by_id, any_ok)`; `any_ok=False` only if EVERY term's sweep
    failed outright."""
    cards_by_id: dict[str, dict] = {}
    any_ok = False
    for term in FURNITURE_TERMS:
        cards, ok = _sweep_term(term)
        any_ok = any_ok or ok
        for c in cards:
            cards_by_id[c["auc_id"]] = c
    return cards_by_id, any_ok


def _fetch_detail(lot_id: str) -> dict | None:
    """Fetch and parse one lot's detail page. Returns:
    - `None` on fetch failure (network exception, blocked, non-200/401/404,
      or an unrecognized 200 page shape) — caller must emit no observation.
    - `{"not_found": True, "http_status": int}` for a confirmed-closed/
      removed lot (HTTP 401 — the real live signal, see module docstring —
      or 404, untested but handled the same way).
    - `{"not_found": False, "url", "current_bid", "current_bid_raw",
      "bid_count", "bid_count_raw", "end_date", "end_date_raw"}` for a
      still-active lot. `*_raw` fields are the literal matched source
      substrings so a future parser fix can recompute the parsed values
      from `raw` alone, without re-scraping.
    """
    url = DETAIL_URL_TMPL.format(id=lot_id)
    try:
        resp = polite_get(url)
    except requests.exceptions.RequestException as e:
        print(f"[public_surplus] RECORDER ERROR: poll() request failed for lot {lot_id}: {e}")
        return None
    if resp.status_code in (403, 429):
        print(f"[public_surplus] RECORDER ERROR: poll() blocked HTTP {resp.status_code} for lot {lot_id} on {resp.url}")
        return None
    if resp.status_code in (401, 404):
        return {"not_found": True, "http_status": resp.status_code}
    if resp.status_code != 200:
        print(f"[public_surplus] RECORDER ERROR: poll() unexpected HTTP {resp.status_code} for lot {lot_id} on {resp.url}")
        return None
    html = resp.text
    if "Time Left:" not in html:
        print(
            f"[public_surplus] RECORDER ERROR: poll() unrecognized page shape for lot {lot_id} "
            f"on {resp.url} (no 'Time Left:' marker — no distinguishable closed-page shape has "
            "ever been observed live for this source, see module docstring; treating a healthy "
            "200 without it as a fetch failure, not 'gone')"
        )
        return None
    price_m = _detail_price_re(lot_id).search(html)
    current_bid_raw = price_m.group(1).strip() if price_m else None
    bid_m = _DETAIL_BIDCOUNT_RE.search(html)
    bid_count, bid_count_raw = _parse_bid_count(bid_m.group(1) if bid_m else None)
    end_m = _end_epoch_re(lot_id).search(html)
    end_date_raw = end_m.group(1) if end_m else None
    return {
        "not_found": False,
        "url": resp.url,
        "current_bid": _parse_money(current_bid_raw),
        "current_bid_raw": current_bid_raw,
        "bid_count": bid_count,
        "bid_count_raw": bid_count_raw,
        "end_date": _parse_epoch_ms(end_date_raw),
        "end_date_raw": end_date_raw,
    }


class PublicSurplusSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        cards_by_id, any_ok = _sweep_all_terms()
        if not any_ok:
            print(
                f"[public_surplus] RECORDER ERROR: discover() aborted — all {len(FURNITURE_TERMS)} "
                "FURNITURE_TERMS fetches failed, 0 observations"
            )
            return []
        result = [_to_observation(c) for c in cards_by_id.values()]
        if not result:
            print(
                f"[public_surplus] WARNING: discover() found 0 active listings across "
                f"{len(FURNITURE_TERMS)} FURNITURE_TERMS queries — check terms / page shape for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        observations: list[Observation] = []
        for lot in lots:
            lot_id = str(lot["source_lot_id"])
            detail = _fetch_detail(lot_id)
            if detail is None:
                continue  # fetch failure for this lot — loud error already printed, skip
            if detail["not_found"]:
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE,
                        source_lot_id=lot_id,
                        status="gone",
                        raw={"recorder_probe": {
                            "result": "not_found",
                            "http_status": detail["http_status"],
                            "url": DETAIL_URL_TMPL.format(id=lot_id),
                        }},
                    ))
                # else: not yet past end_date (or end_date unknown) — nothing this round.
                continue
            observations.append(Observation(
                source=SOURCE,
                source_lot_id=lot_id,
                status="active",
                raw={"detail_page": {
                    "url": detail["url"],
                    "status_marker": "Time Left:",
                    "current_bid_raw": detail["current_bid_raw"],
                    "bid_count_raw": detail["bid_count_raw"],
                    "end_date_raw": detail["end_date_raw"],
                }},
                current_bid=detail["current_bid"],
                bid_count=detail["bid_count"],
                end_date=detail["end_date"],
            ))
        return observations

    def sold_sweep(self) -> list[Observation]:
        return []

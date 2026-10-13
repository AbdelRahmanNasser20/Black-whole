"""Municibid source adapter — Next.js (App Router) category browse pages for
discover()/sold_sweep(), and the per-lot detail page for poll(). Lot data is
read out of the server-rendered React Server Components flight payload
(`self.__next_f.push([1,"..."])` script chunks), not out of visible HTML.

LIVE RECON (verified 2026-10-09 — municibid.com was redesigned in late
September 2026; everything the 2026-07-31 adapter relied on is gone):

- The old `GET /Search/Results?FullTextQuery=…&StatusFilter=…` is dead: it
  redirects to `/search?q=<term>&…&search=<term>` and the response carries no
  `srp-markers-data` JSON marker. **robots.txt now disallows `/search` and
  `/*?*search=`** (checked 2026-10-09: `User-Agent: * / Allow: / /
  Disallow: /search / Disallow: /*?*search=`), so this adapter does NOT use
  full-text search at all. The site's own category browse pages
  (`/browse?category=<id>[&status=completed][&page=N]`, linked from
  `sitemaps/categories.xml`) are allowed and serve the same lot data, so the
  sweep is category-driven instead of term-driven:
  - `BROWSE_CATEGORIES` = Furniture › Chairs (`11097697`) and School Supplies
    › Desks and Chairs (`29720497`) — every lot in those is seating by
    definition and is kept as-is — plus the Furniture top category
    (`160885`, all subcategories) filtered through `FURNITURE_TERMS` on the
    lot title (`_matches_furniture_terms`, word-stem match so "chair" and
    "chairs" both hit), which catches "banquet"/"office furniture"/"seating"
    lots filed under Tables/Miscellaneous — and the Office Supplies category
    (`169128`, same term filter: live lot 86045046 "Office chairs" is filed
    there, not under Furniture). Results are merged/deduped by listing id
    across categories.
- A browse page renders **24 cards per page**, `page` is **0-indexed** and
  omitted for page 0 (the site's own pager links are `?category=…&page=1` for
  UI page 2). Each card is an RSC element whose props are
  `{"listing": {...}, "completed": bool, "watching": null}`; the `listing`
  dict is flat: `id` (int, Municibid's globally unique listing id →
  `source_lot_id`), `title`, `subtitle`, `currentPrice` (number — current bid
  while active, final bid when completed), `bidCount`, `endsAtUtc`,
  `primaryImageUrl`, `location` ("City, ST"), `distanceMiles`,
  `sellerUserName`, `agencyName`, `reservePrice`, `sold` (null while active;
  true/false once completed), `hasVideo`. That dict is stored as `raw`,
  untouched. The page header carries `"<N> active listings"` /
  `"<N> completed listings"` (`_parse_header_count`), and a page past the
  end comes back HTTP 200 with zero cards and a `No listings found.` line —
  verified live on `?category=11097697&page=5` (5 active listings).
- `status=completed` lists closed lots **newest close first** (verified:
  page 0 of Chairs/completed ran 2026-10-08 → older; Furniture/completed
  page 0 covered 2026-10-09 → 2026-10-06 in 24 cards). The completed tab is
  all-time (3,041 Chairs, 11,594 Furniture), so `sold_sweep()` paginates
  only until a page's oldest `endsAtUtc` is older than
  `SOLD_SWEEP_LOOKBACK_DAYS` (7) and drops older cards — it is the safety net
  for closes `poll()` missed, not an archive crawl. It sweeps the two seating
  subcategories only (the Furniture top category's completed pages weigh
  ~4 MB each because of a sidebar seller index, and a 7-day window there is
  ~5 pages for little extra seating coverage).
- Dates: `endsAtUtc` (`"2026-10-20T12:29:00"`, no suffix) **is UTC** —
  verified against the same lot's rendered "End Date: Tuesday, October 20,
  2026 8:29 AM ET" (EDT = UTC-4) and the bidbox's `serverNowUtc`.
  `_parse_utc()` attaches `timezone.utc` directly. Sub-second fractions
  (`"2027-02-16T20:10:06.83"`) occur on some lots and are accepted.
- The detail page is `GET /listing/{id}/{slug}` — the slug is decorative,
  any value works (`DETAIL_URL_TMPL` uses `x`), no redirect. Verified shapes:
  - **Active**: bidbox props `{"listingId": N, "initial": {"currentPrice":
    195, "bidCount": 1, "endsAtUtc": "…", "status": "Active", "leaderMasked",
    "youAreWinning", "yourMaxBid", "increment", "serverNowUtc",
    "reserveMet"}, …}` plus a summary element `{"currentPrice": 195,
    "bidCount": 1, "ended": false}` and an "End Date" / "Current Price" row.
  - **Closed, sold**: `initial.status == "Successful"`, `ended: true`,
    label "Final Bid" (lot 82394410: 21.00 / 8 bids — the same lot and the
    same numbers the 2026-07-31 fixture had, so the final-price semantics
    carried over the redesign).
  - **Closed, unsold**: `initial.status == "Unsuccessful"`, `ended: true`,
    `reserveMet: false`, no "Final Bid" row (lot 86003942: 25.00 / 0 bids).
    `currentPrice` is then the unmet asking price, not a sale — the
    `sold_comps` view's `bid_count > 0` gate already excludes it.
  - **Not found**: HTTP 200 (never a real 404 — the Next.js not-found
    boundary renders inside a 200) with the site's generic `<title>` and an
    `E{"digest":"NEXT_HTTP_ERROR_FALLBACK;404"}` flight error line
    (`_is_not_found_page`). That digest line is the ONLY safe marker: the
    `{"code":"404","title":"We could not find that page", …}` element is
    pre-rendered into EVERY page's flight payload (verified on the live
    active lot 86233903), so matching on it would mark live lots 'gone'. A
    real HTTP 404 is handled too, defensively.
  `poll()` takes `status` from `initial.status` — `"Active"` (and `ended ==
  false`) → `active`, anything else → `closed` — and `current_bid` /
  `bid_count` / `end_date` from `initial`, so one anchor serves every shape.
  A page with neither a bidbox `initial` nor the not-found marker is an
  "unrecognized page shape" → fetch failure for that lot (no observation,
  loud `RECORDER ERROR`), never a guessed status.
- Money: `currentPrice` is a JSON number (`195`, `24755`, `8`); the rendered
  `$$21.00` strings are display-only and not parsed.
- Cloudflare still fronts the host (`server: cloudflare`); every GET in this
  recon came back a plain 200 to `polite_get`'s desktop-Chrome UA. 403/429
  are treated as a block (back off, no data, loud error), never escalated to
  browser automation.

RSC decoding (`_rsc_text`): the flight payload is split across several
`<script>self.__next_f.push([1,"<JS string>"])</script>` tags; each string is
JSON-string-decoded and all are concatenated. Card/bidbox objects are then
located by their literal key prefix (`{"listing":{"id":`, `"listingId":N,
"initial":{`) and parsed with `json.JSONDecoder.raw_decode`, so a title
containing braces or quotes can't break the parse the way a regex would.

Fetch contract (base.py, 2026-10-09): `discover()` raises `SourceFetchFailed`
when EVERY category fetch failed (network, 403/429, page-shape drift) and
returns `[]` only when the fetches succeeded and matched nothing — the
breaker counts the former as a failed attempt and the latter as a success.
A partial failure (some categories ok) still returns what the healthy
categories found, with a loud `RECORDER ERROR` per failed category.
`sold_sweep()` keeps returning `[]` on total failure (loud error), like the
other adapters.

poll() gone semantics: per-lot, not per-batch. `_fetch_detail(lot_id)`
returns `None` only for an outright fetch failure (network exception,
403/429 block, unexpected HTTP status, unrecognized page shape) — `poll()`
skips that lot with no observation and continues; a transient failure never
produces a false 'gone' (append-only — that mistake would be permanent). A
`not_found` result only becomes a `status='gone'` observation if the lot's
own `end_date` (from the caller-supplied `tracked_active()` row) has already
passed; otherwise nothing is emitted this round.

poll()'s `raw` is `{"detail_page": {"url", "listing_id", "initial": <the
untouched bidbox initial dict>, "summary": <the untouched
currentPrice/bidCount/ended dict or None>}}` — the source payload itself, so
a future parser fix can recompute every field from `raw` without
re-scraping.

Fixtures captured live 2026-10-09 under `tests/recorder/fixtures/municibid/`
(real pages trimmed to the RSC flight lines this module reads — whole lines,
re-encoded exactly as Next.js emits them, see the comment at the top of each
file): `browse_chairs_active.html` (5 cards), `browse_desks_chairs_active.
html` (2), `browse_furniture_active_page{0,1,2}.html` (24 + 24 + 1 = the
real 49-lot, 3-page Furniture set), `browse_chairs_completed_page0.html`
(24 closed cards, `sold` true/false mix), `browse_office_supplies_active.
html` (7 cards, one seating match), `browse_empty_page.html` (page 5 of a
5-lot category), `detail_active_86233903.html`,
`detail_closed_successful_82394410.html`,
`detail_closed_unsuccessful_86003942.html`, `detail_not_found.html`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_get

SOURCE = "municibid"

BROWSE_URL = "https://municibid.com/browse"
DETAIL_URL_TMPL = "https://municibid.com/listing/{id}/x"

# (category id, label, keep-all?) — see module docstring. keep-all categories
# are seating by definition; the rest are filtered through FURNITURE_TERMS.
BROWSE_CATEGORIES: list[tuple[str, str, bool]] = [
    ("11097697", "furniture-chairs", True),
    ("29720497", "school_supplies-desks_and_chairs", True),
    ("160885", "furniture", False),
    ("169128", "office_supplies", False),
]
SOLD_SWEEP_CATEGORIES: list[tuple[str, str, bool]] = [c for c in BROWSE_CATEGORIES if c[2]]

PAGE_SIZE = 24
# Hard cap on pages fetched per category per sweep (240 lots) — a real,
# larger result set must not turn into an unbounded burst against a
# Cloudflare-fronted host. Live 2026-10-09: Furniture/active needed 3.
MAX_PAGES_PER_CATEGORY = 10
SOLD_SWEEP_LOOKBACK_DAYS = 7

_RSC_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_HEADER_COUNT_RE = re.compile(r'"browse-head__count","children":"([\d,]+) (active|completed) listings?"')
_CARD_PREFIX = '{"listing":{"id":'
_BIDBOX_RE = re.compile(r'"listingId":(\d+),"initial":\{')
_SUMMARY_RE = re.compile(r'\{"currentPrice":[^{}]*?"ended":(?:true|false)\}')
# ONLY the error digest line — see module docstring: every page's flight
# payload pre-renders the `{"code":"404",…}` not-found template, so that
# string alone would turn every live lot into a false 'gone'.
_NOT_FOUND_MARKERS = ('NEXT_HTTP_ERROR_FALLBACK;404',)
_EMPTY_PAGE_MARKER = "No listings found."

_decoder = json.JSONDecoder()


# --- parsing helpers --------------------------------------------------------

def _parse_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_utc(raw: Any) -> datetime | None:
    """Parse an `endsAtUtc`-style value (`YYYY-MM-DDTHH:MM:SS[.fff]`, no
    suffix, already UTC — see module docstring) to a tz-aware UTC datetime."""
    if not raw:
        return None
    text = str(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _rsc_text(html: str) -> str:
    """Decode and concatenate every `self.__next_f.push([1,"…"])` chunk."""
    parts = []
    for m in _RSC_CHUNK_RE.finditer(html):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    return "".join(parts)


def _objects_at(text: str, prefix: str) -> list[dict]:
    """Every JSON object in `text` that starts with the literal `prefix`
    (which must begin with `{`), parsed with raw_decode."""
    out: list[dict] = []
    start = 0
    while True:
        i = text.find(prefix, start)
        if i < 0:
            return out
        try:
            obj, end = _decoder.raw_decode(text, i)
        except ValueError:
            start = i + len(prefix)
            continue
        if isinstance(obj, dict):
            out.append(obj)
        start = end


def _parse_browse_cards(html: str) -> list[dict]:
    """The page's listing cards, as their `{"listing": {...}, "completed":
    bool, ...}` props dicts, in page order."""
    text = _rsc_text(html)
    cards = []
    for props in _objects_at(text, _CARD_PREFIX):
        listing = props.get("listing")
        if isinstance(listing, dict) and listing.get("id") is not None:
            cards.append(props)
    return cards


def _parse_header_count(html: str) -> int | None:
    m = _HEADER_COUNT_RE.search(_rsc_text(html))
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def _is_empty_page(html: str) -> bool:
    return _EMPTY_PAGE_MARKER in _rsc_text(html)


def _is_not_found_page(html: str) -> bool:
    text = _rsc_text(html)
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _stems(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD_RE.findall(text.lower())}


def _matches_furniture_terms(title: str | None, subtitle: str | None = None) -> bool:
    """True if every word of any FURNITURE_TERMS entry appears (as a stem —
    "chair"/"chairs", "stackable"/"stackables") in the lot's title/subtitle."""
    words = _stems(f"{title or ''} {subtitle or ''}")
    if not words:
        return False
    return any(_stems(term) <= words for term in FURNITURE_TERMS)


def _to_observation(listing: dict, *, status: str) -> Observation | None:
    lot_id = listing.get("id")
    if lot_id is None:
        print(f"[municibid] skipping card with no 'id': {listing.get('title')!r}")
        return None
    return Observation(
        source=SOURCE,
        source_lot_id=str(lot_id),
        status=status,
        raw=listing,
        current_bid=_parse_money(listing.get("currentPrice")),
        bid_count=listing.get("bidCount") if isinstance(listing.get("bidCount"), int) else None,
        end_date=_parse_utc(listing.get("endsAtUtc")),
    )


# --- fetching --------------------------------------------------------

def _fetch_browse_page(category: str, *, completed: bool, page: int = 0) -> tuple[list[dict], int | None] | None:
    """One `GET /browse?category=…[&status=completed][&page=N]`. Returns
    `(cards, header_count)` or `None` on ANY fetch failure (network
    exception, 403/429 block, non-200, no cards AND no "No listings found."
    marker — i.e. a page shape we don't recognise) — never an empty tuple,
    so callers can tell "fetch failed" apart from "nothing there"."""
    params: dict[str, Any] = {"category": category}
    if completed:
        params["status"] = "completed"
    if page:
        params["page"] = page
    try:
        resp = polite_get(BROWSE_URL, params=params)
    except requests.exceptions.RequestException as e:
        print(f"[municibid] RECORDER ERROR: request failed (category={category}, completed={completed}, page={page}): {e}")
        return None
    if resp.status_code in (403, 429):
        print(
            f"[municibid] RECORDER ERROR: blocked HTTP {resp.status_code} on {resp.url} "
            "— Cloudflare or rate-limit; backing off, no data this round"
        )
        return None
    if resp.status_code != 200:
        print(f"[municibid] RECORDER ERROR: unexpected HTTP {resp.status_code} on {resp.url}")
        return None
    html = resp.text
    cards = _parse_browse_cards(html)
    count = _parse_header_count(html)
    if not cards and not _is_empty_page(html) and count is None:
        print(f"[municibid] RECORDER ERROR: no listing cards and no empty-page marker in response from {resp.url}")
        return None
    return cards, count


def _fetch_category(
    category: str, *, completed: bool, max_pages: int = MAX_PAGES_PER_CATEGORY,
    older_than: datetime | None = None,
) -> list[dict] | None:
    """All cards of one category tab, paginating from page 0 until a short
    or empty page (natural end), `max_pages` (loud WARNING), or — when
    `older_than` is given, for the newest-first completed tab — until a page's
    oldest `endsAtUtc` is before it (cards older than it are dropped).
    Returns `None` only if page 0 itself fails; a later page failing keeps
    what was collected and prints a WARNING."""
    cards: list[dict] = []
    header_count: int | None = None
    page = 0
    stop_reason: str | None = None
    while page < max_pages:
        result = _fetch_browse_page(category, completed=completed, page=page)
        if result is None:
            if page == 0:
                return None
            stop_reason = f"page {page} fetch failed"
            break
        page_cards, count = result
        if header_count is None:
            header_count = count
        if older_than is not None:
            kept = []
            for c in page_cards:
                end = _parse_utc(c["listing"].get("endsAtUtc"))
                if end is None or end >= older_than:
                    kept.append(c)
            cards.extend(kept)
            if len(kept) < len(page_cards):
                break  # reached cards older than the window — done
        else:
            cards.extend(page_cards)
        if len(page_cards) < PAGE_SIZE:
            break  # short/empty page = natural end of the result set
        page += 1
    else:
        stop_reason = f"hit the {max_pages}-page cap"
    if stop_reason:
        print(
            f"[municibid] WARNING: browse pagination for category {category} "
            f"(completed={completed}) stopped early ({stop_reason}) — "
            f"{len(cards)} card(s) collected"
        )
    elif older_than is None and header_count is not None and len(cards) != header_count:
        print(
            f"[municibid] WARNING: category {category} (completed={completed}) header says "
            f"{header_count} listings but {len(cards)} card(s) were collected — result set "
            "shifted between throttled page fetches, or the page shape drifted"
        )
    return cards


def _sweep(
    categories: list[tuple[str, str, bool]], *, completed: bool, older_than: datetime | None = None,
) -> tuple[dict[int, dict], bool]:
    """Sweep the given categories, merging/deduping listings by id. keep-all
    categories contribute every card; the others only cards whose title
    matches FURNITURE_TERMS. Returns (listings_by_id, any_category_ok)."""
    listings_by_id: dict[int, dict] = {}
    any_ok = False
    for category, _label, keep_all in categories:
        cards = _fetch_category(category, completed=completed, older_than=older_than)
        if cards is None:
            continue
        any_ok = True
        for props in cards:
            listing = props["listing"]
            if not keep_all and not _matches_furniture_terms(listing.get("title"), listing.get("subtitle")):
                continue
            listings_by_id.setdefault(listing["id"], listing)
    return listings_by_id, any_ok


def _fetch_detail(lot_id: str) -> dict | None:
    """Fetch and parse one lot's detail page. Returns:
    - `None` on fetch failure (network exception, blocked, unexpected HTTP
      status, unrecognized page shape) — caller must emit no observation.
    - `{"not_found": True, "http_status": int}` if the lot is confirmed
      absent (200 + the Next.js 404 boundary, or a real 404).
    - `{"not_found": False, "url", "listing_id", "status", "initial",
      "summary", "current_bid", "bid_count", "end_date"}` for a found lot,
      status re-derived from the page's bidbox `initial.status`.
    """
    url = DETAIL_URL_TMPL.format(id=lot_id)
    try:
        resp = polite_get(url)
    except requests.exceptions.RequestException as e:
        print(f"[municibid] RECORDER ERROR: poll() request failed for lot {lot_id}: {e}")
        return None
    if resp.status_code in (403, 429):
        print(f"[municibid] RECORDER ERROR: poll() blocked HTTP {resp.status_code} for lot {lot_id} on {resp.url}")
        return None
    if resp.status_code == 404:
        return {"not_found": True, "http_status": 404}
    if resp.status_code != 200:
        print(f"[municibid] RECORDER ERROR: poll() unexpected HTTP {resp.status_code} for lot {lot_id} on {resp.url}")
        return None
    text = _rsc_text(resp.text)
    m = _BIDBOX_RE.search(text)
    if m is None:
        if any(marker in text for marker in _NOT_FOUND_MARKERS):
            return {"not_found": True, "http_status": 200}
        print(
            f"[municibid] RECORDER ERROR: poll() unrecognized page shape for lot {lot_id} "
            f"on {resp.url} (no bidbox 'initial' state and no 404 marker)"
        )
        return None
    try:
        initial, _ = _decoder.raw_decode(text, m.end() - 1)
    except ValueError:
        print(f"[municibid] RECORDER ERROR: poll() bidbox 'initial' state is not valid JSON for lot {lot_id} on {resp.url}")
        return None
    if not isinstance(initial, dict):
        print(f"[municibid] RECORDER ERROR: poll() bidbox 'initial' state is not an object for lot {lot_id} on {resp.url}")
        return None
    summary = None
    sm = _SUMMARY_RE.search(text)
    if sm:
        try:
            summary = json.loads(sm.group(0))
        except ValueError:
            summary = None
    site_status = str(initial.get("status") or "")
    ended = bool(summary.get("ended")) if isinstance(summary, dict) else None
    status = "active" if site_status == "Active" and ended is not True else "closed"
    bid_count = initial.get("bidCount")
    return {
        "not_found": False,
        "url": resp.url,
        "listing_id": int(m.group(1)),
        "status": status,
        "initial": initial,
        "summary": summary,
        "current_bid": _parse_money(initial.get("currentPrice")),
        "bid_count": bid_count if isinstance(bid_count, int) else None,
        "end_date": _parse_utc(initial.get("endsAtUtc")),
    }


class MunicibidSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        listings_by_id, any_ok = _sweep(BROWSE_CATEGORIES, completed=False)
        if not any_ok:
            print(
                f"[municibid] RECORDER ERROR: discover() aborted — all {len(BROWSE_CATEGORIES)} "
                "category fetches failed, 0 observations"
            )
            raise SourceFetchFailed(
                f"discover() aborted — all {len(BROWSE_CATEGORIES)} category fetches failed",
                url=BROWSE_URL)
        out = [_to_observation(it, status="active") for it in listings_by_id.values()]
        result = [o for o in out if o is not None]
        if not result:
            print(
                f"[municibid] WARNING: discover() found 0 active furniture listings across "
                f"{len(BROWSE_CATEGORIES)} category sweeps — check BROWSE_CATEGORIES / page shape for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        observations: list[Observation] = []
        budget = PollBudget()  # ten failures in a row ends the batch (source health)
        for lot in lots:
            if budget.exhausted:
                print(f"[municibid] RECORDER ERROR: poll() batch aborted after "
                      f"{budget.consecutive} consecutive failures — "
                      f"{len(lots) - budget.attempted} lot(s) left for the next run")
                break
            lot_id = str(lot["source_lot_id"])
            detail = _fetch_detail(lot_id)
            budget.record(detail is not None)
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
                status=detail["status"],
                raw={"detail_page": {
                    "url": detail["url"],
                    "listing_id": detail["listing_id"],
                    # the untouched source payloads — never re-derived values —
                    # so a future parser fix can recompute every field from raw.
                    "initial": detail["initial"],
                    "summary": detail["summary"],
                }},
                current_bid=detail["current_bid"],
                bid_count=detail["bid_count"],
                end_date=detail["end_date"],
            ))
        self.last_poll_stats = budget.stats(len(lots))
        return observations

    def sold_sweep(self) -> list[Observation]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SOLD_SWEEP_LOOKBACK_DAYS)
        listings_by_id, any_ok = _sweep(SOLD_SWEEP_CATEGORIES, completed=True, older_than=cutoff)
        if not any_ok:
            print(
                f"[municibid] RECORDER ERROR: sold_sweep() aborted — all {len(SOLD_SWEEP_CATEGORIES)} "
                "category fetches failed, 0 observations"
            )
            return []
        out = [_to_observation(it, status="closed") for it in listings_by_id.values()]
        return [o for o in out if o is not None]

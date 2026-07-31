"""Municibid source adapter — server-rendered search-results HTML with an
embedded JSON marker (the full result set) plus per-card HTML (bid counts),
and a server-rendered per-lot detail page for poll().

LIVE RECON (verified 2026-07-31, this task):

- The brief's hinted URL shape is real but it's HTML, not a JSON endpoint:
  `GET https://municibid.com/Search/Results?FullTextQuery=<term>&StatusFilter=<filter>`
  (`StatusFilter=active_only` for live listings, `StatusFilter=completed_only`
  for sold ones — confirmed live, both 200, no auth). Municibid sits behind
  Cloudflare (`server: cloudflare` on every response) but every GET in this
  recon — search pages, detail pages, a nonexistent-listing detail page, a
  bogus-id detail page — came back plain HTTP 200/301/404 with real content;
  no Cloudflare challenge was ever served to a plain `requests` call. Per the
  brief, this adapter does NOT escalate to browser automation if that ever
  changes — see `_fetch_search_page`'s 403/429 handling, which backs off and
  reports loudly instead.
- The search-results HTML embeds a `<script type="application/json"
  id="srp-markers-data">` tag containing the COMPLETE result set (verified:
  `FullTextQuery=chairs&StatusFilter=completed_only` returned 70 JSON items
  regardless of the `page` query param — `page` only changes which 40-item
  slice of *cards* render in the visible HTML grid, not the JSON marker).
  Fields observed: `id` (int, Municibid's own listing id — globally unique,
  used as `source_lot_id`), `title`, `lat`, `lng`, `price` (float — current
  bid for `active_only`, confirmed-equal to the detail page's "Final Bid" for
  `completed_only`, e.g. id 82394410 priced 21.0 in both places), `end`
  (string, see "Dates" below), `img`, `seller`, `city`, `state`. No bid-count
  field.
- Bid counts are NOT in the JSON marker. They're only in the visible
  `<a class="srp-card" href="/Listing/Details/{id}">...<span
  class="srp-card-bids" title="Bids">{n}</span>` card markup, which IS on the
  same response as the JSON marker but is **paginated at 40 cards/page**
  (`page` query param, confirmed 0-indexed: UI "page 2" link is
  `&page=1`). `_fetch_search_page()` parses page-0's cards with
  `bs4` (already a project dependency — `deals/ebay_parse.py` — just not
  previously installed in this worktree's venv; installed here) into an
  `{id: bid_count}` map and merges it onto the JSON items by id. **Known
  limitation**: for a query whose result set exceeds 40 items, only the
  first 40 get a real `bid_count` — the rest carry `bid_count=None`. No
  pagination loop implemented (would multiply request volume for a
  Phase-0 "crude, correct" adapter); documented here and in the report.
  Verified live: `FullTextQuery=chairs&StatusFilter=active_only` → 34 JSON
  items, 34 cards, 34/34 matched (fits on one page). The same query with
  `StatusFilter=completed_only` → 70 JSON items, 40 cards on page 0, so only
  40/70 get a real bid_count.
- `discover()`/`sold_sweep()` sweep `FURNITURE_TERMS` (brief's instruction),
  issuing one `_fetch_search_page(term, status_filter)` call per term and
  merging/deduping by id across terms (a lot matching two terms — e.g.
  "chairs" and "banquet" — is fetched twice but only stored once). This
  means a **single term's fetch failing does not abort the whole sweep** —
  unlike GSA/Purple Wave's single-request `_fetch_*() -> list[dict] | None`
  model, Municibid's sweep is multi-request by construction (one request per
  FURNITURE_TERMS entry), so `discover()`/`sold_sweep()` only report a total
  failure (0 observations, loud `RECORDER ERROR`) if EVERY term's fetch
  failed; a partial failure (some terms ok, some blocked/erroring) still
  returns what the healthy terms found, with a loud error printed per failed
  term. This is a deliberate, documented divergence from the other two
  adapters' all-or-nothing single-fetch semantics.
- Dates: the JSON marker's `end` field (e.g. `"2026-08-04T12:27:00"`) has
  **no timezone suffix but is already UTC** — verified live by cross-checking
  the SAME listing's `end` value against its own detail page's
  `data-action-time="08/04/2026 08:27:00"` attribute (labeled "This Auction
  Ends in:"), which IS genuine US/Eastern local time: interpreting
  `data-action-time` as `America/New_York` and converting to UTC
  (`zoneinfo`) landed EXACTLY on the JSON's `end` value
  (`2026-08-04 12:27:00+00:00` both ways — confirmed with live server `Date:`
  headers as ground truth, ET was UTC-4 / EDT at capture time). So
  `_parse_search_end()` just attaches `timezone.utc` directly — no zoneinfo
  conversion. The site's own `window.timeZoneLabel = 'ET'` JS var is the
  human-display timezone, used only by the detail-page countdown UI.
- The detail page (`GET /Listing/Details/{id}`, 301-redirects to a slug URL
  which `polite_get`/`requests` follows transparently) is used by `poll()`.
  Verified three real shapes:
  - **Not found**: id `999999999` (never issued) → HTTP 200, `<title>...
    - Listing not found</title>`. No literal HTTP 404 was observed for any
    listing id in this recon (defensive 404 handling is included in
    `_fetch_detail` anyway, but is unverified against a real response).
  - **Active**: heading `This Auction Ends in:` + `data-action-time="MM/DD/
    YYYY HH:MM:SS"` (ET, see above) + `<h3>Current Highest Bid:</h3>...
    <span class="NumberPart">25.00</span>` + a `Bids:` badge
    `<span class="awe-rt-AcceptedListingActionCount ...*"
    data-previous-value="1">1</span>`.
  - **Closed/ended**: heading `Auction Ended:` (no `data-action-time`
    anywhere on the page) + `<h1>Final Bid:</h1><h2> $<span
    class="NumberPart">21.00</span></h2>` + the same
    `awe-rt-AcceptedListingActionCount` bids badge (`data-previous-value=
    "8"`).
  - Both active and closed pages ALSO carry a uniform, more reliable end-date
    anchor used instead of the two heading-specific attributes above:
    `<td><strong>End Date</strong></td><td><span class="awe-rt-endingDTTM"
    data-initial-dttm="MM/DD/YYYY HH:MM:SS"></span>` (ET local, same
    conversion as `data-action-time`) — present and identical-valued on both
    page shapes, verified live on both fixtures. `poll()` uses this one
    anchor for `end_date` regardless of status, then separately checks for
    the `This Auction Ends in:` / `Auction Ended:` headings to pick which
    price regex (`Current Highest Bid:` vs `Final Bid:`) applies — this is
    the one place `poll()` genuinely re-derives status from the page instead
    of assuming, per the shared contract.
- Money: `NumberPart` values are plain decimal strings like `"25.00"` or
  `"1,200.00"` (comma thousands separators observed on higher-value lots
  elsewhere in this recon) — `_parse_money` strips commas before
  `Decimal(...)`.

poll() gone semantics: per-lot, not per-batch (see "discover()/sold_sweep()"
above for why Municibid's request shape differs from GSA/Purple Wave).
`_fetch_detail(lot_id)` returns `None` only for an outright fetch failure
(network exception, 403/429 block, non-200/non-404, or an unrecognized page
shape — neither "This Auction Ends in:" nor "Auction Ended:" present and the
title isn't "Listing not found") for THAT lot; `poll()` skips that lot with
no observation and a loud `RECORDER ERROR`, and continues to the next lot —
one bad lot never blocks the others, and a transient failure never produces
a false 'gone' (append-only — that mistake would be permanent). A `not_found`
result only becomes a `status='gone'` observation if the lot's own
`end_date` (from the caller-supplied `tracked_active()` row) has already
passed; otherwise (not yet ended, or end_date unknown) nothing is emitted
this round, matching the shared contract and GSA/Purple Wave's rule for
absence.

Fixtures captured live 2026-07-31 under `tests/recorder/fixtures/municibid/`
(trimmed HTML — real `srp-markers-data` JSON + real `srp-card` markup for the
search pages; real anchor blocks for the detail pages — unrelated chrome
removed, see per-file comments): `discover_active_chairs.html` (34 items/34
cards, `FullTextQuery=chairs&StatusFilter=active_only`), `sold_sweep_chairs.
html` (70 items/40 cards, `StatusFilter=completed_only` — the pagination gap
case), `detail_active_84516049.html`, `detail_closed_82394410.html`,
`detail_not_found.html`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, polite_get

SOURCE = "municibid"

SEARCH_URL = "https://municibid.com/Search/Results"
DETAIL_URL_TMPL = "https://municibid.com/Listing/Details/{id}"

# Michigan/municibid.com listing display timezone — used ONLY for detail-page
# attributes (`data-action-time`, `awe-rt-endingDTTM`'s `data-initial-dttm`).
# The search JSON's `end` field is already UTC — see module docstring.
ET_ZONE = ZoneInfo("America/New_York")

MARKERS_RE = re.compile(
    r'<script type="application/json" id="srp-markers-data"[^>]*>(.*?)</script>', re.S
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
END_ROW_RE = re.compile(r'class="awe-rt-endingDTTM"\s+data-initial-dttm="([^"]+)"')
BID_COUNT_RE = re.compile(
    r'class="[^"]*awe-rt-AcceptedListingActionCount[^"]*"[^>]*data-previous-value="(\d+)"'
)
ACTIVE_PRICE_RE = re.compile(
    r'Current Highest Bid:</h3>[\s\S]{0,300}?<span class="NumberPart">([\d,]+\.\d+)</span>'
)
CLOSED_PRICE_RE = re.compile(
    r'Final Bid:</h1>[\s\S]{0,300}?<span class="NumberPart">([\d,]+\.\d+)</span>'
)


def _parse_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_search_end(raw: Any) -> datetime | None:
    """Parse the srp-markers-data JSON's `end` field. Already UTC — no
    timezone conversion. See module docstring's "Dates" section."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _parse_detail_end(raw: str | None) -> datetime | None:
    """Parse a detail-page `data-initial-dttm`/`data-action-time` value
    (`MM/DD/YYYY HH:MM:SS`, US/Eastern local) to tz-aware UTC."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=ET_ZONE).astimezone(timezone.utc)


def _bid_counts_from_cards(html: str) -> dict[int, int]:
    """Best-effort {listing id: bid count} from the visible srp-card grid.
    Only covers whichever page of cards is in `html` (default page 0, 40
    items) — see module docstring's pagination-gap note."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, int] = {}
    for card in soup.select("a.srp-card"):
        href = card.get("href") or ""
        tail = href.rstrip("/").split("/")[-1]
        if not tail.isdigit():
            continue
        bids_el = card.select_one(".srp-card-bids")
        if bids_el is None:
            continue
        try:
            out[int(tail)] = int(bids_el.get_text(strip=True))
        except ValueError:
            continue
    return out


def _to_observation(item: dict, *, status: str, bid_count: int | None) -> Observation | None:
    lot_id = item.get("id")
    if lot_id is None:
        print(f"[municibid] skipping item with no 'id': {item.get('title')!r}")
        return None
    return Observation(
        source=SOURCE,
        source_lot_id=str(lot_id),
        status=status,
        raw=item,
        current_bid=_parse_money(item.get("price")),
        bid_count=bid_count,
        end_date=_parse_search_end(item.get("end")),
    )


def _fetch_search_page(full_text_query: str, status_filter: str) -> tuple[list[dict], dict[int, int]] | None:
    """One page-0 fetch of `Search/Results?FullTextQuery=...&StatusFilter=...`.
    Returns `(json_items, bid_count_by_id)` or `None` on ANY fetch failure
    (network exception, blocked, non-200, missing/bad JSON marker) — never an
    empty tuple, so callers can tell "fetch failed" apart from "fetch
    succeeded and there's genuinely nothing there."
    """
    try:
        resp = polite_get(
            SEARCH_URL, params={"FullTextQuery": full_text_query, "StatusFilter": status_filter}
        )
    except requests.exceptions.RequestException as e:
        print(f"[municibid] RECORDER ERROR: request failed ({full_text_query!r}, {status_filter}): {e}")
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
    m = MARKERS_RE.search(html)
    if m is None:
        print(f"[municibid] RECORDER ERROR: srp-markers-data script not found in response from {resp.url}")
        return None
    try:
        items = json.loads(m.group(1))
    except ValueError:
        print(f"[municibid] RECORDER ERROR: srp-markers-data is not valid JSON from {resp.url}")
        return None
    if not isinstance(items, list):
        print(f"[municibid] RECORDER ERROR: srp-markers-data is not a list from {resp.url}")
        return None
    return items, _bid_counts_from_cards(html)


def _sweep(status_filter: str) -> tuple[dict[int, dict], dict[int, int], bool]:
    """Sweep FURNITURE_TERMS at the given StatusFilter, merging/deduping by
    id across terms. Returns (items_by_id, bid_counts_by_id, any_term_ok)."""
    items_by_id: dict[int, dict] = {}
    bid_counts: dict[int, int] = {}
    any_ok = False
    for term in FURNITURE_TERMS:
        result = _fetch_search_page(term, status_filter)
        if result is None:
            continue
        any_ok = True
        term_items, term_bids = result
        for it in term_items:
            lid = it.get("id")
            if lid is not None:
                items_by_id[lid] = it
        bid_counts.update(term_bids)
    return items_by_id, bid_counts, any_ok


def _fetch_detail(lot_id: str) -> dict | None:
    """Fetch and parse one lot's detail page. Returns:
    - `None` on fetch failure (network exception, blocked, non-200/404,
      unrecognized page shape) — caller must emit no observation.
    - `{"not_found": True, "http_status": int}` if the lot is confirmed
      absent (200 + "Listing not found" title, or a real 404).
    - `{"not_found": False, "url", "status", "current_bid", "bid_count",
      "end_date", ...}` for a found lot, status re-derived from the page.
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
    html = resp.text
    title_m = TITLE_RE.search(html)
    title = title_m.group(1) if title_m else ""
    if "listing not found" in title.lower():
        return {"not_found": True, "http_status": 200}
    is_active = "This Auction Ends in:" in html
    is_closed = "Auction Ended:" in html
    if not is_active and not is_closed:
        print(
            f"[municibid] RECORDER ERROR: poll() unrecognized page shape for lot {lot_id} "
            f"on {resp.url} (neither active nor ended markers found)"
        )
        return None
    end_m = END_ROW_RE.search(html)
    end_date = _parse_detail_end(end_m.group(1) if end_m else None)
    bid_count = None
    bc_m = BID_COUNT_RE.search(html)
    if bc_m:
        bid_count = int(bc_m.group(1))
    if is_active:
        status = "active"
        price_m = ACTIVE_PRICE_RE.search(html)
    else:
        status = "closed"
        price_m = CLOSED_PRICE_RE.search(html)
    return {
        "not_found": False,
        "url": resp.url,
        "status": status,
        "current_bid": _parse_money(price_m.group(1)) if price_m else None,
        "bid_count": bid_count,
        "end_date": end_date,
    }


class MunicibidSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        items_by_id, bid_counts, any_ok = _sweep("active_only")
        if not any_ok:
            print(
                f"[municibid] RECORDER ERROR: discover() aborted — all {len(FURNITURE_TERMS)} "
                "FURNITURE_TERMS fetches failed, 0 observations"
            )
            return []
        out = [
            _to_observation(it, status="active", bid_count=bid_counts.get(lid))
            for lid, it in items_by_id.items()
        ]
        result = [o for o in out if o is not None]
        if not result:
            print(
                f"[municibid] WARNING: discover() found 0 active furniture listings across "
                f"{len(FURNITURE_TERMS)} FURNITURE_TERMS queries — check terms / StatusFilter for drift"
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
                status=detail["status"],
                raw={"detail_page": {
                    "url": detail["url"],
                    "status": detail["status"],
                    "current_bid": str(detail["current_bid"]) if detail["current_bid"] is not None else None,
                    "bid_count": detail["bid_count"],
                }},
                current_bid=detail["current_bid"],
                bid_count=detail["bid_count"],
                end_date=detail["end_date"],
            ))
        return observations

    def sold_sweep(self) -> list[Observation]:
        items_by_id, bid_counts, any_ok = _sweep("completed_only")
        if not any_ok:
            print(
                f"[municibid] RECORDER ERROR: sold_sweep() aborted — all {len(FURNITURE_TERMS)} "
                "FURNITURE_TERMS fetches failed, 0 observations"
            )
            return []
        out = [
            _to_observation(it, status="closed", bid_count=bid_counts.get(lid))
            for lid, it in items_by_id.items()
        ]
        return [o for o in out if o is not None]

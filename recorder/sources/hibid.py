"""HiBid (`hibid.com`) source adapter — the largest US auction aggregator.
Angular PWA whose own frontend talks to a public, unauthenticated GraphQL
endpoint; no HTML scraping anywhere in this adapter.

LIVE RECON (verified 2026-10-16):

- Endpoint: `POST https://hibid.com/graphql` (JSON). No auth, no cookies, no
  persisted-query requirement — the exact queries below were replayed with
  plain `requests` and answered. Cloudflare fronts the host with two WAF
  rules worth knowing: introspection (`__schema`) answers HTTP 403, and a
  bare `Mozilla/5.0` User-Agent answers HTTP 403 ("Attention Required")
  while the full desktop-Chrome UA that `polite_get`/`polite_post` always
  send passes. `robots.txt` disallows only account/webcast/livecatalog/print
  paths — not `/graphql`, not `/lots`.
- Queries (shapes copied from the PWA bundle `cdn.hibid.com/cdn/pwa/…/main.js`,
  trimmed to the fields we store):
    `lotSearch(input: {searchText, status, countryName, isArchive,
        countAsView: false}, pageNumber, pageLength, sortDirection)`
      → `pagedResults { totalCount, filteredCount, results: [Lot] }`.
      `status: OPEN` + `sortOrder: TIME_LEFT` + `sortDirection: ASC` =
      live lots, soonest close first (discover); `isArchive: true` with the
      default sort `DESC` = closed lots, newest first (sold_sweep; the
      archive answers at most 1000 rows per query, fine for a sweep).
      NOTE: `status: OPEN` still returns the odd `POSTED` (not yet open)
      row — mapped to `active` like any other un-closed state.
    `lot(input: $lotId, countAsView: false)` → `{accessability, lot}` (poll).
      An unknown/purged id answers HTTP 200 with
      `accessability: "EVENTITEM_NOT_FOUND"` and `lot: null` → `gone` once
      past `end_date`. A GraphQL `errors` body (always HTTP 200/400) is a
      fetch failure, never an observation.
- `countAsView: false` on everything — we are a recorder, not a page view.
- Time: **every datetime HiBid serves is naive local to the auctioneer**
  (`bidCloseDateTime: "2026-10-25T18:00:00"`, no offset, timezone unknown
  per auction), and lot closes are staggered inside an auction, so that
  field is NEVER trusted for `end_date`. The authoritative per-lot clock is
  `lotState.timeLeftSeconds` (float seconds from the server's now):
  `end_date = now_utc + timeLeftSeconds`, rounded to the minute so the
  change gate (store.is_changed) doesn't see sub-second jitter as a new end
  time. `timeLeftSeconds <= 0` (closed, webcast-only, or a POSTED lot that
  counts to its open) → `end_date = None`.
- Money: `lotState.highBid` while live; at close `lotState.priceRealized`
  is the final (0 for a no-sale) — `current_bid` prefers `priceRealized`
  when the lot is closed and it is > 0, else `highBid`. `bidCount` is real
  on live lots and on `lot()` details, but the ARCHIVE search reports 0 for
  nearly every row (HiBid hides historical bid counts), so archive-sourced
  rows carry it verbatim and `sold_comps`' `bid_count > 0` gate will skip
  them — the same known gap `public_surplus` has (see recorder/README.md,
  "Coverage metric"). Currency comes per auction
  (`auction.currencyAbbreviation`); `countryName: "United States"` keeps
  the sweep inside the US (HiBid also lists CA/international lots).
- Scope: one `lotSearch` per `FURNITURE_TERMS` term (the server full-text
  matches title + description), soonest-closing first, up to
  `RECORDER_HIBID_MAX_PAGES_PER_TERM` (default 1) pages of 100 — HiBid is
  huge (a single term matches 1,000+ live lots), so the cap deliberately
  tracks the ~100 soonest closes per term and picks up later closes on the
  next sweeps as they approach. A `FURNITURE_TERMS` re-check on
  title + description + category then drops the full-text strays
  ("collapsible wagon" matching "folding chairs").
- IDs: `source_lot_id` = the numeric lot `id` as a string (`"325447181"`).
  Lot page: `https://hibid.com/lot/{id}` (the slug suffix is optional).

raw keys (stable, read by the web layer): `title`, `city`, `state`,
`category`, `image_url`, `url`, `currency`, plus the untouched GraphQL lot
record under `hibid`.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_post

SOURCE = "hibid"

BASE_URL = "https://hibid.com"
GRAPHQL_URL = f"{BASE_URL}/graphql"
HEADERS = {"Content-Type": "application/json", "Origin": BASE_URL, "Referer": f"{BASE_URL}/"}
COUNTRY = "United States"
PAGE_SIZE = 100
SOLD_SWEEP_PAGES = 1               # per term; archive default sort = newest close first

_TAG_RE = re.compile(r"<[^>]+>")

# Lot fields shared by the search page and the detail query. `category` is a
# list (base + parents); `lotState` carries the clock, the money and the flags.
_LOT_FIELDS = """
      id
      itemId
      lead
      lotNumber
      quantity
      description
      featuredPicture { thumbnailLocation fullSizeLocation }
      category { categoryName fullCategory }
      lotState {
        bidCount
        highBid
        minBid
        isClosed
        isArchived
        status
        timeLeftSeconds
        priceRealized
        quantitySold
        productStatus
        biddingExtended
      }
      auction {
        id
        eventName
        eventCity
        eventState
        eventZip
        bidCloseDateTime
        eventDateEnd
        currencyAbbreviation
        auctioneer { name }
      }
      site { domain subdomain }
"""

LOT_SEARCH_QUERY = """
query LotSearch($pageNumber: Int!, $pageLength: Int!, $searchText: String = null, $status: AuctionLotStatus = null, $countryName: String = null, $sortOrder: EventItemSortOrder = null, $sortDirection: SortDirection = DESC, $isArchive: Boolean = false) {
  lotSearch(
    input: {searchText: $searchText, status: $status, countryName: $countryName, isArchive: $isArchive, sortOrder: $sortOrder, countAsView: false}
    pageNumber: $pageNumber
    pageLength: $pageLength
    sortDirection: $sortDirection
  ) {
    pagedResults {
      pageLength
      pageNumber
      totalCount
      filteredCount
      results {%s
      }
    }
  }
}
""" % _LOT_FIELDS

LOT_DETAILS_QUERY = """
query GetLotDetails($lotId: ID!) {
  lot(input: $lotId, countAsView: false) {
    accessability
    lot {%s
    }
  }
}
""" % _LOT_FIELDS


def max_pages_per_term() -> int:
    try:
        return max(1, int(os.getenv("RECORDER_HIBID_MAX_PAGES_PER_TERM") or 1))
    except ValueError:
        return 1


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _post(payload: dict) -> dict | None:
    """The GraphQL `data` dict, or None on any failure (transport, non-200,
    non-JSON, an `errors` body, or a missing `data`)."""
    try:
        resp = polite_post(GRAPHQL_URL, json=payload, headers=HEADERS)
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: request failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"[{SOURCE}] RECORDER ERROR: HTTP {resp.status_code} on {GRAPHQL_URL}")
        return None
    try:
        body = resp.json()
    except ValueError:
        print(f"[{SOURCE}] RECORDER ERROR: non-JSON response from {GRAPHQL_URL}")
        return None
    if not isinstance(body, dict):
        print(f"[{SOURCE}] RECORDER ERROR: unexpected response shape from {GRAPHQL_URL}")
        return None
    if body.get("errors"):
        first = body["errors"][0] if isinstance(body["errors"], list) and body["errors"] else {}
        print(f"[{SOURCE}] RECORDER ERROR: GraphQL error: "
              f"{first.get('message') if isinstance(first, dict) else first}")
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        print(f"[{SOURCE}] RECORDER ERROR: no data in response from {GRAPHQL_URL}")
        return None
    return data


def search_variables(term: str, page: int, *, archive: bool) -> dict:
    """Live sweep: OPEN lots, soonest close first. Archive sweep: closed lots,
    newest close first (the archive's default DESC order)."""
    base = {"pageNumber": page, "pageLength": PAGE_SIZE, "searchText": term,
            "countryName": COUNTRY, "isArchive": archive}
    if archive:
        return {**base, "status": None, "sortOrder": None, "sortDirection": "DESC"}
    return {**base, "status": "OPEN", "sortOrder": "TIME_LEFT", "sortDirection": "ASC"}


def _search_page(term: str, page: int, *, archive: bool) -> tuple[list[dict], int] | None:
    """(result lots, filteredCount) or None on any fetch failure."""
    data = _post({"operationName": "LotSearch", "query": LOT_SEARCH_QUERY,
                  "variables": search_variables(term, page, archive=archive)})
    if data is None:
        return None
    paged = ((data.get("lotSearch") or {}).get("pagedResults"))
    if not isinstance(paged, dict) or not isinstance(paged.get("results"), list):
        print(f"[{SOURCE}] RECORDER ERROR: search {term!r} p{page} — unexpected pagedResults shape")
        return None
    results = [r for r in paged["results"] if isinstance(r, dict)]
    return results, _int(paged.get("filteredCount")) or 0


def _is_furniture(lot: dict) -> bool:
    cats = " ".join((c.get("fullCategory") or "") for c in (lot.get("category") or [])
                    if isinstance(c, dict))
    text = (f"{lot.get('lead') or ''} {_TAG_RE.sub(' ', lot.get('description') or '')} "
            f"{cats}").lower()
    return any(term in text for term in FURNITURE_TERMS)


def _is_closed(lot: dict) -> bool:
    state = lot.get("lotState") or {}
    return bool(state.get("isClosed")) or str(state.get("status") or "").upper() == "CLOSED"


def _end_date(lot: dict, *, now: datetime | None = None) -> datetime | None:
    """now_utc + timeLeftSeconds, minute-rounded (see module docstring). None
    for a closed lot or a clock the site isn't running (<= 0)."""
    if _is_closed(lot):
        return None
    state = lot.get("lotState") or {}
    try:
        seconds = float(state.get("timeLeftSeconds"))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    end = (now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)
    return (end + timedelta(seconds=30)).replace(second=0, microsecond=0)


def _to_observation(lot: dict, *, status: str | None = None,
                    now: datetime | None = None) -> Observation:
    state = lot.get("lotState") or {}
    auction = lot.get("auction") or {}
    closed = _is_closed(lot) or status == "closed"
    current_bid = _money(state.get("highBid"))
    if closed:
        realized = _money(state.get("priceRealized"))
        if realized is not None and realized > 0:
            current_bid = realized
    categories = [c for c in (lot.get("category") or []) if isinstance(c, dict)]
    picture = lot.get("featuredPicture") or {}
    raw = {
        "title": lot.get("lead"),
        "city": auction.get("eventCity"),
        "state": auction.get("eventState"),
        "category": (categories[0].get("fullCategory") or categories[0].get("categoryName"))
        if categories else None,
        "image_url": picture.get("fullSizeLocation") or picture.get("thumbnailLocation"),
        "url": f"{BASE_URL}/lot/{lot.get('id')}",
        "currency": auction.get("currencyAbbreviation") or "USD",
        "hibid": lot,
    }
    end = _end_date(lot, now=now)
    return Observation(
        source=SOURCE,
        source_lot_id=str(lot["id"]),
        status=status or ("closed" if closed else "active"),
        raw=raw,
        current_bid=current_bid,
        bid_count=_int(state.get("bidCount")),
        end_date=end,
    )


class HiBidSource:
    SOURCE = SOURCE

    def _sweep(self, *, archive: bool, max_pages: int) -> tuple[dict[str, dict], bool]:
        """Dedup'd lots by id across every FURNITURE_TERMS query; any_ok tells
        whether at least one query fetched."""
        found: dict[str, dict] = {}
        any_ok = False
        for term in FURNITURE_TERMS:
            page = 1
            while page <= max_pages:
                got = _search_page(term, page, archive=archive)
                if got is None:
                    break
                any_ok = True
                results, filtered = got
                for lot in results:
                    if lot.get("id") is None:
                        continue
                    found.setdefault(str(lot["id"]), lot)
                if not results or page * PAGE_SIZE >= filtered:
                    break
                page += 1
        return found, any_ok

    def discover(self) -> list[Observation]:
        found, any_ok = self._sweep(archive=False, max_pages=max_pages_per_term())
        if not any_ok:
            raise SourceFetchFailed(
                f"discover() aborted — all {len(FURNITURE_TERMS)} FURNITURE_TERMS searches failed",
                url=GRAPHQL_URL)
        now = datetime.now(timezone.utc)
        out = [_to_observation(lot, now=now)
               for lot in found.values()
               if _is_furniture(lot) and not _is_closed(lot)]
        if not out:
            print(f"[{SOURCE}] WARNING: discover() matched 0 furniture lots "
                  f"(search returned {len(found)} candidates) — check FURNITURE_TERMS for drift")
        return out

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        budget = PollBudget()
        observations: list[Observation] = []
        for tracked in lots:
            if budget.exhausted:
                print(f"[{SOURCE}] RECORDER ERROR: poll() aborted after "
                      f"{budget.consecutive} consecutive failures — {budget.stats(len(lots))}")
                break
            lot_id = str(tracked["source_lot_id"])
            data = _post({"operationName": "GetLotDetails", "query": LOT_DETAILS_QUERY,
                          "variables": {"lotId": lot_id}})
            if data is None:
                budget.record(False)
                continue
            wrapper = data.get("lot") or {}
            lot = wrapper.get("lot")
            if isinstance(lot, dict) and lot.get("id") is not None:
                budget.record(True)
                observations.append(_to_observation(lot, now=now))
                continue
            access = str(wrapper.get("accessability") or "")
            if access == "EVENTITEM_NOT_FOUND":
                budget.record(True)
                end_date = tracked.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE, source_lot_id=lot_id, status="gone",
                        raw={"recorder_probe": {"result": "not_found", "http_status": 200,
                                                "url": f"{BASE_URL}/lot/{lot_id}"}},
                    ))
                continue
            # lot: null with any OTHER accessability (restricted, hidden, …)
            # is ambiguous — no observation, retried next run.
            print(f"[{SOURCE}] RECORDER ERROR: lot {lot_id} — no lot in response "
                  f"(accessability={access or '?'})")
            budget.record(False)
        return observations

    def sold_sweep(self) -> list[Observation]:
        """Archive search, newest close first, one page per term. Final price
        = `priceRealized`; `end_date` stays None (HiBid's archive serves only
        naive auctioneer-local clocks — see module docstring); the archive's
        bid counts are unreliable and recorded verbatim."""
        found, any_ok = self._sweep(archive=True, max_pages=SOLD_SWEEP_PAGES)
        if not any_ok:
            print(f"[{SOURCE}] RECORDER ERROR: sold_sweep() — every search failed, 0 observations")
            return []
        return [_to_observation(lot, status="closed")
                for lot in found.values() if _is_furniture(lot)]

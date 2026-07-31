"""MiBid (Michigan) source adapter — the entire active+closed auction catalog
is embedded as a literal JS array on the homepage; per-lot bid data comes
from a small per-guid JSON endpoint; per-lot status/end-date re-derivation
for poll() comes from a per-guid detail page.

LIVE RECON (verified 2026-07-31, this task):

- `GET https://mibid.michigan.gov/` is a Knockout.js single-page app whose
  auction list is NOT fetched via a REST/JSON endpoint at all — it's a
  literal JS array assigned in an inline `<script>` block:
  `this.rawAuctions = ko.observableArray([{...}, {...}, ...]);` inside the
  `AuctionListViewModel` constructor. Verified live: 2,253 auctions in one
  response, no auth, no pagination, no anti-bot challenge. `_fetch_raw_
  auctions()` locates the `this.rawAuctions = ko.observableArray(` marker
  and feeds the remainder to `json.JSONDecoder().raw_decode()` (NOT manual
  bracket-counting — several real titles contain literal `(`/`)` and could
  plausibly contain `[`/`]`, and `raw_decode` correctly respects JSON string
  quoting where a naive bracket scan would not).
- Per-item fields observed: `id` (int), `title`, `primaryImage`,
  `currentBid` (float), `startTime`/`endTime` (naive datetime strings, see
  "Dates"), `numberOfBids` (int — **UNRELIABLE, see below**), `guid`
  (the auction's real unique id — used as `source_lot_id`, see "Why guid not
  id"), `categoryId`, `location`, `status` (int enum, see "Status codes"),
  `isPrivate`, `isCanceled`, `isForTesting`, `hasActiveBids`/`hasWonAuction`/
  `hasLostAuction` (per-logged-in-user flags, irrelevant here), `viewCount`.
- **`numberOfBids` in the bulk embed is always 0** — checked all 2,253 live
  items, every single one reads `numberOfBids: 0`, including closed lots
  with a nonzero `currentBid`. This is a stale/placeholder field in the
  initial server render; live values only arrive later via a SignalR
  WebSocket push (`AuctionUpdated(guid, totalBids, currentBid, endDateTime)`,
  see `/js/AuctionListSocket.js` — NOT used here, WebSocket polling is out of
  scope for a Phase-0 HTTP-poll recorder). The real bid count (and a
  cross-checked-identical `currentBid`) comes from
  `GET /AuctionBid/GetBasicInfo?guid=<guid>` — confirmed live for 6 different
  closed lots: `currentBid` from the bulk embed matched `GetBasicInfo`
  exactly every time, while `numberOfBids` went from the embed's `0` to real
  values (1, 8, 16, 33, ...). `_to_observation(..., enrich=True)` always
  calls `GetBasicInfo` per matched item to get a trustworthy `bid_count`;
  `current_bid` is preferentially taken from the same enrichment call too
  (falls back to the bulk embed's `currentBid` if enrichment fails).
- **Why `guid`, not `id`, is `source_lot_id`**: `store.tracked_active()` rows
  only carry back `{source, source_lot_id, observed_at, end_date,
  current_bid, bid_count}` — no room for a second opaque token. Both re-poll
  endpoints (`/AuctionBid/Index/{guid}` and `/AuctionBid/GetBasicInfo?guid=`)
  key on the auction's `guid`, not its numeric `id`, so `source_lot_id` must
  BE the guid for `poll()` to look a tracked lot up again without re-fetching
  and re-scanning the entire 2,253-item homepage every poll.
- **Status codes** (numeric `status` field) — reverse-engineered from the
  page's own filter predicate (`AuctionListViewModel`'s
  `sortedAndFilteredRawAuctions`): `self.filterStatus() == element.status ||
  self.filterStatus() == 1 && element.status != 4`, with default
  `filterStatus() == 1` and unconditional `&& element.status != 5 &&
  element.status != 6` — i.e. the site's own default "Auctions" list is
  everything with `status != 4/5/6`. Cross-checked against real `endTime`
  values (today is 2026-07-31 at capture time): distribution was
  `{4: 2181, 1: 37, 6: 16, 2: 12, 3: 7}`. All `status:4` items had `endTime`
  in the past (as far back as 2025) → **4 = closed/ended**. All `status:1/2/3`
  items had `endTime` in the near future → **1, 2, 3 = active** (three
  sub-states of "currently open for bidding" the UI doesn't otherwise
  distinguish; treated uniformly as `active` here). `status:6` items had
  `endTime` further out AND — confirmed by directly opening a `status:6`
  item's own detail page, which rendered completely normally — represent
  real, viewable, **not-yet-started** auctions the homepage's default view
  simply hides (their `startTime` was also in the future). `status:5` was
  never observed live (0 of 2,253). Neither 5 nor 6 maps cleanly onto our
  `{active, closed, gone}` enum — `_status_from_code()` returns `None` for
  both, and `discover()`/`sold_sweep()` filter on `_status_from_code(...) ==
  "active"`/`"closed"` (fix round 1, 2026-07-31 — wires the previously-
  unused helper in as the single source of truth for the code→enum mapping,
  replacing two duplicated inline membership checks), which excludes 5/6 the
  same way the site's own default view does. If `poll()` ever encountered
  one on a tracked lot it wouldn't come from `status` at all (poll() derives
  status purely from `end_date` vs now — see "poll()" below).
- **`FURNITURE_TERMS` matches, live**: 36 titles matched (exact
  case-insensitive substring match against the shared `FURNITURE_TERMS`
  list), **ALL of them `status:4` (closed)** — zero currently-active
  furniture/seating lots on MiBid at recon time. `discover()` is therefore
  legitimately empty right now; proven via the offline fixture test instead
  (which exercises the `status in (1,2,3)` branch with a synthetic
  status-override on a real item, since no real active-furniture example
  exists to capture). `sold_sweep()` has full live coverage (36 real items).
- **Dates**: both `endTime`/`startTime` (bulk embed) and `endDateTime`
  (`GetBasicInfo`) are naive `"YYYY-MM-DDTHH:MM:SS"` strings with NO
  timezone marker. Confirmed **US/Eastern local** (not UTC) by reading the
  detail page's own un-executed JS literal, which is unambiguous:
  `this.endDateTime = ko.observable(dayjs('8/3/2026 10:00:00 AM',
  'M/D/YYYY h:mm:ss A').tz('America/Detroit', true).utc());` — the site
  itself parses this exact string as `America/Detroit` local time. All
  three representations (`endTime` in the bulk embed, `endDateTime` from
  `GetBasicInfo`, and this dayjs literal) carried the identical wall-clock
  value for the same auction in this recon, confirming they're the same
  naive-local encoding. `_parse_dt()` uses `zoneinfo.ZoneInfo("America/
  Detroit")` (the exact zone the site's own code names) then converts to
  UTC.
- **poll()** is per-lot (brief: "per-lot page"), matching Municibid's shape
  rather than GSA/Purple Wave's single-big-fetch shape — see their
  docstrings for why that's a deliberate divergence. Two calls per due lot:
  1. `GET /AuctionBid/Index/{guid}` (`_fetch_detail_page`) — for gone-
     detection and `end_date` (via the same dayjs literal shown above,
     regex-extracted with NO JS execution needed since it's a literal, not a
     binding). **Confirmed not-found signals, both live-tested**: a
     genuinely random nonexistent guid → real HTTP 404 with empty body
     (the brief's "page 404 → 'gone'"); the well-formed-but-never-issued
     all-zeros guid → HTTP 200 with `<title>Locked - MiBid</title>` and body
     text "The auction is currently in a draft state, is being edited by an
     admin, or is for testing only." (MiBid's own soft-404 for a
     syntactically valid but non-existent/inaccessible id) — both are
     treated as `not_found` here, matched against the shared "gone requires
     end_date already passed" gate.
  2. `GET /AuctionBid/GetBasicInfo?guid={guid}` (`_fetch_basic_info`) — for
     `current_bid`/`bid_count`, best-effort (a failure here still lets the
     lot's existence/status/end_date observation go through with
     `current_bid=bid_count=None`, since `GetBasicInfo` doesn't reliably
     signal absence — see the 404 test above returning 200 with placeholder-
     looking data for a bad guid; it must never be trusted for gone-
     detection, only for enrichment of an already-confirmed-found lot).
  `status` is derived by comparing the freshly-parsed `end_date` to `now`
  (closed if passed, else active) — MiBid's detail page has no static
  "ended" text without executing its Knockout bindings, but its own
  literal end-time IS reliable, so deriving status from that comparison is
  the correct per-lot re-check (never hardcoded), matching the brief: "a
  post-end poll that still serves the lot with a final price maps to
  status='closed'".

poll()'s `raw` (fix round 1, 2026-07-31 — see task-3 review finding #1):
`{"detail_page": {"url", "end_date_raw"}, "basic_info": info}`.
`end_date_raw` is the LITERAL matched `dayjs('...', 'M/D/YYYY h:mm:ss A')`
source string (e.g. `"8/3/2026 10:00:00 AM"`), not just the already-parsed
`end_date` — previously `detail_page` carried only a URL, dropping this
literal. `basic_info` was already the untouched `GetBasicInfo` JSON response
dict (itself the literal source payload: `currentBid`/`numberOfBids`/
`endDateTime` exactly as the API returned them) — unchanged, already
satisfied the "raw is sacred" guarantee. Together, a future parser fix can
recompute `current_bid`/`bid_count`/`end_date` from `raw` alone, without
re-scraping the lot.

Fixtures captured live 2026-07-31 under `tests/recorder/fixtures/mibid/`:
`discover_sample.html` (hand-curated real items: 8 of the 36 real closed
furniture matches, 3 real non-furniture active controls, 2 real `status:6`
excluded items — see file header comment), `detail_active_8006.html` /
`detail_closed_5342.html` (real dayjs endDateTime literals),
`detail_locked_soft404.html` (real "Locked" soft-404 body), and
`basic_info_active_8006.json` / `basic_info_closed_5342.json` /
`basic_info_by_guid.json` (real `GetBasicInfo` responses, the latter keyed
by guid for all 8 items in `discover_sample.html`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, polite_get

SOURCE = "mibid"

HOME_URL = "https://mibid.michigan.gov/"
DETAIL_URL_TMPL = "https://mibid.michigan.gov/AuctionBid/Index/{guid}"
BASIC_INFO_URL = "https://mibid.michigan.gov/AuctionBid/GetBasicInfo"

RAW_AUCTIONS_MARKER = "this.rawAuctions = ko.observableArray("

# Site's own status codes — see module docstring's "Status codes" section.
ACTIVE_STATUS_CODES = (1, 2, 3)
CLOSED_STATUS_CODE = 4

# The site's own display/parse timezone (verified live via the un-executed
# dayjs(...).tz('America/Detroit', true) literal on detail pages).
DETROIT_ZONE = ZoneInfo("America/Detroit")

TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
ENDDATETIME_RE = re.compile(
    r"this\.endDateTime = ko\.observable\(dayjs\('([^']+)',\s*'M/D/YYYY h:mm:ss A'\)"
    r"\.tz\('America/Detroit', true\)\.utc\(\)\);"
)


def _parse_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(raw: Any) -> datetime | None:
    """Parse a naive `YYYY-MM-DDTHH:MM:SS[.ffffff]` string as US/Eastern
    (America/Detroit) local time and convert to UTC. See module docstring's
    "Dates" section for the live cross-check that established this."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(tzinfo=DETROIT_ZONE).astimezone(timezone.utc)


def _parse_detail_dt(raw: str | None) -> datetime | None:
    """Parse the detail page's `dayjs('M/D/YYYY h:mm:ss A', ...)` literal
    (US/Eastern local) to tz-aware UTC."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=DETROIT_ZONE).astimezone(timezone.utc)


def _is_furniture(title: str) -> bool:
    text = (title or "").lower()
    return any(term in text for term in FURNITURE_TERMS)


def _status_from_code(code: Any) -> str | None:
    """Re-derive from the item's own status code rather than assuming —
    returns None for codes outside our {active, closed} mapping (5, 6, or
    anything unrecognized); caller must not emit those."""
    if code in ACTIVE_STATUS_CODES:
        return "active"
    if code == CLOSED_STATUS_CODE:
        return "closed"
    return None


def _fetch_raw_auctions() -> list[dict] | None:
    """Fetch the homepage and extract the full `rawAuctions` array. Returns
    `None` on ANY fetch failure (network exception, blocked, non-200,
    missing marker, bad JSON, unexpected shape) — never an empty list, so
    callers can tell "fetch failed" apart from "fetch succeeded and there's
    genuinely nothing there."
    """
    try:
        resp = polite_get(HOME_URL)
    except requests.exceptions.RequestException as e:
        print(f"[mibid] RECORDER ERROR: request failed: {e}")
        return None
    if resp.status_code in (403, 429):
        print(f"[mibid] RECORDER ERROR: blocked HTTP {resp.status_code} on {resp.url} — backing off, no data this round")
        return None
    if resp.status_code != 200:
        print(f"[mibid] RECORDER ERROR: unexpected HTTP {resp.status_code} on {resp.url}")
        return None
    html = resp.text
    idx = html.find(RAW_AUCTIONS_MARKER)
    if idx == -1:
        print(f"[mibid] RECORDER ERROR: rawAuctions marker not found in response from {resp.url} — page shape changed?")
        return None
    start = idx + len(RAW_AUCTIONS_MARKER)
    try:
        data, _end = json.JSONDecoder().raw_decode(html, start)
    except ValueError as e:
        print(f"[mibid] RECORDER ERROR: failed to parse rawAuctions JSON from {resp.url}: {e}")
        return None
    if not isinstance(data, list):
        print(f"[mibid] RECORDER ERROR: rawAuctions is not a list from {resp.url}")
        return None
    return data


def _fetch_basic_info(guid: str) -> dict | None:
    """Best-effort live bid enrichment. Returns None on any failure — NEVER
    used for gone-detection (see module docstring: a nonexistent guid does
    not error here, it returns 200 with placeholder-looking data)."""
    try:
        resp = polite_get(BASIC_INFO_URL, params={"guid": guid})
    except requests.exceptions.RequestException as e:
        print(f"[mibid] WARNING: GetBasicInfo request failed for guid {guid}: {e}")
        return None
    if resp.status_code != 200:
        print(f"[mibid] WARNING: GetBasicInfo unexpected HTTP {resp.status_code} for guid {guid}")
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"[mibid] WARNING: GetBasicInfo non-JSON response for guid {guid}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _to_observation(item: dict, *, status: str, enrich: bool = True) -> Observation | None:
    guid = item.get("guid")
    if not guid:
        print(f"[mibid] skipping item with no 'guid': {item.get('title')!r}")
        return None
    current_bid = _parse_money(item.get("currentBid"))
    bid_count = None
    if enrich:
        info = _fetch_basic_info(guid)
        if info is not None:
            if info.get("currentBid") is not None:
                current_bid = _parse_money(info.get("currentBid"))
            bid_count = _int_or_none(info.get("numberOfBids"))
    return Observation(
        source=SOURCE,
        source_lot_id=guid,
        status=status,
        raw=item,
        current_bid=current_bid,
        bid_count=bid_count,
        end_date=_parse_dt(item.get("endTime")),
    )


def _fetch_detail_page(guid: str) -> dict | None:
    """Fetch and parse one lot's detail page. Returns:
    - `None` on fetch failure (network exception, blocked, non-200-non-404,
      or the endDateTime literal couldn't be found/parsed) — caller must
      emit no observation.
    - `{"not_found": True, "http_status": int}` for a confirmed real 404 or
      the "Locked - MiBid" soft-404 (see module docstring).
    - `{"not_found": False, "url": str, "end_date": datetime, "end_date_raw":
      str}` for a found lot. `end_date_raw` is the LITERAL matched
      `dayjs('...', 'M/D/YYYY h:mm:ss A')` string (e.g. `"8/3/2026 10:00:00
      AM"`) — kept alongside the parsed `end_date` so `poll()`'s `raw` can
      carry the actual source text, not just the derived value (see
      `poll()`'s `raw` construction below).
    """
    url = DETAIL_URL_TMPL.format(guid=guid)
    try:
        resp = polite_get(url)
    except requests.exceptions.RequestException as e:
        print(f"[mibid] RECORDER ERROR: poll() request failed for guid {guid}: {e}")
        return None
    if resp.status_code == 404:
        return {"not_found": True, "http_status": 404}
    if resp.status_code in (403, 429):
        print(f"[mibid] RECORDER ERROR: poll() blocked HTTP {resp.status_code} for guid {guid} on {resp.url}")
        return None
    if resp.status_code != 200:
        print(f"[mibid] RECORDER ERROR: poll() unexpected HTTP {resp.status_code} for guid {guid} on {resp.url}")
        return None
    html = resp.text
    title_m = TITLE_RE.search(html)
    title = title_m.group(1) if title_m else ""
    if "locked" in title.lower():
        return {"not_found": True, "http_status": 200}
    end_m = ENDDATETIME_RE.search(html)
    if end_m is None:
        print(f"[mibid] RECORDER ERROR: poll() could not find endDateTime literal for guid {guid} on {resp.url} — unrecognized page shape")
        return None
    end_date_raw = end_m.group(1)
    end_date = _parse_detail_dt(end_date_raw)
    if end_date is None:
        print(f"[mibid] RECORDER ERROR: poll() could not parse endDateTime {end_date_raw!r} for guid {guid}")
        return None
    return {"not_found": False, "url": resp.url, "end_date": end_date, "end_date_raw": end_date_raw}


class MiBidSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        items = _fetch_raw_auctions()
        if items is None:
            print("[mibid] RECORDER ERROR: discover() aborted — fetch failed, 0 observations")
            return []
        matches = [
            it for it in items
            if _status_from_code(it.get("status")) == "active"
            and not it.get("isCanceled")
            and _is_furniture(it.get("title", ""))
        ]
        out = [_to_observation(it, status="active") for it in matches]
        result = [o for o in out if o is not None]
        if not result:
            print(
                f"[mibid] WARNING: discover() found 0 active furniture lots out of "
                f"{len(items)} total auctions — check FURNITURE_TERMS / status filter for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        observations: list[Observation] = []
        for lot in lots:
            guid = str(lot["source_lot_id"])
            detail = _fetch_detail_page(guid)
            if detail is None:
                continue  # fetch failure for this lot — loud error already printed, skip
            if detail["not_found"]:
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE,
                        source_lot_id=guid,
                        status="gone",
                        raw={"recorder_probe": {
                            "result": "not_found",
                            "http_status": detail["http_status"],
                            "url": DETAIL_URL_TMPL.format(guid=guid),
                        }},
                    ))
                # else: not yet past end_date (or end_date unknown) — nothing this round.
                continue
            end_date = detail["end_date"]
            status = "closed" if end_date <= now else "active"
            info = _fetch_basic_info(guid)
            current_bid = _parse_money(info.get("currentBid")) if info else None
            bid_count = _int_or_none(info.get("numberOfBids")) if info else None
            observations.append(Observation(
                source=SOURCE,
                source_lot_id=guid,
                status=status,
                raw={
                    # end_date_raw is the literal matched dayjs(...) source
                    # string, not the derived datetime — see
                    # _fetch_detail_page's docstring.
                    "detail_page": {"url": detail["url"], "end_date_raw": detail["end_date_raw"]},
                    # basic_info is the untouched GetBasicInfo JSON response
                    # itself (already the literal source payload — currentBid/
                    # numberOfBids/endDateTime as the API returned them).
                    "basic_info": info,
                },
                current_bid=current_bid,
                bid_count=bid_count,
                end_date=end_date,
            ))
        return observations

    def sold_sweep(self) -> list[Observation]:
        items = _fetch_raw_auctions()
        if items is None:
            print("[mibid] RECORDER ERROR: sold_sweep() aborted — fetch failed, 0 observations")
            return []
        matches = [
            it for it in items
            if _status_from_code(it.get("status")) == "closed"
            and not it.get("isCanceled")
            and _is_furniture(it.get("title", ""))
        ]
        out = [_to_observation(it, status="closed") for it in matches]
        return [o for o in out if o is not None]

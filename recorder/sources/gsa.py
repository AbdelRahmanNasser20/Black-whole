"""GSA Auctions source adapter — official api.data.gov Auctions API.

LIVE RECON (verified 2026-07-31, this task):

- Endpoint + docs: `https://gsa.github.io/auctions_api/` (official docs site,
  `https://gsa.github.io/auctions_api/basics` + `.../fields`). Confirmed
  current shape live:
  `GET https://api.gsa.gov/assets/gsaauctions/v2/auctions?api_key=<key>&format=JSON`.
  This is a plain GET, no headers required. It 303-redirects once before
  landing on the JSON (handled transparently by `requests`/`polite_get`).
  Rate limit per the docs: 5,000 calls/day, 5 calls/5s (site-wide default;
  tighter with `DEMO_KEY`).
- Verified live with `DEMO_KEY` (no `GSA_API_KEY` was present in this
  worktree's `.env`): HTTP 200, `{"Results": [...]}` with 1,080 items at
  fetch time.
- Response shape is camelCase, one flat dict per lot — NOT the PascalCase
  names in the docs' "Field reference" table (that table describes the
  underlying data-dictionary column names, not the actual JSON keys). Real
  keys observed: `saleNo`, `lotNo`, `aucStartDt`, `aucEndDt` (date-only,
  `"YYYY-MM-DD"`, no time component — see below), `itemName`, `lotInfo` (HTML
  description), `propertyCity`/`propertyState`/`propertyZip`,
  `auctionStatus` (observed values: `"Active"` (939), `"Preview"` (140), and
  one stray lowercase `"preview"` — always compare case-insensitively),
  `biddersCount` (number of bidders, not a bid-count field — GSA doesn't
  publish one; this is the closest available signal, used as `bid_count`),
  `highBidAmount` (float or null), `agencyName`, `itemDescURL`, `imageURL`.
- `(saleNo, lotNo)` is a unique key across all 1,080 items observed (checked
  live). `source_lot_id = f"{saleNo}-{lotNo}"` — matches the
  `gsa-{a}-{b}-{lot}` pattern already used in this project's research
  (`docs/blackwhole-28/research/R2-data-model.md`), since `saleNo` itself
  already contains embedded dashes (e.g. `"2-1-QSC-I-26-318"`).
- `discover()` scope: per the brief, "GSA serves ACTIVE auctions only" — so
  discover only keeps `auctionStatus.lower() == "active"` items, further
  filtered by a case-insensitive `FURNITURE_TERMS` match against
  `itemName + lotInfo`. Verified live 2026-07-31: 77 furniture/seating
  matches out of 1,080 total items (titles included "LOUNGE CHAIRS", "TASK
  CHAIR", "Office Fabric Workstation Chairs", "Leather chairs", etc.) —
  comfortably >0.
- No sold/closed feed exists — GSA simply drops a lot from the active list
  once it closes. `sold_sweep()` returns `[]` per the brief; the close
  itself is detected by `poll()`: a tracked lot's `(saleNo, lotNo)` missing
  from a HEALTHY fresh full-list fetch, **after its own `end_date` has
  passed**, is reported `status='gone'` (last known snapshot is the de-facto
  final price — `capture_method` stays `last_snapshot`, decided at the
  `sold_comps` view layer, not here). A lot missing before its `end_date` (or
  with an unknown `end_date`) is not yet eligible to be "gone" — it emits no
  observation this round and is retried on the next due poll. See "poll()
  gone semantics" below — this is a fix-round-1 correction; the original cut
  of this adapter inferred 'gone' from mere absence, which both violated the
  brief (task-2-brief.md:24 — 'gone' means confirmed-absent, not just
  not-found-this-round) and meant a single failed HTTP call would mass-mark
  every tracked lot 'gone' (append-only — unrecoverable).
- Money: `highBidAmount` is a bare float or `null` — `Decimal(str(value))`
  when present.
- Dates: `aucEndDt`/`aucStartDt` are date-only strings with NO time
  component (confirmed on all 1,080 live items — 100% match `^\\d{4}-\\d{2}-\\d{2}$`).
  Since GSA doesn't publish the exact intraday close time, `end_date` is
  parsed as `23:59:59 UTC` of that date (end-of-day) rather than midnight —
  midnight would make an auction closing "today" look already-passed for the
  entire day it's still open, which would wrongly trigger the schedule's
  confirming-poll / hot-cadence logic all day.
- `GSA_API_KEY` env var, falling back to the literal `DEMO_KEY` with a
  printed warning (DEMO_KEY is shared/rate-limited; sign up free at
  https://api.data.gov/signup/).

poll() gone semantics (fix round 1): `_fetch_auctions()` returns `None` to
mean "the fetch itself failed" (network exception, non-200, bad JSON, bad
shape) — distinct from a healthy fetch that happens to return an empty or
non-matching list. `poll()` treats a failed fetch as "no information this
round": it emits NO observations at all (not even 'gone' ones) and prints a
loud `RECORDER ERROR` line naming the source and reason, so a transient
outage never gets misrecorded as every tracked lot vanishing. Only a HEALTHY
fetch in which a tracked lot's id is absent, AND that lot's own `end_date`
(supplied by the caller via `store.tracked_active()`'s row) is not None and
has already passed, produces a `status='gone'` observation.

poll() also re-derives status from the found item's own `auctionStatus`
(`_status_from_auction_status`) rather than hardcoding `'active'` — GSA
hasn't been observed to publish a "closed"/"sold" status value (it simply
drops closed lots), but re-checking rather than assuming keeps `poll()`
consistent with `discover()` and correct if that ever changes.

Fixture captured live 2026-07-31 under
`tests/recorder/fixtures/gsa/discover_sample.json` — 20 items: 7 furniture
matches + 13 non-furniture control items (including the one stray lowercase
`"preview"` status item), full untouched dicts inside the same
`{"Results": [...]}` wrapper the real API returns.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, polite_get

SOURCE = "gsa"

AUCTIONS_URL = "https://api.gsa.gov/assets/gsaauctions/v2/auctions"

GSA_API_KEY_ENV = "GSA_API_KEY"
DEMO_KEY_FALLBACK = "DEMO_KEY"


def _api_key() -> str:
    key = os.environ.get(GSA_API_KEY_ENV)
    if key:
        return key
    print(
        "[gsa] WARNING: GSA_API_KEY not set — falling back to the public DEMO_KEY "
        "(shared, rate-limited). Sign up free at https://api.data.gov/signup/ and "
        "set GSA_API_KEY in .env."
    )
    return DEMO_KEY_FALLBACK


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


def _parse_end_date(date_str: Any) -> datetime | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
    except ValueError:
        return None
    # date-only field, no published close time — use end-of-day UTC (see docstring).
    return d.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


def _lot_id(item: dict) -> str | None:
    sale_no = item.get("saleNo")
    lot_no = item.get("lotNo")
    if sale_no is None or lot_no is None:
        return None
    return f"{sale_no}-{lot_no}"


def _is_furniture(item: dict) -> bool:
    text = f"{item.get('itemName') or ''} {item.get('lotInfo') or ''}".lower()
    return any(term in text for term in FURNITURE_TERMS)


def _status_from_auction_status(item: dict) -> str:
    """Re-derive status from the item's own auctionStatus rather than assuming.

    GSA hasn't been observed to publish an explicit closed/sold status (it
    just drops closed lots from the feed — see module docstring), but if it
    ever does, map it sensibly instead of blindly reporting 'active' for any
    found item.
    """
    text = str(item.get("auctionStatus", "")).lower()
    if "closed" in text or "sold" in text:
        return "closed"
    return "active"


def _to_observation(item: dict) -> Observation | None:
    lot_id = _lot_id(item)
    if lot_id is None:
        print(f"[gsa] skipping item with no saleNo/lotNo: {item.get('itemName')!r}")
        return None
    return Observation(
        source=SOURCE,
        source_lot_id=lot_id,
        status=_status_from_auction_status(item),
        raw=item,
        current_bid=_parse_money(item.get("highBidAmount")),
        bid_count=_int_or_none(item.get("biddersCount")),
        end_date=_parse_end_date(item.get("aucEndDt")),
    )


def _redact_key(text: str, key: str) -> str:
    """Strip a live GSA_API_KEY value out of arbitrary error text (e.g. a
    requests exception's str(), which can embed the full request URL — query
    string and all — via the underlying urllib3 message). IMPORTANT 4 fix:
    cron logs must never leak the key."""
    if key:
        return text.replace(key, "<redacted>")
    return text


def _fetch_auctions() -> list[dict] | None:
    """Fetch the full active-auctions list. Returns None on ANY fetch failure
    (network exception, blocked, non-200, bad JSON, unexpected shape) — never
    an empty list, so callers can tell "fetch failed" apart from "fetch
    succeeded and there's genuinely nothing there." See module docstring's
    "poll() gone semantics" for why this distinction is load-bearing.

    IMPORTANT 4 fix (BLACKWHOLE-28 whole-branch review): every error path
    below logs the constant `AUCTIONS_URL` (no query string), never
    `resp.url` — `resp.url` is the actual request URL requests.py built,
    which includes `?api_key=<key>&format=JSON`, and printing it would leak
    the key into cron logs on every blocked/malformed-response error. The
    network-exception path additionally redacts the key out of the
    exception's own string, since `requests`/`urllib3` exception messages can
    embed the full failed request URL (key included).
    """
    key = _api_key()
    try:
        resp = polite_get(AUCTIONS_URL, params={"api_key": key, "format": "JSON"})
    except requests.exceptions.RequestException as e:
        print(f"[gsa] RECORDER ERROR: request failed: {_redact_key(str(e), key)}")
        return None
    if resp.status_code in (403, 429):
        print(f"[gsa] RECORDER ERROR: blocked HTTP {resp.status_code} on {AUCTIONS_URL} — backing off, no data this round")
        return None
    if resp.status_code != 200:
        print(f"[gsa] RECORDER ERROR: unexpected HTTP {resp.status_code} on {AUCTIONS_URL}")
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"[gsa] RECORDER ERROR: non-JSON response from {AUCTIONS_URL}")
        return None
    if not isinstance(data, dict) or "Results" not in data:
        print(f"[gsa] RECORDER ERROR: unexpected response shape (no 'Results' key) from {AUCTIONS_URL}")
        return None
    results = data.get("Results")
    if not isinstance(results, list):
        print(f"[gsa] RECORDER ERROR: 'Results' is not a list in response from {AUCTIONS_URL}")
        return None
    return results


class GSASource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        items = _fetch_auctions()
        if items is None:
            print("[gsa] RECORDER ERROR: discover() aborted — fetch failed, 0 observations")
            return []
        active_furniture = [
            it for it in items
            if str(it.get("auctionStatus", "")).lower() == "active" and _is_furniture(it)
        ]
        out = [_to_observation(it) for it in active_furniture]
        result = [o for o in out if o is not None]
        if not result:
            print(
                f"[gsa] WARNING: discover() found 0 furniture-matched active lots "
                f"out of {len(items)} total fetched — check FURNITURE_TERMS / "
                f"auctionStatus filter for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        items = _fetch_auctions()
        if items is None:
            print(
                f"[gsa] RECORDER ERROR: poll() fetch failed — skipping gone-detection "
                f"this round for {len(lots)} tracked lot(s), emitting no observations"
            )
            return []
        now = datetime.now(timezone.utc)
        by_id = {lid: it for it in items if (lid := _lot_id(it)) is not None}
        observations: list[Observation] = []
        for lot in lots:
            lot_id = str(lot["source_lot_id"])
            item = by_id.get(lot_id)
            if item is not None:
                obs = _to_observation(item)
                if obs is not None:
                    observations.append(obs)
                continue
            end_date = lot.get("end_date")
            if end_date is not None and end_date <= now:
                observations.append(Observation(
                    source=SOURCE,
                    source_lot_id=lot_id,
                    status="gone",
                    raw={"recorder_probe": {"result": "not_found", "http_status": 200, "url": AUCTIONS_URL}},
                ))
            # else: absent from a healthy fetch but not yet past end_date (or
            # end_date unknown) — emit nothing this round, retried later.
        return observations

    def sold_sweep(self) -> list[Observation]:
        return []

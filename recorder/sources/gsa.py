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
  from a fresh full-list fetch is reported `status='gone'` (last known
  snapshot is the de-facto final price — `capture_method` stays
  `last_snapshot`, decided at the `sold_comps` view layer, not here).
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

Fixture captured live 2026-07-31 under
`tests/recorder/fixtures/gsa/discover_sample.json` — 20 items (15 furniture
matches across several categories + 5 non-furniture control items, including
the one stray lowercase `"preview"` status item), full untouched dicts inside
the same `{"Results": [...]}` wrapper the real API returns.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

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


def _to_observation(item: dict) -> Observation | None:
    lot_id = _lot_id(item)
    if lot_id is None:
        print(f"[gsa] skipping item with no saleNo/lotNo: {item.get('itemName')!r}")
        return None
    return Observation(
        source=SOURCE,
        source_lot_id=lot_id,
        status="active",
        raw=item,
        current_bid=_parse_money(item.get("highBidAmount")),
        bid_count=_int_or_none(item.get("biddersCount")),
        end_date=_parse_end_date(item.get("aucEndDt")),
    )


def _fetch_auctions() -> list[dict]:
    resp = polite_get(AUCTIONS_URL, params={"api_key": _api_key(), "format": "JSON"})
    if resp.status_code in (403, 429):
        print(f"[gsa] blocked: HTTP {resp.status_code} on {resp.url} — backing off, returning what we have")
        return []
    if resp.status_code != 200:
        print(f"[gsa] ERROR: unexpected HTTP {resp.status_code} on {resp.url}")
        return []
    try:
        data = resp.json()
    except ValueError:
        print(f"[gsa] ERROR: non-JSON response from {resp.url}")
        return []
    if not isinstance(data, dict) or "Results" not in data:
        print(f"[gsa] ERROR: unexpected response shape (no 'Results' key) from {resp.url}")
        return []
    results = data.get("Results")
    if not isinstance(results, list):
        print(f"[gsa] ERROR: 'Results' is not a list in response from {resp.url}")
        return []
    return results


class GSASource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        items = _fetch_auctions()
        active_furniture = [
            it for it in items
            if str(it.get("auctionStatus", "")).lower() == "active" and _is_furniture(it)
        ]
        out = [_to_observation(it) for it in active_furniture]
        return [o for o in out if o is not None]

    def poll(self, lots: list[dict]) -> list[Observation]:
        tracked_ids = {str(lot["source_lot_id"]) for lot in lots}
        if not tracked_ids:
            return []
        items = _fetch_auctions()
        by_id = {lid: it for it in items if (lid := _lot_id(it)) is not None}
        observations: list[Observation] = []
        for lot_id in tracked_ids:
            item = by_id.get(lot_id)
            if item is not None:
                obs = _to_observation(item)
                if obs is not None:
                    observations.append(obs)
            else:
                observations.append(Observation(
                    source=SOURCE,
                    source_lot_id=lot_id,
                    status="gone",
                    raw={"recorder_probe": {"result": "not_found", "http_status": 200, "url": AUCTIONS_URL}},
                ))
        return observations

    def sold_sweep(self) -> list[Observation]:
        return []

"""GovDeals source adapter — thin wrapper over `deals.adapters.govdeals.GovDealsAdapter`
(the maestro JSON search API already built for the `deals/` closing-price tracker).
Import-only reuse per the shared contract — this module never edits `deals/` or
`auction_extractors/`, it only imports pure functions/classes from them.

LIVE RECON (verified 2026-07-31, this task):

- `GovDealsAdapter.discover(category_ids, search_text, max_pages)` is a
  generator of `deals.models.Lot`. Its HTTP calls happen INSIDE `deals/`
  code via bare `requests.post(...).raise_for_status()` (not this repo's
  `polite_get`) — a documented, brief-sanctioned exception for this one
  source. `_safe_discover()` below wraps generator consumption in
  try/except so a mid-sweep failure (network exception, non-200 —
  `raise_for_status()` raises `requests.exceptions.HTTPError`, a
  `RequestException` subclass) is a loud, non-fatal fetch failure —
  never silently swallowed, and whatever lots were already collected
  before the failure are kept rather than thrown away.
- `GovDealsAdapter()._headers()` calls `auction_extractors.govdeals_chairs_
  extraction._resolve_maestro_key()` on every request. That function
  self-heals by design (scrapes the current key from GovDeals' public JS
  bundle, falls back to a hardcoded constant on ANY failure, including
  network errors — verified by reading its source) — it never raises, so
  it needs no extra guarding here. The key is never hardcoded in this
  module, matching the "self-heals" contract.
- `source_lot_id` = `deals.models.lot_key(asset_id, account_id, auction_id)`
  = `f"{asset_id}/{account_id}/{auction_id}"` — reused directly rather than
  reimplemented, so recorder ids and `deals/`'s own ids never drift apart.
- Furniture scope, verified live: `discover(category_ids="372,47B,47C,47A,
  46,47D,28E,266", search_text="chairs")` (the exact cluster `deals/`'s own
  CLI default uses) returned real furniture lots on the first page (e.g.
  asset 41961/account 432/auction 2, "STA" status, `end_utc` tz-aware UTC
  already). Per the brief, `discover()` ALSO sweeps the remaining
  `FURNITURE_TERMS` (everything except "chairs", which the category sweep
  above already covers) with `category_ids=""` (no category filter) — each
  capped at `TERM_MAX_PAGES` pages. Merged/deduped by `lot_key` across all
  sweeps (a lot matching two terms is fetched twice, stored once) — same
  pattern `recorder/sources/municibid.py` uses for its own multi-request
  FURNITURE_TERMS sweep.
- `Lot.end_utc` (`deals/mapping.py::_utc`) parses `assetAuctionEndDateUtc`
  via `datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.
  utc)` — verified live to already be tz-aware UTC on every item fetched in
  this recon. `Snapshot.end_utc` (built by `GovDealsAdapter.refetch` from
  the same `Lot.end_utc`) matches. However `Snapshot.observed_at`
  (`datetime.now().astimezone()` in `deals/adapters/govdeals.py::refetch`)
  is tz-aware in the RUNNING MACHINE's LOCAL zone, NOT UTC — verified live,
  came back tagged `-07:00`/`MST`. `_ensure_utc()` below is applied
  uniformly to every datetime pulled out of `deals/` objects (never trusted
  by field name alone) rather than assuming which fields are already UTC —
  this module's `observed_at` is deliberately never set on `Observation`
  either way (left `None` so `listing_snapshots.observed_at` gets the DB's
  own `now()` default — matches every other adapter's convention; we only
  need `_ensure_utc` for `end_date`).
- Money: `Lot.current_bid`/`Snapshot.current_bid` are already Python
  `float` (parsed strictly by `deals/mapping.py::_price`, which raises
  `ValueError` for a missing/unparseable `currentBid` — caught and
  logged-and-skipped INSIDE `GovDealsAdapter.discover()`/`.refetch()`
  itself, so a bad record never reaches this module as a `Lot`/`Snapshot`
  at all). `_parse_money()` here just wraps `Decimal(str(value))` for the
  `float -> Decimal` conversion this repo's `Observation.current_bid`
  contract requires.
- Status: `Lot.status`/`Snapshot.status` carry the raw `assetStatusCd`
  field verbatim. Every item observed live in this recon — including ones
  ending mere minutes in the future — carried `"STA"` ("started"/active).
  No closed/sold status code has ever been observed; GovDeals lots simply
  VANISH from the maestro search feed the instant an auction closes (per
  the brief: "GovDeals lots vanish at close → last_snapshot capture").
  `_status_of()` is therefore a defensive, generic case-insensitive
  substring check (mirrors `recorder/sources/gsa.py::_status_from_
  auction_status`'s identical "never observed but don't hardcode"
  reasoning) so a future/unobserved status value containing "sold" or
  "closed" maps correctly instead of a found-but-actually-closed lot
  silently reporting 'active'.
- `poll()`'s primary signal for a closed/removed lot is therefore ABSENCE
  from `adapter.refetch(keys)`'s result dict, exactly like GSA/Purple
  Wave's "healthy fetch + absent + end_date passed => gone" rule — NOT a
  status-code check (see above, no closed code has ever been observed).
- `refetch()`'s own doc-comment: "sweep the auctionclose-sorted firehose
  until we've matched all wanted keys or run dry" (up to 60 pages, NO
  category/search filter) — this means a poll for an already-closed lot
  (which `refetch` will never find) walks the FULL 60-page sweep every
  time it's called. This is `deals/` code we don't modify (import-only
  reuse) — noted here as a known performance characteristic, not a bug in
  this module.
- `Snapshot` (unlike `Lot`) has no `.raw` field — the brief's instruction
  is to build `raw` from `dataclasses.asdict(snapshot)`. That asdict
  output (7 flat scalar fields: `asset_id`, `account_id`, `auction_id`,
  `observed_at`, `bid_count`, `current_bid`, `end_utc`, `status`) IS this
  module's entire poll()-path payload — there is no richer upstream
  payload to fall back to, so it's also the full "recompute without
  re-scraping" record for a `poll()`-sourced row. `json.dumps(..., default
  =str)` (used by `recorder/store.py::observation_row`) stringifies the
  two embedded `datetime` objects automatically.

Fixtures captured live 2026-07-31 under `tests/recorder/fixtures/govdeals/`:
`lot_raw_examples.json` (4 real `Lot.raw` maestro asset dicts from the
"372,...,266" category + "chairs" search — untouched dicts, the exact shape
`asset_to_lot()` consumes) and `snapshot_examples.json` (3 real
`dataclasses.asdict(Snapshot)` outputs from `adapter.refetch()` on 3 of
those same lots' keys, datetimes serialized to ISO strings for JSON, with
`observed_at`'s real local-zone offset preserved verbatim — see the
`_ensure_utc` note above). Offline tests reconstruct real `Lot`/`Snapshot`
objects from these fixtures via `deals.mapping.asset_to_lot` (a pure
function, imported not modified) and `deals.models.Snapshot(**...)`, then
exercise this module's own mapping functions on them — no network calls.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from deals.adapters.govdeals import GovDealsAdapter
from deals.models import Lot, Snapshot, lot_key

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS

SOURCE = "govdeals"

# The furniture category cluster `deals/`'s own CLI default sweeps
# (docs/superpowers plan; also the literal default in deals.cli's
# `discover --categories 372,47B,47C,47A,46,47D,28E,266`).
FURNITURE_CATEGORY_IDS = "372,47B,47C,47A,46,47D,28E,266"

# Cap pages modestly (brief: "cap pages modestly per the brief" /
# "max_pages<=10 per term" for the FURNITURE_TERMS sweep). GovDeals is
# high-volume — the category+"chairs" sweep is the primary, comprehensive
# discover() path, so it gets a larger cap than the supplementary
# single-term sweeps.
CATEGORY_MAX_PAGES = 20
TERM_MAX_PAGES = 10


def _status_of(status_text: str | None) -> str:
    """Re-derive status from GovDeals' own `assetStatusCd` rather than
    hardcoding 'active' for any found lot. See module docstring: every
    item observed live carried 'STA'; no closed/sold code has ever been
    seen (GovDeals lots vanish from search entirely at close instead — the
    real close signal is ABSENCE, handled separately in poll()). This is a
    defensive substring check in case that ever changes, mirroring
    recorder/sources/gsa.py's identical justification.
    """
    text = (status_text or "").lower()
    if "sold" in text or "closed" in text:
        return "closed"
    return "active"


def _parse_lot_key(source_lot_id: str) -> tuple[int, int, int] | None:
    parts = source_lot_id.split("/")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize any datetime coming out of `deals/` objects to tz-aware
    UTC. NOT all of it is guaranteed UTC by field name alone — see module
    docstring's note on `Snapshot.observed_at` being local-zone, not UTC.
    Applied uniformly rather than trusting a field's name.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _lot_to_observation(lot: Lot) -> Observation:
    return Observation(
        source=SOURCE,
        source_lot_id=lot_key(lot.asset_id, lot.account_id, lot.auction_id),
        status=_status_of(lot.status),
        raw=lot.raw,
        current_bid=_parse_money(lot.current_bid),
        bid_count=lot.bid_count,
        end_date=_ensure_utc(lot.end_utc),
    )


def _snapshot_to_observation(key: str, snapshot: Snapshot) -> Observation:
    """`Snapshot` has no `.raw` — `raw` is `dataclasses.asdict(snapshot)`
    itself (the brief's instruction). See module docstring: this asdict
    output IS the full payload for a poll()-sourced row, nothing richer to
    fall back to.
    """
    raw = asdict(snapshot)
    return Observation(
        source=SOURCE,
        source_lot_id=key,
        status=_status_of(snapshot.status),
        raw=raw,
        current_bid=_parse_money(snapshot.current_bid),
        bid_count=snapshot.bid_count,
        end_date=_ensure_utc(snapshot.end_utc),
    )


def _safe_discover(
    adapter: GovDealsAdapter, *, category_ids: str, search_text: str, max_pages: int, label: str
) -> tuple[list[Lot], bool]:
    """Consume `adapter.discover(...)` defensively. Its HTTP calls happen
    inside `deals/` code (documented exception — see module docstring), so
    exceptions can surface mid-generator. Returns `(lots_collected_so_far,
    ok)` — `ok=False` means the sweep stopped early on a fetch failure, but
    whatever was already collected is kept, never discarded.
    """
    lots: list[Lot] = []
    try:
        for lot in adapter.discover(category_ids=category_ids, search_text=search_text, max_pages=max_pages):
            lots.append(lot)
    except requests.exceptions.RequestException as e:
        print(f"[govdeals] RECORDER ERROR: {label} sweep failed after {len(lots)} lot(s): {e}")
        return lots, False
    except Exception as e:  # noqa: BLE001 — any other deals/-internal failure must still be loud, not fatal
        print(f"[govdeals] RECORDER ERROR: {label} sweep failed unexpectedly after {len(lots)} lot(s): {e}")
        return lots, False
    return lots, True


class GovDealsSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        adapter = GovDealsAdapter()
        by_key: dict[str, Lot] = {}
        any_ok = False

        lots, ok = _safe_discover(
            adapter,
            category_ids=FURNITURE_CATEGORY_IDS,
            search_text="chairs",
            max_pages=CATEGORY_MAX_PAGES,
            label="category cluster ('chairs' + furniture categories)",
        )
        any_ok = any_ok or ok
        for lot in lots:
            by_key[lot_key(lot.asset_id, lot.account_id, lot.auction_id)] = lot

        for term in FURNITURE_TERMS:
            if term == "chairs":
                continue  # already covered by the category+"chairs" sweep above
            lots, ok = _safe_discover(
                adapter,
                category_ids="",
                search_text=term,
                max_pages=TERM_MAX_PAGES,
                label=f"term sweep ({term!r})",
            )
            any_ok = any_ok or ok
            for lot in lots:
                by_key[lot_key(lot.asset_id, lot.account_id, lot.auction_id)] = lot

        if not any_ok and not by_key:
            print("[govdeals] RECORDER ERROR: discover() aborted — every sweep failed, 0 observations")
            return []
        result = [_lot_to_observation(lot) for lot in by_key.values()]
        if not result:
            print(
                "[govdeals] WARNING: discover() found 0 lots across the furniture category "
                "cluster + FURNITURE_TERMS sweeps — check category ids / search terms for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        keys: list[tuple[int, int, int]] = []
        key_by_lot_id: dict[str, tuple[int, int, int]] = {}
        for lot in lots:
            lot_id = str(lot["source_lot_id"])
            parsed = _parse_lot_key(lot_id)
            if parsed is None:
                print(
                    f"[govdeals] RECORDER ERROR: poll() cannot parse source_lot_id "
                    f"{lot_id!r} as asset/account/auction — skipping"
                )
                continue
            keys.append(parsed)
            key_by_lot_id[lot_id] = parsed

        if not keys:
            return []

        adapter = GovDealsAdapter()
        try:
            snapshots = adapter.refetch(keys)
        except requests.exceptions.RequestException as e:
            print(
                f"[govdeals] RECORDER ERROR: poll() refetch failed — skipping gone-detection "
                f"this round for {len(keys)} tracked lot(s), emitting no observations: {e}"
            )
            return []
        except Exception as e:  # noqa: BLE001
            print(
                f"[govdeals] RECORDER ERROR: poll() refetch failed unexpectedly — skipping "
                f"gone-detection this round for {len(keys)} tracked lot(s), emitting no observations: {e}"
            )
            return []

        now = datetime.now(timezone.utc)
        observations: list[Observation] = []
        for lot in lots:
            lot_id = str(lot["source_lot_id"])
            parsed = key_by_lot_id.get(lot_id)
            if parsed is None:
                continue  # unparseable id — already logged above, skip
            k = lot_key(*parsed)
            snapshot = snapshots.get(k)
            if snapshot is not None:
                observations.append(_snapshot_to_observation(k, snapshot))
                continue
            end_date = lot.get("end_date")
            if end_date is not None and end_date <= now:
                observations.append(Observation(
                    source=SOURCE,
                    source_lot_id=k,
                    status="gone",
                    raw={"recorder_probe": {
                        "result": "not_found",
                        "http_status": 200,
                        "url": "govdeals-maestro-search-refetch",
                    }},
                ))
            # else: absent from a healthy refetch but not yet past end_date
            # (or end_date unknown) — emit nothing this round, retried later.
        return observations

    def sold_sweep(self) -> list[Observation]:
        return []

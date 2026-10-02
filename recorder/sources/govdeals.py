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

FIX ROUND 1 (review finding #2, 2026-07-31) — per-lot corroboration before
'gone': absence-from-the-60-page-refetch-sweep alone is a weaker signal than
it looks — the sweep can overflow its own page cap, or an anti-snipe
extension can move a lot's `end_date` later without this module's tracked
copy knowing yet. Before trusting a healthy-refetch absence past a tracked
lot's own `end_date`, `poll()` now corroborates with ONE extra call to
`GovDealsAdapter.fetch_detail(asset_id, account_id)` (the same per-lot
maestro detail endpoint `deals/`'s own gallery-fetch path already uses) —
capped at `CORROBORATION_CAP_PER_BATCH` calls per `poll()` invocation (loud
`WARNING` if the cap truncates coverage; any lot past the cap falls back to
the pre-fix "absence alone" 'gone' behavior for that round only).

LIVE RECON for the corroboration path (verified 2026-07-31, this fix round):
- `fetch_detail(asset_id, account_id)` — unlike the search/refetch payload —
  carries NO pricing fields at all (`currentBid`/`bidCount`/`assetBidPrice`
  are all absent from its ~90-key response). An 'active'/'closed' Observation
  built from corroboration therefore always carries `current_bid=None,
  bid_count=None` — the next `poll()` due-cycle will pick up real pricing
  once the lot is either found in `refetch()` again or the situation
  resolves.
- Its end-date field is `assetAuctionEndDate` — NOT `assetAuctionEndDateUtc`
  (that UTC-suffixed field, present on search results, is simply absent
  here) — and is **naive US/Eastern local time**, not UTC. Verified live by
  cross-checking asset 41961/432's `fetch_detail()` value
  (`"2026-07-31T09:01:00"`, no offset) against the SAME lot's known-UTC
  `assetAuctionEndDateUtc` from `discover()` (`2026-07-31T13:01:00+00:00`,
  a 4-hour EDT gap) — exact match after `zoneinfo`-converting Eastern→UTC.
  `_parse_detail_end_date()` does that conversion (mirrors
  `recorder/sources/municibid.py`'s identical ET-conversion pattern for its
  own detail-page timestamps — GovDeals, like Municibid, is a US East Coast
  operation).
- A `(asset_id, account_id)` pair that doesn't correspond to a real asset
  (verified live with a bogus id and a real `asset_id` paired with a wrong
  `account_id`) returns **HTTP 204 with an empty body** — NOT 404. Because
  `GovDealsAdapter.fetch_detail()` calls `r.raise_for_status()` (which does
  NOT raise on 204, a 2xx code) before `r.json()`, the empty body surfaces
  as `requests.exceptions.JSONDecodeError` (`"Expecting value: line 1 column
  1"`) — which, per `requests`' own class hierarchy, IS a
  `requests.exceptions.RequestException` subclass, so it must be caught
  BEFORE the generic `RequestException` handler or it would be
  mis-classified as a plain network failure. This 204-via-JSONDecodeError
  IS this endpoint's real "doesn't exist" signal and is treated as
  corroborating 'gone' — NOT as "fetch_detail RAISES (network) → emit
  nothing" (a real HTTPError — 403/429/5xx — or a real connection failure
  still hits the "emit nothing" branch, since those say nothing about
  whether the lot exists).

Residual caveat (documented per review, not solved further here): if
`fetch_detail` itself keeps failing for a genuinely-absent lot on every poll
cycle (persistent network trouble reaching just that one corroboration
call), that lot never gets marked 'gone' and lingers in `store.
tracked_active()` indefinitely — this is the deliberately SAFE failure
direction (never guess 'gone' without evidence, matching the append-only-
unrecoverable-mistake principle), but it means corroboration failure is not
self-healing on its own; an operator/coverage-report (`recorder/store.py::
coverage()`) would need to surface a lot that's stopped updating for an
unusually long time. Also: `fetch_detail` is keyed by `(asset_id,
account_id)` only, not the full 3-part `lot_key` this module tracks by — an
`auction_id` mismatch (the same asset/account re-auctioned) is not
independently checked; this mirrors the same 2-key limitation already
present in `deals/adapters/govdeals.py::fetch_detail`'s own signature and
is not something this module can fix without editing `deals/`.

FIX ROUND 2 (review finding, 2026-07-31) — the "gone_unverified" batch
guard: fix round 1's `_corroborate_absence` originally folded the
`JSONDecodeError` case straight into the same `"gone"` verdict as a real
CLEAN parsed-but-empty response, AND printed nothing for it — both wrong.
`JSONDecodeError` carries no `.response` (confirmed by reading `requests`'
own `Response.json()` source: it raises `RequestsJSONDecodeError(e.msg,
e.doc, e.pos)` with no `response=` kwarg), and `GovDealsAdapter.
fetch_detail()` doesn't expose the underlying `Response` object either — so
THIS module genuinely cannot tell a real HTTP 204 apart from a WAF/anti-bot
interstitial page or a transient malformed-but-200 body from the exception
alone. If a systemic event (an anti-bot challenge, an outage) hit the
corroboration endpoint for several lots in the same `poll()` batch, the
pre-fix code would have silently, permanently marked every one of them
'gone' — the exact append-only-unrecoverable failure class the fix-round-1
PS 401 guard exists to prevent, left unguarded on the GovDeals side.

Now: `JSONDecodeError` returns a distinct `"gone_unverified"` verdict (never
folded into plain `"gone"`) and ALWAYS prints a `RECORDER NOTE` line — never
silent, even for the single, common, legitimate case. `poll()` collects
every corroboration verdict for the batch BEFORE trusting any of them, then
runs a batch-level check mirroring `public_surplus.py`'s 401 guard exactly:
`CORROBORATION_BLOCK_MIN_COUNT` (3) AND `CORROBORATION_BLOCK_MIN_FRACTION`
(80%) of the batch's corroboration ATTEMPTS (not the whole `poll()` batch —
lots that resolved via `refetch()` or the cap-fallback path don't count
toward this denominator) hitting `"gone_unverified"` trips a loud
`RECORDER ERROR` and suppresses ALL 'gone' observations derived from THOSE
unverified signals that round; explicit `"active"`/`"closed"`/clean-`"gone"`
verdicts (a real parsed-but-empty 200) from the same batch still stand. A
single ambiguous body among an otherwise-healthy batch still becomes
`'gone'` exactly as fix round 1 shipped it.

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

import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests

from deals.adapters.govdeals import GovDealsAdapter
from deals.mapping import asset_to_lot
from deals.models import Lot, Snapshot, lot_key
from deals.tracking import CLOSE_GRACE, LIVE_STATUS

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS

SOURCE = "govdeals"
ALLSURPLUS_SOURCE = "allsurplus"

# maestro businessId → recorder source. One maestro host and key serve both
# GovDeals ("GD") and AllSurplus ("GI"); "AD" lots are US GovDeals lots (the
# bidbox calls them GD). A GI lot that turns up in the GovDeals sweep is
# recorded as allsurplus — never under govdeals (its currency is often not USD).
BUSINESS_SOURCES = {"GD": SOURCE, "AD": SOURCE, "GI": ALLSURPLUS_SOURCE}
MAESTRO_SOURCES = (SOURCE, ALLSURPLUS_SOURCE)


def source_for_business(business_id: str | None) -> str:
    return BUSINESS_SOURCES.get((business_id or "").strip().upper(), SOURCE)


def business_for_source(source: str) -> str:
    return "GI" if source == ALLSURPLUS_SOURCE else "GD"

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

# --- scope switch (2026-09-29, "track everything") ---------------------------
#
# `RECORDER_GOVDEALS_SCOPE=all` (the default) sweeps the WHOLE site: one
# empty-`categoryIds`, empty-`searchText` search sorted by close time, paged
# until the feed runs dry (≤ `RECORDER_GOVDEALS_ALL_MAX_PAGES`, 120 lots a
# page). Measured 2026-09-29: see recorder/README.md "Scope". `furniture` is the
# old chairs/seating sweep, kept for a cheap rollback.
SCOPE_ALL = "all"
SCOPE_FURNITURE = "furniture"
ALL_MAX_PAGES_DEFAULT = 300   # 27,578 live lots = 230 pages on 2026-09-29


def scope() -> str:
    v = (os.getenv("RECORDER_GOVDEALS_SCOPE") or SCOPE_ALL).strip().lower()
    return SCOPE_FURNITURE if v == SCOPE_FURNITURE else SCOPE_ALL


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def all_max_pages() -> int:
    return _env_int("RECORDER_GOVDEALS_ALL_MAX_PAGES", ALL_MAX_PAGES_DEFAULT)


# --- per-run bounds for poll() (a 5-minute cron must finish in 5 minutes) -----
#
# Whole-site GovDeals closes ~4,200 lots/day (deal_lots, Jul-Aug 2026: ~29k a
# week), bunched at popular close times. Two caps keep one run bounded:
#
# - `RECORDER_GOVDEALS_BIDBOX_MAX_PER_RUN` (default 120): past-end lots read
#   from the bidbox per poll, **just-closed first**; the rest stay due and go
#   next run. The bidbox keeps answering for weeks, so a late read loses
#   nothing — but the freshest closes are the ones a soft-close may still move.
# - `RECORDER_GOVDEALS_REFETCH_HORIZON_HOURS` (default 24): only lots closing
#   inside this horizon (or with no clock) join the search refetch. Far lots are
#   re-observed by `discover` anyway; letting them into the refetch made it
#   walk its full page budget every run looking for lots hundreds of pages deep.
#   `RECORDER_GOVDEALS_REFETCH_MAX_PAGES` (default 40) caps that walk.
BIDBOX_MAX_PER_RUN_DEFAULT = 120
REFETCH_HORIZON_HOURS_DEFAULT = 24
REFETCH_MAX_PAGES_DEFAULT = 40


def bidbox_max_per_run() -> int:
    return _env_int("RECORDER_GOVDEALS_BIDBOX_MAX_PER_RUN", BIDBOX_MAX_PER_RUN_DEFAULT)


def refetch_horizon() -> timedelta:
    return timedelta(hours=_env_int("RECORDER_GOVDEALS_REFETCH_HORIZON_HOURS",
                                    REFETCH_HORIZON_HOURS_DEFAULT))

# Cap on per-lot `fetch_detail` corroboration calls per `poll()` invocation
# (fix round 1, review finding #2) — loud WARNING if truncated; any
# absent-past-end-date lot beyond the cap falls back to the pre-fix
# "absence alone" 'gone' behavior for that round only.
CORROBORATION_CAP_PER_BATCH = 25

# Batch-level suspected-systemic-event thresholds for JSONDecodeError-derived
# ("gone_unverified") corroboration verdicts (fix round 2, review finding) —
# mirrors recorder/sources/public_surplus.py's BLOCK_SUSPECT_MIN_COUNT/
# _MIN_FRACTION for its own 401 guard. A single ambiguous empty/unparseable
# body is still trusted as 'gone' (that IS the verified-live 204 "doesn't
# exist" signal in the common case); several in the SAME poll() batch looks
# more like a WAF/anti-bot interstitial or an outage hitting the
# corroboration endpoint broadly than several simultaneous real absences.
CORROBORATION_BLOCK_MIN_COUNT = 3
CORROBORATION_BLOCK_MIN_FRACTION = 0.8

# GovDeals' `fetch_detail()` end-date field (`assetAuctionEndDate`) is naive
# US/Eastern local time — see module docstring's "LIVE RECON for the
# corroboration path".
_GOVDEALS_TZ = ZoneInfo("America/New_York")


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


def _parse_detail_end_date(raw: Any) -> datetime | None:
    """Parse `fetch_detail()`'s `assetAuctionEndDate` — naive US/Eastern
    local time, NOT the UTC-suffixed field search results carry. See module
    docstring's "LIVE RECON for the corroboration path"."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=_GOVDEALS_TZ).astimezone(timezone.utc)


def _corroborate_absence(adapter: GovDealsAdapter, asset_id: int, account_id: int) -> tuple[str, dict | None]:
    """Fix round 1 (review finding #2): before trusting a healthy-refetch
    absence as 'gone', corroborate with the per-lot maestro detail endpoint.
    Returns `(verdict, payload)`:
    - `("active", detail)` — the asset/account pair still resolves to a real,
      non-closed record. `detail` is the raw `fetch_detail()` payload.
    - `("closed", detail)` — resolves, but `assetStatusCd` reads sold/closed
      (see `_status_of`'s "never observed but don't hardcode" caveat).
    - `("gone", empty_detail)` — a CLEAN 200 response that parsed fine but is
      falsy/empty (defensive — not observed live, but a real parsed signal,
      not an ambiguous one).
    - `("gone_unverified", None)` — fix round 2 (review finding): the body
      failed to parse as JSON at all (`requests.exceptions.JSONDecodeError`).
      Live recon found this IS GovDeals' real "asset/account pair doesn't
      exist" signal (HTTP 204 empty body — `raise_for_status()` doesn't
      reject 204, so the empty body surfaces from `r.json()` instead) — but
      `GovDealsAdapter.fetch_detail()` doesn't expose the underlying
      `Response`, and `JSONDecodeError` itself carries no `.response` (
      verified by reading `requests`' own `Response.json()` source: it
      raises `RequestsJSONDecodeError(e.msg, e.doc, e.pos)` with no
      `response=` kwarg). So THIS module cannot distinguish a real 204 from
      a WAF/anti-bot interstitial or a transient malformed-but-200 body from
      the exception alone. Treated as a TENTATIVE 'gone' — `poll()` runs a
      batch-level guard (`CORROBORATION_BLOCK_MIN_COUNT`/`_MIN_FRACTION`,
      mirrors `public_surplus.py`'s 401 guard) before trusting it, since a
      systemic event hitting several lots in one batch would otherwise mass-
      mark them 'gone' permanently (append-only-unrecoverable).
    - `("unknown", None)` — the call raised something that says NOTHING
      about whether the lot exists (a real HTTPError — 403/429/5xx — or a
      network-level RequestException, or an unexpected exception). Per the
      fetch-failure rule, the caller must emit NOTHING for this lot this
      round, not even 'gone'.
    """
    try:
        detail = adapter.fetch_detail(asset_id, account_id)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        print(
            f"[govdeals] RECORDER ERROR: poll() corroboration fetch_detail HTTP {status} "
            f"for {asset_id}/{account_id} — treating as fetch failure, emitting nothing "
            "for this lot this round"
        )
        return "unknown", None
    except requests.exceptions.JSONDecodeError:
        # Fix round 2 (review finding): never silent, and never unilaterally
        # trusted — see the "gone_unverified" case in this function's
        # docstring above. poll()'s batch-level guard makes the final call.
        print(
            f"[govdeals] RECORDER NOTE: corroboration empty/unparseable body for "
            f"{asset_id}/{account_id} — treating as gone (204 signal, unverified)"
        )
        return "gone_unverified", None
    except requests.exceptions.RequestException as e:
        print(
            f"[govdeals] RECORDER ERROR: poll() corroboration fetch_detail failed for "
            f"{asset_id}/{account_id}: {e} — emitting nothing for this lot this round"
        )
        return "unknown", None
    except Exception as e:  # noqa: BLE001
        print(
            f"[govdeals] RECORDER ERROR: poll() corroboration fetch_detail failed "
            f"unexpectedly for {asset_id}/{account_id}: {e} — emitting nothing for this "
            "lot this round"
        )
        return "unknown", None

    if not detail:
        # Any empty detail — the real 204 purge signal, OR a 200 with an empty
        # body/object (what a WAF interstitial can look like) — is only a
        # TENTATIVE gone, subject to the batch guard in _absence_observations.
        # fetch_detail returns {} for both, so neither is trusted alone.
        status = getattr(adapter, "last_detail_status", None)
        print(f"[govdeals] RECORDER NOTE: corroboration empty detail (HTTP {status}) for "
              f"{asset_id}/{account_id} — treating as gone (unverified)")
        return "gone_unverified", None
    if _status_of(detail.get("assetStatusCd")) == "closed":
        return "closed", detail
    return "active", detail


# --- bidbox finals (2026-09-28) ---------------------------------------------
#
# The search sweep can only ever see a lot while it is live, so every close
# used to be recorded as `last_snapshot` — the last bid seen before the lot
# vanished. Soft close adds ~3 min per late bid, so that snapshot routinely
# missed the finish (audit: 10 of 15 lots were 15-73 % low; 5282/3780/2 was
# recorded $460, the bidbox says $1,725). The per-lot bidbox keeps answering
# for days after close with the final `currentBid`, `bidCount`, the extended
# `assetAuctionEndDateUTC` and a closed `assetStatusCd`. Purged ⇒ HTTP 204,
# which `fetch_bid_state` returns as `{}`.
#
# Codes seen live 2026-09-28: STA (live), SOA (sold, awaiting payment), RNM
# (reserve not met), CNB (read after RNM / no bid). Code comments elsewhere
# list SOL, CLO, CAN, HFR. Outcome names match `deals.tracking.close_outcome`
# (PR #105) so the two line up once both merge.

CAPTURE_BIDBOX_FINAL = "bidbox_final"
CAPTURE_BIDBOX_RECHECK = "bidbox_recheck"
CAPTURE_BIDBOX_LIVE = "bidbox_live"

SOLD_CODES = frozenset({"SOA", "SOL"})
RESERVE_NOT_MET_CODES = frozenset({"RNM", "CNB"})
CANCELLED_CODES = frozenset({"CAN"})

# Payment default / relist check: one more bidbox read this long after an
# SOA final. The bidbox answered 25 days after close for 5282/3780/2, so the
# read usually lands; a 204 is recorded as purged, never guessed.
SOA_RECHECK_AFTER = timedelta(days=7)
SOA_RECHECK_LIMIT_PER_RUN = 20

# ≥1 s between bidbox requests in every path (poll, recheck, backfill).
BIDBOX_MIN_INTERVAL_SECONDS = 1.0
_bidbox_last_at: list[float] = []


def _bidbox_throttle() -> None:
    now = time.monotonic()
    if _bidbox_last_at:
        wait = BIDBOX_MIN_INTERVAL_SECONDS - (now - _bidbox_last_at[0])
        if wait > 0:
            time.sleep(wait)
    _bidbox_last_at[:] = [time.monotonic()]


class PoliteGovDealsAdapter(GovDealsAdapter):
    """`GovDealsAdapter` with every search page throttled (≤1 request/s on the
    maestro host, shared with the bidbox throttle) and counted, and an optional
    page ceiling. `deals/` is import-only here, so the politeness lives in this
    subclass; `discover()`/`refetch()` are inherited and page through
    `self._search_page`, so both are throttled without being copied."""

    def __init__(self, max_page: int | None = None, business_id: str = "GD"):
        super().__init__(business_id=business_id)
        self.max_page = max_page
        self.requests = 0

    def _search_page(self, category_ids: str, search_text: str, page: int, rows: int = 120) -> list[dict]:
        if self.max_page is not None and page > self.max_page:
            return []   # the inherited loops read an empty page as "run dry"
        _bidbox_throttle()
        self.requests += 1
        return super()._search_page(category_ids, search_text, page, rows)


def _adapter(max_page: int | None = None, business_id: str = "GD") -> GovDealsAdapter:
    return PoliteGovDealsAdapter(max_page=max_page, business_id=business_id)


def adapter_for_source(source: str, max_page: int | None = None) -> GovDealsAdapter:
    return _adapter(max_page=max_page, business_id=business_for_source(source))


def _adapter_source(adapter) -> str:
    return source_for_business(getattr(adapter, "business_id", "GD"))


def close_outcome(status_code: str | None, bid_count: int | None) -> str:
    """Outcome of a closed lot from its bidbox code. Same precedence as
    `deals.tracking.close_outcome` in PR #105 (not imported — not on main):
    a reserve-not-met or cancelled lot is never a sale, whatever its bids."""
    if status_code in RESERVE_NOT_MET_CODES:
        return "reserve_not_met"
    if status_code in CANCELLED_CODES:
        return "cancelled"
    if not bid_count:
        return "no_bid"
    if status_code in SOLD_CODES:
        return "sold"
    return "unknown"   # CLO, HFR, STA-past-grace, anything new — raw code kept


def fetch_bidbox(adapter: GovDealsAdapter, key: tuple[int, int, int]) -> tuple[str, dict | None]:
    """One throttled bidbox read. Returns `(result, payload)`:
    `("ok", raw)`, `("purged", None)` on a 204 / empty body, or
    `("error", None)` on anything that says nothing about the lot
    (network, HTTP error, non-JSON body, no `currentBid`)."""
    _bidbox_throttle()
    lot_id = lot_key(*key)
    try:
        raw = adapter.fetch_bid_state(*key)
    except requests.exceptions.RequestException as e:   # includes JSONDecodeError
        print(f"[govdeals] RECORDER ERROR: bidbox fetch failed for {lot_id}: {e}")
        return "error", None
    except Exception as e:  # noqa: BLE001
        print(f"[govdeals] RECORDER ERROR: bidbox fetch failed unexpectedly for {lot_id}: {e}")
        return "error", None
    if not raw:
        return "purged", None
    if raw.get("currentBid") is None or _parse_money(raw.get("currentBid")) is None:
        print(f"[govdeals] RECORDER ERROR: bidbox for {lot_id} has no usable currentBid — skipping")
        return "error", None
    return "ok", raw


def _bidbox_end(raw: dict) -> datetime | None:
    s = raw.get("assetAuctionEndDateUTC")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _bidbox_bid_count(raw: dict) -> int | None:
    try:
        return int(raw.get("bidCount"))
    except (TypeError, ValueError):
        return None


def classify_bidbox(raw: dict, now: datetime) -> str:
    """What a post-close bidbox read means. Pure.

    - `"extended"` — STA and the clock is in the future: soft close moved it.
    - `"grace"`    — STA, clock passed < CLOSE_GRACE ago: may still extend or
      flip; read again next run.
    - `"final"`    — any non-STA code, or STA past the grace (stuck; outcome
      `unknown`, raw code kept).
    """
    code = raw.get("assetStatusCd")
    end = _bidbox_end(raw)
    if code == LIVE_STATUS:
        if end is None or end > now:
            return "extended"
        if now < end + CLOSE_GRACE:
            return "grace"
    return "final"


def bidbox_observation(key: str, raw: dict, *, method: str, url: str | None = None,
                       extra: dict | None = None, source: str = SOURCE) -> Observation:
    """Observation from a bidbox payload. `raw` stays untouched under
    `"bidbox"`; the recorder's own verdict sits beside it under
    `"recorder_capture"` (read by `sold_comps`, migration 014)."""
    code = raw.get("assetStatusCd")
    bids = _bidbox_bid_count(raw)
    capture = {
        "method": method,
        "status_code": code,
        "http_status": 200,
        "url": url or f"{source}-maestro-bidbox/{key}",
    }
    if method != CAPTURE_BIDBOX_LIVE:
        capture["outcome"] = close_outcome(code, bids)
    if extra:
        capture.update(extra)
    return Observation(
        source=source,
        source_lot_id=key,
        status="active" if method == CAPTURE_BIDBOX_LIVE else "closed",
        raw={"recorder_capture": capture, "bidbox": raw},
        current_bid=_parse_money(raw.get("currentBid")),
        bid_count=bids,
        end_date=_bidbox_end(raw),
    )


def resolve_with_bidbox(adapter: GovDealsAdapter, parsed: tuple[int, int, int],
                        now: datetime) -> tuple[str, Observation | None]:
    """Read one past-end lot's bidbox and decide. Returns `(result, obs)`:
    `final`/`extended` carry an Observation; `grace`/`error` carry None
    (retry next run); `purged` carries None (caller falls back to the old
    absence path, i.e. `last_snapshot`)."""
    k = lot_key(*parsed)
    src = _adapter_source(adapter)
    result, raw = fetch_bidbox(adapter, parsed)
    if result != "ok":
        return result, None
    verdict = classify_bidbox(raw, now)
    if verdict == "extended":
        return "extended", bidbox_observation(k, raw, method=CAPTURE_BIDBOX_LIVE, source=src)
    if verdict == "grace":
        return "grace", None
    return "final", bidbox_observation(k, raw, method=CAPTURE_BIDBOX_FINAL, source=src)


def recheck_observation(key: str, prior: dict, result: str, raw: dict | None,
                        source: str = SOURCE) -> Observation | None:
    """The 7-day SOA re-check row. `prior` is the SOA final's stored row
    (`current_bid`, `bid_count`, `end_date`, `status_code`).

    Written whether or not anything moved: it is also the "already checked"
    marker (`store.soa_recheck_due` only picks lots whose LATEST row is still
    the SOA `bidbox_final`), so each SOA final gets exactly one re-check. On a
    204 the prior final's numbers are carried over and the row says
    `result: purged` — nothing is guessed. `error` returns None (try again
    next run)."""
    if result == "error":
        return None
    if result == "purged" or raw is None:
        return Observation(
            source=source, source_lot_id=key, status="closed",
            raw={"recorder_capture": {
                "method": CAPTURE_BIDBOX_RECHECK, "result": "purged", "http_status": 204,
                "status_code": prior.get("status_code"),
                "outcome": close_outcome(prior.get("status_code"), prior.get("bid_count")),
                "changed": None, "url": f"{source}-maestro-bidbox/{key}",
            }},
            current_bid=prior.get("current_bid"), bid_count=prior.get("bid_count"),
            end_date=prior.get("end_date"),
        )
    obs = bidbox_observation(key, raw, method=CAPTURE_BIDBOX_RECHECK, source=source)
    changed = (
        raw.get("assetStatusCd") != prior.get("status_code")
        or obs.current_bid != prior.get("current_bid")
        or obs.bid_count != prior.get("bid_count")
    )
    obs.raw["recorder_capture"].update({"result": "ok", "changed": changed,
                                        "prior_status_code": prior.get("status_code")})
    return obs


def _lot_to_observation(lot: Lot) -> Observation:
    """Search asset → Observation. The source follows the asset's own
    `businessId`: a GI lot is `allsurplus` whichever sweep found it."""
    return Observation(
        source=source_for_business((lot.raw or {}).get("businessId")),
        source_lot_id=lot_key(lot.asset_id, lot.account_id, lot.auction_id),
        status=_status_of(lot.status),
        raw=lot.raw,
        current_bid=_parse_money(lot.current_bid),
        bid_count=lot.bid_count,
        end_date=_ensure_utc(lot.end_utc),
    )


def _snapshot_to_observation(key: str, snapshot: Snapshot, source: str = SOURCE) -> Observation:
    """`Snapshot` has no `.raw` — `raw` is `dataclasses.asdict(snapshot)`
    itself (the brief's instruction). See module docstring: this asdict
    output IS the full payload for a poll()-sourced row, nothing richer to
    fall back to.
    """
    raw = asdict(snapshot)
    return Observation(
        source=source,
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
    BUSINESS_ID = "GD"

    def _make_adapter(self, max_page: int | None = None) -> GovDealsAdapter:
        return _adapter(max_page=max_page, business_id=self.BUSINESS_ID)

    def discover(self, scope_override: str | None = None) -> list[Observation]:
        adapter = self._make_adapter()
        if (scope_override or scope()) == SCOPE_ALL:
            return self._discover_all(adapter)
        return self._discover_furniture(adapter)

    def _discover_all(self, adapter: GovDealsAdapter) -> list[Observation]:
        """Whole site: the close-time-sorted firehose, paged until it runs dry."""
        pages = all_max_pages()
        lots, ok = _safe_discover(adapter, category_ids="", search_text="",
                                  max_pages=pages, label="whole-site sweep")
        by_key = {lot_key(l.asset_id, l.account_id, l.auction_id): l for l in lots}
        if not ok and not by_key:
            print("[govdeals] RECORDER ERROR: discover() aborted — whole-site sweep failed, 0 observations")
            return []
        if not by_key:
            print("[govdeals] WARNING: discover() found 0 lots in the whole-site sweep — "
                  "check the maestro search for drift")
            return []
        reqs = getattr(adapter, "requests", None)
        print(f"[govdeals] discover scope=all lots={len(by_key)} "
              f"requests={reqs if reqs is not None else '?'} max_pages={pages}")
        if len(lots) >= pages * 120:
            print(f"[govdeals] WARNING: whole-site sweep hit RECORDER_GOVDEALS_ALL_MAX_PAGES={pages} — "
                  "lots closing latest were not seen this sweep; raise the cap")
        return [_lot_to_observation(lot) for lot in by_key.values()]

    def _discover_furniture(self, adapter: GovDealsAdapter) -> list[Observation]:
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
        """Re-check tracked lots.

        Lots whose stored `end_date` has passed go to the bidbox first (see
        "bidbox finals" above): a final becomes a `closed` row with the real
        price, an extension a fresh `active` row, a lot inside the 15-min
        grace (or a failed read) is retried next run. Only a purged lot (204)
        falls through to the old absence path (`_absence_observations`), which
        yields `gone` and so `last_snapshot`. Lots still before their clock
        (or with no clock) use the search refetch as before — and the 60-page
        refetch is skipped entirely when no such lot is due.
        """
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        adapter = self._make_adapter(max_page=_env_int("RECORDER_GOVDEALS_REFETCH_MAX_PAGES",
                                                       REFETCH_MAX_PAGES_DEFAULT))
        observations: list[Observation] = []
        upcoming: list[tuple[str, tuple[int, int, int]]] = []
        past_end: list[tuple[datetime, tuple[int, int, int]]] = []
        purged: list[tuple[str, tuple[int, int, int]]] = []
        horizon = now + refetch_horizon()
        far = 0

        for lot in lots:
            lot_id = str(lot["source_lot_id"])
            parsed = _parse_lot_key(lot_id)
            if parsed is None:
                print(
                    f"[govdeals] RECORDER ERROR: poll() cannot parse source_lot_id "
                    f"{lot_id!r} as asset/account/auction — skipping"
                )
                continue
            end_date = lot.get("end_date")
            if end_date is None or end_date > now:
                if end_date is not None and end_date > horizon:
                    far += 1          # discover re-observes it; keep the refetch shallow
                    continue
                upcoming.append((lot_id, parsed))
                continue
            past_end.append((end_date, parsed))

        # Just-closed first, capped: the rest stay due and go next run.
        past_end.sort(key=lambda t: t[0], reverse=True)
        cap = bidbox_max_per_run()
        if len(past_end) > cap:
            print(f"[govdeals] RECORDER INFO: {len(past_end)} past-end lot(s) due, bidbox cap "
                  f"{cap} this run — the {len(past_end) - cap} oldest wait for the next run")
        if far:
            print(f"[govdeals] RECORDER INFO: {far} lot(s) closing beyond the "
                  f"{refetch_horizon()} refetch horizon left to discover")
        for _end, parsed in past_end[:cap]:
            result, obs = resolve_with_bidbox(adapter, parsed, now)
            if obs is not None:
                observations.append(obs)
            elif result == "purged":
                print(f"[govdeals] RECORDER INFO: bidbox 204 (purged) for {lot_key(*parsed)} — "
                      "falling back to absence detection (last_snapshot)")
                purged.append((lot_key(*parsed), parsed))
            # grace / error: nothing this round; the lot stays due.

        if upcoming:
            try:
                snapshots = adapter.refetch([p for _, p in upcoming])
            except requests.exceptions.RequestException as e:
                print(
                    f"[govdeals] RECORDER ERROR: poll() refetch failed — skipping "
                    f"{len(upcoming)} not-yet-closed lot(s) this round: {e}"
                )
                snapshots = None
            except Exception as e:  # noqa: BLE001
                print(
                    f"[govdeals] RECORDER ERROR: poll() refetch failed unexpectedly — skipping "
                    f"{len(upcoming)} not-yet-closed lot(s) this round: {e}"
                )
                snapshots = None
            if snapshots is not None:
                for _lot_id, parsed in upcoming:
                    k = lot_key(*parsed)
                    snapshot = snapshots.get(k)
                    if snapshot is not None:
                        observations.append(_snapshot_to_observation(k, snapshot, self.SOURCE))
                    # absent but not yet past end_date (or unknown) — retried later.

        observations.extend(self._absence_observations(adapter, purged))
        return observations

    def recheck_finals(self, rows: list[dict]) -> list[Observation]:
        """The 7-day SOA re-check: one bidbox read per row from
        `store.soa_recheck_due()`; returns the re-check rows to insert."""
        adapter = self._make_adapter()
        out: list[Observation] = []
        for row in rows:
            parsed = _parse_lot_key(str(row["source_lot_id"]))
            if parsed is None:
                continue
            result, raw = fetch_bidbox(adapter, parsed)
            obs = recheck_observation(lot_key(*parsed), row, result, raw, source=self.SOURCE)
            if obs is not None:
                out.append(obs)
        return out

    def _absence_observations(self, adapter: GovDealsAdapter,
                              items: list[tuple[str, tuple[int, int, int]]]) -> list[Observation]:
        """Pre-bidbox 'gone' detection, now reached only when the bidbox
        answered 204: corroborate with `fetch_detail`, capped, with the
        batch-level "gone_unverified" guard (fix rounds 1-2, docstring)."""
        observations: list[Observation] = []
        corroboration_calls = 0
        corroboration_cap_warned = False
        # Corroboration verdicts collected in this pass BEFORE any of them
        # are trusted (fix round 2, review finding): the "gone_unverified"
        # batch guard needs to see the WHOLE batch's corroboration outcome
        # first, exactly like public_surplus.py's 401 guard.
        pending: list[tuple[str, str, dict | None, int, int]] = []  # (k, verdict, payload, asset_id, account_id)

        for k, parsed in items:
            asset_id, account_id, _auction_id = parsed
            if corroboration_calls >= CORROBORATION_CAP_PER_BATCH:
                if not corroboration_cap_warned:
                    print(
                        f"[govdeals] WARNING: poll() corroboration cap "
                        f"({CORROBORATION_CAP_PER_BATCH}) hit this batch — remaining "
                        "absent-past-end-date lot(s) fall back to absence-alone 'gone' "
                        "detection (no per-lot detail corroboration) for this round"
                    )
                    corroboration_cap_warned = True
                observations.append(Observation(
                    source=self.SOURCE,
                    source_lot_id=k,
                    status="gone",
                    raw={"recorder_probe": {
                        "result": "not_found",
                        "http_status": 200,
                        "url": "govdeals-maestro-search-refetch",
                    }},
                ))
                continue

            corroboration_calls += 1
            verdict, payload = _corroborate_absence(adapter, asset_id, account_id)
            pending.append((k, verdict, payload, asset_id, account_id))

        # Fix round 2 (review finding): batch-level suspected-systemic-event
        # guard over this round's "gone_unverified" (JSONDecodeError) verdicts
        # — mirrors public_surplus.py's 401 block guard. A single ambiguous
        # empty/unparseable body is still trusted (the common, legitimate
        # live-204 case); several in the same batch looks more like a WAF/
        # anti-bot interstitial or outage than several simultaneous real
        # absences, and 'gone' is append-only-unrecoverable if wrong.
        unverified_count = sum(1 for _, verdict, *_r in pending if verdict == "gone_unverified")
        total_corroborations = len(pending)
        suspected_event = (
            total_corroborations > 0
            and unverified_count >= CORROBORATION_BLOCK_MIN_COUNT
            and (unverified_count / total_corroborations) >= CORROBORATION_BLOCK_MIN_FRACTION
        )
        if suspected_event:
            print(
                f"[govdeals] RECORDER ERROR: poll() suspects a systemic corroboration "
                f"failure — {unverified_count}/{total_corroborations} corroboration "
                f"attempt(s) in this batch returned an empty/unparseable body (threshold: "
                f">= {CORROBORATION_BLOCK_MIN_COUNT} AND >= "
                f"{CORROBORATION_BLOCK_MIN_FRACTION:.0%}). A real per-lot 204 doesn't-exist "
                "signal is rare and independent — this many at once looks like a WAF/anti-"
                "bot interstitial or an outage hitting the corroboration endpoint broadly, "
                "not simultaneous real absences. Suppressing ALL 'gone' observations "
                "derived from these unverified signals this round; explicit closed/active/"
                "clean-gone verdicts from the same batch still stand."
            )

        for k, verdict, payload, asset_id, account_id in pending:
            if verdict == "active":
                observations.append(Observation(
                    source=self.SOURCE,
                    source_lot_id=k,
                    status="active",
                    raw=payload,
                    current_bid=None,  # fetch_detail carries no pricing fields — see docstring
                    bid_count=None,
                    end_date=_parse_detail_end_date(payload.get("assetAuctionEndDate")),
                ))
            elif verdict == "closed":
                observations.append(Observation(
                    source=self.SOURCE,
                    source_lot_id=k,
                    status="closed",
                    raw=payload,
                    current_bid=None,
                    bid_count=None,
                    end_date=_parse_detail_end_date(payload.get("assetAuctionEndDate")),
                ))
            elif verdict == "gone":
                observations.append(Observation(
                    source=self.SOURCE,
                    source_lot_id=k,
                    status="gone",
                    raw={"recorder_probe": {
                        "result": "not_found",
                        "http_status": 200,
                        "url": f"govdeals-maestro-detail/{asset_id}/{account_id}",
                    }},
                ))
            elif verdict == "gone_unverified":
                if suspected_event:
                    continue  # suppressed — see the batch-level warning above
                observations.append(Observation(
                    source=self.SOURCE,
                    source_lot_id=k,
                    status="gone",
                    raw={"recorder_probe": {
                        "result": "not_found",
                        "http_status": 204,
                        "url": f"govdeals-maestro-detail/{asset_id}/{account_id}",
                    }},
                ))
            # verdict == "unknown": corroboration itself failed for real
            # reasons (network/HTTP error) — emit NOTHING for this lot this
            # round, per the fetch-failure rule (loud error already printed
            # by _corroborate_absence).
        return observations

    def sold_sweep(self) -> list[Observation]:
        return []

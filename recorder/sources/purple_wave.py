"""Purple Wave source adapter — official www.purplewave.com JSON search API.

LIVE RECON (verified 2026-07-31, this task):

- The brief's hinted host `api.purplewave.com` is dead: every path (including
  `/`, `/robots.txt`, `/v1/search/search`) returns HTTP 503 "Service
  Temporarily Unavailable" from an openresty/APISIX gateway. That is a hard
  failure, not an anti-bot response — no page on that host responds.
- The REAL API lives on the main site's own domain:
  `GET https://www.purplewave.com/v1/search/search`. Confirmed by loading
  `https://www.purplewave.com/search?q=chairs` and reading the shipped bundle
  `https://scripts.purplewave.com/js/1.222.1/combined.js`, which defines the
  frontend's own Backbone collection with `url = "/v1/search/search"`.
  `www.purplewave.com/robots.txt` disallows `/v1/` for crawlers (a common SPA
  pattern — keeping raw JSON responses out of search-engine indexes), but this
  is the site's own public API with no auth and no anti-bot challenge
  encountered anywhere on this host; `q=<text>` is NOT a working free-text
  filter (confirmed live — it's silently ignored, same 5 items returned
  regardless of `q` value). The one real filter param is `filters=<key>:<value>`.
- Category id: grepping `combined.js` for its static category tree found
  `industry:"business_personal", industry_category_id:423` with a nested
  `family:"furniture", family_category_id:646`. Verified live 2026-07-31:
  `?perPage=500&filters=family_category_id:646` returned exactly 38 items,
  matching the "Furniture (38)" facet count rendered in the search page's own
  sidebar at the same moment. Chosen over `FURNITURE_TERMS` text-matching —
  Purple Wave's own curated taxonomy catches lots a naive "chairs"/"seating"
  keyword search would miss (observed items included "(3) bleachers" and
  "DeBough MFG. CO lockers"). `FURNITURE_TERMS` is imported per the shared
  contract but unused here; the category id is the precise filter.
- Sold/closed lots (CORRECTED — see BLACKWHOLE-28 whole-branch review
  CRITICAL 1, re-verified live 2026-07-31): `dateType=past` ALONE is not
  enough. The site is an 18-year archive and its default sort is
  oldest-first — `?perPage=500&filters=family_category_id:646&dateType=past`
  returns lots from **2008** with `sold="No"`, `current_bid=0`,
  `bid_count=0`: zero usable comps. Two more params are required:
  1. `sold:Yes` appended to `filters` with a **semicolon** join (confirmed
     live — `,` was not tested first because `;` worked on the first try
     and was verified to actually narrow results, not silently no-op like
     `q=` and `filters=id:<n>` do elsewhere on this API):
     `filters=family_category_id:646;sold:Yes`.
  2. `dateRanges=<year>,<year-1>` (computed at call time from
     `datetime.now(timezone.utc).year`, e.g. `dateRanges=2026,2025` on
     2026-07-31) — narrows to the last ~2 years instead of the full archive.
  Combined and verified live 2026-07-31:
  `?perPage=500&filters=family_category_id:646;sold:Yes&dateType=past&dateRanges=2026,2025`
  returned **500/500** items with `closed=1`, `sold="Yes"`, a priced
  `current_bid`, and `bid_count>0` (dates spanning 2025-01-07 to
  2026-04-28) — this is the `api_final` capture method (a real winning bid,
  not a snapshot). `sold_sweep()` builds both params on every call; a
  fetch that comes back with 0 rows having `bid_count>0` prints a loud
  WARNING (harvest sanity check — see `sold_sweep()`).
- No confirmed single-item detail endpoint exists (`filters=id:<n>` is
  silently ignored by the API — same no-op as `q`). `poll()` therefore re-runs
  the same `family_category_id:646` sweep (current volume is tiny, ~38 active
  items, so `perPage=500` always covers it in one page) and matches tracked
  ids against the fresh result set.
- Money: `current_bid` comes back as either a decimal string ("55.00") or a
  bare number (325) depending on the item — always parsed via
  `Decimal(str(value))`.
- Dates: `auction_timestamp` is already ISO-8601 UTC with a trailing "Z"
  (e.g. "2026-08-04T16:00:00.000Z"), parsed directly by
  `datetime.fromisoformat` (Python 3.11+ handles the "Z" suffix natively).
  Falls back to the `endtime`/`auction_endtime` unix-seconds field if
  `auction_timestamp` is ever absent.
- `source_lot_id` = `str(item["id"])`, Purple Wave's own global numeric item
  id — matches the `purplewave-{id}` pattern already used elsewhere in this
  project's research (`docs/blackwhole-28/research/R2-data-model.md`).

poll() gone semantics (fix round 1): `_fetch_search()` returns `None` to mean
"the fetch itself failed" (network exception, blocked, non-200, bad JSON, bad
shape) — distinct from a healthy fetch that returns an empty or
non-matching list. `poll()` treats a failed fetch as "no information this
round": it emits NO observations at all (not even 'gone' ones) and prints a
loud `RECORDER ERROR` line, so a transient outage is never misrecorded as
every tracked lot vanishing (append-only — that mistake would be permanent).
Only a HEALTHY fetch in which a tracked lot's id is absent, AND that lot's
own `end_date` (supplied by the caller via `store.tracked_active()`'s row) is
not None and has already passed, produces a `status='gone'` observation. A
lot absent from a healthy fetch but not yet past its end_date (or with an
unknown end_date) emits nothing this round — it's retried on the next due
poll. This corrects the original cut of this adapter, which inferred 'gone'
from mere absence regardless of end_date or fetch health, violating
task-2-brief.md:24.

Fixtures captured live 2026-07-31 under `tests/recorder/fixtures/purple_wave/`
(trimmed to 6 items each — full untouched item dicts, just a shorter list):
`discover_furniture.json` (the family_category_id:646 active sweep) and
`sold_sweep.json` (the same sweep + `dateType=past`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, polite_get  # noqa: F401  (FURNITURE_TERMS kept per contract; unused — see docstring)

SOURCE = "purple_wave"

SEARCH_URL = "https://www.purplewave.com/v1/search/search"

# "Furniture" family (under industry "Business and Personal Property", id 423),
# resolved from combined.js's static category tree and confirmed live — see
# module docstring.
FURNITURE_FAMILY_CATEGORY_ID = 646

# Current ACTIVE furniture volume is tiny (~38 items, observed 2026-07-31),
# so one page always covers discover()/poll(). sold_sweep() draws from the
# much larger historical pool, though, and DOES hit this cap live (returned
# exactly 500 on a 2026-07-31 live smoke test) — no pagination implemented
# for either path; sold_sweep() is a best-effort recent-past sample, not a
# complete historical backfill. Revisit if discover()/poll() ever needs more
# than one page.
SEARCH_PER_PAGE = 500


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


def _parse_end_date(item: dict) -> datetime | None:
    ts = item.get("auction_timestamp")
    if ts:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            pass
    epoch = item.get("endtime") or item.get("auction_endtime")
    if epoch is not None:
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    return None


def _status_of(item: dict) -> str:
    return "closed" if item.get("closed") in (1, True, "1") else "active"


def _to_observation(item: dict, *, status: str | None = None) -> Observation | None:
    lot_id = item.get("id")
    if lot_id is None:
        print(f"[purple_wave] skipping item with no 'id': {item.get('item')!r}")
        return None
    return Observation(
        source=SOURCE,
        source_lot_id=str(lot_id),
        status=status or _status_of(item),
        raw=item,
        current_bid=_parse_money(item.get("current_bid")),
        bid_count=_int_or_none(item.get("bid_count")),
        end_date=_parse_end_date(item),
    )


def _fetch_search(*, extra_params: dict | None = None, extra_filters: str | None = None) -> list[dict] | None:
    """Run the family_category_id:646 search. Returns None on ANY fetch
    failure (network exception, blocked, non-200, bad JSON, unexpected shape)
    — never an empty list, so callers can tell "fetch failed" apart from
    "fetch succeeded and there's genuinely nothing there." See module
    docstring's "poll() gone semantics" for why this distinction matters.

    `extra_filters` is appended to the `filters` param with a semicolon join
    (the API's multi-filter separator — confirmed live, see module docstring's
    "Sold/closed lots" section), e.g. "sold:Yes".
    """
    filters = f"family_category_id:{FURNITURE_FAMILY_CATEGORY_ID}"
    if extra_filters:
        filters = f"{filters};{extra_filters}"
    params = {
        "perPage": SEARCH_PER_PAGE,
        "filters": filters,
    }
    if extra_params:
        params.update(extra_params)
    try:
        resp = polite_get(SEARCH_URL, params=params)
    except requests.exceptions.RequestException as e:
        print(f"[purple_wave] RECORDER ERROR: request failed: {e}")
        return None
    if resp.status_code in (403, 429):
        print(f"[purple_wave] RECORDER ERROR: blocked HTTP {resp.status_code} on {resp.url} — backing off, no data this round")
        return None
    if resp.status_code != 200:
        print(f"[purple_wave] RECORDER ERROR: unexpected HTTP {resp.status_code} on {resp.url}")
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"[purple_wave] RECORDER ERROR: non-JSON response from {resp.url}")
        return None
    if not isinstance(data, list):
        print(f"[purple_wave] RECORDER ERROR: unexpected response shape (not a list) from {resp.url}")
        return None
    return data


class PurpleWaveSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        items = _fetch_search()
        if items is None:
            print("[purple_wave] RECORDER ERROR: discover() aborted — fetch failed, 0 observations")
            return []
        out = [_to_observation(it) for it in items]
        result = [o for o in out if o is not None]
        if not result:
            print(
                f"[purple_wave] WARNING: discover() found 0 furniture observations "
                f"out of {len(items)} raw items — check family_category_id for drift"
            )
        return result

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        items = _fetch_search()
        if items is None:
            print(
                f"[purple_wave] RECORDER ERROR: poll() fetch failed — skipping "
                f"gone-detection this round for {len(lots)} tracked lot(s), "
                f"emitting no observations"
            )
            return []
        now = datetime.now(timezone.utc)
        by_id = {str(it["id"]): it for it in items if it.get("id") is not None}
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
                    raw={"recorder_probe": {"result": "not_found", "http_status": 200, "url": SEARCH_URL}},
                ))
            # else: absent from a healthy fetch but not yet past end_date (or
            # end_date unknown) — emit nothing this round, retried later.
        return observations

    def sold_sweep(self) -> list[Observation]:
        # CRITICAL 1 fix (BLACKWHOLE-28 whole-branch review): dateType=past
        # alone sweeps the full 18-year archive oldest-first — 0 usable comps.
        # sold:Yes (filters, semicolon-joined) + dateRanges=<year>,<year-1>
        # (computed here, not hardcoded) narrow it to actually-sold, recent
        # lots. See module docstring's "Sold/closed lots" section.
        now = datetime.now(timezone.utc)
        date_ranges = f"{now.year},{now.year - 1}"
        items = _fetch_search(
            extra_filters="sold:Yes",
            extra_params={"dateType": "past", "dateRanges": date_ranges},
        )
        if items is None:
            print("[purple_wave] RECORDER ERROR: sold_sweep() aborted — fetch failed, 0 observations")
            return []
        out = [_to_observation(it, status="closed") for it in items]
        result = [o for o in out if o is not None]
        priced = sum(1 for o in result if (o.bid_count or 0) > 0)
        if priced == 0:
            print(
                f"[purple_wave] WARNING: sold_sweep() returned {len(result)} closed "
                f"observation(s) but 0 have bid_count>0 — check the sold:Yes / "
                f"dateRanges={date_ranges} params for drift (harvest sanity check)"
            )
        return result

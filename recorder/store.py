"""Persistence layer for the closing-price recorder — Supabase `listing_snapshots`.

Append-only: this module INSERTs rows and nothing else. No UPDATE, no DELETE.
All SQL lives here; sources (recorder/sources/*.py) never touch the DB
directly — they produce `Observation`s and hand them to `insert_observations`.

`sold_comps` is a derived VIEW (see scripts/sql/004_listing_snapshots.sql) —
recomputable from snapshots, never written to directly.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Iterable

import psycopg

from automation import db

from recorder.models import Observation

INSERT_SQL = """
INSERT INTO listing_snapshots
    (source, source_lot_id, status, current_bid, bid_count, end_date, raw, observed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s, now()))
"""

# `status` filter is applied in the OUTER query, never inside the DISTINCT ON
# subquery: filtering inside DISTINCT ON would return a stale 'active' row
# for a lot whose latest snapshot has since flipped to closed/gone — exactly
# the bug the recorder exists to avoid.
#
# CRITICAL 2 fix (BLACKWHOLE-28 whole-branch review): Postgres now() is fixed
# for the whole transaction, and a discover+sold_sweep batch is one
# executemany() call — every row inserted in that batch gets the IDENTICAL
# observed_at. `ORDER BY ... observed_at DESC` alone is then nondeterministic
# among same-batch rows for the same lot: which row "wins" as latest is
# arbitrary, so a closed lot could resurrect into the poll set (or an active
# one could silently drop out) depending on row order Postgres happens to
# pick. `id DESC` breaks the tie deterministically — BIGSERIAL id always
# reflects true insert order, even within one transaction.
#
# MINOR (whole-branch review): `{source_filter}` below is filled in by
# `.format()` in `tracked_active()`, but that's safe — the spliced-in text is
# always one of two hardcoded constants this module chooses ("" or "WHERE
# source = %s"), never caller/user input. The actual `source` VALUE still
# flows through the driver via that `%s` placeholder, parameterized exactly
# like every other query in this file — `.format()` only ever assembles SQL
# *shape*, never SQL *values*.
_TRACKED_ACTIVE_SQL = """
SELECT source, source_lot_id, observed_at, end_date, current_bid, bid_count
FROM (
    SELECT DISTINCT ON (source, source_lot_id)
           source, source_lot_id, observed_at, end_date, current_bid, bid_count, status
    FROM listing_snapshots
    {source_filter}
    ORDER BY source, source_lot_id, observed_at DESC, id DESC
) latest
WHERE status = 'active'
ORDER BY source, source_lot_id
"""

_COVERAGE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source, source_lot_id)
           source, source_lot_id, end_date
    FROM listing_snapshots
    ORDER BY source, source_lot_id, observed_at DESC, id DESC
), closed_window AS (
    SELECT source, source_lot_id, end_date
    FROM latest
    WHERE end_date IS NOT NULL
      AND end_date BETWEEN now() - make_interval(days => %s) AND now()
), covered AS (
    SELECT cw.source, cw.source_lot_id,
           EXISTS (
               SELECT 1 FROM listing_snapshots s
               WHERE s.source = cw.source AND s.source_lot_id = cw.source_lot_id
                 AND s.observed_at > cw.end_date
           ) AS is_covered
    FROM closed_window cw
)
SELECT source,
       COUNT(*) AS closed_lots,
       COUNT(*) FILTER (WHERE is_covered) AS covered,
       COUNT(*) FILTER (WHERE NOT is_covered) AS missed
FROM covered
GROUP BY source
ORDER BY source
"""


def _require_aware(value: datetime | None, field: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(
            f"recorder.store: {field} must be tz-aware UTC (got naive datetime {value!r})"
        )


def observation_row(o: Observation) -> tuple:
    """Map an Observation to the positional params for INSERT_SQL."""
    _require_aware(o.end_date, "end_date")
    _require_aware(o.observed_at, "observed_at")
    return (
        o.source,
        o.source_lot_id,
        o.status,
        o.current_bid,
        o.bid_count,
        o.end_date,
        json.dumps(o.raw, default=str),
        o.observed_at,
    )


def insert_observations(obs: Iterable[Observation]) -> int:
    """Append every observation as a new row. Returns the count inserted.

    Deliberately dumb: it inserts exactly what it is handed. Deciding what is
    worth keeping is `filter_changed`'s job, so this stays a straight
    append-only writer and the gating logic stays pure and testable.
    """
    rows = [observation_row(o) for o in obs]
    if not rows:
        return 0
    db.executemany(INSERT_SQL, rows)
    return len(rows)


# Latest known state per lot, for change-gating. Same DISTINCT ON + `id DESC`
# tie-break as _TRACKED_ACTIVE_SQL, and for the same reason: a whole batch
# shares one now(), so observed_at alone does not order rows within it.
_LATEST_STATE_SQL = """
SELECT DISTINCT ON (source, source_lot_id)
       source, source_lot_id, status, current_bid, bid_count, end_date
FROM listing_snapshots
WHERE source = %s AND source_lot_id = ANY(%s)
ORDER BY source, source_lot_id, observed_at DESC, id DESC
"""

_GATED_FIELDS = ("status", "current_bid", "bid_count", "end_date")


def is_changed(obs: Observation, prev: dict | None) -> bool:
    """Is this observation worth storing? Pure — no DB, no clock beyond `obs`.

    `discover()` has no idea whether it has seen a lot before, so every
    stale-refresh re-INSERTed a full row for every active lot even when nothing
    moved. Measured 2026-08-28: 31,383 of 47,115 rows were interior duplicates.

    Three reasons to keep a row, and the third is the subtle one:

    1. No previous row — the first sighting is always evidence.
    2. Any gated field differs. `current_bid` is Decimal and `end_date` is
       tz-aware; both compare correctly against the DB's own values, and a
       None/non-None flip counts as a change.
    3. **The observation is after the lot's end_date.** Post-close evidence must
       always land even when every field looks identical, because `coverage()`
       decides whether a close was *caught* by looking for a snapshot past
       end_date. Gate that away and a caught close is misreported as missed —
       which would make the recorder lie about the one thing it exists to do.
    """
    if prev is None:
        return True
    for f in _GATED_FIELDS:
        if getattr(obs, f) != prev.get(f):
            return True
    end = prev.get("end_date") or obs.end_date
    if end is not None and obs.observed_at is not None and obs.observed_at > end:
        return True
    return False


def filter_changed(obs: Iterable[Observation]) -> list[Observation]:
    """Drop observations that repeat the lot's latest stored state verbatim.

    One query per source rather than per lot. An observation with no
    `observed_at` (the common case — the DB stamps now()) cannot be compared
    against end_date, so rule 3 in `is_changed` simply does not fire for it;
    that is safe, because a genuine close also flips `status`, which rule 2
    catches.
    """
    obs = list(obs)
    if not obs:
        return []

    by_source: dict[str, list[Observation]] = {}
    for o in obs:
        by_source.setdefault(o.source, []).append(o)

    latest: dict[tuple[str, str], dict] = {}
    for source, group in by_source.items():
        ids = sorted({o.source_lot_id for o in group})
        for row in db.fetch_all(_LATEST_STATE_SQL, (source, ids)):
            latest[(row["source"], row["source_lot_id"])] = row

    return [o for o in obs
            if is_changed(o, latest.get((o.source, o.source_lot_id)))]


def tracked_active(source: str | None = None) -> list[dict]:
    """Latest snapshot per (source, source_lot_id), filtered to status == 'active'."""
    if source is not None:
        sql = _TRACKED_ACTIVE_SQL.format(source_filter="WHERE source = %s")
        rows = db.fetch_all(sql, (source,))
    else:
        sql = _TRACKED_ACTIVE_SQL.format(source_filter="")
        rows = db.fetch_all(sql)
    return list(rows)


def table_size_pretty() -> str:
    """Pretty-printed on-disk size of `listing_snapshots` (IMPORTANT 6 —
    storage-size visibility; discover() is change-gated via `filter_changed`
    since 2026-08-28, and the coverage report is still where the operator
    will notice a growth problem first)."""
    row = db.fetch_one(
        "SELECT pg_size_pretty(pg_total_relation_size('listing_snapshots')) AS size"
    )
    return row["size"] if row else "unknown"


def newest_observed_at(source: str) -> datetime | None:
    """Most recent observed_at for a source, or None if nothing recorded yet."""
    row = db.fetch_one(
        "SELECT MAX(observed_at) AS max_observed_at FROM listing_snapshots WHERE source = %s",
        (source,),
    )
    return row["max_observed_at"] if row else None


def coverage(days: int = 7) -> list[dict]:
    """Per-source coverage over the last `days`: closed lots vs. how many were
    caught by a post-close observation. Plus an `_all` roll-up row.

    A lot "counts closed" when its latest-known end_date fell in the window
    (now() - days .. now()). It "counts covered" when any observation exists
    with observed_at > end_date (i.e. we caught the close).
    """
    rows = db.fetch_all(_COVERAGE_SQL, (days,))
    result: list[dict[str, Any]] = []
    total_closed = total_covered = total_missed = 0
    for r in rows:
        closed, covered, missed = r["closed_lots"], r["covered"], r["missed"]
        pct = round(100.0 * covered / closed, 1) if closed else 0.0
        result.append({
            "source": r["source"], "closed_lots": closed,
            "covered": covered, "missed": missed, "pct": pct,
        })
        total_closed += closed
        total_covered += covered
        total_missed += missed
    total_pct = round(100.0 * total_covered / total_closed, 1) if total_closed else 0.0
    result.append({
        "source": "_all", "closed_lots": total_closed,
        "covered": total_covered, "missed": total_missed, "pct": total_pct,
    })
    return result


# --- GovDeals bidbox finals (2026-09-28) -------------------------------------
#
# Both queries are reads. `raw->'recorder_capture'->>'method'` is written by
# recorder/sources/govdeals.py::bidbox_observation — the recorder's verdict
# lives in raw beside the untouched payload, so no new column was needed.

# `couldn't get a connection` = psycopg_pool.PoolTimeout (an OperationalError),
# which is how a full pooler surfaces through automation/db.py's pool.
_POOL_FULL_MARKERS = ("max clients", "too many clients", "remaining connection slots",
                      "couldn't get a connection")


def _read_with_backoff(fn, *args, attempts: int = 4, base_delay: float = 2.0):
    """Run a read, backing off when the Supabase session pooler is full
    ("max clients reached"). Anything else raises straight through."""
    import time

    import psycopg

    for i in range(attempts):
        try:
            return fn(*args)
        except psycopg.OperationalError as e:
            if i == attempts - 1 or not any(m in str(e).lower() for m in _POOL_FULL_MARKERS):
                raise
            delay = base_delay * (2 ** i)
            print(f"recorder.store: pooler full, retrying in {delay:.0f}s ({e})")
            time.sleep(delay)


# SOA finals whose latest row is still that final and is >= the recheck age.
# A re-check writes a newer row (always — it is the done-marker), so a lot
# drops out of this set after exactly one re-check.
_SOA_RECHECK_DUE_SQL = """
SELECT source_lot_id, observed_at, current_bid, bid_count, end_date,
       raw->'recorder_capture'->>'status_code' AS status_code
FROM (
    SELECT DISTINCT ON (source_lot_id) *
    FROM listing_snapshots
    WHERE source = 'govdeals'
    ORDER BY source_lot_id, observed_at DESC, id DESC
) latest
WHERE status = 'closed'
  AND raw->'recorder_capture'->>'method' = 'bidbox_final'
  AND raw->'recorder_capture'->>'status_code' = ANY(%s)
  AND observed_at <= now() - make_interval(secs => %s)
ORDER BY observed_at
LIMIT %s
"""


def soa_recheck_due(codes: list[str], older_than_seconds: float, limit: int) -> list[dict]:
    return list(_read_with_backoff(
        db.fetch_all, _SOA_RECHECK_DUE_SQL, (list(codes), older_than_seconds, limit)))


# GovDeals lots whose last known clock fell in the window, that are no longer
# being polled (latest row closed/gone), and that have never had a bidbox
# final. `last_price` is what sold_comps reports for them today.
_FINALS_BACKFILL_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, status
    FROM listing_snapshots WHERE source = %s
    ORDER BY source_lot_id, observed_at DESC, id DESC
), ends AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, end_date
    FROM listing_snapshots WHERE source = %s AND end_date IS NOT NULL
    ORDER BY source_lot_id, observed_at DESC, id DESC
), priced AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, current_bid, bid_count
    FROM listing_snapshots WHERE source = %s AND current_bid IS NOT NULL
    ORDER BY source_lot_id, observed_at DESC, id DESC
)
SELECT l.source_lot_id, l.status, e.end_date,
       p.current_bid AS last_price, p.bid_count AS last_bid_count
FROM latest l
JOIN ends e USING (source_lot_id)
LEFT JOIN priced p USING (source_lot_id)
WHERE l.status IN ('closed', 'gone')
  AND e.end_date >= now() - make_interval(days => %s)
  AND e.end_date < now()
  AND NOT EXISTS (
      SELECT 1 FROM listing_snapshots s
      WHERE s.source = %s AND s.source_lot_id = l.source_lot_id
        AND s.raw->'recorder_capture'->>'method' IN ('bidbox_final', 'bidbox_recheck')
  )
ORDER BY e.end_date DESC
LIMIT %s
"""


def finals_backfill_candidates(source: str, since_days: int, limit: int) -> list[dict]:
    return list(_read_with_backoff(
        db.fetch_all, _FINALS_BACKFILL_SQL,
        (source, source, source, since_days, source, limit)))

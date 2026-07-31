"""Persistence layer for the closing-price recorder — Supabase `listing_snapshots`.

Append-only: this module INSERTs rows and nothing else. No UPDATE, no DELETE.
All SQL lives here; sources (recorder/sources/*.py) never touch the DB
directly — they produce `Observation`s and hand them to `insert_observations`.

`sold_comps` is a derived VIEW (see scripts/sql/004_listing_snapshots.sql) —
recomputable from snapshots, never written to directly.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

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
_TRACKED_ACTIVE_SQL = """
SELECT source, source_lot_id, observed_at, end_date, current_bid, bid_count
FROM (
    SELECT DISTINCT ON (source, source_lot_id)
           source, source_lot_id, observed_at, end_date, current_bid, bid_count, status
    FROM listing_snapshots
    {source_filter}
    ORDER BY source, source_lot_id, observed_at DESC
) latest
WHERE status = 'active'
ORDER BY source, source_lot_id
"""

_COVERAGE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source, source_lot_id)
           source, source_lot_id, end_date
    FROM listing_snapshots
    ORDER BY source, source_lot_id, observed_at DESC
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
    """Append every observation as a new row. Returns the count inserted."""
    rows = [observation_row(o) for o in obs]
    if not rows:
        return 0
    db.executemany(INSERT_SQL, rows)
    return len(rows)


def tracked_active(source: str | None = None) -> list[dict]:
    """Latest snapshot per (source, source_lot_id), filtered to status == 'active'."""
    if source is not None:
        sql = _TRACKED_ACTIVE_SQL.format(source_filter="WHERE source = %s")
        rows = db.fetch_all(sql, (source,))
    else:
        sql = _TRACKED_ACTIVE_SQL.format(source_filter="")
        rows = db.fetch_all(sql)
    return list(rows)


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

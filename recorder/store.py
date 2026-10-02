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
    WHERE source = %s
    ORDER BY source_lot_id, observed_at DESC, id DESC
) latest
WHERE status = 'closed'
  AND raw->'recorder_capture'->>'method' = 'bidbox_final'
  AND raw->'recorder_capture'->>'status_code' = ANY(%s)
  AND observed_at <= now() - make_interval(secs => %s)
ORDER BY observed_at
LIMIT %s
"""


def soa_recheck_due(codes: list[str], older_than_seconds: float, limit: int,
                    source: str = "govdeals") -> list[dict]:
    return list(_read_with_backoff(
        db.fetch_all, _SOA_RECHECK_DUE_SQL, (source, list(codes), older_than_seconds, limit)))


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


# --- lot archive (2026-09-29) -------------------------------------------------
#
# Reads only. The archive itself lives in R2 (recorder/lot_archive.py); the
# optional `lot_archive` index table is migration 015 (PENDING) — every caller
# here degrades to "no index" until it is applied.

# GovDeals lots whose last known clock fell in the window and has passed, that
# are closed/gone — or still 'active' a day after their clock (a missed
# confirming poll) — newest close first. `bidbox` is the recorder's own stored
# bidbox payload when the lot has a bidbox_final row, so the archiver can skip
# that read.
_ARCHIVE_CANDIDATES_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, status
    FROM listing_snapshots WHERE source = %(source)s
    ORDER BY source_lot_id, observed_at DESC, id DESC
), ends AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, end_date
    FROM listing_snapshots WHERE source = %(source)s AND end_date IS NOT NULL
    ORDER BY source_lot_id, observed_at DESC, id DESC
), finals AS (
    SELECT DISTINCT ON (source_lot_id) source_lot_id, raw->'bidbox' AS bidbox
    FROM listing_snapshots
    WHERE source = %(source)s AND status = 'closed'
      AND raw->'recorder_capture'->>'method' IN ('bidbox_final', 'bidbox_recheck')
      AND raw ? 'bidbox'
    ORDER BY source_lot_id, observed_at DESC, id DESC
)
SELECT l.source_lot_id, l.status, e.end_date, f.bidbox
FROM latest l
JOIN ends e USING (source_lot_id)
LEFT JOIN finals f USING (source_lot_id)
WHERE e.end_date >= now() - make_interval(days => %(since_days)s)
  AND e.end_date < now() - make_interval(secs => %(min_age)s)
  AND (l.status IN ('closed', 'gone') OR e.end_date < now() - interval '1 day')
  {not_indexed}
ORDER BY e.end_date DESC
LIMIT %(limit)s
"""

_NOT_INDEXED = """AND NOT EXISTS (SELECT 1 FROM lot_archive a
                   WHERE a.source = %(source)s AND a.lot_key = l.source_lot_id)"""


def lot_archive_index_exists() -> bool:
    row = _read_with_backoff(db.fetch_one, "SELECT to_regclass('lot_archive') AS reg")
    return bool(row and row.get("reg"))


def archive_candidates(since_days: int, limit: int, min_age_seconds: float = 3600,
                       use_index: bool | None = None, source: str = "govdeals") -> list[dict]:
    if use_index is None:
        use_index = lot_archive_index_exists()
    sql = _ARCHIVE_CANDIDATES_SQL.format(not_indexed=_NOT_INDEXED if use_index else "")
    return list(_read_with_backoff(db.fetch_all, sql, {
        "source": source, "since_days": since_days, "min_age": min_age_seconds, "limit": limit}))


_LOT_TIMELINE_SQL = """
SELECT observed_at, status, current_bid, bid_count, end_date,
       raw->'recorder_capture'->>'method' AS method,
       raw->'recorder_capture'->>'status_code' AS status_code
FROM listing_snapshots
WHERE source = %s AND source_lot_id = %s
ORDER BY observed_at, id
"""

# The richest maestro search payload we kept for the lot (discover rows carry
# the untouched search asset; poll rows carry a Snapshot asdict).
_LOT_SEARCH_RAW_SQL = """
SELECT raw FROM listing_snapshots
WHERE source = %s AND source_lot_id = %s AND raw ? 'assetShortDescription'
ORDER BY observed_at DESC, id DESC LIMIT 1
"""

_LOT_BID_OBS_SQL = """
SELECT observed_at, bid_count, current_bid, high_bidder_username, visitors, hits,
       watcher_count, end_utc, status
FROM deal_bid_observations
WHERE asset_id = %s AND account_id = %s AND auction_id = %s
ORDER BY observed_at
"""

_DEAL_LOT_SQL = """
SELECT title, description, native_category_id, native_category_name, seller, city,
       state, zip, lat, lng, end_utc, opening_bid, currency_code, first_seen_at
FROM deal_lots WHERE site = 'govdeals' AND asset_id = %s AND account_id = %s AND auction_id = %s
"""


def _num(v):
    return None if v is None else float(v)


def lot_timeline(lot_key: str, source: str = "govdeals") -> tuple[list[dict], dict | None]:
    """(timeline, search_raw) for one GovDeals lot from our own tables.
    Timeline points: {t, source, status, current_bid, bid_count, end_date,
    method, high_bidder}. search_raw falls back to a maestro-shaped dict
    rebuilt from `deal_lots` scalars when no snapshot kept the search asset."""
    a, b, c = (int(p) for p in lot_key.split("/"))
    points: list[dict] = []
    for r in _read_with_backoff(db.fetch_all, _LOT_TIMELINE_SQL, (source, lot_key)):
        points.append({"t": r["observed_at"], "source": "recorder", "status": r["status"],
                       "current_bid": _num(r["current_bid"]), "bid_count": r["bid_count"],
                       "end_date": r["end_date"], "method": r["method"],
                       "status_code": r["status_code"]})
    try:
        for r in _read_with_backoff(db.fetch_all, _LOT_BID_OBS_SQL, (a, b, c)):
            points.append({"t": r["observed_at"], "source": "bidbox_sample", "status": r["status"],
                           "current_bid": _num(r["current_bid"]), "bid_count": r["bid_count"],
                           "end_date": r["end_utc"], "high_bidder": r["high_bidder_username"],
                           "visitors": r["visitors"], "hits": r["hits"],
                           "watchers": r["watcher_count"]})
    except psycopg.Error as e:   # optional table — never lose the lot over it
        print(f"recorder.store: deal_bid_observations unreadable ({e})")
    points.sort(key=lambda p: p["t"])

    row = _read_with_backoff(db.fetch_one, _LOT_SEARCH_RAW_SQL, (source, lot_key))
    search_raw = row["raw"] if row else None
    if search_raw is None:
        d = _read_with_backoff(db.fetch_one, _DEAL_LOT_SQL, (a, b, c))
        if d:
            search_raw = {
                "assetShortDescription": d["title"], "assetLongDescription": d["description"],
                "assetCategory": d["native_category_id"], "categoryDescription": d["native_category_name"],
                "companyName": d["seller"], "locationCity": d["city"], "locationState": d["state"],
                "locationZip": d["zip"], "latitude": d["lat"], "longitude": d["lng"],
                "assetAuctionEndDateUtc": d["end_utc"].isoformat() if d["end_utc"] else None,
                "assetBidPrice": _num(d["opening_bid"]), "currencyCode": d["currency_code"],
                "_rebuilt_from": "deal_lots",
            }
    return points, search_raw


_INDEX_UPSERT_SQL = """
INSERT INTO lot_archive (source, lot_key, title, canonical_category, category_name, city, state,
                         seller, closed_at, final_price, bid_count, outcome, status_code,
                         photo_count, completeness, archived_at{currency_col})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s{currency_val})
ON CONFLICT (source, lot_key) DO UPDATE SET{currency_set}
    title = EXCLUDED.title, canonical_category = EXCLUDED.canonical_category,
    category_name = EXCLUDED.category_name, city = EXCLUDED.city, state = EXCLUDED.state,
    seller = EXCLUDED.seller, closed_at = EXCLUDED.closed_at, final_price = EXCLUDED.final_price,
    bid_count = EXCLUDED.bid_count, outcome = EXCLUDED.outcome, status_code = EXCLUDED.status_code,
    photo_count = EXCLUDED.photo_count, completeness = EXCLUDED.completeness,
    archived_at = EXCLUDED.archived_at
"""


_lot_archive_cols: dict[str, bool] = {}


def lot_archive_has_currency() -> bool:
    """lot_archive.currency arrives with migration 019 (APPLIED to prod 2026-10-02). Checked once
    per process; absent → the index row is written without it."""
    if "currency" not in _lot_archive_cols:
        row = _read_with_backoff(
            db.fetch_one,
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_name = 'lot_archive' AND column_name = 'currency'")
        _lot_archive_cols["currency"] = bool(row)
    return _lot_archive_cols["currency"]


def upsert_archive_index(meta: dict) -> None:
    """Index row for one archived lot (migration 015). Only called when the
    table exists; R2 stays the record either way."""
    cur = lot_archive_has_currency()
    sql = _INDEX_UPSERT_SQL.format(
        currency_col=", currency" if cur else "", currency_val=", %s" if cur else "",
        currency_set=" currency = EXCLUDED.currency," if cur else "")
    extra = (meta.get("currency"),) if cur else ()
    db.execute(sql, (
        meta.get("source") or "govdeals", meta["lot_key"], (meta.get("title") or "")[:300], meta.get("canonical_category"),
        meta.get("category_name"), meta.get("city"), meta.get("state"), meta.get("seller"),
        meta.get("closed_at"), meta.get("final_price"), meta.get("bid_count"),
        meta.get("outcome"), meta.get("status_code"), meta.get("photo_count"),
        meta.get("completeness"), meta.get("archived_at"), *extra))


def database_size_mb() -> float:
    row = _read_with_backoff(db.fetch_one, "SELECT pg_database_size(current_database()) AS b")
    return row["b"] / 1e6 if row else 0.0


# --- source health (migration 018, APPLIED to prod 2026-10-02) -----------------
#
# One row per source: the circuit breaker in recorder/health.py. Loaded once
# and saved once per run. Until 018 is applied every caller degrades to an
# in-memory breaker (a NOTE, never a failed run) — the run still caps a dead
# source through PollBudget and the faster connect timeout.

_HEALTH_COLS = ("source", "state", "consecutive_failures", "last_attempt_at",
                "last_success_at", "next_attempt_at", "last_error", "updated_at")
# Migration 020 (APPLIED to prod 2026-10-02) adds this; on a database without
# it the column is neither read nor written.
_HEALTH_OPTIONAL_COLS = ("last_discover_at",)


def _health_upsert_sql(cols: tuple[str, ...]) -> str:
    """Upsert over a fixed, code-owned column list (never caller input)."""
    vals = ", ".join("COALESCE(%s, now())" if c == "updated_at" else "%s" for c in cols)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "source")
    return (f"INSERT INTO recorder_source_health ({', '.join(cols)}) VALUES ({vals}) "
            f"ON CONFLICT (source) DO UPDATE SET {sets}")


def _health_cols() -> tuple[str, ...]:
    rows = _read_with_backoff(
        db.fetch_all,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'recorder_source_health' AND column_name = ANY(%s)",
        (list(_HEALTH_OPTIONAL_COLS),))
    have = {r["column_name"] for r in rows}
    return _HEALTH_COLS + tuple(c for c in _HEALTH_OPTIONAL_COLS if c in have)


def source_health_table_exists() -> bool:
    row = _read_with_backoff(db.fetch_one, "SELECT to_regclass('recorder_source_health') AS reg")
    return bool(row and row.get("reg"))


def load_source_health() -> dict[str, dict] | None:
    """{source: row} from recorder_source_health, or None when the table
    does not exist (migration 018 not applied on this database)."""
    if not source_health_table_exists():
        return None
    rows = _read_with_backoff(db.fetch_all, "SELECT * FROM recorder_source_health")
    return {r["source"]: dict(r) for r in rows}


def save_source_health(rows: list[dict]) -> int:
    """Upsert the changed breaker rows (one executemany)."""
    if not rows:
        return 0
    cols = _health_cols()
    db.executemany(_health_upsert_sql(cols), [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)

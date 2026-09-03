"""SQL for the tracking list (`tracked_lots`). See deals/tracking.py.

DDL of record: scripts/sql/005_tracked_lots.sql (applied by `store.init_schema`).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from automation import db

DDL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sql" / "005_tracked_lots.sql"

_LIVE_COLS = ("end_utc", "status", "bid_count", "current_bid", "currency_code",
              "high_bidder", "high_bidder_username", "visitors", "hits", "watcher_count")


def init_schema() -> None:
    for stmt in filter(str.strip, DDL_PATH.read_text().split(";")):
        db.execute(stmt)


def upsert(asset_id: int, account_id: int, *, auction_id: int | None, label: str,
           source: str, title: str | None, url: str | None, note: str | None = None) -> dict:
    """Insert or refresh a membership row.

    On conflict the label/note/title are refreshed (the operator re-adding a lot
    under a new label is a rename, not an error). If the auction id has moved —
    the lot relisted — the row is re-armed: closed_at and the finals are
    cleared so the new auction gets followed too. The previous auction's
    history stays in deal_bid_observations under its own auction id.
    """
    row = db.fetch_one("""
        INSERT INTO tracked_lots (asset_id, account_id, auction_id, label, source, title, url, note,
                                  next_poll_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (asset_id, account_id) DO UPDATE SET
            label = EXCLUDED.label,
            note = COALESCE(EXCLUDED.note, tracked_lots.note),
            title = COALESCE(EXCLUDED.title, tracked_lots.title),
            url = COALESCE(EXCLUDED.url, tracked_lots.url),
            auction_id = COALESCE(EXCLUDED.auction_id, tracked_lots.auction_id),
            closed_at = CASE WHEN EXCLUDED.auction_id IS NOT NULL
                              AND EXCLUDED.auction_id IS DISTINCT FROM tracked_lots.auction_id
                             THEN NULL ELSE tracked_lots.closed_at END,
            final_bid = CASE WHEN EXCLUDED.auction_id IS NOT NULL
                              AND EXCLUDED.auction_id IS DISTINCT FROM tracked_lots.auction_id
                             THEN NULL ELSE tracked_lots.final_bid END,
            final_bid_count = CASE WHEN EXCLUDED.auction_id IS NOT NULL
                              AND EXCLUDED.auction_id IS DISTINCT FROM tracked_lots.auction_id
                             THEN NULL ELSE tracked_lots.final_bid_count END,
            final_bidder = CASE WHEN EXCLUDED.auction_id IS NOT NULL
                              AND EXCLUDED.auction_id IS DISTINCT FROM tracked_lots.auction_id
                             THEN NULL ELSE tracked_lots.final_bidder END,
            final_bidder_username = CASE WHEN EXCLUDED.auction_id IS NOT NULL
                              AND EXCLUDED.auction_id IS DISTINCT FROM tracked_lots.auction_id
                             THEN NULL ELSE tracked_lots.final_bidder_username END,
            next_poll_at = now(),
            poll_error = NULL
        RETURNING *""",
        (asset_id, account_id, auction_id, label, source, title, url, note))
    return row


def delete(asset_id: int, account_id: int) -> bool:
    return bool(db.execute("DELETE FROM tracked_lots WHERE asset_id=%s AND account_id=%s",
                           (asset_id, account_id)))


def patch(asset_id: int, account_id: int, *, label: str | None = None,
          note: str | None = None) -> dict | None:
    sets, params = [], []
    if label is not None:
        sets.append("label=%s"); params.append(label)
    if note is not None:
        sets.append("note=%s"); params.append(note)
    if not sets:
        return get(asset_id, account_id)
    params += [asset_id, account_id]
    return db.fetch_one(f"UPDATE tracked_lots SET {', '.join(sets)} "
                        "WHERE asset_id=%s AND account_id=%s RETURNING *", tuple(params))


def get(asset_id: int, account_id: int) -> dict | None:
    return db.fetch_one("SELECT * FROM tracked_lots WHERE asset_id=%s AND account_id=%s",
                        (asset_id, account_id))


def known_keys() -> set[tuple[int, int]]:
    return {(r["asset_id"], r["account_id"])
            for r in db.fetch_all("SELECT asset_id, account_id FROM tracked_lots")}


def list_all(label: str | None = None) -> list[dict]:
    """Open lots first (soonest close on top), then closed ones newest-first."""
    where, params = "", ()
    if label:
        where, params = "WHERE label=%s", (label,)
    return db.fetch_all(f"""SELECT * FROM tracked_lots {where}
        ORDER BY (closed_at IS NOT NULL), end_utc ASC NULLS LAST, closed_at DESC, added_at DESC""",
        params)


def labels() -> list[dict]:
    return db.fetch_all("""SELECT label, count(*) AS n,
            count(*) FILTER (WHERE closed_at IS NULL) AS open
        FROM tracked_lots GROUP BY label ORDER BY label""")


def due(now: datetime) -> list[dict]:
    return db.fetch_all("""SELECT * FROM tracked_lots
        WHERE closed_at IS NULL AND (next_poll_at IS NULL OR next_poll_at <= %s)
        ORDER BY end_utc ASC NULLS LAST""", (now,))


def mark_error(asset_id: int, account_id: int, error: str, next_poll_at: datetime) -> None:
    db.execute("""UPDATE tracked_lots SET poll_error=%s, last_polled_at=now(), next_poll_at=%s
        WHERE asset_id=%s AND account_id=%s""", (error[:500], next_poll_at, asset_id, account_id))


def record_state(state, *, next_poll_at: datetime | None, closed_at: datetime | None) -> None:
    """Mirror the newest bidbox read onto the row; stamp finals on close.

    Two statements rather than one CASE-laden UPDATE: psycopg can't infer a
    parameter's type from `CASE WHEN %s IS NULL`, and the finals must only be
    written on the closing poll anyway."""
    db.execute("""UPDATE tracked_lots SET
            auction_id=%s, end_utc=%s, status=%s, bid_count=%s, current_bid=%s,
            currency_code=%s, high_bidder=%s, high_bidder_username=%s,
            visitors=%s, hits=%s, watcher_count=%s,
            last_polled_at=%s, next_poll_at=%s::timestamptz, poll_error=NULL
        WHERE asset_id=%s AND account_id=%s""",
        (state.auction_id, state.end_utc, state.status, state.bid_count, state.current_bid,
         state.currency_code, state.high_bidder, state.high_bidder_username,
         state.visitors, state.hits, state.watcher_count,
         state.observed_at, next_poll_at,
         state.asset_id, state.account_id))
    if closed_at is not None:
        db.execute("""UPDATE tracked_lots SET
                closed_at = COALESCE(tracked_lots.closed_at, %s),
                final_bid=%s, final_bid_count=%s, final_bidder=%s, final_bidder_username=%s
            WHERE asset_id=%s AND account_id=%s""",
            (closed_at, state.current_bid, state.bid_count, state.high_bidder,
             state.high_bidder_username, state.asset_id, state.account_id))


def history(asset_id: int, account_id: int) -> list[dict]:
    """Every observation for this asset, across all its auctions, oldest first."""
    return db.fetch_all("""SELECT auction_id, observed_at, bid_count, current_bid, currency_code,
            high_bidder, high_bidder_username, bid_increment, visitors, hits, watcher_count,
            end_utc, status
        FROM deal_bid_observations WHERE asset_id=%s AND account_id=%s
        ORDER BY observed_at ASC, id ASC""", (asset_id, account_id))


def rival_lots(bidder_ids: list[int], *, exclude: tuple[int, int] | None = None,
               limit: int = 40) -> list[dict]:
    """Other lots each of these bidders has been seen leading — the context
    that turns `sa*****` from a string into "the one who keeps buying Fort
    Myer chairs"."""
    if not bidder_ids:
        return []
    where = ["o.high_bidder = ANY(%s)"]
    params: list = [bidder_ids]
    if exclude:
        where.append("NOT (o.asset_id=%s AND o.account_id=%s)")
        params += list(exclude)
    params.append(limit)
    return db.fetch_all(f"""
        SELECT o.high_bidder AS bidder_id, max(o.high_bidder_username) AS handle,
               o.asset_id, o.account_id, o.auction_id,
               max(o.current_bid) AS max_bid, max(o.observed_at) AS last_seen,
               coalesce(max(l.title), max(t.title)) AS title,
               max(l.outcome) AS outcome, max(l.final_bid) AS final_bid,
               bool_or(l.outcome_complete AND l.high_bidder = o.high_bidder) AS won
        FROM deal_bid_observations o
        LEFT JOIN deal_lots l ON l.asset_id=o.asset_id AND l.account_id=o.account_id
                              AND l.auction_id=o.auction_id
        LEFT JOIN tracked_lots t ON t.asset_id=o.asset_id AND t.account_id=o.account_id
        WHERE {' AND '.join(where)}
        GROUP BY o.high_bidder, o.asset_id, o.account_id, o.auction_id
        ORDER BY last_seen DESC LIMIT %s""", tuple(params))

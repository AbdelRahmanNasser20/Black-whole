"""SQL for `inventory_auction_watch`. See `automation/auction_sync.py`.

DDL of record: `scripts/sql/012_inventory_auction_watch.sql`.

Every function here is I/O. The decisions live in `auction_sync` as pure
functions so they can be tested without a database.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import db

DDL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sql" / "012_inventory_auction_watch.sql"

TABLE = "inventory_auction_watch"

STATE_LIVE = "live"
STATE_EXPIRED = "expired"

# Keep the stored strings inside the column widths the DDL declares — a
# provider error message is unbounded and this table is deliberately not.
_REASON_MAX = 200
_ERROR_MAX = 500


def schema_ready() -> bool:
    """True once migration 011 has been applied to this database.

    The sync is shipped ahead of the migration (operator gate), so every entry
    point checks this and says what to run instead of raising UndefinedTable
    from somewhere three frames down.
    """
    row = db.fetch_one(f"SELECT to_regclass('public.{TABLE}') AS t")
    return bool(row and row.get("t"))


def get(lot_id: str) -> dict | None:
    return db.fetch_one(f"SELECT * FROM {TABLE} WHERE lot_id = %s", (str(lot_id),))


def list_all() -> list[dict]:
    return db.fetch_all(f"SELECT * FROM {TABLE} ORDER BY state, lot_id")


def expired_lot_ids() -> list[str]:
    """Lots we flipped fake-sold-out and are still watching for a relist.

    These no longer carry `status = 'active_bid'`, so the sync's candidate
    query cannot find them from `inventory` alone — this is the other half of
    the watch set.
    """
    return [r["lot_id"] for r in
            db.fetch_all(f"SELECT lot_id FROM {TABLE} WHERE state = %s ORDER BY lot_id",
                         (STATE_EXPIRED,))]


def mark_checked(lot_id: str, *, asset_id: int, account_id: int, auction_id: int | None,
                 end_utc: datetime | None, now: datetime, live: bool) -> None:
    """A poll that resolved and changed nothing. Keeps the row's idea of the
    current auction fresh so the NEXT poll can compare against it."""
    db.execute(
        f"""INSERT INTO {TABLE} (lot_id, asset_id, account_id, auction_id, state,
                                 end_utc, last_checked_at, last_seen_live_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lot_id) DO UPDATE SET
                auction_id        = COALESCE(EXCLUDED.auction_id, {TABLE}.auction_id),
                end_utc           = COALESCE(EXCLUDED.end_utc, {TABLE}.end_utc),
                last_checked_at   = EXCLUDED.last_checked_at,
                last_seen_live_at = COALESCE(EXCLUDED.last_seen_live_at, {TABLE}.last_seen_live_at),
                poll_error        = NULL,
                updated_at        = EXCLUDED.updated_at""",
        (str(lot_id), asset_id, account_id, auction_id,
         STATE_LIVE if live else STATE_EXPIRED,
         end_utc, now, now if live else None, now))


def mark_expired(lot_id: str, *, asset_id: int, account_id: int, auction_id: int | None,
                 reason: str, end_utc: datetime | None, now: datetime,
                 prior_status: str | None) -> None:
    """Record WHEN and WHY a lot came off the channels.

    `expired_at` is stamped once (COALESCE) — a lot that expires, relists and
    expires again keeps the first date, which is the one the operator means by
    "when did this stop being available".
    """
    db.execute(
        f"""INSERT INTO {TABLE} (lot_id, asset_id, account_id, auction_id, state, reason,
                                 end_utc, expired_at, last_checked_at, prior_status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lot_id) DO UPDATE SET
                auction_id      = COALESCE(EXCLUDED.auction_id, {TABLE}.auction_id),
                state           = EXCLUDED.state,
                reason          = EXCLUDED.reason,
                end_utc         = COALESCE(EXCLUDED.end_utc, {TABLE}.end_utc),
                expired_at      = COALESCE({TABLE}.expired_at, EXCLUDED.expired_at),
                last_checked_at = EXCLUDED.last_checked_at,
                prior_status    = COALESCE(EXCLUDED.prior_status, {TABLE}.prior_status),
                poll_error      = NULL,
                updated_at      = EXCLUDED.updated_at""",
        (str(lot_id), asset_id, account_id, auction_id, STATE_EXPIRED,
         (reason or "")[:_REASON_MAX], end_utc, now, now, prior_status, now))


def mark_relisted(lot_id: str, *, asset_id: int, account_id: int, auction_id: int | None,
                  reason: str, end_utc: datetime | None, now: datetime) -> None:
    """The auction is live again. Back to `live`, and `expired_at` is cleared so
    the row reads as a lot that is currently available, not one that once was."""
    db.execute(
        f"""INSERT INTO {TABLE} (lot_id, asset_id, account_id, auction_id, state, reason,
                                 end_utc, relisted_at, last_checked_at, last_seen_live_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lot_id) DO UPDATE SET
                auction_id        = COALESCE(EXCLUDED.auction_id, {TABLE}.auction_id),
                state             = EXCLUDED.state,
                reason            = EXCLUDED.reason,
                end_utc           = COALESCE(EXCLUDED.end_utc, {TABLE}.end_utc),
                relisted_at       = EXCLUDED.relisted_at,
                expired_at        = NULL,
                last_checked_at   = EXCLUDED.last_checked_at,
                last_seen_live_at = EXCLUDED.last_seen_live_at,
                poll_error        = NULL,
                updated_at        = EXCLUDED.updated_at""",
        (str(lot_id), asset_id, account_id, auction_id, STATE_LIVE,
         (reason or "")[:_REASON_MAX], end_utc, now, now, now, now))


def mark_error(lot_id: str, *, asset_id: int, account_id: int, error: str,
               now: datetime) -> None:
    """A poll that could not decide. Writes the error and NOTHING else — the
    lot keeps whatever state it had. Maestro answers 204 for a purged asset,
    and "the endpoint went quiet" must never read as "the auction ended"."""
    db.execute(
        f"""INSERT INTO {TABLE} (lot_id, asset_id, account_id, state, poll_error,
                                 last_checked_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lot_id) DO UPDATE SET
                poll_error      = EXCLUDED.poll_error,
                last_checked_at = EXCLUDED.last_checked_at,
                updated_at      = EXCLUDED.updated_at""",
        (str(lot_id), asset_id, account_id, STATE_LIVE, (error or "")[:_ERROR_MAX], now, now))


def forget(lot_id: str) -> None:
    db.execute(f"DELETE FROM {TABLE} WHERE lot_id = %s", (str(lot_id),))

"""CRUD over `listing_channels` — one row per lot × channel.

Thin by design: every function is one parameterized statement through
`automation.db` (pooled). No business rules live here; `sync.py` decides what
state a lot *should* be in, this module records what state it *is* in.

Hard rules:
  * Channel and state names are validated against `automation.channels`
    BEFORE any SQL — a typo is a ValueError, never a CHECK violation.
  * `%s` placeholders only. Lot ids never get interpolated into SQL.
  * `upsert` COALESCEs `external_id` / `url` / `payload_hash` so a state-only
    write never wipes a known far-side id; `last_error` is NOT sticky — a clean
    sync clears it.
  * Nothing here returns `inventory.storage_note`. The matrix/queue joins pick
    columns explicitly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import db, inventory
from . import CHANNELS, STATES


def _check_channel(channel: str) -> None:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel!r} (known: {', '.join(CHANNELS)})")


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown channel state: {state!r} (known: {', '.join(STATES)})")


# ─────────────────────────────── reads ───────────────────────────────

def get(lot_id: str, channel: str) -> dict | None:
    _check_channel(channel)
    return db.fetch_one(
        "SELECT * FROM listing_channels WHERE lot_id = %s AND channel = %s",
        (str(lot_id), channel),
    )


def get_by_id(row_id: int) -> dict | None:
    return db.fetch_one("SELECT * FROM listing_channels WHERE id = %s", (int(row_id),))


def list_for_lot(lot_id: str) -> list[dict]:
    return db.fetch_all(
        "SELECT * FROM listing_channels WHERE lot_id = %s ORDER BY channel",
        (str(lot_id),),
    )


def list_by_state(channel: str, state: str) -> list[dict]:
    _check_channel(channel)
    _check_state(state)
    return db.fetch_all(
        "SELECT * FROM listing_channels WHERE channel = %s AND state = %s ORDER BY updated_at DESC",
        (channel, state),
    )


def list_all() -> list[dict]:
    return db.fetch_all("SELECT * FROM listing_channels ORDER BY lot_id, channel")


def current_map() -> dict[tuple[str, str], dict]:
    """Every row keyed by (lot_id, channel) — the sync engine's view of 'now'."""
    return {(r["lot_id"], r["channel"]): r for r in list_all()}


def recent_synced_at(channel: str, since: datetime) -> list[datetime]:
    """`last_synced_at` of every row on `channel` touched at/after `since`.

    The browser-channel pacer counts list actions per UTC day and enforces the
    spacing from these timestamps (they are stamped by `upsert`, i.e. by a real
    adapter call — a state flip through `set_state` does not count).
    """
    _check_channel(channel)
    rows = db.fetch_all(
        """
        SELECT last_synced_at FROM listing_channels
        WHERE channel = %s AND last_synced_at IS NOT NULL AND last_synced_at >= %s
        ORDER BY last_synced_at DESC
        """,
        (channel, since),
    )
    return [r["last_synced_at"] for r in rows or []]


# ─────────────────────────────── writes ───────────────────────────────

def upsert(
    lot_id: str,
    channel: str,
    *,
    state: str,
    external_id: str | None = None,
    url: str | None = None,
    payload_hash: str | None = None,
    last_error: str | None = None,
) -> dict:
    """Record the result of a sync action. Stamps `last_synced_at`."""
    _check_channel(channel)
    _check_state(state)
    row = db.fetch_one(
        """
        INSERT INTO listing_channels
            (lot_id, channel, state, external_id, url, payload_hash, last_error, last_synced_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (lot_id, channel) DO UPDATE SET
            state = EXCLUDED.state,
            external_id = COALESCE(EXCLUDED.external_id, listing_channels.external_id),
            url = COALESCE(EXCLUDED.url, listing_channels.url),
            payload_hash = COALESCE(EXCLUDED.payload_hash, listing_channels.payload_hash),
            last_error = EXCLUDED.last_error,
            last_synced_at = now(),
            updated_at = now()
        RETURNING *
        """,
        (str(lot_id), channel, state, external_id, url, payload_hash, last_error),
    )
    return row or {}


def set_state(lot_id: str, channel: str, state: str, *, last_error: str | None = None) -> dict | None:
    """Flip a row's state without claiming a sync happened (no `last_synced_at`).

    Creates the row when it does not exist yet — `remove_lot` / `restore_lot`
    call this on lots that were listed before the table existed.
    """
    _check_channel(channel)
    _check_state(state)
    return db.fetch_one(
        """
        INSERT INTO listing_channels (lot_id, channel, state, last_error, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (lot_id, channel) DO UPDATE SET
            state      = EXCLUDED.state,
            last_error = EXCLUDED.last_error,
            updated_at = now()
        RETURNING *
        """,
        (str(lot_id), channel, state, last_error),
    )


def approve(row_id: int) -> dict | None:
    """Operator approved a queued (re)list: `pending_approval` → `queued`.

    Only rows that are actually waiting move; approving anything else is a
    no-op (returns None) so a stale admin tab cannot re-queue a live listing.
    """
    return db.fetch_one(
        """
        UPDATE listing_channels
           SET state = %s, approved_at = now(), last_error = NULL, updated_at = now()
         WHERE id = %s AND state = 'pending_approval'
        RETURNING *
        """,
        ("queued", int(row_id)),
    )


def reject(row_id: int) -> dict | None:
    """Operator declined: `pending_approval` → `off`. The lot stays on every other channel."""
    return db.fetch_one(
        """
        UPDATE listing_channels
           SET state = %s, updated_at = now()
         WHERE id = %s AND state = 'pending_approval'
        RETURNING *
        """,
        ("off", int(row_id)),
    )


# ─────────────────────────────── admin views ───────────────────────────────

# The inventory columns the Channels tab needs. Explicit so `storage_note`
# (facility address + gate code) can never ride along.
_LOT_COLS = ("lot_id", "title", "status", "quantity_remaining")


def _cell(row: dict | None) -> dict[str, Any]:
    if not row:
        return {"state": "off", "url": None}
    cell: dict[str, Any] = {"state": row.get("state") or "off", "url": row.get("url")}
    if row.get("last_error"):
        cell["last_error"] = row["last_error"]
    return cell


def matrix() -> list[dict]:
    """One dict per inventory lot: {lot_id, title, status, qty, channels: {c: {state, url[, last_error]}}}.

    Total over CHANNELS — a lot with no row on a channel reads `off`, so the
    admin grid never has holes. Ordered like `inventory.list_all()` (newest edit first).
    """
    lots = inventory.list_all()
    by_key = current_map()
    out: list[dict] = []
    for lot in lots:
        lot_id = lot["lot_id"]
        out.append({
            "lot_id": lot_id,
            "title": lot.get("title"),
            "status": lot.get("status"),
            "qty": lot.get("quantity_remaining"),
            "channels": {c: _cell(by_key.get((lot_id, c))) for c in CHANNELS},
        })
    return out


def queue() -> list[dict]:
    """Everything waiting for the operator (`pending_approval`), newest first,
    with the lot's title/status/qty joined in for the admin table."""
    return db.fetch_all(
        """
        SELECT lc.*, i.title, i.status, i.quantity_remaining
          FROM listing_channels lc
          JOIN inventory i ON i.lot_id = lc.lot_id
         WHERE lc.state = %s
         ORDER BY lc.updated_at DESC, lc.id DESC
        """,
        ("pending_approval",),
    )

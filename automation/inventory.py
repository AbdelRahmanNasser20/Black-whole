"""Inventory ledger — the bridge between parsed listings and marketplace publications.

Why this exists: FB and eBay draft URLs were previously fire-and-forget console
output. Re-running the pipeline on a lot we'd already published would spend
marketplace API budget a second time. This module is the single source of truth
for "what we've parsed, what's up where, and how many are left to sell."

Two tables, one SQLite file at `~/.listing_automation/inventory.db`:
  - `inventory`  : one row per GovDeals lot, keyed by lot_id
  - `inquiries`  : customer contact-form submissions (buy/sell)

Both are read/written from the FastAPI dashboard and from run.py. sqlite3 is
stdlib and fine for single-user localhost — no ORM.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import STATE_ROOT

DB_PATH = STATE_ROOT / "inventory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    lot_id                 TEXT PRIMARY KEY,
    seller_id              TEXT,
    govdeals_url           TEXT,
    folder_name            TEXT,
    folder_path            TEXT,
    sku                    TEXT,
    title                  TEXT,
    description            TEXT,
    city                   TEXT,
    state                  TEXT,
    chair_type             TEXT,
    dimensions             TEXT,
    quantity_original      INTEGER,
    quantity_remaining     INTEGER,
    price_per_chair        REAL,
    hero_image             TEXT,
    status                 TEXT NOT NULL DEFAULT 'draft',
    facebook_url           TEXT,
    facebook_published_at  TEXT,
    ebay_url               TEXT,
    ebay_published_at      TEXT,
    parsed_at              TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory(status);

CREATE TABLE IF NOT EXISTS inquiries (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                   TEXT NOT NULL,
    lot_id                 TEXT,
    name                   TEXT NOT NULL,
    email                  TEXT,
    phone                  TEXT,
    quantity_interested    INTEGER,
    message                TEXT,
    status                 TEXT NOT NULL DEFAULT 'new',
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inquiries_lot ON inquiries(lot_id);
CREATE INDEX IF NOT EXISTS idx_inquiries_status ON inquiries(status);
"""

PUBLIC_STATUSES = ("listed", "draft")
ALL_STATUSES = ("draft", "listed", "hidden", "sold_out")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def get(lot_id: str) -> dict | None:
    if not lot_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inventory WHERE lot_id = ?", (str(lot_id),)
        ).fetchone()
    return _row_to_dict(row)


def list_all(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM inventory WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM inventory ORDER BY updated_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def list_public() -> list[dict]:
    """Rows customers should see on /listings — visible and actually have stock."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM inventory
            WHERE status IN ('listed', 'draft')
              AND (quantity_remaining IS NULL OR quantity_remaining > 0)
            ORDER BY
              CASE status WHEN 'listed' THEN 0 ELSE 1 END,
              COALESCE(quantity_remaining, 0) DESC,
              updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    """Headline counts for the landing page."""
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE status IN ('listed','draft')"
        ).fetchone()[0]
        chairs = conn.execute(
            "SELECT COALESCE(SUM(quantity_remaining), 0) FROM inventory "
            "WHERE status IN ('listed','draft')"
        ).fetchone()[0]
        cities = conn.execute(
            "SELECT COUNT(DISTINCT city) FROM inventory "
            "WHERE city IS NOT NULL AND city != '' "
            "AND status IN ('listed','draft')"
        ).fetchone()[0]
    return {"lots": int(total), "chairs": int(chairs or 0), "cities": int(cities)}


def upsert_from_run(
    *,
    lot_id: str,
    seller_id: str | None,
    govdeals_url: str | None,
    folder_name: str | None,
    folder_path: str | None,
    sku: str | None,
    title: str | None,
    description: str | None,
    city: str | None,
    state: str | None,
    chair_type: str | None,
    dimensions: str | None,
    quantity: int | None,
    price_per_chair: float | None,
    hero_image: str | None,
) -> dict:
    """Create/update an inventory row from a completed pipeline run.

    Preserves user-editable fields on update: `quantity_remaining`, `status`,
    `price_per_chair` (if already set), `hero_image` (if already set), and any
    stored FB/eBay URLs. A re-run should refresh metadata, not stomp edits.
    """
    if not lot_id:
        raise ValueError("lot_id required")
    now = _now()
    existing = get(lot_id)
    with connect() as conn:
        if existing is None:
            conn.execute(
                """
                INSERT INTO inventory (
                    lot_id, seller_id, govdeals_url, folder_name, folder_path,
                    sku, title, description, city, state, chair_type, dimensions,
                    quantity_original, quantity_remaining, price_per_chair,
                    hero_image, status, parsed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    str(lot_id), seller_id, govdeals_url, folder_name, folder_path,
                    sku, title, description, city, state, chair_type, dimensions,
                    quantity, quantity, price_per_chair, hero_image, now, now,
                ),
            )
        else:
            # Keep user edits. Only refresh the "as-parsed" fields.
            conn.execute(
                """
                UPDATE inventory SET
                    seller_id         = COALESCE(?, seller_id),
                    govdeals_url      = COALESCE(?, govdeals_url),
                    folder_name       = COALESCE(?, folder_name),
                    folder_path       = COALESCE(?, folder_path),
                    sku               = COALESCE(?, sku),
                    title             = COALESCE(?, title),
                    description       = COALESCE(?, description),
                    city              = COALESCE(?, city),
                    state             = COALESCE(?, state),
                    chair_type        = COALESCE(?, chair_type),
                    dimensions        = COALESCE(?, dimensions),
                    quantity_original = COALESCE(?, quantity_original),
                    price_per_chair   = COALESCE(price_per_chair, ?),
                    hero_image        = COALESCE(hero_image, ?),
                    updated_at        = ?
                WHERE lot_id = ?
                """,
                (
                    seller_id, govdeals_url, folder_name, folder_path, sku,
                    title, description, city, state, chair_type, dimensions,
                    quantity, price_per_chair, hero_image, now, str(lot_id),
                ),
            )
        conn.commit()
    return get(lot_id)  # re-read


def set_platform_url(
    lot_id: str, platform: str, url: str | None, clear_timestamp: bool = False
) -> dict | None:
    """Record an FB or eBay URL + publish timestamp. Used by run.py post-phase
    AND by the admin backfill UI for listings posted manually pre-tracking."""
    if platform not in ("facebook", "ebay"):
        raise ValueError(f"unknown platform: {platform}")
    col_url = f"{platform}_url"
    col_ts = f"{platform}_published_at"
    now = _now()
    ts = None if (url is None or clear_timestamp) else now
    with connect() as conn:
        conn.execute(
            f"UPDATE inventory SET {col_url} = ?, {col_ts} = ?, updated_at = ? "
            f"WHERE lot_id = ?",
            (url, ts, now, str(lot_id)),
        )
        # Promote to 'listed' on first successful publish.
        if url:
            conn.execute(
                "UPDATE inventory SET status = 'listed', updated_at = ? "
                "WHERE lot_id = ? AND status = 'draft'",
                (now, str(lot_id)),
            )
        conn.commit()
    return get(lot_id)


def set_fields(lot_id: str, **fields: Any) -> dict | None:
    """Admin inline-edit path. Whitelisted columns only.

    `quantity_remaining = 0` auto-flips status to 'sold_out' unless status is
    being set explicitly in the same call.
    """
    allowed = {
        "quantity_remaining", "price_per_chair", "status", "hero_image",
        "title", "description", "chair_type", "dimensions", "city", "state",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return get(lot_id)
    # Auto-sold-out rule
    if (
        "quantity_remaining" in clean
        and "status" not in clean
        and clean["quantity_remaining"] is not None
        and int(clean["quantity_remaining"]) <= 0
    ):
        clean["status"] = "sold_out"
    if "status" in clean and clean["status"] not in ALL_STATUSES:
        raise ValueError(f"invalid status: {clean['status']}")
    clean["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [str(lot_id)]
    with connect() as conn:
        conn.execute(f"UPDATE inventory SET {cols} WHERE lot_id = ?", params)
        conn.commit()
    return get(lot_id)


def delete(lot_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM inventory WHERE lot_id = ?", (str(lot_id),))
        conn.commit()
        return cur.rowcount > 0


def insert_manual(
    *,
    lot_id: str,
    title: str,
    quantity: int,
    price_per_chair: float | None = None,
    city: str | None = None,
    state: str | None = None,
    chair_type: str | None = None,
    dimensions: str | None = None,
    description: str | None = None,
    folder_name: str | None = None,
    hero_image: str | None = None,
) -> dict:
    """Admin-created row for a lot that was never run through the pipeline."""
    if get(lot_id):
        raise ValueError(f"lot_id {lot_id} already exists")
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO inventory (
                lot_id, title, description, city, state, chair_type, dimensions,
                quantity_original, quantity_remaining, price_per_chair,
                folder_name, hero_image, status, parsed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                str(lot_id), title, description, city, state, chair_type, dimensions,
                quantity, quantity, price_per_chair, folder_name, hero_image, now, now,
            ),
        )
        conn.commit()
    return get(lot_id)


# ───────────────────────────── inquiries ─────────────────────────────

def create_inquiry(
    *,
    kind: str,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    message: str | None = None,
    lot_id: str | None = None,
    quantity_interested: int | None = None,
) -> dict:
    if kind not in ("buy", "sell"):
        raise ValueError("kind must be 'buy' or 'sell'")
    if not name or not name.strip():
        raise ValueError("name required")
    if not email and not phone:
        raise ValueError("email or phone required")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO inquiries (
                kind, lot_id, name, email, phone, quantity_interested,
                message, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                kind, str(lot_id) if lot_id else None, name.strip(),
                (email or "").strip() or None, (phone or "").strip() or None,
                quantity_interested, (message or "").strip() or None, now,
            ),
        )
        conn.commit()
        inquiry_id = cur.lastrowid
    return get_inquiry(inquiry_id)


def get_inquiry(inquiry_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inquiries WHERE id = ?", (int(inquiry_id),)
        ).fetchone()
    return _row_to_dict(row)


def list_inquiries(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM inquiries WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM inquiries ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def set_inquiry_status(inquiry_id: int, status: str) -> dict | None:
    if status not in ("new", "contacted", "closed"):
        raise ValueError(f"invalid status: {status}")
    with connect() as conn:
        conn.execute(
            "UPDATE inquiries SET status = ? WHERE id = ?",
            (status, int(inquiry_id)),
        )
        conn.commit()
    return get_inquiry(inquiry_id)


def link_inquiry(inquiry_id: int, lot_id: str | None) -> dict | None:
    with connect() as conn:
        conn.execute(
            "UPDATE inquiries SET lot_id = ? WHERE id = ?",
            (str(lot_id) if lot_id else None, int(inquiry_id)),
        )
        conn.commit()
    return get_inquiry(inquiry_id)


def delete_inquiry(inquiry_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM inquiries WHERE id = ?", (int(inquiry_id),)
        )
        conn.commit()
        return cur.rowcount > 0

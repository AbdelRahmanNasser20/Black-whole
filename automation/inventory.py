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

from .config import ATTACHMENTS_ROOT, STATE_ROOT

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
    zip_code               TEXT,
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

-- Auction favorites: a starred GovDeals/PublicSurplus lot the user wants
-- countdown alerts on. Snapshot the asset metadata at star-time so the card
-- still renders even if the listings_db row scrolls out of the active window.
CREATE TABLE IF NOT EXISTS auction_favorites (
    asset_id        TEXT PRIMARY KEY,
    link            TEXT NOT NULL,
    title           TEXT,
    quantity        INTEGER,
    end_date_iso    TEXT,        -- normalized ISO 8601 (UTC); NULL when unparseable
    end_date_raw    TEXT,        -- original string from the scraper
    image_url       TEXT,
    location        TEXT,
    starred_at      TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL,
    notes           TEXT
);

-- Idempotency log: one row per (asset_id, interval_label) the moment we ship
-- a Telegram alert. Cleared for an asset whenever its end_date changes (relist).
CREATE TABLE IF NOT EXISTS auction_alerts_sent (
    asset_id        TEXT NOT NULL,
    interval_label  TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (asset_id, interval_label)
);
"""

PUBLIC_STATUSES = ("listed", "draft", "owned", "won_pickup")
ALL_STATUSES = (
    "draft", "listed", "hidden", "sold_out",
    "owned", "won_pickup", "active_bid", "lost",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_MIGRATIONS: tuple[str, ...] = (
    # Added 2026-04-21: track Facebook Business page posts and paid ad
    # placements alongside the existing Marketplace/eBay URLs. Four ALTERs,
    # one column each so partial migration state is recoverable.
    "ALTER TABLE inventory ADD COLUMN fb_business_url TEXT",
    "ALTER TABLE inventory ADD COLUMN fb_business_published_at TEXT",
    "ALTER TABLE inventory ADD COLUMN ad_url TEXT",
    "ALTER TABLE inventory ADD COLUMN ad_published_at TEXT",
    # Added 2026-05-08: pickup ZIP from the GovDeals asset page.
    "ALTER TABLE inventory ADD COLUMN zip_code TEXT",
    # Added 2026-05-08: GovDeals seller / facility contact (admin-only).
    "ALTER TABLE inventory ADD COLUMN contact_name TEXT",
    "ALTER TABLE inventory ADD COLUMN contact_email TEXT",
    "ALTER TABLE inventory ADD COLUMN contact_phone TEXT",
    # Added 2026-05-20: per-lot GovDeals account credentials + winning-bid
    # buyer certificate attachment. The credentials are plain text — single-
    # operator local SQLite, file mode 600 via `umask`. The cert path is
    # relative to ATTACHMENTS_ROOT (so the row stays portable if the root
    # moves).
    "ALTER TABLE inventory ADD COLUMN govdeals_username TEXT",
    "ALTER TABLE inventory ADD COLUMN govdeals_password TEXT",
    "ALTER TABLE inventory ADD COLUMN buyer_cert_filename TEXT",
    "ALTER TABLE inventory ADD COLUMN buyer_cert_path TEXT",
)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            # Column already exists — idempotent migration.
            pass
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
    zip_code: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
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
                    sku, title, description, city, state, zip_code,
                    contact_name, contact_email, contact_phone, chair_type,
                    dimensions, quantity_original, quantity_remaining,
                    price_per_chair, hero_image, status, parsed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    str(lot_id), seller_id, govdeals_url, folder_name, folder_path,
                    sku, title, description, city, state, zip_code,
                    contact_name, contact_email, contact_phone, chair_type,
                    dimensions, quantity, quantity, price_per_chair, hero_image,
                    now, now,
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
                    zip_code          = COALESCE(?, zip_code),
                    contact_name      = COALESCE(?, contact_name),
                    contact_email     = COALESCE(?, contact_email),
                    contact_phone     = COALESCE(?, contact_phone),
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
                    title, description, city, state, zip_code,
                    contact_name, contact_email, contact_phone, chair_type,
                    dimensions, quantity, price_per_chair, hero_image, now,
                    str(lot_id),
                ),
            )
        conn.commit()
    return get(lot_id)  # re-read


# Platform name → (url_column, timestamp_column). Adding a new surface =
# one migration + one entry here + the validator in the API layer.
_PLATFORM_COLUMNS: dict[str, tuple[str, str]] = {
    "facebook":    ("facebook_url",    "facebook_published_at"),
    "ebay":        ("ebay_url",        "ebay_published_at"),
    "fb_business": ("fb_business_url", "fb_business_published_at"),
    "ad":          ("ad_url",          "ad_published_at"),
}

# Only these platforms promote status draft→listed. A Facebook Business page
# post or an ad isn't a live marketplace listing; don't mark the lot "listed"
# just because we linked promotional content to it.
_MARKETPLACE_PLATFORMS: frozenset[str] = frozenset({"facebook", "ebay"})


def set_platform_url(
    lot_id: str, platform: str, url: str | None, clear_timestamp: bool = False
) -> dict | None:
    """Record a marketplace/promotional URL + publish timestamp.

    Platforms: 'facebook', 'ebay', 'fb_business', 'ad'. Used by run.py post-phase
    AND by the admin UI for listings posted manually.
    """
    if platform not in _PLATFORM_COLUMNS:
        raise ValueError(f"unknown platform: {platform}")
    col_url, col_ts = _PLATFORM_COLUMNS[platform]
    now = _now()
    ts = None if (url is None or clear_timestamp) else now
    with connect() as conn:
        conn.execute(
            f"UPDATE inventory SET {col_url} = ?, {col_ts} = ?, updated_at = ? "
            f"WHERE lot_id = ?",
            (url, ts, now, str(lot_id)),
        )
        # Promote to 'listed' on first successful publish — but only for
        # real marketplaces, not promotional posts/ads.
        if url and platform in _MARKETPLACE_PLATFORMS:
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
        "zip_code", "contact_name", "contact_email", "contact_phone",
        "govdeals_username", "govdeals_password",
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


def _sanitize_filename(name: str) -> str:
    """Strip directory components and unsafe chars from an uploaded filename."""
    base = Path(name or "").name
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in base).strip(". ")
    return safe or "buyer_cert"


def _cert_dir(lot_id: str) -> Path:
    safe_lot = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(lot_id))
    return ATTACHMENTS_ROOT / safe_lot


def buyer_cert_abs_path(row: dict) -> Path | None:
    """Resolve the stored `buyer_cert_path` (relative to ATTACHMENTS_ROOT) to an
    absolute filesystem path. Returns None if the row has no cert."""
    rel = (row or {}).get("buyer_cert_path")
    if not rel:
        return None
    p = (ATTACHMENTS_ROOT / rel).resolve()
    # Path-traversal guard: confine to ATTACHMENTS_ROOT.
    if not str(p).startswith(str(ATTACHMENTS_ROOT.resolve())):
        return None
    return p


def attach_buyer_cert(lot_id: str, filename: str, data: bytes) -> dict | None:
    """Persist `data` under ATTACHMENTS_ROOT/<lot_id>/<filename>, replacing
    any prior cert for the lot. Updates the inventory row."""
    if not get(lot_id):
        return None
    safe_name = _sanitize_filename(filename)
    target_dir = _cert_dir(lot_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    # Remove any prior cert file so we don't accumulate orphans on rename.
    existing = get(lot_id) or {}
    prior = buyer_cert_abs_path(existing)
    if prior and prior.exists():
        try:
            prior.unlink()
        except OSError:
            pass
    target = target_dir / safe_name
    target.write_bytes(data)
    rel = str(target.relative_to(ATTACHMENTS_ROOT))
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE inventory SET buyer_cert_filename = ?, buyer_cert_path = ?, "
            "updated_at = ? WHERE lot_id = ?",
            (safe_name, rel, now, str(lot_id)),
        )
        conn.commit()
    return get(lot_id)


def delete_buyer_cert(lot_id: str) -> dict | None:
    row = get(lot_id)
    if not row:
        return None
    prior = buyer_cert_abs_path(row)
    if prior and prior.exists():
        try:
            prior.unlink()
        except OSError:
            pass
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE inventory SET buyer_cert_filename = NULL, buyer_cert_path = NULL, "
            "updated_at = ? WHERE lot_id = ?",
            (now, str(lot_id)),
        )
        conn.commit()
    return get(lot_id)


def delete(lot_id: str) -> bool:
    row = get(lot_id)
    prior = buyer_cert_abs_path(row) if row else None
    if prior and prior.exists():
        try:
            prior.unlink()
        except OSError:
            pass
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
    zip_code: str | None = None,
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
                lot_id, title, description, city, state, zip_code, chair_type,
                dimensions, quantity_original, quantity_remaining, price_per_chair,
                folder_name, hero_image, status, parsed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                str(lot_id), title, description, city, state, zip_code, chair_type,
                dimensions, quantity, quantity, price_per_chair, folder_name,
                hero_image, now, now,
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

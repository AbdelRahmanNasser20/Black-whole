"""Inventory ledger — the bridge between parsed listings and marketplace publications.

Why this exists: FB and eBay draft URLs were previously fire-and-forget console
output. Re-running the pipeline on a lot we'd already published would spend
marketplace API budget a second time. This module is the single source of truth
for "what we've parsed, what's up where, and how many are left to sell."

Three tables, all in the shared Supabase Postgres DB (`blackwhole`):
  - `inventory`   : one row per GovDeals lot, keyed by lot_id
  - `inquiries`   : customer contact-form submissions (buy/sell)
  - `subscribers` : new-inventory alert signups (BLACKWHOLE-10); DDL of record
                    in `scripts/sql/001_subscribers.sql`

Both are read/written from the FastAPI dashboard and from run.py. Storage goes
through `automation.db` (psycopg over Supabase) — no ORM. Schema lives in
Supabase (managed via migrations), not created at runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import db
from .config import ATTACHMENTS_ROOT

# Re-export the connection opener so favorites.py (and any other caller) can do
# `inventory.connect()` exactly as before — it now hands back a psycopg
# connection with dict rows instead of a sqlite3.Connection.
connect = db.connect

PUBLIC_STATUSES = ("listed", "draft", "owned", "won_pickup")
ALL_STATUSES = (
    "draft", "listed", "hidden", "sold_out",
    "owned", "won_pickup", "active_bid", "lost",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row else None


def get(lot_id: str) -> dict | None:
    if not lot_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inventory WHERE lot_id = %s", (str(lot_id),)
        ).fetchone()
    return _row_to_dict(row)


def list_all(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM inventory WHERE status = %s ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM inventory ORDER BY updated_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def list_public() -> list[dict]:
    """Rows customers should see on /listings — visible and actually have stock.

    Includes everything in PUBLIC_STATUSES: marketplace listings/drafts AND lots
    we own or won at auction (`owned` / `won_pickup`) — those are real available
    inventory, not just GovDeals drafts. `lost` / `hidden` / `sold_out` stay off.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM inventory
            WHERE status = ANY(%s)
              AND (quantity_remaining IS NULL OR quantity_remaining > 0)
            ORDER BY
              CASE status WHEN 'listed' THEN 0 ELSE 1 END,
              COALESCE(quantity_remaining, 0) DESC,
              updated_at DESC
            """,
            (list(PUBLIC_STATUSES),),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    """Headline counts for the landing page (same visible set as list_public)."""
    statuses = list(PUBLIC_STATUSES)
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM inventory WHERE status = ANY(%s) "
            "AND (quantity_remaining IS NULL OR quantity_remaining > 0)",
            (statuses,),
        ).fetchone()["n"]
        chairs = conn.execute(
            "SELECT COALESCE(SUM(quantity_remaining), 0) AS n FROM inventory "
            "WHERE status = ANY(%s)",
            (statuses,),
        ).fetchone()["n"]
        cities = conn.execute(
            "SELECT COUNT(DISTINCT city) AS n FROM inventory "
            "WHERE city IS NOT NULL AND city != '' AND status = ANY(%s)",
            (statuses,),
        ).fetchone()["n"]
    return {"lots": int(total), "chairs": int(chairs or 0), "cities": int(cities)}


# Statuses whose lots belong in the Facebook Business catalog feed
# (BLACKWHOLE-7). Deliberately narrower than PUBLIC_STATUSES: 'draft' lots are
# unconfirmed GovDeals scrapes, not real sellable stock, so they stay off the
# FB shop even though they show on our own /listings page.
CATALOG_FEED_STATUSES = ("listed", "owned", "won_pickup")


def list_catalog_feed() -> list[dict]:
    """Sellable lots for the Facebook Business catalog feed (BLACKWHOLE-7).

    ``status IN CATALOG_FEED_STATUSES`` AND ``quantity_remaining > 0`` — sold-out
    (0/NULL), hidden, lost and draft lots are excluded. Price/image completeness
    (FB rejects rows missing either) is enforced downstream by
    ``automation.catalog_feed``, which drops incomplete rows. Read-only.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM inventory
            WHERE status = ANY(%s)
              AND quantity_remaining IS NOT NULL
              AND quantity_remaining > 0
            ORDER BY updated_at DESC
            """,
            (list(CATALOG_FEED_STATUSES),),
        ).fetchall()
    return [dict(r) for r in rows]


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
    hero_image_url: str | None = None,
    image_urls: list[str] | None = None,
) -> dict:
    """Create/update an inventory row from a completed pipeline run.

    Preserves user-editable fields on update: `quantity_remaining`, `status`,
    `price_per_chair` (if already set), `hero_image` (if already set), and any
    stored FB/eBay URLs. A re-run should refresh metadata, not stomp edits.

    `hero_image_url` / `image_urls` are the durable Supabase Storage URLs
    (BLACKWHOLE-6). They REFRESH on each run (a re-upload may improve them) but
    a None — meaning "no upload this run" — never wipes an existing value.
    """
    if not lot_id:
        raise ValueError("lot_id required")
    now = _now()
    img_urls_param = Jsonb(image_urls) if image_urls is not None else None
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
                    price_per_chair, hero_image, hero_image_url, image_urls,
                    status, parsed_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s)
                """,
                (
                    str(lot_id), seller_id, govdeals_url, folder_name, folder_path,
                    sku, title, description, city, state, zip_code,
                    contact_name, contact_email, contact_phone, chair_type,
                    dimensions, quantity, quantity, price_per_chair, hero_image,
                    hero_image_url, img_urls_param,
                    now, now,
                ),
            )
        else:
            # Keep user edits. Only refresh the "as-parsed" fields. Image URLs
            # refresh when a fresh upload supplied them, else keep what's there.
            conn.execute(
                """
                UPDATE inventory SET
                    seller_id         = COALESCE(%s, seller_id),
                    govdeals_url      = COALESCE(%s, govdeals_url),
                    folder_name       = COALESCE(%s, folder_name),
                    folder_path       = COALESCE(%s, folder_path),
                    sku               = COALESCE(%s, sku),
                    title             = COALESCE(%s, title),
                    description       = COALESCE(%s, description),
                    city              = COALESCE(%s, city),
                    state             = COALESCE(%s, state),
                    zip_code          = COALESCE(%s, zip_code),
                    contact_name      = COALESCE(%s, contact_name),
                    contact_email     = COALESCE(%s, contact_email),
                    contact_phone     = COALESCE(%s, contact_phone),
                    chair_type        = COALESCE(%s, chair_type),
                    dimensions        = COALESCE(%s, dimensions),
                    quantity_original = COALESCE(%s, quantity_original),
                    price_per_chair   = COALESCE(price_per_chair, %s),
                    hero_image        = COALESCE(hero_image, %s),
                    hero_image_url    = COALESCE(%s, hero_image_url),
                    image_urls        = COALESCE(%s, image_urls),
                    updated_at        = %s
                WHERE lot_id = %s
                """,
                (
                    seller_id, govdeals_url, folder_name, folder_path, sku,
                    title, description, city, state, zip_code,
                    contact_name, contact_email, contact_phone, chair_type,
                    dimensions, quantity, price_per_chair, hero_image,
                    hero_image_url, img_urls_param, now,
                    str(lot_id),
                ),
            )
        conn.commit()
    return get(lot_id)  # re-read


def set_images(
    lot_id: str, hero_image_url: str | None, image_urls: list[str] | None
) -> dict | None:
    """Stamp durable image URLs onto a row (backfill / out-of-band upload).

    None values are ignored (COALESCE), so this only ever adds/updates URLs —
    it never clears them.
    """
    now = _now()
    img_urls_param = Jsonb(image_urls) if image_urls is not None else None
    with connect() as conn:
        conn.execute(
            """
            UPDATE inventory SET
                hero_image_url = COALESCE(%s, hero_image_url),
                image_urls     = COALESCE(%s, image_urls),
                updated_at     = %s
            WHERE lot_id = %s
            """,
            (hero_image_url, img_urls_param, now, str(lot_id)),
        )
        conn.commit()
    return get(lot_id)


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
            f"UPDATE inventory SET {col_url} = %s, {col_ts} = %s, updated_at = %s "
            f"WHERE lot_id = %s",
            (url, ts, now, str(lot_id)),
        )
        # Promote to 'listed' on first successful publish — but only for
        # real marketplaces, not promotional posts/ads.
        if url and platform in _MARKETPLACE_PLATFORMS:
            conn.execute(
                "UPDATE inventory SET status = 'listed', updated_at = %s "
                "WHERE lot_id = %s AND status = 'draft'",
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
        "quantity_remaining", "quantity_original", "price_per_chair", "status", "hero_image",
        "title", "subtitle", "description", "chair_type", "dimensions", "city", "state",
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
    cols = ", ".join(f"{k} = %s" for k in clean)
    params = list(clean.values()) + [str(lot_id)]
    with connect() as conn:
        conn.execute(f"UPDATE inventory SET {cols} WHERE lot_id = %s", params)
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
            "UPDATE inventory SET buyer_cert_filename = %s, buyer_cert_path = %s, "
            "updated_at = %s WHERE lot_id = %s",
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
            "updated_at = %s WHERE lot_id = %s",
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
        cur = conn.execute("DELETE FROM inventory WHERE lot_id = %s", (str(lot_id),))
        conn.commit()
        return cur.rowcount > 0


def insert_manual(
    *,
    lot_id: str,
    title: str,
    quantity: int,
    subtitle: str | None = None,
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
                lot_id, title, subtitle, description, city, state, zip_code, chair_type,
                dimensions, quantity_original, quantity_remaining, price_per_chair,
                folder_name, hero_image, status, parsed_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s)
            """,
            (
                str(lot_id), title, subtitle, description, city, state, zip_code, chair_type,
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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', %s)
            RETURNING id
            """,
            (
                kind, str(lot_id) if lot_id else None, name.strip(),
                (email or "").strip() or None, (phone or "").strip() or None,
                quantity_interested, (message or "").strip() or None, now,
            ),
        )
        inquiry_id = cur.fetchone()["id"]
        conn.commit()
    return get_inquiry(inquiry_id)


def get_inquiry(inquiry_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inquiries WHERE id = %s", (int(inquiry_id),)
        ).fetchone()
    return _row_to_dict(row)


def list_inquiries(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM inquiries WHERE status = %s ORDER BY created_at DESC",
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
            "UPDATE inquiries SET status = %s WHERE id = %s",
            (status, int(inquiry_id)),
        )
        conn.commit()
    return get_inquiry(inquiry_id)


def link_inquiry(inquiry_id: int, lot_id: str | None) -> dict | None:
    with connect() as conn:
        conn.execute(
            "UPDATE inquiries SET lot_id = %s WHERE id = %s",
            (str(lot_id) if lot_id else None, int(inquiry_id)),
        )
        conn.commit()
    return get_inquiry(inquiry_id)


def delete_inquiry(inquiry_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM inquiries WHERE id = %s", (int(inquiry_id),)
        )
        conn.commit()
        return cur.rowcount > 0


# ───────────────────────────── subscribers ─────────────────────────────
# New-inventory alert signups (BLACKWHOLE-10). Distinct from `inquiries`
# (one-off contact) — a subscriber is a standing "ping me when chairs land"
# registration and the join surface for the CRM (BWCRM-26, match on
# email/phone). `unsubscribed` is the do-not-blast terminal state the future
# blast job filters on.

SUBSCRIBER_STATUSES = ("new", "contacted", "matched", "unsubscribed")
SUBSCRIBER_SOURCES = ("site_listings", "site_landing", "site_detail", "operator")


def create_subscriber(
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    quantity_wanted: int | None = None,
    use_case: str | None = None,
    chair_type: str | None = None,
    timeline: str | None = None,
    budget_per_chair: str | None = None,
    delivery: str | None = None,
    notes: str | None = None,
    source: str = "site_listings",
) -> dict:
    if not (email or "").strip() and not (phone or "").strip():
        raise ValueError("email or phone required")
    if source not in SUBSCRIBER_SOURCES:
        raise ValueError(f"invalid source: {source}")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO subscribers (
                name, email, phone, city, state, zip_code, quantity_wanted,
                use_case, chair_type, timeline, budget_per_chair, delivery,
                notes, source, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s)
            RETURNING id
            """,
            (
                (name or "").strip() or None,
                (email or "").strip() or None, (phone or "").strip() or None,
                (city or "").strip() or None, (state or "").strip() or None,
                (zip_code or "").strip() or None, quantity_wanted,
                (use_case or "").strip() or None, (chair_type or "").strip() or None,
                (timeline or "").strip() or None, (budget_per_chair or "").strip() or None,
                (delivery or "").strip() or None, (notes or "").strip() or None,
                source, now,
            ),
        )
        subscriber_id = cur.fetchone()["id"]
        conn.commit()
    return get_subscriber(subscriber_id)


def get_subscriber(subscriber_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE id = %s", (int(subscriber_id),)
        ).fetchone()
    return _row_to_dict(row)


def list_subscribers(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM subscribers WHERE status = %s ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM subscribers ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def set_subscriber_status(subscriber_id: int, status: str) -> dict | None:
    if status not in SUBSCRIBER_STATUSES:
        raise ValueError(f"invalid status: {status}")
    with connect() as conn:
        conn.execute(
            "UPDATE subscribers SET status = %s WHERE id = %s",
            (status, int(subscriber_id)),
        )
        conn.commit()
    return get_subscriber(subscriber_id)


def delete_subscriber(subscriber_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM subscribers WHERE id = %s", (int(subscriber_id),)
        )
        conn.commit()
        return cur.rowcount > 0

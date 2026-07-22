"""
SQLite cache for GovDeals chair listings.

Why: the slow/expensive step in govdeals_chairs_extraction.py is fetching the
long description for each asset (one Playwright page load per lot, polite
delay between loads). On a 150-listing run that's ~1 minute of pure I/O on a
healthy network — much more if Akamai is being unfriendly. Most listings
don't change between runs, so once we've seen the description we can skip the
page load entirely on subsequent runs.

What's cached (stable across runs):
  - title, description
  - quantity, quantity_source, quantity_confidence
  - location, lot_number

What's NOT cached (always read fresh from the live scrape):
  - price, end_date, time_left  — these change every run

Schema is intentionally minimal. Path defaults to ``state/listings.db``.
Override with the ``LISTINGS_DB_PATH`` env var.

Disable the cache entirely for one run with ``LISTINGS_DB_DISABLE=1``.
Force-refresh (ignore cached descriptions) with ``FETCH_GOVDEALS_FORCE_REFRESH=1``.

Quick inspection from the shell:
    .venv/bin/python listings_db.py            # stats
    .venv/bin/python listings_db.py --top 20   # 20 biggest cached lots
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from paths import STATE_DIR

DB_PATH = Path(os.getenv("LISTINGS_DB_PATH") or (STATE_DIR / "listings.db"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    asset_id               TEXT PRIMARY KEY,
    link                   TEXT NOT NULL,
    title                  TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    quantity               INTEGER,
    quantity_source        TEXT,
    quantity_confidence    TEXT,
    price                  TEXT,
    location               TEXT,
    lot_number             TEXT,
    end_date               TEXT,
    time_left              TEXT,
    image_url              TEXT NOT NULL DEFAULT '',
    pickup_zip             TEXT,
    contact_email          TEXT,
    contact_phone          TEXT,
    description_fetched_at TEXT,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_quantity ON listings(quantity);
CREATE INDEX IF NOT EXISTS idx_listings_end_date ON listings(end_date);
"""

# Defensive migrations for DBs created before a column was added. SQLite
# raises if the column already exists; we swallow that so connect() stays
# idempotent.
_MIGRATIONS = (
    "ALTER TABLE listings ADD COLUMN image_url TEXT NOT NULL DEFAULT ''",
    # Added 2026-05-08: pickup ZIP scraped from the GovDeals asset detail page.
    "ALTER TABLE listings ADD COLUMN pickup_zip TEXT",
    # Added 2026-05-08: GovDeals seller contact (admin-only, never on public site).
    "ALTER TABLE listings ADD COLUMN contact_email TEXT",
    "ALTER TABLE listings ADD COLUMN contact_phone TEXT",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_asset_id(url: str) -> str:
    """Return a stable cache key for a listing URL across supported sites.

    GovDeals: ``/en/asset/<assetId>/<sellerId>`` → ``<assetId>/<sellerId>``
              (legacy format — kept unprefixed for back-compat with existing rows).
    PublicSurplus: ``/sms/auction/view?auc=<aucId>`` → ``ps:<aucId>``.
    BidSpotter: ``bidspotter.com/…/lot-<lotGuid>`` → ``bs:<lotGuid>``
                (relists mint a new GUID, so one key = one auction round).

    Returns empty string if the URL matches none (uncacheable).
    """
    u = url or ""
    m = re.search(r"/asset/(\d+)/(\d+)", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"[?&]auc=(\d+)", u)
    if m:
        return f"ps:{m.group(1)}"
    m = re.search(
        r"bidspotter\.com/.*/lot-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        u,
    )
    if m:
        return f"bs:{m.group(1)}"
    return ""


def is_disabled() -> bool:
    return os.getenv("LISTINGS_DB_DISABLE") == "1"


def force_refresh() -> bool:
    return os.getenv("FETCH_GOVDEALS_FORCE_REFRESH") == "1"


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            # Column already exists — the migration already ran.
            pass
    conn.commit()
    return conn


def get_cached(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    if not asset_id:
        return None
    row = conn.execute("SELECT * FROM listings WHERE asset_id = ?", (asset_id,)).fetchone()
    return dict(row) if row else None


def upsert_listing(conn: sqlite3.Connection, listing: dict) -> str:
    """Insert or update a listing keyed by asset_id.

    Returns one of: ``"insert"``, ``"update"``, ``"skip"`` (uncacheable URL).
    Description is preserved on update if the new value is empty (so a run
    that didn't fetch descriptions never wipes a previously-cached one).
    """
    asset_id = extract_asset_id(listing.get("link") or "")
    if not asset_id:
        return "skip"

    now = _now()
    new_desc = listing.get("description") or ""
    desc_fetched_at = listing.get("description_fetched_at")
    if new_desc and not desc_fetched_at:
        desc_fetched_at = now

    new_image = listing.get("image_url") or ""
    new_zip = listing.get("pickup_zip") or ""
    new_email = listing.get("contact_email") or ""
    new_phone = listing.get("contact_phone") or ""

    existing = get_cached(conn, asset_id)
    if existing is None:
        conn.execute(
            """
            INSERT INTO listings (
                asset_id, link, title, description, quantity, quantity_source,
                quantity_confidence, price, location, lot_number, end_date,
                time_left, image_url, pickup_zip, contact_email, contact_phone,
                description_fetched_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                listing.get("link", ""),
                listing.get("title", ""),
                new_desc,
                listing.get("quantity"),
                listing.get("quantity_source"),
                listing.get("quantity_confidence"),
                listing.get("price"),
                listing.get("location"),
                listing.get("lot_number"),
                listing.get("end_date"),
                listing.get("time_left"),
                new_image,
                new_zip or None,
                new_email or None,
                new_phone or None,
                desc_fetched_at,
                now,
                now,
            ),
        )
        conn.commit()
        return "insert"

    # Update path: keep the old description / image / zip if the new one is empty.
    final_desc = new_desc or existing["description"] or ""
    final_image = new_image or (existing["image_url"] if "image_url" in existing.keys() else "") or ""
    final_zip = new_zip or (existing["pickup_zip"] if "pickup_zip" in existing.keys() else "") or ""
    final_email = new_email or (existing["contact_email"] if "contact_email" in existing.keys() else "") or ""
    final_phone = new_phone or (existing["contact_phone"] if "contact_phone" in existing.keys() else "") or ""
    final_desc_fetched_at = desc_fetched_at or existing["description_fetched_at"]
    conn.execute(
        """
        UPDATE listings SET
            link                   = ?,
            title                  = ?,
            description            = ?,
            quantity               = ?,
            quantity_source        = ?,
            quantity_confidence    = ?,
            price                  = ?,
            location               = ?,
            lot_number             = ?,
            end_date               = ?,
            time_left              = ?,
            image_url              = ?,
            pickup_zip             = ?,
            contact_email          = ?,
            contact_phone          = ?,
            description_fetched_at = ?,
            last_seen_at           = ?
        WHERE asset_id = ?
        """,
        (
            listing.get("link", existing["link"]),
            listing.get("title", existing["title"]),
            final_desc,
            listing.get("quantity", existing["quantity"]),
            listing.get("quantity_source", existing["quantity_source"]),
            listing.get("quantity_confidence", existing["quantity_confidence"]),
            listing.get("price", existing["price"]),
            listing.get("location", existing["location"]),
            listing.get("lot_number", existing["lot_number"]),
            listing.get("end_date", existing["end_date"]),
            listing.get("time_left", existing["time_left"]),
            final_image,
            final_zip or None,
            final_email or None,
            final_phone or None,
            final_desc_fetched_at,
            now,
            asset_id,
        ),
    )
    conn.commit()
    return "update"


def hydrate_from_cache(
    listings: list[dict],
    conn: sqlite3.Connection | None = None,
) -> tuple[int, int, int]:
    """Fill description + cached quantity into any listing already in the DB.

    Returns ``(cache_hits, cache_misses, relists)``. Listings are mutated in
    place. A listing is a "hit" only when the cached description is non-empty
    AND the cached end_date matches the fresh end_date from this scrape.

    Why the end_date check: GovDeals sometimes relists an ended auction under
    the same ``asset_id`` with a new end date. Without this check we'd hand
    back stale cached data and skip the fresh fetch — effectively missing the
    relist (which may have new quantity, price, condition, etc.). When the
    end_date differs we treat it as a cache miss so the pipeline re-fetches
    the description, re-runs quantity inference, and overwrites the row.

    Honors ``FETCH_GOVDEALS_FORCE_REFRESH=1`` (returns 0 hits, treats every
    listing as a miss) and ``LISTINGS_DB_DISABLE=1`` (returns 0 hits without
    even opening the DB).
    """
    if is_disabled() or force_refresh():
        return 0, len(listings), 0

    own_conn = conn is None
    if own_conn:
        conn = connect()
    hits = misses = relists = 0
    try:
        for item in listings:
            asset_id = extract_asset_id(item.get("link") or "")
            if not asset_id:
                misses += 1
                continue
            cached = get_cached(conn, asset_id)
            if not (cached and cached.get("description")):
                misses += 1
                continue
            # Cache-key extension: if end_date has rolled forward, the auction
            # has been relisted — treat as a miss so we re-fetch.
            cached_end = (cached.get("end_date") or "").strip()
            fresh_end = (item.get("end_date") or "").strip()
            if cached_end and fresh_end and cached_end != fresh_end:
                relists += 1
                misses += 1
                item["_cache_relist"] = True
                item["_cache_prev_end_date"] = cached_end
                continue
            # The cached description is the source of truth for this column.
            item["description"] = cached["description"]
            # Quantity from cache wins — at least as good as the live
            # title-only regex (which is also already in `item`).
            if cached.get("quantity"):
                item["quantity"] = cached["quantity"]
                item["quantity_source"] = (
                    cached.get("quantity_source") or item.get("quantity_source")
                )
                item["quantity_confidence"] = (
                    cached.get("quantity_confidence") or item.get("quantity_confidence")
                )
            # image_url: cache wins if present, since every run hits it fresh.
            if cached.get("image_url"):
                item["image_url"] = cached["image_url"]
            if cached.get("pickup_zip"):
                item["pickup_zip"] = cached["pickup_zip"]
            if cached.get("contact_email"):
                item["contact_email"] = cached["contact_email"]
            if cached.get("contact_phone"):
                item["contact_phone"] = cached["contact_phone"]
            item["_cache_hit"] = True
            hits += 1
    finally:
        if own_conn:
            conn.close()
    return hits, misses, relists


def store_listings(
    listings: list[dict],
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Upsert every listing. Returns a counter of {insert, update, skip}.

    Skips silently when ``LISTINGS_DB_DISABLE=1`` is set.
    """
    if is_disabled():
        return {"insert": 0, "update": 0, "skip": 0, "disabled": len(listings)}

    own_conn = conn is None
    if own_conn:
        conn = connect()
    counts = {"insert": 0, "update": 0, "skip": 0}
    try:
        for item in listings:
            kind = upsert_listing(conn, item)
            counts[kind] = counts.get(kind, 0) + 1
    finally:
        if own_conn:
            conn.close()
    return counts


def stats(conn: sqlite3.Connection | None = None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        with_desc = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE description != ''"
        ).fetchone()[0]
        biggest = conn.execute(
            "SELECT MAX(quantity) FROM listings"
        ).fetchone()[0] or 0
        return {
            "db": str(DB_PATH),
            "total_listings": total,
            "with_description": with_desc,
            "biggest_quantity": biggest,
        }
    finally:
        if own_conn:
            conn.close()


def top_by_quantity(limit: int = 20, conn: sqlite3.Connection | None = None) -> list[dict]:
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT asset_id, title, quantity, quantity_source, price, link, last_seen_at
            FROM listings
            WHERE quantity IS NOT NULL
            ORDER BY quantity DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def _cli() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Inspect the listings cache DB.")
    ap.add_argument("--top", type=int, metavar="N", help="print the top N rows by quantity")
    args = ap.parse_args()

    if args.top:
        rows = top_by_quantity(args.top)
        if not rows:
            print("(empty)")
            return 0
        for r in rows:
            print(
                f"  qty={r['quantity']:>6}  {r['quantity_source'] or '?':<15}  "
                f"{(r['title'] or '')[:50]:<50}  {r['link']}"
            )
        return 0

    print(json.dumps(stats(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

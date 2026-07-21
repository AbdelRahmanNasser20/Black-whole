"""One-off (re-runnable) transfer of the auction scrape cache into Supabase.

Source: auction_extractors/state/listings.db  (SQLite, table `listings`)
Target: Supabase Postgres, table `auction_listings` (created if absent).

Idempotent: creates the table if needed and upserts every row by asset_id, so
running it again after a fresh scrape brings Supabase up to date.

    python scripts/transfer_listings_to_supabase.py
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from automation import db  # noqa: E402  (must load .env first)

_DEFAULT_SQLITE_PATH = (
    Path(__file__).resolve().parent.parent
    / "auction_extractors" / "state" / "listings.db"
)


def _sqlite_path() -> Path:
    """Source SQLite cache, honoring ``LISTINGS_DB_PATH``.

    Must match ``auction_extractors/listings_db.py`` — the discovery cron points
    the scrape at ``/tmp/listings.db`` (ephemeral container fs), so the transfer
    has to read the same env var instead of the hardcoded state-dir default,
    which doesn't exist in the cron container.
    """
    return Path(os.getenv("LISTINGS_DB_PATH") or _DEFAULT_SQLITE_PATH)

# Mirror of the SQLite `listings` schema. *_at columns become timestamptz;
# end_date / time_left stay free-text (they hold strings like "April 15, 2026").
DDL = """
CREATE TABLE IF NOT EXISTS auction_listings (
    asset_id               text PRIMARY KEY,
    link                   text NOT NULL,
    title                  text NOT NULL,
    description            text NOT NULL DEFAULT '',
    quantity               integer,
    quantity_source        text,
    quantity_confidence    text,
    price                  text,
    location               text,
    lot_number             text,
    end_date               text,
    time_left              text,
    description_fetched_at timestamptz,
    first_seen_at          timestamptz NOT NULL,
    last_seen_at           timestamptz NOT NULL,
    image_url              text NOT NULL DEFAULT '',
    pickup_zip             text,
    contact_email          text,
    contact_phone          text
);
CREATE INDEX IF NOT EXISTS idx_auction_listings_quantity ON auction_listings(quantity);
CREATE INDEX IF NOT EXISTS idx_auction_listings_last_seen ON auction_listings(last_seen_at);
"""

COLS = [
    "asset_id", "link", "title", "description", "quantity", "quantity_source",
    "quantity_confidence", "price", "location", "lot_number", "end_date",
    "time_left", "description_fetched_at", "first_seen_at", "last_seen_at",
    "image_url", "pickup_zip", "contact_email", "contact_phone",
]
TS_COLS = {"description_fetched_at", "first_seen_at", "last_seen_at"}

# quantity: a failed-LLM run sends NULL; COALESCE keeps the last verified count
# instead of erasing it. Every other column takes the fresh value. The trailing
# WHERE guard stops a stale re-transfer (e.g. the dashboard reading the
# checked-in state/listings.db) from pushing last_seen_at — and every other
# column — backward over fresher Supabase rows.
_UPDATE_SET = ", ".join(
    (
        "quantity = COALESCE(EXCLUDED.quantity, auction_listings.quantity)"
        if c == "quantity"
        else f"{c} = EXCLUDED.{c}"
    )
    for c in COLS
    if c != "asset_id"
)

UPSERT = f"""
INSERT INTO auction_listings ({", ".join(COLS)})
VALUES ({", ".join(["%s"] * len(COLS))})
ON CONFLICT (asset_id) DO UPDATE SET
{_UPDATE_SET}
WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at
"""


def _row_to_params(row: sqlite3.Row) -> list:
    out = []
    for c in COLS:
        v = row[c]
        if c in TS_COLS and (v is None or v == ""):
            v = None  # empty string isn't a valid timestamptz
        out.append(v)
    return out


def main() -> None:
    sqlite_path = _sqlite_path()
    if not sqlite_path.exists():
        raise SystemExit(f"source not found: {sqlite_path}")

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(f"SELECT {', '.join(COLS)} FROM listings").fetchall()
    src.close()
    print(f"read {len(rows)} rows from {sqlite_path}")

    with db.connect() as conn:
        conn.execute(DDL)
        conn.cursor().executemany(UPSERT, [_row_to_params(r) for r in rows])
        conn.commit()

    total = db.fetch_one("SELECT count(*) AS n FROM auction_listings")["n"]
    print(f"auction_listings now holds {total} rows in Supabase")


if __name__ == "__main__":
    main()

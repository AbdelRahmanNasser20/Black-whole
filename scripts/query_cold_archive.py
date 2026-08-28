#!/usr/bin/env python
"""Query the closed-auction history that lives in R2, not in Postgres.

`deal_lots.raw` is cold-stored to Cloudflare R2 once a lot closes (see
`deals/raw_archive.py`) because keeping it in Supabase is what pushed the
database past the free tier's 500 MB read-only ceiling. The data is not gone —
it is 11x smaller and one HTTP range-request away. This is the reader.

DuckDB queries the objects **in place** over HTTPS; nothing is downloaded whole
and nothing is restored into Postgres. R2 charges no egress, so scanning the
whole archive costs nothing but time (~1-8s).

Two shapes live under the same prefix and both are readable:
  * `closed_2026-08-23.parquet` — the one-off bulk export, with the maestro
    response FLATTENED into top-level columns (assetId, currentBid, bidCount,
    assetShortDescription, ...). Fastest to scan; columnar and zstd-compressed.
  * `incremental/*.jsonl.gz` — what the daily `archive-raw` cron writes. One
    JSON object per line with the blob intact under `raw`, so fields are read
    with `json_extract_string(raw, '$.field')`.

Examples:
    python scripts/query_cold_archive.py --like chair --zero-bid
    python scripts/query_cold_archive.py --like "banquet" --max-bid 200 --limit 40
    python scripts/query_cold_archive.py --sql "SELECT count(*) FROM archive"
    python scripts/query_cold_archive.py --columns
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  (loads .env -> R2_PUBLIC_BASE)

PARQUET = "archive/deal_lots_raw/closed_2026-08-23.parquet"
INCREMENTAL = "archive/deal_lots_raw/incremental/*.jsonl.gz"


def _base() -> str:
    b = os.getenv("R2_PUBLIC_BASE")
    if not b:
        sys.exit("R2_PUBLIC_BASE is not set — see .env")
    return b.rstrip("/")


def _connect():
    try:
        import duckdb
    except ImportError:
        sys.exit("pip install duckdb")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    return con


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--like", help="case-insensitive substring of the lot title")
    ap.add_argument("--zero-bid", action="store_true", help="only lots that closed with no bids")
    ap.add_argument("--max-bid", type=float, help="only lots that closed at or below this price")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--incremental", action="store_true",
                    help="read the daily jsonl.gz objects instead of the bulk parquet")
    ap.add_argument("--columns", action="store_true", help="list available columns and exit")
    ap.add_argument("--sql", help="raw SQL; the archive is bound as the table `archive`")
    a = ap.parse_args()

    base, con = _base(), _connect()
    src = (f"read_json_auto('{base}/{INCREMENTAL}')" if a.incremental
           else f"read_parquet('{base}/{PARQUET}')")
    con.execute(f"CREATE VIEW archive AS SELECT * FROM {src}")

    t0 = time.time()
    if a.columns:
        rows = con.execute("DESCRIBE archive").fetchall()
        print("\n".join(f"  {r[0]:<38} {r[1]}" for r in rows))
        return 0

    if a.sql:
        rows = con.execute(a.sql).fetchall()
    else:
        # The parquet is flattened; the jsonl.gz keeps the blob under `raw`.
        title = ("json_extract_string(raw, '$.assetShortDescription')" if a.incremental
                 else "assetShortDescription")
        bid = ("CAST(json_extract_string(raw, '$.currentBid') AS DOUBLE)" if a.incremental
               else "currentBid")
        cnt = ("CAST(json_extract_string(raw, '$.bidCount') AS INTEGER)" if a.incremental
               else "bidCount")
        aid = "asset_id" if a.incremental else "assetId"
        acc = "account_id" if a.incremental else "accountId"

        where = ["1=1"]
        params: list = []
        if a.like:
            where.append(f"lower({title}) LIKE ?")
            params.append(f"%{a.like.lower()}%")
        if a.zero_bid:
            where.append(f"{cnt} = 0")
        if a.max_bid is not None:
            where.append(f"{bid} <= ?")
            params.append(a.max_bid)
        rows = con.execute(
            f"""SELECT {aid}, {acc}, {bid} AS bid, {cnt} AS bids, {title} AS title
                FROM archive WHERE {' AND '.join(where)}
                ORDER BY bid, bids LIMIT {int(a.limit)}""", params).fetchall()

    def _num(v, cast=float, default=0):
        # The bulk parquet stores maestro's fields as text, so currentBid /
        # bidCount arrive as strings; the jsonl.gz path casts in SQL. Coerce
        # here rather than assuming, so one odd row can't kill the whole print.
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    for r in rows:
        if len(r) == 5:
            # asset first, then account — swapping them 204s (see memory:
            # govdeals-url-asset-account-order).
            print(f"  https://www.govdeals.com/en/asset/{r[0]}/{r[1]}"
                  f"  ${_num(r[2]):<9.2f} bids={_num(r[3], int):<4} {str(r[4])[:64]}")
        else:
            print("  " + "  ".join(str(x) for x in r))
    print(f"\n  {len(rows):,} rows in {time.time() - t0:.1f}s "
          f"(scanned in place on R2 — no egress cost, nothing restored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

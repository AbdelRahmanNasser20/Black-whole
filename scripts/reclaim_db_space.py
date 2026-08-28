#!/usr/bin/env python
"""Return dead space to the disk when `VACUUM FULL` won't fit.

The problem this solves, learned the hard way on 2026-08-28: nulling
`deal_lots.raw` for 178,589 closed lots freed 432 MB *inside* the table and
changed `pg_database_size` by nothing. Postgres only hands space back to the
filesystem when the relation is rewritten, and `VACUUM FULL` rewrites by
building a second copy alongside the first — so on a volume that is already
full, the one command that can shrink the database is the one command that
cannot run:

    psycopg.errors.DiskFull: could not extend file "base/5/2862876"

**The way out is to rewrite the TOAST table by itself first.**
`VACUUM FULL pg_toast.pg_toast_<oid>` is accepted, and it needs only as much
free space as the *live* TOAST data — not the whole relation. Oversized values
(jsonb blobs, long descriptions) are exactly what TOAST holds, so on a table
bloated by blob churn this is where nearly all the recoverable space is:

    deal_lots TOAST   538 MB -> 106 MB   (33s)
    database         1147 MB -> 662 MB

That freed enough room for the ordinary whole-table `VACUUM FULL` to run.

Order matters and is not arbitrary. Small tables first, because each rewrite
needs only its own live size and every success buys headroom for the next; the
TOAST of the biggest table before that table itself; the whole table last.

`VACUUM FULL` takes an ACCESS EXCLUSIVE lock, so concurrent readers block for
the duration (~1-3 min on a 900 MB table). Suspend the Render crons first if a
blocked run would matter.

    python scripts/reclaim_db_space.py --dry-run
    python scripts/reclaim_db_space.py --table deal_lots
    python scripts/reclaim_db_space.py --all
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, db  # noqa: E402,F401


SIZES_SQL = """
    SELECT c.relname AS name,
           pg_total_relation_size(c.oid)                     AS total,
           pg_relation_size(c.oid)                           AS heap,
           COALESCE(pg_total_relation_size(c.reltoastrelid), 0) AS toast,
           t.relname                                         AS toast_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    ORDER BY pg_total_relation_size(c.oid)
"""


def db_size() -> int:
    return db.fetch_one("SELECT pg_database_size(current_database()) AS n")["n"]


def tables() -> list[dict]:
    return db.fetch_all(SIZES_SQL)


def _vacuum_full(target: str) -> None:
    # VACUUM cannot run inside a transaction block, hence autocommit.
    with db.connect(autocommit=True) as c:
        c.execute("SET statement_timeout = 0")
        c.execute(f"VACUUM FULL {target}")


def reclaim(row: dict, *, mb) -> None:
    """TOAST first (cheap, and where blob bloat lives), then the whole table."""
    name = row["name"]
    if row["toast_name"] and row["toast"] > 8192:
        before, t0 = row["toast"], time.time()
        try:
            _vacuum_full(f"pg_toast.{row['toast_name']}")
            after = db.fetch_one("SELECT pg_total_relation_size(%s) AS n",
                                 (f"pg_toast.{row['toast_name']}",))["n"]
            print(f"  toast {name:<22} {mb(before)} -> {mb(after)}  "
                  f"(freed {mb(before - after)}, {time.time() - t0:.0f}s)", flush=True)
        except Exception as e:                        # noqa: BLE001
            print(f"  toast {name:<22} FAILED {type(e).__name__}: {str(e)[:60]}",
                  flush=True)

    before, t0 = db.fetch_one("SELECT pg_total_relation_size(%s) AS n", (name,))["n"], time.time()
    try:
        _vacuum_full(name)
        with db.connect(autocommit=True) as c:
            c.execute(f"ANALYZE {name}")
        after = db.fetch_one("SELECT pg_total_relation_size(%s) AS n", (name,))["n"]
        print(f"  table {name:<22} {mb(before)} -> {mb(after)}  "
              f"(freed {mb(before - after)}, {time.time() - t0:.0f}s)", flush=True)
    except Exception as e:                            # noqa: BLE001
        # Not fatal: a table too big to rewrite right now may fit after the
        # smaller ones ahead of it have given their space back.
        print(f"  table {name:<22} FAILED {type(e).__name__}: {str(e)[:60]}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", action="append", help="only this table (repeatable)")
    ap.add_argument("--all", action="store_true", help="every public table, smallest first")
    ap.add_argument("--dry-run", action="store_true", help="show sizes and exit")
    a = ap.parse_args()

    def mb(n: int) -> str:
        return f"{n / 1e6:8.1f} MB"

    rows = tables()
    print(f"  database: {mb(db_size())}\n")
    for r in rows:
        print(f"  {r['name']:<28} {mb(r['total'])}  heap={mb(r['heap'])} "
              f"toast={mb(r['toast'])}")

    if a.dry_run or not (a.all or a.table):
        if not a.dry_run:
            print("\n  nothing selected — pass --all or --table NAME")
        return 0

    targets = [r for r in rows if (a.all or r["name"] in (a.table or []))]
    print(f"\n  rewriting {len(targets)} table(s), smallest first\n")
    for r in targets:
        reclaim(r, mb=mb)
    print(f"\n  database now: {mb(db_size())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

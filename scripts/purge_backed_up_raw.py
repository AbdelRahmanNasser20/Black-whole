#!/usr/bin/env python
"""Null `deal_lots.raw` for closed lots whose blob is provably archived.

Why a script and not a one-line UPDATE: the fast bulk form
(`UPDATE … WHERE (a,b,c) IN (SELECT … LIMIT n)`) is correct only while the
purgeable set is a subset of what has actually been exported to R2. Lots keep
closing while the purge runs — the first attempt on 2026-08-28 nulled 100,000
rows and 99 lots closed underneath it — so that subset relation has to be
re-proved at run time, not assumed from a check made an hour earlier.

So this does the proof first and refuses to run if it fails:

    archived keys  = every key in ~/.blackwhole/backups/deal_lots_raw_*.jsonl.gz
                     (the same bytes that were readback-verified into R2)
    purgeable keys = outcome_complete IS TRUE AND outcome IS NOT NULL
                     AND raw IS NOT NULL
    gate           = purgeable - archived  MUST be empty

`outcome IS NOT NULL` is load-bearing, not decoration: it keeps the purge safe
even if the outcome_complete/outcome invariant is ever violated, and it spares
the rows `deals/backfill.py` still needs. Reading the archive files also
exercises their gzip CRC, so a truncated backup fails the gate instead of
silently shrinking the archived set.

Nothing here reads `raw` back out of the database — re-exporting ~420 MB
through the pooler would burn egress on a project restricted partly for it.

Idempotent and resumable: pending is defined by state (`raw IS NOT NULL`), not
a cursor, so an interrupted run just resumes and a drained backlog is a no-op.

    python scripts/purge_backed_up_raw.py              # dry run (default)
    python scripts/purge_backed_up_raw.py --execute
    python scripts/purge_backed_up_raw.py --restore    # rollback from archive
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time

from automation import config, db  # noqa: F401  (config populates BLACKWHOLE_DB_URL)

BACKUP_GLOB = os.path.expanduser("~/.blackwhole/backups/deal_lots_raw_*.jsonl.gz")

PURGEABLE_SQL = """
    SELECT asset_id, account_id, auction_id FROM deal_lots
    WHERE outcome_complete IS TRUE AND outcome IS NOT NULL AND raw IS NOT NULL
"""

# The bulk form: one statement, one index scan, no per-row round trip. Safe
# only behind the coverage gate above.
PURGE_SQL = """
    UPDATE deal_lots SET raw = NULL
    WHERE (asset_id, account_id, auction_id) IN (
        SELECT asset_id, account_id, auction_id FROM deal_lots
        WHERE outcome_complete IS TRUE AND outcome IS NOT NULL AND raw IS NOT NULL
        LIMIT %s)
"""

RESTORE_SQL = """
    UPDATE deal_lots SET raw = %s
    WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND raw IS NULL
"""


def archived_keys(*, with_blobs: bool = False):
    """Key set (or key->blob map) from every verified local archive file."""
    files = sorted(glob.glob(BACKUP_GLOB))
    if not files:
        sys.exit(f"no archive files match {BACKUP_GLOB} — refusing to purge")
    out = {} if with_blobs else set()
    for path in files:
        n = 0
        with gzip.open(path, "rt") as fh:          # CRC-checks as it reads
            for line in fh:
                if not line.strip():
                    continue
                o = json.loads(line)
                k = (o["asset_id"], o["account_id"], o["auction_id"])
                if with_blobs:
                    out[k] = o["raw"]
                else:
                    out.add(k)
                n += 1
        print(f"  {os.path.basename(path)}: {n:,}")
    return out


def db_size() -> str:
    return db.fetch_one(
        "SELECT pg_size_pretty(pg_database_size(current_database())) AS s")["s"]


def coverage():
    """(purgeable keys, keys missing from the archive). Read-only."""
    arch = archived_keys()
    rows = db.fetch_all(PURGEABLE_SQL)
    purge = [(r["asset_id"], r["account_id"], r["auction_id"]) for r in rows]
    missing = [k for k in purge if k not in arch]
    print(f"\n  archived keys total : {len(arch):,}")
    print(f"  purgeable in DB     : {len(purge):,}")
    print(f"  NOT YET ARCHIVED    : {len(missing):,}   <-- must be 0 to purge")
    print(f"  db size             : {db_size()}")
    return purge, missing


def do_purge(batch: int) -> int:
    total, t0 = 0, time.time()
    while True:
        try:
            n = db.execute(PURGE_SQL, (batch,))
        except Exception as e:                      # noqa: BLE001
            # Read-only flip mid-run is the expected failure on a restricted
            # free-tier project. Everything nulled so far is committed, and
            # re-running resumes from wherever it stopped.
            print(f"\n  STOPPED: {type(e).__name__}: {e}")
            print(f"  nulled {total:,} before stopping; re-run to resume.")
            return total
        if not n:
            break
        total += n
        print(f"  nulled {total:,} ({time.time() - t0:.0f}s)", flush=True)
    return total


def do_restore(batch: int) -> int:
    blobs = archived_keys(with_blobs=True)
    rows = db.fetch_all("""SELECT asset_id, account_id, auction_id FROM deal_lots
        WHERE raw IS NULL AND outcome_complete IS TRUE""")
    todo = [(json.dumps(blobs[k]) if not isinstance(blobs[k], str) else blobs[k], *k)
            for k in ((r["asset_id"], r["account_id"], r["auction_id"]) for r in rows)
            if k in blobs]
    print(f"  restorable: {len(todo):,}")
    for i in range(0, len(todo), batch):
        db.executemany(RESTORE_SQL, todo[i:i + batch])
        print(f"  restored {min(i + batch, len(todo)):,}/{len(todo):,}", flush=True)
    return len(todo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="actually null (default: dry run)")
    ap.add_argument("--restore", action="store_true", help="rollback: refill raw from the archive")
    ap.add_argument("--batch", type=int, default=10000)
    a = ap.parse_args()

    if a.restore:
        print("RESTORE — refilling raw from local archive files")
        n = do_restore(min(a.batch, 1000))
        print(f"\nrestored {n:,}. db size now {db_size()}")
        return 0

    print("Coverage proof (read-only):")
    purge, missing = coverage()
    if missing:
        print(f"\nABORT: {len(missing):,} purgeable rows are not in any archive file.")
        print("Run the raw archiver first:  python -m deals.cli archive-raw")
        return 1
    if not purge:
        print("\nnothing pending — already drained.")
        return 0
    if not a.execute:
        print(f"\nDRY RUN — would null {len(purge):,} rows. Re-run with --execute.")
        return 0

    print(f"\nEXECUTE — nulling {len(purge):,} archived blobs")
    n = do_purge(a.batch)
    print(f"\nnulled {n:,}. db size now {db_size()}")
    print("Space is NOT returned until: VACUUM FULL deal_lots; ANALYZE deal_lots;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

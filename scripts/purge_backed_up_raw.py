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

The purge itself is restricted to a TEMP TABLE of the proven keys rather than
to the bare predicate. That is what closes the race: lots close continuously
(125 did during one 20-minute window), and a blanket
`WHERE outcome_complete IS TRUE AND raw IS NOT NULL` re-evaluates at execution
time, so a lot that closed one second after the gate would be nulled with its
blob nowhere. Joining against a fixed key set makes the purge provably a subset
of what was exported, no matter how long it runs.

A small delta is healed rather than fatal: missing keys are exported and
readback-verified first, then folded into the key set.

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

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, db, r2_images  # noqa: E402,F401 (config -> DB URL)
from deals.raw_archive import (  # noqa: E402
    ARCHIVE_PREFIX, parse_batch, serialize_batch)

BACKUP_GLOB = os.path.expanduser("~/.blackwhole/backups/deal_lots_raw_*.jsonl.gz")

PURGEABLE_SQL = """
    SELECT asset_id, account_id, auction_id FROM deal_lots
    WHERE outcome_complete IS TRUE AND outcome IS NOT NULL AND raw IS NOT NULL
"""

# Row-wise IN against a subquery over the TEMP key table. The predicate columns
# are repeated on the inner select so a lot that is somehow no longer purgeable
# is skipped even though its key is in the set.
PURGE_SQL = """
    UPDATE deal_lots SET raw = NULL
    WHERE (asset_id, account_id, auction_id) IN (
        SELECT l.asset_id, l.account_id, l.auction_id
        FROM deal_lots l JOIN purge_keys k
          ON l.asset_id = k.asset_id AND l.account_id = k.account_id
         AND l.auction_id = k.auction_id
        WHERE l.outcome_complete IS TRUE AND l.outcome IS NOT NULL
          AND l.raw IS NOT NULL
        LIMIT %s)
"""

TEMP_DDL = """
    CREATE TEMP TABLE purge_keys (
        asset_id BIGINT, account_id BIGINT, auction_id BIGINT
    ) ON COMMIT PRESERVE ROWS
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


def heal_missing(missing: list[tuple[int, int, int]]) -> str:
    """Export the keys that closed since the last archive, verify, keep a copy.

    Returns the local backup path. Raises rather than returning on any failure —
    an unverified export must never widen the set the purge is allowed to touch.
    """
    cfg = r2_images.env_config()
    if not cfg:
        sys.exit("R2 is not configured — cannot archive the delta")
    s3 = r2_images.client(cfg)

    want, rows = set(missing), []
    ids = sorted({k[0] for k in missing})
    for i in range(0, len(ids), 200):
        for r in db.fetch_all(
                """SELECT asset_id, account_id, auction_id, raw FROM deal_lots
                   WHERE asset_id = ANY(%s) AND raw IS NOT NULL""", (ids[i:i + 200],)):
            if (r["asset_id"], r["account_id"], r["auction_id"]) in want:
                rows.append(r)

    blob = serialize_batch(rows)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = f"{ARCHIVE_PREFIX}/heal_{stamp}_{len(rows)}.jsonl.gz"
    if not r2_images.put_object(s3, bucket=cfg["bucket"], path=path, data=blob,
                                content_type="application/gzip"):
        sys.exit(f"R2 upload failed for {path!r} — nothing purged")

    got = s3.get_object(Bucket=cfg["bucket"], Key=path)["Body"].read()
    if parse_batch(got) != [(r["asset_id"], r["account_id"], r["auction_id"]) for r in rows]:
        sys.exit(f"readback mismatch for {path!r} — nothing purged")

    local = os.path.expanduser(f"~/.blackwhole/backups/deal_lots_raw_heal_{stamp}.jsonl.gz")
    with open(local, "wb") as fh:
        fh.write(blob)
    print(f"  healed {len(rows):,} -> {path} ({len(blob)/1e6:.2f} MB) + {local}")
    return local


def do_purge(keys, batch: int) -> int:
    """Null only `keys`, batched, over one session holding the TEMP table."""
    total, t0 = 0, time.time()
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute(TEMP_DDL)
        with cur.copy("COPY purge_keys (asset_id, account_id, auction_id) "
                      "FROM STDIN") as cp:
            for k in keys:
                cp.write_row(k)
        cur.execute("CREATE INDEX ON purge_keys (asset_id, account_id, auction_id)")
        cur.execute("ANALYZE purge_keys")
        print(f"  key set loaded: {len(keys):,}", flush=True)

        while True:
            try:
                cur.execute(PURGE_SQL, (batch,))
            except Exception as e:                  # noqa: BLE001
                # A read-only flip mid-run is the expected failure on a
                # restricted free-tier project. Everything nulled so far is
                # committed; re-running resumes from wherever it stopped.
                print(f"\n  STOPPED: {type(e).__name__}: {e}")
                print(f"  nulled {total:,} before stopping; re-run to resume.")
                return total
            if not cur.rowcount:
                break
            total += cur.rowcount
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
        if not a.execute:
            print(f"\n  {len(missing):,} rows would be archived first (--execute).")
        else:
            print(f"\nHealing {len(missing):,} lots that closed since the last archive:")
            heal_missing(missing)
    if not purge:
        print("\nnothing pending — already drained.")
        return 0
    if not a.execute:
        print(f"\nDRY RUN — would null {len(purge):,} rows. Re-run with --execute.")
        return 0

    # Re-read the archive from disk so the healed delta is included, then purge
    # ONLY the intersection. Lots closing from here on are simply not in the
    # set — they wait for the next run instead of racing this one.
    arch = archived_keys()
    covered = [k for k in purge if k in arch]
    print(f"\nEXECUTE — nulling {len(covered):,} archived blobs "
          f"({len(purge) - len(covered):,} left for the next run)")
    n = do_purge(covered, a.batch)
    print(f"\nnulled {n:,}. db size now {db_size()}")
    print("Space is NOT returned until: VACUUM FULL deal_lots; ANALYZE deal_lots;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

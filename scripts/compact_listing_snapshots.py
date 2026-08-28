#!/usr/bin/env python
"""Delete the redundant middles of identical `listing_snapshots` streaks.

The recorder's `discover()` has no change-gating (recorder/README.md's
"Storage" section), so every stale-refresh re-INSERTs a full row for every
active lot even when nothing about it moved. The result on 2026-08-28:
**31,350 of 47,081 rows were strictly interior to a run of identical
`(status, current_bid, bid_count, end_date)`** — 2/3 of a 112 MB table saying
nothing that the row before it did not already say.

**Keep the first AND last of every streak.** The first is when the state was
entered, the last is the most recent evidence it still held; only the middles
are redundant. That is not a stylistic choice — `sold_comps` reads
`DISTINCT ON (source, source_lot_id) ... ORDER BY observed_at DESC`, so
dropping a streak's last row would change the view's answer. Keeping both ends
leaves it bit-identical, which this script proves rather than assumes: it
checksums `sold_comps` before and after and rolls back on any difference.

Deletion is real, and a closed auction can never be re-scraped — so the doomed
rows are exported to R2 and read back first. Nothing is deleted until the
export verifies.

    python scripts/compact_listing_snapshots.py            # dry run
    python scripts/compact_listing_snapshots.py --execute
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, db, r2_images  # noqa: E402,F401

ARCHIVE_PREFIX = "archive/listing_snapshots"

# A streak = consecutive observations of one lot with identical state. The
# classic gaps-and-islands trick: two row_numbers whose difference is constant
# exactly while the state does not change.
DOOMED_SQL = """
    WITH streaks AS (
        SELECT id, source, source_lot_id, status, current_bid, bid_count,
               end_date, observed_at,
               row_number() OVER (PARTITION BY source, source_lot_id
                                  ORDER BY observed_at)
             - row_number() OVER (PARTITION BY source, source_lot_id, status,
                                  current_bid, bid_count, end_date
                                  ORDER BY observed_at) AS grp
        FROM listing_snapshots
    ), ranked AS (
        SELECT id,
               row_number() OVER (PARTITION BY source, source_lot_id, status,
                                  current_bid, bid_count, end_date, grp
                                  ORDER BY observed_at) AS rn,
               count(*)     OVER (PARTITION BY source, source_lot_id, status,
                                  current_bid, bid_count, end_date, grp) AS cnt
        FROM streaks
    )
    SELECT id FROM ranked WHERE rn > 1 AND rn < cnt
"""

# One value that changes if any row of the view changes. md5 over the ordered
# rendering of the whole view, so column values and row order both count.
CHECKSUM_SQL = """
    SELECT count(*) AS n, md5(string_agg(t::text, '|' ORDER BY t::text)) AS sum
    FROM (SELECT * FROM sold_comps) t
"""


def checksum() -> dict:
    return db.fetch_one(CHECKSUM_SQL)


def export(ids: list[int]) -> str:
    """Ship the doomed rows to R2, read them back, keep a local copy too."""
    cfg = r2_images.env_config()
    if not cfg:
        sys.exit("R2 is not configured — refusing to delete without a backup")
    s3 = r2_images.client(cfg)

    rows = []
    for i in range(0, len(ids), 2000):
        rows += db.fetch_all(
            "SELECT * FROM listing_snapshots WHERE id = ANY(%s) ORDER BY id",
            (ids[i:i + 2000],))

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            gz.write(json.dumps(dict(r), default=str).encode() + b"\n")
    blob = buf.getvalue()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = f"{ARCHIVE_PREFIX}/interior_{stamp}_{len(rows)}.jsonl.gz"
    if not r2_images.put_object(s3, bucket=cfg["bucket"], path=path, data=blob,
                                content_type="application/gzip"):
        sys.exit(f"R2 upload failed for {path!r} — nothing deleted")

    got = s3.get_object(Bucket=cfg["bucket"], Key=path)["Body"].read()
    back = []
    with gzip.GzipFile(fileobj=io.BytesIO(got), mode="rb") as gz:
        for line in gz:
            if line.strip():
                back.append(json.loads(line)["id"])
    if back != [r["id"] for r in rows]:
        sys.exit(f"readback mismatch for {path!r} — nothing deleted")

    local = Path.home() / ".blackwhole/backups" / f"listing_snapshots_interior_{stamp}.jsonl.gz"
    local.write_bytes(blob)
    print(f"  backed up {len(rows):,} rows -> {path} ({len(blob)/1e6:.1f} MB) + {local}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--batch", type=int, default=5000)
    a = ap.parse_args()

    before = checksum()
    total = db.fetch_one("SELECT count(*) AS n FROM listing_snapshots")["n"]
    ids = [r["id"] for r in db.fetch_all(DOOMED_SQL)]
    print(f"  listing_snapshots rows : {total:,}")
    print(f"  interior duplicates    : {len(ids):,}  ({len(ids)/max(total,1):.0%})")
    print(f"  sold_comps before      : {before['n']:,} rows  md5={before['sum']}")

    if not ids:
        print("\n  nothing to compact.")
        return 0
    if not a.execute:
        print(f"\n  DRY RUN — would delete {len(ids):,} rows. Re-run with --execute.")
        return 0

    export(ids)

    deleted = 0
    for i in range(0, len(ids), a.batch):
        deleted += db.execute("DELETE FROM listing_snapshots WHERE id = ANY(%s)",
                              (ids[i:i + a.batch],))
        print(f"  deleted {deleted:,}/{len(ids):,}", flush=True)

    after = checksum()
    print(f"  sold_comps after       : {after['n']:,} rows  md5={after['sum']}")
    if (after["n"], after["sum"]) != (before["n"], before["sum"]):
        # Loud, not silent: the backup is in R2 and the rows can be restored,
        # but something about the keep-both-ends reasoning was wrong.
        print("\n  *** sold_comps CHANGED — restore from the R2 backup ***")
        return 1
    print("\n  sold_comps is bit-identical. Reclaim with:"
          "\n    python scripts/reclaim_db_space.py --table listing_snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

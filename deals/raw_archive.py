"""Cold-storage the `deal_lots.raw` blob for closed lots.

Why: `raw` is the full GovDeals maestro response kept per lot forever. It is
~2.7 KB/row against a ~473-byte row otherwise, and it is what pushed the
Supabase database past the free tier's 500 MB read-only ceiling. Nothing reads
it once a lot has closed — `store.due_for_poll` filters `outcome_complete IS
NOT TRUE` and `verdict_store.lots_for_analysis` filters `outcome IS NULL`, so
both only ever touch open lots. Relist detection (`deals/relist.py`) reads only
scalar columns and is unaffected.

But "nothing reads it *today*" is not "we will never want it": the operator
mines closed-auction history for pricing patterns, and a closed GovDeals page
cannot be re-scraped. So this does not delete — it *moves*. Blobs go to
Cloudflare R2 (10 GB free, zero egress) as gzipped JSONL, one object per run,
queryable in place with DuckDB:

    SELECT * FROM read_json_auto('https://<base>/archive/deal_lots_raw/incremental/*.jsonl.gz')

Gzip JSONL rather than Parquet on purpose: `pyarrow` is not a dependency and
would bloat the Docker image for every Render service. DuckDB reads either.

**The rule: never null on faith.** Every run exports, reads the object back out
of R2, and proves the returned keys match what it fetched. Any mismatch aborts
with nothing nulled. Pending work is defined by state (`raw IS NOT NULL`), not
a cursor, so a partial run simply resumes and an empty backlog is a no-op —
the same property that lets `backfill_classify` live as a cron.
"""
from __future__ import annotations

import gzip
import io
import json
import sys
from datetime import datetime, timezone

from automation import db, r2_images

ARCHIVE_PREFIX = "archive/deal_lots_raw/incremental"

# `outcome IS NOT NULL` is the load-bearing conjunct, not decoration: it keeps
# the purge safe even if the outcome_complete/outcome invariant is ever
# violated (such a row simply never becomes pending), and it protects the rows
# `deals/backfill.py` still needs (`outcome IS NULL AND end_utc < now()`).
# The lag absorbs anti-snipe extensions — a re-swept lot repopulates `raw` via
# upsert_lot's EXCLUDED.raw, and would just be archived again later.
PENDING_SQL = """
    SELECT asset_id, account_id, auction_id, raw
    FROM deal_lots
    WHERE outcome_complete IS TRUE
      AND outcome IS NOT NULL
      AND raw IS NOT NULL
      AND updated_at < now() - make_interval(hours => %s)
    ORDER BY closed_at NULLS LAST
    LIMIT %s
"""

COUNT_SQL = """
    SELECT count(*) AS n FROM deal_lots
    WHERE outcome_complete IS TRUE AND outcome IS NOT NULL AND raw IS NOT NULL
      AND updated_at < now() - make_interval(hours => %s)
"""

NULL_SQL = """
    UPDATE deal_lots SET raw = NULL
    WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND raw IS NOT NULL
"""


def _key(row) -> tuple[int, int, int]:
    return (row["asset_id"], row["account_id"], row["auction_id"])


def serialize_batch(rows) -> bytes:
    """Rows -> gzipped JSONL. Pure; the round-trip partner of parse_batch."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            line = json.dumps({
                "asset_id": r["asset_id"],
                "account_id": r["account_id"],
                "auction_id": r["auction_id"],
                "raw": r["raw"],
            }, default=str)
            gz.write(line.encode("utf-8") + b"\n")
    return buf.getvalue()


def parse_batch(blob: bytes) -> list[tuple[int, int, int]]:
    """Gzipped JSONL -> the keys it contains. Pure."""
    out = []
    with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as gz:
        for line in gz:
            if line.strip():
                o = json.loads(line)
                out.append((o["asset_id"], o["account_id"], o["auction_id"]))
    return out


def pending_count(lag_hours: int = 48) -> int:
    return db.fetch_one(COUNT_SQL, (lag_hours,))["n"]


def run_archive_raw(*, limit: int = 6000, lag_hours: int = 48,
                    null_after: bool = True) -> dict:
    """Export one batch of closed-lot blobs to R2, verify, then null them."""
    meter = {"pending": 0, "exported": 0, "nulled": 0, "bytes": 0, "key": None}

    cfg = r2_images.env_config()
    if not cfg:
        # Hard stop, never a silent skip: continuing would mean nulling blobs
        # with nowhere to put them.
        raise RuntimeError(
            "R2 is not configured (R2_ACCOUNT_ID/ACCESS_KEY_ID/SECRET_ACCESS_KEY/"
            "BUCKET/PUBLIC_BASE) — refusing to archive without a destination")

    meter["pending"] = pending_count(lag_hours)
    rows = db.fetch_all(PENDING_SQL, (lag_hours, limit))
    if not rows:
        print("[raw_archive] nothing pending", file=sys.stderr)
        return meter

    keys = [_key(r) for r in rows]
    blob = serialize_batch(rows)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"{ARCHIVE_PREFIX}/{stamp}_{len(rows)}.jsonl.gz"

    s3 = r2_images.client(cfg)
    if not r2_images.put_object(s3, bucket=cfg["bucket"], path=path,
                                data=blob, content_type="application/gzip"):
        raise RuntimeError(f"R2 upload failed for {path!r} — nothing nulled")

    # Read it back. An upload that "succeeded" but stored truncated or
    # unreadable bytes is exactly the failure that would make the null
    # unrecoverable, and it is invisible without this step.
    got = s3.get_object(Bucket=cfg["bucket"], Key=path)["Body"].read()
    if parse_batch(got) != keys:
        raise RuntimeError(
            f"readback mismatch for {path!r} — nothing nulled "
            f"(expected {len(keys)} keys)")

    meter["exported"] = len(keys)
    meter["bytes"] = len(blob)
    meter["key"] = path
    print(f"[raw_archive] verified {len(keys):,} blobs in {path} "
          f"({len(blob)/1e6:.1f} MB)", file=sys.stderr)

    if null_after:
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            db.executemany(NULL_SQL, chunk)
            meter["nulled"] += len(chunk)
            print(f"[raw_archive] nulled {meter['nulled']:,}/{len(keys):,}",
                  file=sys.stderr)
    return meter

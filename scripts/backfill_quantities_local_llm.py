#!/usr/bin/env python
"""Re-run the chair-quantity pass on stuck rows using the LOCAL Claude CLI.

Why this exists: the quantity pass NULLs a row's count when the LLM fails
(BLACKWHOLE-4 — an unverified regex number must never be trusted), and the read
path filters `WHERE quantity >= 50`, so a NULL row silently disappears from the
Auctions tab. On 2026-07-20 both hosted providers ran dry — Groq hit its 100k
tokens/day cap and the Gemini key's credits were depleted — which left 625 rows
with no count and emptied the site.

This backfill routes those rows through `provider="claude_max"`, i.e. the
operator's local `claude` CLI on their Claude subscription, so recovering the
listings costs no API credit. Results are written back as `quantity_source
='llm'`, the same trusted source any hosted provider produces — `trusted_
quantity()` treats them identically.

SCOPE — this is a local remedy, not a prod fix. The Render discovery cron runs
in Docker with no `claude` CLI and no subscription auth, so it still needs a
funded API provider. Run this locally after a failed run, or until credit is
restored.

Usage (from the repo root, venv active):
    python scripts/backfill_quantities_local_llm.py --dry-run
    python scripts/backfill_quantities_local_llm.py --limit 60
    python scripts/backfill_quantities_local_llm.py            # all stuck rows

Idempotent and resumable: it only ever selects rows whose quantity is still
NULL, so re-running picks up wherever the last run stopped.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "auction_extractors"))

# config first — importing it is what loads the repo-root .env (and therefore
# BLACKWHOLE_DB_URL). automation.db only reads the env var; it never loads it.
from automation import config as _config  # noqa: E402,F401
from automation import db  # noqa: E402
import quantity_llm  # noqa: E402

# Active listings first — those are the ones the site can actually show. Within
# that, biggest-looking lots first (longest description ~ most to extract from)
# so a --limit run buys the most visible recovery.
SELECT_STUCK = """
    SELECT asset_id, title, coalesce(description, '') AS description
    FROM auction_listings
    -- Doubled wildcards escape the literal percent signs: this query uses
    -- named placeholders, so a single one before a letter is read as a
    -- placeholder and psycopg raises ProgrammingError. Note that applies to
    -- comments too -- psycopg scans the whole string, so never write a bare
    -- percent-letter pair in one.
    WHERE link ILIKE '%%govdeals.com%%'
      AND quantity IS NULL
      AND quantity_source IN ('llm_failed', 'llm_missing')
      AND (%(active_only)s IS FALSE
           OR end_date > to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS'))
    ORDER BY length(coalesce(description, '')) DESC
    LIMIT %(limit)s
"""

UPDATE_ROW = """
    UPDATE auction_listings
    SET quantity = %(quantity)s,
        quantity_source = %(source)s,
        quantity_confidence = %(confidence)s
    WHERE asset_id = %(asset_id)s
      AND quantity IS NULL
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10_000, help="max rows to process")
    ap.add_argument("--dry-run", action="store_true", help="extract but do not write")
    ap.add_argument("--all-rows", action="store_true",
                    help="include already-ended lots (default: active only)")
    ap.add_argument("--model", default=None,
                    help="override CLAUDE_MAX_MODEL (default: sonnet)")
    args = ap.parse_args()

    if args.model:
        os.environ["CLAUDE_MAX_MODEL"] = args.model
    model = os.getenv("CLAUDE_MAX_MODEL", "sonnet")

    # Pin the provider chain to the local CLI. Passing gemini_api_key=None is
    # not enough — _provider_chain also reads GEMINI_API_KEY straight from the
    # environment, so a .env key would silently re-enter the chain and spend
    # hosted credit, which is exactly what this script exists to avoid. Naming
    # the primary as the only candidate leaves the chain a single entry.
    os.environ["QUANTITY_LLM_FALLBACK"] = "claude_max"

    rows = db.fetch_all(SELECT_STUCK, {
        "limit": args.limit,
        "active_only": not args.all_rows,
    })
    if not rows:
        print("Nothing to backfill — no stuck rows match.")
        return 0

    chunks = (len(rows) + quantity_llm._CHUNK - 1) // quantity_llm._CHUNK
    print(f"{len(rows)} stuck rows -> ~{chunks} chunks via local claude CLI "
          f"(model={model}, active_only={not args.all_rows})")

    t0 = time.time()
    out = quantity_llm.refine_quantities_with_llm(
        [dict(r) for r in rows],
        provider="claude_max",
        # Local CLI only — no hosted fallback, so a dry provider can't quietly
        # spend credit we're trying to avoid spending.
        ollama_base_url="", ollama_model="", ollama_timeout=1,
        groq_api_key=None, openai_api_key=None, gemini_api_key=None,
    )
    elapsed = time.time() - t0

    recovered = [r for r in out if r.get("quantity") is not None
                 and r.get("quantity_source") == "llm"]
    still_stuck = len(out) - len(recovered)
    big = [r for r in recovered if (r.get("quantity") or 0) >= 50]

    print(f"\nextracted in {elapsed:.0f}s: {len(recovered)}/{len(out)} recovered, "
          f"{still_stuck} still stuck, {len(big)} at qty>=50 (these become visible)")

    if args.dry_run:
        for r in big[:15]:
            print(f"  [dry-run] {r['asset_id']}: qty={r['quantity']} "
                  f"({r.get('quantity_confidence')}) {r['title'][:55]}")
        print("\n--dry-run: nothing written.")
        return 0

    written = 0
    with db.connect() as conn, conn.cursor() as cur:
        for r in recovered:
            cur.execute(UPDATE_ROW, {
                "quantity": int(r["quantity"]),
                "source": "llm",
                "confidence": r.get("quantity_confidence") or "unknown",
                "asset_id": r["asset_id"],
            })
            written += cur.rowcount
    print(f"wrote {written} rows to auction_listings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

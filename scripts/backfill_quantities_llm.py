#!/usr/bin/env python
"""Re-run the quantity LLM over auction_listings rows whose count is untrusted.

Two failure modes feed this script, and neither heals on its own:

  1. BLACKWHOLE-4 — the regex quantity path misreads comma-formatted counts
     ("2,100" -> 2) and lot/asset numbers ("UAB ... (1)" -> 105), so
     `regex_*` rows are excluded from the read surface.
  2. The nightly sweep's LLM pass NULLs a whole chunk's quantity the moment
     its provider chain gives out (`quantity_source='llm_failed'`), because an
     unverified regex seed must never be shipped as a trusted count. Groq's
     free tier runs dry partway through most sweeps, so 20-56% of every
     GovDeals scrape has landed llm_failed since 2026-07-20 — 419 of 743 rows
     on 2026-08-16.

`top_chairs.TRUSTED_QUANTITY_SOURCES` serves only `llm` / `structured`, so a
row in either state is invisible to the Auctions tab AND to alerts. This script
was written as a one-shot corrective pass for (1) and never scheduled, so (2)
accumulated: https://www.govdeals.com/en/asset/420/9312 "LOT: (199) BANQUET
CHAIRS" (Las Vegas, NV — 199 chairs) sat llm_failed from 2026-08-05 until
2026-08-16 and was never alerted on. It now runs as the `quantity-backfill`
Render cron (see render.yaml) so failed rows self-heal within a day.

Idempotent and resumable: a recovered row flips to `quantity_source='llm'` and
stops matching the candidate query, so re-running is a no-op once drained.

Usage (from the repo root, venv active):
    ./.venv/bin/python scripts/backfill_quantities_llm.py --dry-run --limit 5
    ./.venv/bin/python scripts/backfill_quantities_llm.py --limit 300
    ./.venv/bin/python scripts/backfill_quantities_llm.py --sources llm_failed
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
# The flat modules in auction_extractors/ (quantity_llm, paths, listings_db)
# still use bare imports of each other, so the directory itself has to be on
# the path. Absolute, not the old relative "auction_extractors": Render's cron
# sets WORKDIR=/app rather than cd-ing here, and a relative entry silently
# resolves to nothing if cwd ever moves.
sys.path.insert(0, os.path.join(REPO_ROOT, "auction_extractors"))

# config first — importing it is what loads the repo-root .env (and therefore
# BLACKWHOLE_DB_URL). automation.db only reads the env var; it never loads it.
from automation import config as _config  # noqa: E402,F401
from automation import db  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO_ROOT, "auction_extractors", ".env"))

# Reuse the read surface's own liveness test rather than re-deriving one. Both
# this script and /api/auctions must agree on "would this row be shown", or we
# spend quota reviving rows the site will drop anyway.
from auction_extractors.top_chairs import _is_active, _is_non_chair_lot  # noqa: E402
from quantity_infer import explicit_title_quantity  # noqa: E402
from quantity_llm import refine_quantities_with_llm  # noqa: E402

BATCH = 12
SOURCES = ("regex_fulltext", "regex_title", "llm_failed", "llm_missing")

# groq, not gemini: the Gemini key's prepay balance is spent and every call
# comes back 429 RESOURCE_EXHAUSTED, so the old default burned a round trip per
# chunk before falling through. Groq's free tier is the one that actually
# answers — and is the same provider the nightly sweep uses (LLM_PROVIDER=groq).
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER_BACKFILL", "groq")

# Matches the read surface's `max_stale_days=2` default. A listing the scrapers
# stopped re-seeing that long ago has almost certainly closed, whatever its
# end_date column claims.
DEFAULT_MAX_STALE_DAYS = 2

# The Auctions tab and the alert path both query `quantity >= 50`, so
# recovering a 4-chair lot changes nothing anyone will see. A count at or above
# that threshold sitting in the title is the cheapest predictor we have of a
# bulk lot worth a request — it is exactly the shape of the row we lost,
# "LOT: (199) BANQUET CHAIRS".
BULK_TITLE_HINT = 50

_CHAIR_WORD_RE = re.compile(r"chair|seating|banquet", re.I)

# Pull only rows that are plausibly still live. The recency bound is what keeps
# this off the full 15k-row table; end_date is TEXT in four mutually
# incompatible shapes (GovDeals ISO-naive "2026-08-21T17:00:13" AND human
# "April 24, 2026 05:00 PM EDT", BidSpotter/PublicSurplus ISO-Z, and 1.4k empty
# PS rows), so it is parsed in Python by _is_active instead of cast in SQL.
# String-comparing those formats is not a near miss but backwards: 'A' > '2',
# so every ended human-format row would read as active.
SELECT_CANDIDATES = """
    SELECT asset_id, link, title, coalesce(description, '') AS description,
           quantity AS old_q, quantity_source, end_date, last_seen_at
    FROM auction_listings
    WHERE quantity_source = ANY(%(sources)s)
      AND last_seen_at >= now() - make_interval(days => %(window_days)s)
"""

# The guard on quantity_source is what makes a concurrent write safe: the
# nightly sweep upserts the same rows, and without it a slow batch here could
# overwrite a fresher count the sweep had just landed.
UPDATE_ROW = """
    UPDATE auction_listings
    SET quantity = %(quantity)s,
        quantity_source = 'llm',
        quantity_confidence = %(confidence)s
    WHERE asset_id = %(asset_id)s
      AND quantity_source = ANY(%(sources)s)
"""


def _end_dt(row: dict) -> datetime:
    """Auction close time as a UTC datetime, or "far future" when unknown.

    Unknown sorts last on purpose: a row we cannot date is a worse bet than one
    we know closes tomorrow, but it is still a candidate (Public Surplus leaves
    end_date empty on 1.4k rows and those lots are real).
    """
    from dateutil import parser as _date_parser

    raw = (row.get("end_date") or "").strip()
    if raw:
        try:
            dt = _date_parser.parse(raw, fuzzy=True)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            pass
    return datetime.max.replace(tzinfo=timezone.utc)


def _priority(title: str | None) -> int:
    """0 = looks like a bulk chair lot, 1 = some kind of chair, 2 = neither.

    Rows are sorted by this before end date so that a --limit that cannot
    cover the backlog spends its requests on the lots that would actually
    surface, not on the sailboats and automotive fluids the sweep also drags
    into llm_failed.

    The count is read by ``explicit_title_quantity``, not by "does any number
    in the title clear 50". Every Public Surplus title is prefixed with its
    asset number — "#4062937 - 5 Green Office Chairs" — so the loose test
    scored a five-chair lot as a bulk candidate and the whole PS backlog
    crowded out the GovDeals lots the limit was meant to buy.
    """
    t = title or ""
    if not _CHAIR_WORD_RE.search(t) or _is_non_chair_lot(t):
        return 2
    claim = explicit_title_quantity(t)
    if claim and claim[0] >= BULK_TITLE_HINT:
        return 0
    return 1


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--limit", type=int, default=300,
        help="max rows to send to the LLM this run (default: 300 = 25 requests "
             "at BATCH=12; Groq's free 14,400/day is already shared with six "
             "deals-* crons)",
    )
    ap.add_argument(
        "--provider", default=DEFAULT_PROVIDER,
        help=f"quantity_llm provider (default: $LLM_PROVIDER_BACKFILL or "
             f"{DEFAULT_PROVIDER!r})",
    )
    ap.add_argument(
        "--sources", nargs="+", default=list(SOURCES), metavar="SRC",
        help=f"quantity_source values to re-parse (default: {' '.join(SOURCES)})",
    )
    ap.add_argument(
        "--max-stale-days", type=int, default=DEFAULT_MAX_STALE_DAYS,
        help="drop rows not re-seen by a scrape within this many days "
             f"(default: {DEFAULT_MAX_STALE_DAYS}, matching /api/auctions)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="select and rank candidates, then stop — no LLM call, no write",
    )
    return ap.parse_args()


def _load_candidates(sources: list[str], max_stale_days: int) -> list[dict]:
    """Live, untrusted-quantity rows, best bets first."""
    rows = db.fetch_all(
        SELECT_CANDIDATES,
        # +1 day of slack because _is_active compares truncated whole days
        # (`(now - seen).days > max_stale_days`); the SQL bound has to be a
        # superset of the Python filter or it would drop rows the site keeps.
        {"sources": sources, "window_days": max_stale_days + 1},
    )
    now = datetime.now(timezone.utc)
    live = []
    for r in rows:
        # _is_active wants last_seen_at as an ISO string; psycopg hands back a
        # datetime for timestamptz. Same normalization auctions_supabase does.
        seen = r.get("last_seen_at")
        r["last_seen_at"] = seen.isoformat() if isinstance(seen, datetime) else seen
        if _is_active(r, now, max_stale_days):
            live.append(r)
    live.sort(key=lambda r: (_priority(r.get("title")), _end_dt(r)))
    return live


def main() -> int:
    args = _parse_args()
    sources = list(args.sources)

    candidates = _load_candidates(sources, args.max_stale_days)
    selected = candidates[: max(0, args.limit)]
    tiers = [sum(1 for r in selected if _priority(r.get("title")) == t) for t in (0, 1, 2)]
    print(
        f"{len(candidates)} live rows with untrusted quantity "
        f"(sources: {' '.join(sources)}; max_stale_days={args.max_stale_days}); "
        f"taking {len(selected)} "
        f"[bulk-chair={tiers[0]} chair={tiers[1]} other={tiers[2]}] "
        f"in {(len(selected) + BATCH - 1) // BATCH} request(s) via {args.provider}",
        flush=True,
    )

    if args.dry_run:
        for r in selected[:20]:
            end = r.get("end_date") or "?"
            print(
                f"  [dry-run] p{_priority(r.get('title'))} {r['asset_id']:>24} "
                f"ends {end:<28} {(r.get('title') or '')[:60]}",
                flush=True,
            )
        if len(selected) > 20:
            print(f"  [dry-run] ... and {len(selected) - 20} more", flush=True)
        print("SUMMARY: --dry-run — no LLM call, nothing written.", flush=True)
        return 0

    if not selected:
        print("SUMMARY: nothing pending — exiting clean.", flush=True)
        return 0

    t0 = time.time()
    updated = changed = no_count = failed_batches = 0
    for i in range(0, len(selected), BATCH):
        chunk = selected[i : i + BATCH]
        try:
            out = refine_quantities_with_llm(
                [{"title": r["title"], "description": r.get("description") or ""} for r in chunk],
                provider=args.provider,
                ollama_base_url="",
                ollama_model="",
                ollama_timeout=300,
                groq_api_key=os.getenv("GROQ_API_KEY"),
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                gemini_api_key=os.getenv("GEMINI_API_KEY"),
            )
        except Exception as e:  # noqa: BLE001
            failed_batches += 1
            print(f"  batch {i // BATCH} failed: {e!r}", flush=True)
            continue
        for r, o in zip(chunk, out):
            q = o.get("quantity")
            if not q:
                # The LLM pass failed or skipped this row and already NULLed it.
                # Leave the row untouched so the next run retries it, rather
                # than writing a count nothing verified (BLACKWHOLE-4 AC4).
                no_count += 1
                continue
            db.execute(
                UPDATE_ROW,
                {
                    "quantity": q,
                    "confidence": o.get("quantity_confidence", "medium"),
                    "asset_id": r["asset_id"],
                    "sources": sources,
                },
            )
            updated += 1
            if q != r["old_q"]:
                changed += 1
        if (i // BATCH) % 10 == 0:
            print(f"  ...{i + len(chunk)}/{len(selected)} done, {changed} changed", flush=True)

    print(
        f"SUMMARY: {len(selected)} attempted, {updated} now llm-sourced, "
        f"{changed} quantities changed, {no_count} still without a count, "
        f"{failed_batches} batch(es) failed, {len(candidates) - len(selected)} "
        f"left for the next run, {time.time() - t0:.0f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

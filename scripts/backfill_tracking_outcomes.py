#!/usr/bin/env python
"""Correct closed tracked lots stored as 'sold' that never sold.

Before the fix, a lot's outcome came from its bid count alone, and a lot that
closed on the clock while the bidbox still read STA was stored 'sold' forever.
3357/527 ran to 20 bids / $1,850 with the reserve unmet. This re-reads the
bidbox of every closed tracked lot and, where GovDeals says RNM/CNB (reserve
not met) or CAN (cancelled), corrects `tracked_lots.status` and
`deal_lots.outcome`. A confirmed non-sale is never turned back into 'sold'.

    .venv/bin/python scripts/backfill_tracking_outcomes.py --dry-run
    .venv/bin/python scripts/backfill_tracking_outcomes.py --apply

`--dry-run` only reads (DB SELECT + GovDeals bidbox GETs) and prints what it
would change. Idempotent: a second `--apply` finds nothing to do.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  (loads .env)
from deals import sites, tracking, tracking_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the corrections, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the corrections")
    a = ap.parse_args()

    rows = tracking_store.closed_for_backfill()
    changes = tracking.backfill_outcomes(sites.get_adapter("govdeals"), rows, apply=a.apply)
    verb = "fixed" if a.apply else "would fix"
    for c in changes:
        (s_old, s_new), (o_old, o_new) = c["status"], c["outcome"]
        parts = []
        if s_new:
            parts.append(f"status {s_old} → {s_new}")
        if o_new:
            parts.append(f"outcome {o_old} → {o_new}")
        print(f"{verb} {c['key'][0]}/{c['key'][1]} (auction {c['key'][2]}, bidbox "
              f"{c['bidbox'] or 'unreadable'}): {', '.join(parts)}  {c['title'] or ''}")
    print(f"{len(rows)} closed tracked lot(s) checked, {len(changes)} {verb}"
          + ("" if a.apply else " — re-run with --apply to write"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

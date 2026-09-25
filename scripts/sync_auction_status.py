#!/usr/bin/env python
"""Reconcile the ledger with GovDeals: expire dead auctions, catch relists.

The same pass the web process runs every 30 minutes (`_auction_sync_loop` in
`automation/web/app.py`), for the terminal. Lots we are bidding on
(`status = 'active_bid'`) whose auction has closed are taken off the site and
the FB catalog feed as fake-sold-out; lots we already expired that come back
live are restored and announced on Telegram.

    .venv/bin/python scripts/sync_auction_status.py --once --dry-run
    .venv/bin/python scripts/sync_auction_status.py --once
    .venv/bin/python scripts/sync_auction_status.py --once --lot gd-420-9312

`--dry-run` writes nothing — not the ledger, not the watch table, no Telegram —
and prints the decision it would have made for every lot. Run it first.

Exit code is 1 when the pass could not run at all (missing migration); a lot
the sync could not read is reported as `unresolved` and is not an error exit,
because an unreadable lot is left untouched by design.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  (loads .env)
from automation import auction_sync  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="run a single pass (the only mode; the loop lives in the web process)")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and print, write nothing, send nothing")
    ap.add_argument("--lot", action="append", dest="lots", metavar="LOT_ID",
                    help="restrict the pass to these lot ids (repeatable). Ignores the "
                         "status filter, so this can force-check a lot that is not active_bid")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args()

    if not args.once:
        ap.error("pass --once (there is no daemon mode here — the web process runs the loop)")

    report = auction_sync.sync_once(dry_run=args.dry_run, lot_ids=args.lots)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        head = "DRY RUN — nothing written" if report.get("dry_run") else "applied"
        print(f"\n[auction-sync] {head}: checked={report['checked']} "
              f"expired={report['expired']} relisted={report['relisted']} "
              f"unchanged={report['unchanged']} unresolved={report['unresolved']} "
              f"errors={report['errors']}")
        for a in report.get("actions", []):
            print(f"  {a['action']:<7} {a['lot_id']}  ({a['reason']})")
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Put the buyer behind every existing BUY inquiry on the alert list.

New inquiries do this by themselves (`inventory.subscribe_from_inquiry`, called
from POST /contact). This script covers the rows that came before.

    .venv/bin/python scripts/backfill_inquiry_subscribers.py            # dry run
    .venv/bin/python scripts/backfill_inquiry_subscribers.py --apply

Safe to run again: a person already on the list (same email or phone) is skipped.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import inventory  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    args = ap.parse_args()
    with inventory.connect() as conn:
        rows = conn.execute("SELECT * FROM inquiries WHERE kind = 'buy' ORDER BY id").fetchall()
    added = 0
    for r in rows:
        tag = f"inquiry #{r['id']} qty={r.get('quantity_interested')} zip={r.get('zip_code')} " \
              f"email={'y' if r.get('email') else 'n'} phone={'y' if r.get('phone') else 'n'}"
        if not args.apply:
            print("would check:", tag)
            continue
        sub = inventory.subscribe_from_inquiry(r)
        added += bool(sub)
        print(("added subscriber #%s <- " % sub["id"]) if sub else "skipped (on the list / not eligible) <- ", tag)
    print(f"{len(rows)} buy inquiries; {added} added" if args.apply else f"{len(rows)} buy inquiries (dry run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Price anchors — daily search for banquet chairs priced above ours.

  .venv/bin/python scripts/price_anchors.py run [--dry-run] [--no-photos]
  .venv/bin/python scripts/price_anchors.py seed scripts/data/price_anchors_seed_2026-10-10.json
  .venv/bin/python scripts/price_anchors.py list [--tier hotel|event|new]
  .venv/bin/python scripts/price_anchors.py photos

`run` = one Claude Haiku research turn (web search + fetch) → upsert into
public.price_anchors → copy new photos to the private R2 bucket → mark rows not
seen for 21 days `gone` → one Telegram line. Needs ANTHROPIC_API_KEY, the DB,
and (for photos) R2_* + LOT_ARCHIVE_R2_BUCKET. Migration 026 must be applied.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from automation import config  # noqa: F401,E402  (loads .env)
from automation import price_anchors as pa  # noqa: E402


def _photos() -> str:
    try:
        saved, failed = pa.archive_missing_images()
    except Exception as e:  # noqa: BLE001 - reported, never silent
        return f"photos NOT saved ({type(e).__name__}: {e})"
    return f"photos saved {saved}, failed {failed}"


def cmd_run(args) -> int:
    known = [] if args.dry_run else pa.known_urls()
    res = pa.search(known_urls=known, max_searches=args.max_searches)
    print(f"{len(res.listings)} listings, {len(res.rejected)} rejected, "
          f"{res.searches} searches, ~${pa.cost_usd(res):.3f}, stop={res.stop_reason}")
    for url, why in res.rejected:
        print(f"  rejected: {why} — {url}")
    if args.dry_run:
        print(json.dumps(res.listings, indent=2, default=str))
        return 0
    out = pa.upsert(res.listings)
    new = sum(1 for r in out if r["inserted"])
    photos = "photos skipped" if args.no_photos else _photos()
    gone = pa.mark_gone()
    line = (f"Price anchors: {len(out)} seen ({new} new), {gone} marked gone, {photos}. "
            f"~${pa.cost_usd(res):.3f}")
    print(line)
    try:
        from automation import telegram_alerts as tg
        if tg.is_configured():
            tg.send_message_sync(line, topic="deals")
    except Exception as e:  # noqa: BLE001 - the alert is best-effort
        print(f"telegram failed: {e}", file=sys.stderr)
    return 0 if res.listings else 1


def cmd_seed(args) -> int:
    items = json.loads(pathlib.Path(args.path).read_text())
    rows, bad = [], 0
    for item in items:
        row, why = pa.normalize(item, min_price=0.01)
        if row is None:
            bad += 1
            print(f"  skipped: {why} — {item.get('listing_url')}")
        else:
            rows.append(row)
    out = pa.upsert(rows)
    print(f"seeded {len(out)} ({sum(1 for r in out if r['inserted'])} new), skipped {bad}")
    if not args.no_photos:
        print(_photos())
    return 0


def cmd_list(args) -> int:
    for r in pa.list_active(args.tier):
        qty = f"{r['qty']:,}" if r["qty"] else "?"
        print(f"{r['tier']:<5} ${r['price_per_chair']:>7}  qty {qty:>6}  {r['title'][:60]:<60}  "
              f"{r['location'] or ''}  {'📷' if r['image_r2_key'] else '  '}  {r['listing_url']}")
    return 0


def cmd_photos(_args) -> int:
    print(_photos())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="search and print; no DB, no photos")
    r.add_argument("--no-photos", action="store_true")
    r.add_argument("--max-searches", type=int, default=pa.MAX_SEARCHES)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("seed")
    s.add_argument("path")
    s.add_argument("--no-photos", action="store_true")
    s.set_defaults(fn=cmd_seed)
    ls = sub.add_parser("list")
    ls.add_argument("--tier", choices=sorted(pa.TIERS))
    ls.set_defaults(fn=cmd_list)
    p = sub.add_parser("photos")
    p.set_defaults(fn=cmd_photos)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

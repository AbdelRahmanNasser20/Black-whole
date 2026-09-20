#!/usr/bin/env python
"""Seed `listing_channels` from what `inventory` already knows (multichannel Phase 1.8).

For every inventory row:
  * site, fb_catalog  → `sync.desired_state` (live for sellable stock, delisted otherwise),
                        with the payload hash stamped so the first sync pass is a noop;
  * fb_marketplace    → live where `facebook_url` is a real Marketplace item
                        (`marketplace/item/<id>`), never for a page post;
  * ebay              → live where `ebay_url` is set.

Idempotent (one `store.upsert` per lot × channel, ON CONFLICT). Dry-run is the
default; nothing is written without `--apply`. Needs migration
`scripts/sql/011_listing_channels.sql` applied first (operator gate).

    ./.venv/bin/python scripts/backfill_listing_channels.py            # plan only
    ./.venv/bin/python scripts/backfill_listing_channels.py --apply    # write
    ./.venv/bin/python scripts/backfill_listing_channels.py --lot 31225-atl --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, inventory  # noqa: E402,F401  (config loads .env)
from automation.channels import store, sync  # noqa: E402
from automation.catalog_feed import site_base_url  # noqa: E402

FB_ITEM_ID_RE = re.compile(r"marketplace/item/(\d+)")


@dataclass(frozen=True)
class Planned:
    lot_id: str
    channel: str
    state: str
    url: str | None = None
    external_id: str | None = None
    payload_hash: str | None = None


def plan_rows(rows: list[dict]) -> list[Planned]:
    base = site_base_url()
    out: list[Planned] = []
    for row in rows:
        lot_id = str(row["lot_id"])
        for ch in ("site", "fb_catalog"):
            state = sync.desired_state(row, ch)
            url = f"{base}/listings/{lot_id}" if (ch == "site" and state == "live") else None
            out.append(Planned(lot_id, ch, state, url=url, payload_hash=sync.payload_hash(row, ch)))
        fb = row.get("facebook_url") or ""
        m = FB_ITEM_ID_RE.search(fb)
        if m:
            out.append(Planned(lot_id, "fb_marketplace", "live", url=fb, external_id=m.group(1),
                               payload_hash=sync.payload_hash(row, "fb_marketplace")))
        eb = row.get("ebay_url")
        if eb:
            out.append(Planned(lot_id, "ebay", "live", url=eb, payload_hash=sync.payload_hash(row, "ebay")))
    return out


def run(*, apply: bool, lot_id: str | None = None, log=print) -> dict:
    rows = inventory.list_all()
    if lot_id:
        rows = [r for r in rows if str(r["lot_id"]) == lot_id]
    plan = plan_rows(rows)
    written = 0
    for p in plan:
        log(f"  {'WRITE' if apply else 'plan '} {p.lot_id:<16} {p.channel:<15} {p.state:<9} {p.url or ''}")
        if apply:
            store.upsert(p.lot_id, p.channel, state=p.state, url=p.url,
                         external_id=p.external_id, payload_hash=p.payload_hash)
            written += 1
    return {"lots": len(rows), "planned": len(plan), "written": written}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to listing_channels (default: dry-run)")
    ap.add_argument("--lot", help="backfill a single lot_id")
    args = ap.parse_args(argv)
    summary = run(apply=args.apply, lot_id=args.lot)
    mode = "applied" if args.apply else "dry-run — nothing written; re-run with --apply"
    print(f"{summary['lots']} lots · {summary['planned']} rows planned · {summary['written']} written ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

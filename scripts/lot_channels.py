#!/usr/bin/env python3
"""One command per channel action. Site + FB Business catalog are read off the
ledger, so they follow the row; Marketplace goes through the family account.

    # put a GovDeals lot everywhere (site + Marketplace + business catalog)
    ./.venv/bin/python scripts/lot_channels.py add https://www.govdeals.com/en/asset/420/9312 \
        --price 25 --title "199 Banquet Chairs — Tan Crown-Back (Las Vegas, NV)" \
        --blurb "Tan fabric banquet chairs on gold frames …"

    # a mixed lot, split into two rows (kind:qty:price[:photo-indexes])
    ./.venv/bin/python scripts/lot_channels.py add https://www.govdeals.com/en/asset/53677/357 \
        --split "chairs:60:20:2-3,tables:14:65:0-1"

    # ledger + copy only, no browser (Marketplace later with `post`)
    ./.venv/bin/python scripts/lot_channels.py add <url> --price 25 --no-publish
    ./.venv/bin/python scripts/lot_channels.py post gd-420-9312

    # take a lot off everything as "moved": fake sold-out + Mark as sold on FB
    ./.venv/bin/python scripts/lot_channels.py remove bs-10118-metal
    ./.venv/bin/python scripts/lot_channels.py remove bs-10118-metal --channels site,business

    # fix copy on a row (title / blurb / price / type) and refresh the FB plan entry
    ./.venv/bin/python scripts/lot_channels.py copy gd-53677-357-tables --title "…" --blurb "…"

    # the FB Business catalog file (same rows as the live feed) for a manual upload
    ./.venv/bin/python scripts/lot_channels.py catalog

    # where is every lot right now?
    ./.venv/bin/python scripts/lot_channels.py status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import inventory, lot_channels as lc, lot_images  # noqa: E402
from automation import progress  # noqa: E402


def _channels(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return lc.CHANNELS
    out = tuple(c.strip() for c in spec.split(",") if c.strip())
    bad = [c for c in out if c not in lc.CHANNELS]
    if bad:
        raise SystemExit(f"unknown channel(s) {bad}; choose from {lc.CHANNELS}")
    return out


def cmd_add(a) -> int:
    progress.emit("run", status="started", url=a.url)
    rc = 0
    try:
        results = lc.add_lot(
            a.url, price=a.price, split=a.split, title=a.title, blurb=a.blurb,
            chair_type=a.chair_type, quantity=a.quantity, channels=_channels(a.channels),
            publish=not a.no_publish, fb_city=a.fb_city, fb_state=a.fb_state,
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't trace-dump into the dashboard
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        progress.emit("run", status="finished", ok=False)
        return 1
    print()
    for r in results:
        on = ", ".join(r.channels) or "nothing"
        print(f"{r.lot_id}: {'created' if r.created else 'updated'} · {r.photos} photos · on: {on}")
        print(f"  site      {lc.SITE_BASE}/listings/{r.lot_id}")
        print(f"  business  {lc.SITE_BASE}/catalog/facebook.csv  (row id {r.lot_id})")
        if r.fb_url:
            print(f"  fb        {r.fb_url}")
        elif r.fb_error:
            print(f"  fb        NOT POSTED — {r.fb_error}")
            rc = 2
    progress.emit("run", status="finished", ok=rc == 0)
    return rc


def cmd_post(a) -> int:
    for lot_id in a.lot_ids:
        if not lc.plan_entry_for(lot_id):
            print(f"{lot_id}: no plan entry — run `add … --no-publish` first", file=sys.stderr)
            return 2
    rc = 0
    for i, lot_id in enumerate(a.lot_ids):
        url, err = lc.post_to_facebook(lot_id, spacing_s=a.spacing if i else 0)
        if not url:
            rc = 2
    return rc


def cmd_remove(a) -> int:
    rc = 0
    for lot_id in a.lot_ids:
        try:
            res = lc.remove_lot(lot_id, channels=_channels(a.channels))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            rc = 1
            continue
        if res.fb_marked_sold is False:
            rc = 2
    return rc


def cmd_restore(a) -> int:
    lc.restore_lot(a.lot_id, status=a.status)
    return 0


def cmd_copy(a) -> int:
    row = inventory.get(a.lot_id)
    if not row:
        print(f"no inventory row {a.lot_id!r}", file=sys.stderr)
        return 1
    fields = {k: v for k, v in {
        "title": a.title, "description": a.blurb, "price_per_chair": a.price,
        "chair_type": a.chair_type, "quantity_remaining": a.quantity,
    }.items() if v is not None}
    if fields:
        row = inventory.set_fields(a.lot_id, **fields) or row
        print(f"  ✓ ledger: {', '.join(fields)}")
    entry = lc.plan_entry(
        lot_id=a.lot_id, title=row.get("title") or "", price=float(row.get("price_per_chair") or 0),
        city=row.get("city") or "", state=row.get("state") or "", zip_code=row.get("zip_code"),
        quantity=row.get("quantity_remaining"), blurb=row.get("description") or "",
        photo_urls=lot_images.resolve(row).urls, unit=lc.unit_word(row), profile_id=lc.profile_id(),
        notes=(lc.plan_entry_for(a.lot_id) or {}).get("notes", ""),
        fb_city=a.fb_city, fb_state=a.fb_state,
    )
    lc.upsert_plan_entry(entry)
    print(f"  ✓ FB plan entry refreshed ({lc.PLAN_PATH.name})")
    if (lc.plan_entry_for(a.lot_id) or {}).get("fb_listing_url"):
        print("  · listing is live on Marketplace — push the new copy with "
              f"LISTING_CHROME_PROFILE={lc.FB_PROFILE} scripts/fb_edit_listing.py --sku {a.lot_id} --save")
    return 0


def cmd_catalog(a) -> int:
    path, n = lc.write_catalog_csv(Path(a.out) if a.out else lc.CATALOG_CSV)
    print(f"{n} rows → {path}")
    print(f"live feed: {lc.SITE_BASE}/catalog/facebook.csv")
    print("upload: Commerce Manager → catalog 'Chairs' → Data sources → Upload file → this CSV")
    return 0 if n else 1


def cmd_status(a) -> int:
    rows = lc.channel_matrix(a.lot_ids or None)
    if a.live_only:
        rows = [r for r in rows if r["site"] or r["business"] or r["fb"] == "live"]
    print(f"{'lot_id':45s} {'status':14s} {'qty':>6s}  site  biz  fb")
    for r in rows:
        site = "SOLD" if (not r["site"] and r["sold_strip"]) else ("yes" if r["site"] else "-")
        print(f"{r['lot_id'][:45]:45s} {r['status'] or '':14s} {str(r['qty'] or ''):>6s}  "
              f"{site:5s} {'yes' if r['business'] else '-':4s} {r['fb'] or '-'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="list a GovDeals lot on site + Marketplace + business catalog")
    p.add_argument("url")
    p.add_argument("--price", type=float, help="$ per unit (required for a new single lot)")
    p.add_argument("--split", help="kind:qty:price[:photo-idx],… e.g. chairs:60:20:2-3,tables:14:65:0-1")
    p.add_argument("--title")
    p.add_argument("--blurb", help="plain prose for the site + FB (no boilerplate — that is added)")
    p.add_argument("--chair-type", dest="chair_type", help="storefront tag, e.g. 'Banquet Chairs'")
    p.add_argument("--quantity", type=int, help="override the headcount parsed from GovDeals")
    p.add_argument("--channels", help="comma list of site,fb,business (default all)")
    p.add_argument("--no-publish", action="store_true", help="skip the Marketplace post")
    p.add_argument("--fb-city", dest="fb_city", help="if Facebook has no pin for the ledger city")
    p.add_argument("--fb-state", dest="fb_state")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("post", help="post already-added lot(s) to Marketplace, in order")
    p.add_argument("lot_ids", nargs="+")
    p.add_argument("--spacing", type=int, default=1200, help="seconds between posts (default 20 min)")
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("remove", help="mark lot(s) as moved: fake sold-out + Mark as sold on FB")
    p.add_argument("lot_ids", nargs="+")
    p.add_argument("--channels", help="comma list of site,fb,business (default all)")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("restore", help="undo remove on the ledger (site + feed)")
    p.add_argument("lot_id")
    p.add_argument("--status", default="active_bid", choices=inventory.ALL_STATUSES)
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("copy", help="edit a lot's copy/price and refresh its FB plan entry")
    p.add_argument("lot_id")
    p.add_argument("--title")
    p.add_argument("--blurb")
    p.add_argument("--price", type=float)
    p.add_argument("--chair-type", dest="chair_type")
    p.add_argument("--quantity", type=int)
    p.add_argument("--fb-city", dest="fb_city")
    p.add_argument("--fb-state", dest="fb_state")
    p.set_defaults(fn=cmd_copy)

    p = sub.add_parser("catalog", help="write the FB Business catalog CSV for manual upload")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("status", help="per-lot channel matrix")
    p.add_argument("lot_ids", nargs="*")
    p.add_argument("--live-only", action="store_true")
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())

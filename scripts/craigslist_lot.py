#!/usr/bin/env python3
"""One inventory lot ↔ one Craigslist post (furniture, for sale by owner — free).

    # what would go out (title / body / ZIP / site / photo count), nothing posted
    ./.venv/bin/python scripts/craigslist_lot.py post 9006 --dry-run

    # post it for real — needs the profile signed in at accounts.craigslist.org
    CRAIGSLIST_LIVE=1 ./.venv/bin/python scripts/craigslist_lot.py post 9006

    # take it down / bump it (manage-page selectors are unverified — watch the first run)
    CRAIGSLIST_LIVE=1 ./.venv/bin/python scripts/craigslist_lot.py delist 9006
    CRAIGSLIST_LIVE=1 ./.venv/bin/python scripts/craigslist_lot.py renew 9006

    # where is every lot on Craigslist right now?
    ./.venv/bin/python scripts/craigslist_lot.py status

Sign in once: `python run.py --login-only`, log in at accounts.craigslist.org
in that window, close it. The sync loop (`channel_craigslist_enabled`) uses the
same adapter and the same profile.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import craigslist, inventory  # noqa: E402
from automation.channels import store as channel_store  # noqa: E402
from automation.publish import registry  # noqa: E402


def _adapter():
    registry.load_builtin()
    adapter = registry.get("craigslist")
    if adapter is None:
        sys.exit("craigslist adapter failed to load — see the [publish] line above")
    return adapter


def _row(lot_id: str) -> dict:
    row = inventory.get(lot_id)
    if not row:
        sys.exit(f"no inventory row {lot_id!r}")
    row.pop("storage_note", None)
    return row


def _require_live(what: str) -> None:
    if not craigslist._live_enabled():
        sys.exit(f"refusing to {what}: set CRAIGSLIST_LIVE=1 (or pass --dry-run to preview)")


def cmd_post(a) -> int:
    row = _row(a.lot_id)
    post = craigslist.build_lot_post(row)
    photos = craigslist.lot_photo_urls(row)
    print(f"lot      {post.lot_id}")
    print(f"site     {post.subdomain}.craigslist.org  (subarea: {post.subarea_pref or 'first option'})")
    print(f"title    {post.title}  [{len(post.title)}/{craigslist.CL_TITLE_MAX}]")
    print(f"price    ${post.price}   zip {post.postal}   area {post.city}")
    print(f"photos   {len(photos)}")
    print("body     " + "\n         ".join(post.body.splitlines()))
    if a.dry_run:
        print("\n(dry run — nothing posted)")
        return 0
    _require_live("post")
    if row.get("craigslist_url") and not a.force:
        print(f"\nalready on Craigslist: {row['craigslist_url']}  (--force to post again)")
        return 1
    res = _adapter().publish_lot(row)
    print(f"\nlive: {res['url']}  (id {res['external_id']})")
    return 0


def cmd_delist(a) -> int:
    _require_live("delist")
    row = _row(a.lot_id)
    current = channel_store.get(a.lot_id, "craigslist")
    _adapter().unpublish_lot(row, current)
    print(f"delisted {a.lot_id}")
    return 0


def cmd_renew(a) -> int:
    _require_live("renew")
    row = _row(a.lot_id)
    current = channel_store.get(a.lot_id, "craigslist")
    ok = _adapter().renew_lot(row, current)
    print(f"renew {a.lot_id}: {'clicked' if ok else 'no renew control found (too soon, or selector drift)'}")
    return 0 if ok else 1


def cmd_status(a) -> int:
    rows = [r for r in channel_store.list_all() if r.get("channel") == "craigslist"]
    if not rows:
        print("no craigslist rows in listing_channels")
        return 0
    for r in sorted(rows, key=lambda r: str(r.get("lot_id"))):
        print(f"{str(r.get('lot_id')):<44} {str(r.get('state')):<17} {r.get('url') or ''}"
              f"{('  ! ' + r['last_error']) if r.get('last_error') else ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post", help="post one lot in its own city")
    p.add_argument("lot_id")
    p.add_argument("--dry-run", action="store_true", help="print the copy, post nothing")
    p.add_argument("--force", action="store_true", help="post even if inventory.craigslist_url is set")
    p.set_defaults(fn=cmd_post)
    p = sub.add_parser("delist", help="delete the lot's live post")
    p.add_argument("lot_id")
    p.set_defaults(fn=cmd_delist)
    p = sub.add_parser("renew", help="renew (bump) the lot's live post")
    p.add_argument("lot_id")
    p.set_defaults(fn=cmd_renew)
    p = sub.add_parser("status", help="lot × craigslist rows from listing_channels")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

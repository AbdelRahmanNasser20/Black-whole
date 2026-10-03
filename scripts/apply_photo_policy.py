#!/usr/bin/env python3
"""Make every lot's R2 photos match the watermark rule (automation/photo_policy.py).

The rule: the tiled watermark shows only on black-whole.com, only for a lot
still `active_bid`. Every other channel, and every lot we own/won/sold, gets
the clean (dewatermarked, unwatermarked) copy. The DB stores clean URLs.

Per row (inventory, plus `auction_favorites` — treated as `active_bid`):
- HEAD the clean (`.c.jpg`) and, for bid lots, the watermarked (`.jpg`) object
  of every photo; render a missing one from its source (lot folder on this
  host, or the pre-disguise R2 object named in a disguise-backfill log);
- owned lots whose folder holds a different photo set re-upload from the
  folder (our own photos win over the GovDeals scrape);
- rewrite `hero_image_url` / `image_urls` to the clean URLs.

- Dry-run by default (reads the DB and HEADs R2, writes nothing).
- `--apply` uploads and writes; every row changed is logged old→new to
  ~/.listing_automation/logs/photo-policy-<ts>.jsonl; `--rollback <log>`
  restores the old URLs. Objects are never deleted.
- `--drop-missing` removes gallery photos whose clean copy can't be made
  (no source anywhere). Off by default: they stay and are reported.

    .venv/bin/python scripts/apply_photo_policy.py                 # plan, all rows
    .venv/bin/python scripts/apply_photo_policy.py --lot 31225     # one lot
    .venv/bin/python scripts/apply_photo_policy.py --apply
    .venv/bin/python scripts/apply_photo_policy.py --rollback <log.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import config, db, favorite_images, favorites, image_disguise, inventory  # noqa: E402
from automation import photo_policy, photo_sync, r2_images  # noqa: E402

_ROW_SQL = ("SELECT lot_id, status, hero_image_url, image_urls, folder_path, folder_name, "
            "hero_image FROM inventory {where} ORDER BY status, lot_id")


def _log_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(config.LOG_DIR) / f"photo-policy-{ts}.jsonl"


def _channels_line(status: str | None) -> str:
    site = "WM" if photo_policy.wants_watermark(status, photo_policy.SITE) else "clean"
    return f"site={site} other channels=clean"


def _print_plan(plan: photo_sync.LotPlan) -> dict:
    c = plan.counts()
    bits = [f"photos={c['photos']}"]
    if c["clean_missing"]:
        bits.append(f"clean_missing={c['clean_missing']}"
                    + (f" (no source {c['clean_missing_no_source']})" if c["clean_missing_no_source"] else ""))
    if plan.needs_wm and c["wm_missing"]:
        bits.append(f"wm_missing={c['wm_missing']}"
                    + (f" (no source {c['wm_missing_no_source']})" if c["wm_missing_no_source"] else ""))
    if c["db_rewrite"]:
        bits.append(f"db_rewrite={c['db_rewrite']}")
    if plan.reupload_from_folder:
        bits.append(f"folder_reupload={c['folder_reupload']} [{plan.note}]")
    print(f"  {plan.table[:3]}:{plan.ident:<28} {str(plan.status):<14} "
          f"{_channels_line(plan.status):<34} {' '.join(bits)}")
    return c


def run(args) -> int:
    cfg = r2_images.env_config()
    if not cfg:
        raise SystemExit("R2 is not configured — nothing to check against")
    s3 = r2_images.client(cfg)
    exists = photo_sync.r2_exists_fn(s3, cfg["bucket"])
    legacy = photo_sync.legacy_sources()
    print(f"sources: {len(legacy)} pre-disguise objects from disguise-backfill logs\n")

    where, params = "", ()
    if args.lot:
        where, params = "WHERE lot_id = %s", (args.lot,)
    rows = db.fetch_all(_ROW_SQL.format(where=where), params)
    plans = [photo_sync.plan_inventory_row(dict(r), exists=exists, public_base=cfg["public_base"],
                                           legacy=legacy) for r in rows]
    if not args.lot and favorite_images.clean_columns_available():
        for fav in favorites.list_all():
            key = favorite_images.r2_key(fav.asset_id)
            if key and (fav.clean_hero_url or fav.clean_image_urls):
                plans.append(photo_sync.plan_lot(
                    table="auction_favorites", ident=fav.asset_id, lot_key=key,
                    status=photo_policy.WATERMARK_STATUS, hero=fav.clean_hero_url,
                    gallery=list(fav.clean_image_urls), exists=exists,
                    public_base=cfg["public_base"], legacy=legacy))

    totals: dict[str, int] = {}
    lots_touched = 0
    log_file = _log_path() if args.apply else None
    for plan in plans:
        c = _print_plan(plan)
        for k, v in c.items():
            totals[k] = totals.get(k, 0) + v
        if not args.apply:
            continue
        new = photo_sync.apply_plan(plan, s3=s3, cfg=cfg, drop_missing=args.drop_missing)
        if not new:
            continue
        old = ([p.stored for p in plan.photos if p.role == "hero"] or [None])[0]
        entry = {"table": plan.table, "id": plan.ident,
                 "old": {"hero": old, "urls": [p.stored for p in plan.photos if p.role == "gallery"]},
                 "new": {"hero": new[0], "urls": new[1]}}
        with log_file.open("a") as fh:  # log before the write: a crash stays reversible
            fh.write(json.dumps(entry) + "\n")
        if plan.table == "inventory":
            inventory.set_images(plan.ident, new[0], new[1])
        else:
            favorites.set_clean_images(plan.ident, new[0], new[1])
        lots_touched += 1

    mode = "APPLIED" if args.apply else "dry-run (nothing uploaded or written)"
    print(f"\n{mode}: {len(plans)} row(s) checked")
    for k in ("photos", "clean_missing", "clean_missing_no_source", "wm_missing",
              "wm_missing_no_source", "db_rewrite", "folder_reupload"):
        print(f"  {k:<26} {totals.get(k, 0)}")
    affected = sum(1 for p in plans if any(p.counts()[k] for k in
                   ("clean_missing", "wm_missing", "db_rewrite", "folder_reupload")))
    print(f"  rows --apply would change  {affected}")
    if args.apply:
        print(f"  rows written               {lots_touched}")
        if log_file and lots_touched:
            print(f"rollback log: {log_file}")
    return 0


def rollback(path: Path) -> int:
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for e in reversed(entries):
        old = e["old"]
        if e["table"] == "inventory":
            inventory.set_images(e["id"], old["hero"], old["urls"])
        else:
            favorites.set_clean_images(e["id"], old["hero"], old["urls"])
        print(f"  restored {e['table']}:{e['id']}")
    print(f"rolled back {len(entries)} row(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="plan only (the default)")
    mode.add_argument("--apply", action="store_true", help="upload missing variants and rewrite URLs")
    ap.add_argument("--lot", help="only this inventory lot_id (skips favorites)")
    ap.add_argument("--drop-missing", action="store_true",
                    help="with --apply: drop gallery photos whose clean copy has no source")
    ap.add_argument("--rollback", type=Path, help="restore the old URLs from a photo-policy log")
    args = ap.parse_args()
    if args.rollback:
        return rollback(args.rollback)
    if not image_disguise.enabled():
        raise SystemExit("IMAGE_DISGUISE is off — the p/ layout this script maintains is not in use")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

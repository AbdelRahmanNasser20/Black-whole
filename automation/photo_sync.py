"""Bring a lot's R2 photos in line with `photo_policy` — plan, then apply.

`photo_policy` decides which variant each channel shows; this module makes sure
the objects it points at exist and that `inventory` stores the clean URLs.
Two callers share it:

* `scripts/apply_photo_policy.py` — the one-shot walk over every row (dry-run
  by default, `--apply` uploads + rewrites).
* `on_status_change()` — called by `inventory.set_fields` when a lot crosses
  `active_bid` (bid won/lost, relist, admin edit). Runs `sync_lot` in a
  background thread. The site already flips at read time (the variant is
  derived from `status`), so this only fills gaps: our own folder photos for a
  lot we now hold, a missing clean or watermarked twin. It never drops a photo.

Per photo the plan needs a *source* — the dewatermarked bytes the variant is
rendered from — when a variant is missing on R2. In order:

1. the lot folder on this host (`lot_images.local_image_paths`), matched by the
   opaque gallery token (`listing_images.opaque_gallery_path` hashes the
   source bytes, so a match is exact);
2. the pre-disguise R2 object recorded in a `disguise-backfill-*.jsonl` log
   (`scripts/disguise_live_images.py` kept those objects and logged old→new);
3. for the hero, the source of gallery photo 0 (they are the same photo).

No source → the photo is reported `missing`. Nothing is ever uploaded raw, and
the GovDeals mark is never handled here: every source is already dewatermarked.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from automation import listing_images as li
from automation import lot_images, photo_policy, r2_images

ENV_AUTOSYNC = "PHOTO_POLICY_AUTOSYNC"


@dataclass
class Photo:
    role: str                      # "hero" | "gallery"
    stored: str                    # URL as stored in the DB
    clean_path: str | None = None  # object key of the clean variant (None = not ours)
    wm_path: str | None = None
    clean_exists: bool | None = None
    wm_exists: bool | None = None
    source: str | None = None      # "folder:<path>" | "legacy:<url>" | None

    @property
    def ours(self) -> bool:
        return self.clean_path is not None


@dataclass
class LotPlan:
    table: str                     # "inventory" | "auction_favorites"
    ident: str
    lot_key: str
    status: str | None
    photos: list[Photo] = field(default_factory=list)
    folder_files: list[str] = field(default_factory=list)
    reupload_from_folder: bool = False
    note: str = ""

    @property
    def needs_wm(self) -> bool:
        return photo_policy.keeps_watermark_variant(self.status)

    def site_variant(self) -> str:
        return "watermarked" if photo_policy.wants_watermark(self.status, photo_policy.SITE) else "clean"

    def counts(self) -> dict:
        ours = [p for p in self.photos if p.ours]
        c = {
            "photos": len(ours),
            "clean_missing": sum(1 for p in ours if not p.clean_exists),
            "clean_missing_no_source": sum(1 for p in ours if not p.clean_exists and not p.source),
            "wm_missing": sum(1 for p in ours if self.needs_wm and not p.wm_exists),
            "wm_missing_no_source": sum(1 for p in ours if self.needs_wm and not p.wm_exists
                                        and not p.source),
            "db_rewrite": sum(1 for p in ours if p.stored != photo_policy.clean_url(p.stored)),
            "folder_reupload": len(self.folder_files) if self.reupload_from_folder else 0,
        }
        return c


# ───────────────────────────── sources ─────────────────────────────

def _path_of(url: str | None, public_base: str) -> str | None:
    if not url or not url.startswith(public_base.rstrip("/") + "/"):
        return None
    return urlsplit(url).path.lstrip("/")


def legacy_sources(log_dir: str | os.PathLike | None = None) -> dict[str, str]:
    """`{watermarked object key: pre-disguise URL}` from the disguise backfill logs."""
    if log_dir is None:
        from automation import config
        log_dir = config.LOG_DIR
    out: dict[str, str] = {}
    for f in sorted(Path(log_dir).glob("disguise-backfill-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            old, new = e.get("old") or {}, e.get("new") or {}
            pairs = [(old.get("hero"), new.get("hero"))]
            pairs += list(zip(old.get("urls") or [], new.get("urls") or []))
            for o, n in pairs:
                if o and n:
                    key = urlsplit(n).path.lstrip("/")
                    out.setdefault(photo_policy.watermarked_path(key), o)
    return out


def _folder_index(lot_key: str, paths: list[str]) -> dict[str, str]:
    """`{watermarked gallery key: local file}` — exact match on the source bytes."""
    out: dict[str, str] = {}
    for p in paths:
        try:
            data = Path(p).read_bytes()
        except OSError:
            continue
        key = li.opaque_gallery_path(lot_key, data)
        if key:
            out[key] = p
    return out


# ───────────────────────────── plan ─────────────────────────────

def plan_lot(*, table: str, ident: str, lot_key: str, status: str | None,
             hero: str | None, gallery: list[str], row: dict | None = None,
             exists=None, public_base: str, legacy: dict[str, str] | None = None) -> LotPlan:
    """What this lot needs. `exists(key) -> bool` checks R2 (injected for tests)."""
    plan = LotPlan(table=table, ident=ident, lot_key=lot_key, status=status)
    local = lot_images.local_image_paths(row) if row else []
    plan.folder_files = local
    index = _folder_index(lot_key, local) if local else {}
    legacy = legacy or {}

    stored = ([("hero", hero)] if hero else []) + [("gallery", u) for u in gallery or []]
    for role, url in stored:
        ph = Photo(role=role, stored=url)
        path = _path_of(url, public_base)
        if path and li.is_disguised_url(url):
            ph.clean_path = photo_policy.clean_path(path)
            ph.wm_path = photo_policy.watermarked_path(path)
            ph.clean_exists = bool(exists(ph.clean_path)) if exists else None
            ph.wm_exists = (bool(exists(ph.wm_path)) if exists and plan.needs_wm else None)
            if ph.wm_path in index:
                ph.source = f"folder:{index[ph.wm_path]}"
            elif ph.wm_path in legacy:
                ph.source = f"legacy:{legacy[ph.wm_path]}"
        plan.photos.append(ph)

    # The hero is gallery photo 0 under another key: borrow its source.
    heroes = [p for p in plan.photos if p.role == "hero" and p.ours and not p.source]
    first = next((p for p in plan.photos if p.role == "gallery" and p.ours), None)
    for h in heroes:
        if first and first.source:
            h.source = first.source

    # Owned lots show our own photos: when the folder on this host holds a set
    # that differs from what R2 serves, the folder wins.
    if (table == "inventory" and photo_policy.is_owned(status) and local):
        db_keys = {p.wm_path for p in plan.photos if p.role == "gallery" and p.ours}
        if set(index) != db_keys:
            plan.reupload_from_folder = True
            plan.note = f"folder has {len(local)} photo(s), R2 gallery {len(db_keys)}"
    return plan


# ───────────────────────────── apply ─────────────────────────────

def _fetch_source(source: str) -> bytes | None:
    kind, _, ref = source.partition(":")
    try:
        if kind == "folder":
            return Path(ref).read_bytes()
        if kind == "legacy":
            import httpx
            resp = httpx.get(ref, timeout=90.0, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        print(f"    ! source unreadable {ref[:100]}: {e}", file=sys.stderr)
    return None


def apply_plan(plan: LotPlan, *, s3, cfg: dict, drop_missing: bool = False,
               log=print) -> tuple[str | None, list[str]] | None:
    """Upload what's missing, then return the clean `(hero, gallery)` to store.

    None when nothing in the DB needs to change. Photos whose clean variant is
    still missing are kept as stored unless `drop_missing` (the hook never drops).
    """
    # Fill the variants of what is stored first, even when the folder takes
    # over below: FB plan entries, caches and old pages still name these
    # objects, and `photo_policy.clean_url` sends them to the `.o.jpg` twin.
    cache: dict[str, bytes | None] = {}
    for ph in plan.photos:
        if not ph.ours or not ph.source:
            continue
        need_clean = not ph.clean_exists
        need_wm = plan.needs_wm and not ph.wm_exists
        if not (need_clean or need_wm):
            continue
        if ph.source not in cache:
            cache[ph.source] = _fetch_source(ph.source)
        src = cache[ph.source]
        if not src:
            continue
        key = li.key_base(plan.lot_key)
        if need_clean:
            out = li.prepare_for_web(src, "jpg", key=key, status=plan.status, channel=None)
            if out and r2_images.put_object(s3, bucket=cfg["bucket"], path=ph.clean_path,
                                            data=out[0], content_type=out[2]):
                ph.clean_exists = True
        if need_wm:
            out = li.prepare_for_web(src, "jpg", key=key, status=plan.status,
                                     channel=photo_policy.SITE)
            if out and r2_images.put_object(s3, bucket=cfg["bucket"], path=ph.wm_path,
                                            data=out[0], content_type=out[2]):
                ph.wm_exists = True

    if plan.reupload_from_folder:
        up = li.upload_lot_images(plan.lot_key, plan.folder_files, status=plan.status)
        if up:
            return up["hero_image_url"], list(up["image_urls"])
        log(f"    ! {plan.ident}: folder re-upload returned nothing — keeping R2's set")

    hero, gallery, changed = None, [], False
    for ph in plan.photos:
        url = ph.stored
        if ph.ours:
            if ph.clean_exists:
                url = photo_policy.clean_url(ph.stored)
            elif drop_missing and ph.role == "gallery":
                changed = True
                continue
        changed = changed or url != ph.stored
        if ph.role == "hero":
            hero = url
        else:
            gallery.append(url)
    return (hero, gallery) if changed else None


# ───────────────────────────── one lot, end to end ─────────────────────────────

def r2_exists_fn(s3, bucket: str):
    memo: dict[str, bool] = {}

    def exists(key: str) -> bool:
        if key not in memo:
            memo[key] = r2_images.object_exists(s3, bucket=bucket, path=key)
        return memo[key]
    return exists


def plan_inventory_row(row: dict, *, exists, public_base: str,
                       legacy: dict[str, str] | None = None) -> LotPlan:
    return plan_lot(table="inventory", ident=row["lot_id"], lot_key=row["lot_id"],
                    status=row.get("status"), hero=row.get("hero_image_url"),
                    gallery=list(row.get("image_urls") or []), row=row, exists=exists,
                    public_base=public_base, legacy=legacy)


def sync_lot(lot_id: str, *, log=print) -> LotPlan | None:
    """Plan + apply one inventory lot. Never drops a photo. Never raises."""
    try:
        from automation import inventory
        cfg = r2_images.env_config()
        row = inventory.get(lot_id)
        if not cfg or not row:
            return None
        s3 = r2_images.client(cfg)
        plan = plan_inventory_row(row, exists=r2_exists_fn(s3, cfg["bucket"]),
                                  public_base=cfg["public_base"], legacy=legacy_sources())
        new = apply_plan(plan, s3=s3, cfg=cfg, drop_missing=False, log=log)
        if new:
            inventory.set_images(lot_id, new[0], new[1])
            log(f"[photo_sync] {lot_id}: photos now clean in the DB ({len(new[1])})")
        return plan
    except Exception as e:  # noqa: BLE001 - a photo sync must never break a status write
        log(f"[photo_sync] {lot_id}: {type(e).__name__}: {e}")
        return None


def autosync_enabled() -> bool:
    return (os.getenv(ENV_AUTOSYNC) or "1").strip().lower() not in ("0", "off", "false", "no")


def on_status_change(lot_id: str, prior: str | None, new: str | None, *,
                     background: bool = True) -> bool:
    """Hook for a status write. True when a sync was started.

    Only a move into or out of `active_bid` matters: that is the one boundary
    where the site's variant changes and an owned lot's own photos take over.
    """
    if not lot_id or prior == new or not autosync_enabled():
        return False
    wm = photo_policy.WATERMARK_STATUS
    if wm not in (prior, new):
        return False
    if not r2_images.is_configured():
        return False
    if background:
        threading.Thread(target=sync_lot, args=(str(lot_id),), daemon=True,
                         name=f"photo-sync-{lot_id}").start()
    else:
        sync_lot(str(lot_id))
    return True

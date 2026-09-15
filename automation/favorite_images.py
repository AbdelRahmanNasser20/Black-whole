"""Clean photos for favorited auctions (public map "incoming" pins).

A favorite has no inventory row, so `lot_channels.mirror_photos` can't stamp it.
This module runs the same download → dewatermark.ai → R2 path under a synthetic
key (`fav-<sha256(asset_id)[:12]>` — opaque, so the public photo URL cannot be
turned back into the auction) and stamps `auction_favorites.clean_*` instead.
Only GovDeals favorites are supported (the gallery fetch is GovDeals-only).
Never reads the favorite's raw CDN photo column — that file is watermarked, and
`strict=True` means a photo dewatermark.ai could not clean is dropped, not shipped.
"""
from __future__ import annotations

import hashlib
import os
import re

from automation import favorites, lot_channels

FAVORITE_PHOTO_LIMIT = 6   # hero + 5: enough for a pin popup, ~6 dewatermark calls per lot
_GD_KEY = re.compile(r"^(\d+)/(\d+)$")


def r2_key(asset_id: str | None) -> str | None:
    """Opaque R2 key stem for a favorite's photos, or None if not a GovDeals id.

    A hash, not the asset id: the public map serves these URLs, and the lot the
    operator is bidding on must not be readable off the filename.
    """
    key = (asset_id or "").strip()
    if not _GD_KEY.match(key):
        return None
    return "fav-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def mirror_favorite_photos(asset_id: str, *, log=print, force: bool = False) -> dict | None:
    """Fetch → dewatermark → R2 → stamp `auction_favorites.clean_*`. None on any skip."""
    key = r2_key(asset_id)
    if key is None:
        log(f"  - {asset_id}: not a GovDeals favorite, skipped")
        return None
    if not force:
        fav = favorites.get(asset_id)
        if fav is not None and fav.clean_hero_url:
            log(f"  = {asset_id}: already has clean photos")
            return {"hero_image_url": fav.clean_hero_url, "image_urls": fav.clean_image_urls}
    asset, account = (int(x) for x in asset_id.split("/"))
    try:
        detail = lot_channels.fetch_detail(asset, account)
    except Exception as exc:  # noqa: BLE001 — closed lot / swapped ids
        log(f"  ! {asset_id}: gallery unavailable ({exc})")
        return None
    urls = lot_channels.gallery_urls(detail)
    if not urls:
        log(f"  ! {asset_id}: no photos on GovDeals")
        return None
    # Cap here (a favorite is worth 6 photos) *and* hand the cap down, so the
    # budget can never be blown by a caller that forgets to slice.
    urls = urls[:FAVORITE_PHOTO_LIMIT]
    result = lot_channels.clean_and_upload(key, urls, log, dewatermark=True,
                                           limit=FAVORITE_PHOTO_LIMIT, strict=True)
    if not result:
        log(f"  ! {asset_id}: nothing clean to publish")
        return None
    favorites.set_clean_images(asset_id, result["hero_image_url"], result["image_urls"])
    log(f"  ✓ {asset_id}: {len(result['image_urls'])} clean photos on R2")
    return result


def on_star_enabled() -> bool:
    """`FAVORITE_PHOTOS_ON_STAR=0` turns the star route's background mirror off."""
    return os.environ.get("FAVORITE_PHOTOS_ON_STAR", "1").strip().lower() not in ("0", "false", "no", "off", "")

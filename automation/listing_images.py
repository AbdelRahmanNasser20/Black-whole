"""Durable listing images via Supabase Storage (BLACKWHOLE-6).

The website serves images from the local Desktop download folder, so on the
deployed (Render) instance every `/image/...` request 404s — the folder isn't
there. This module mirrors a lot's cleaned (dewatermarked) photos into the
shared public `listing-images` bucket in the `blackwhole` Supabase project and
returns durable public URLs. Postgres (`inventory.hero_image_url` /
`inventory.image_urls`) stores only those URL strings — no bytes in the DB.

One source of truth: this is the SAME bucket + key contract the CRM's
`api/services/listing_images.py` (BWCRM-18) already uses. GovDeals hero objects
stay flat (`<lot_id>.<ext>`) so the rows the CRM already populated keep working;
gallery images are namespaced under a `<key>/NN.<ext>` prefix.

Auth uses the Storage REST API with `SUPABASE_STORAGE_KEY` + a bucket-scoped
write policy (`listing_images_insert`). All uploads are best-effort: any failure
returns None / skips, so a missing key or a network blip never crashes a run —
the site just falls back to local-disk serving.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import httpx
from PIL import Image

from . import config

DEFAULT_BUCKET = "listing-images"

# Web-delivery optimization (egress guard). Raw GovDeals photos run 5-9 MB
# each; serving them full-size burned through the Supabase egress quota and
# got the whole Storage service 402-restricted. Cap the long edge and
# re-encode as JPEG before upload — a chair photo doesn't need more.
MAX_IMAGE_DIM = 1600
JPEG_QUALITY = 82
# Objects are content-stable (upserts replace in place, and the site re-reads
# URLs from the DB), so let browsers cache for a week.
CACHE_CONTROL = "max-age=604800"

# content-type / extension mapping (mirror of the CRM helper).
_CT_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}
_KNOWN_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
_EXT_CT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def env_config() -> dict | None:
    """Storage config from `automation.config`, or None if unconfigured.

    Returns ``{"base", "key", "bucket"}``. None means "no storage" — callers
    must degrade to local serving rather than error.
    """
    base = (config.SUPABASE_STORAGE_URL or "").strip()
    key = (config.SUPABASE_STORAGE_KEY or "").strip()
    if not base or not key:
        return None
    bucket = (config.LISTING_IMAGES_BUCKET or "").strip() or DEFAULT_BUCKET
    return {"base": base, "key": key, "bucket": bucket}


def is_configured() -> bool:
    return env_config() is not None


def key_base(lot_id) -> str | None:
    """Sanitized object-key stem for a lot.

    Reproduces the keys the CRM already wrote: real GovDeals ids stay as-is
    (``31225`` -> ``31225``), and synthetic ids get unsafe chars folded to ``_``
    (``folder:Orange_Red_242`` -> ``folder_Orange_Red_242``). None if no id.
    """
    lid = str(lot_id or "").strip()
    if not lid:
        return None
    return re.sub(r"[^A-Za-z0-9._-]", "_", lid)


def public_url(object_path: str, *, base: str, bucket: str = DEFAULT_BUCKET) -> str:
    """Durable public URL for an object already in the bucket."""
    return (
        f"{base.rstrip('/')}/storage/v1/object/public/"
        f"{bucket}/{object_path.lstrip('/')}"
    )


def guess_ext(name: str, content_type: str = "") -> str:
    """Best-effort file extension: content-type first, then path, default jpg."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    path = (name or "").split("?")[0]
    _, _, tail = path.rpartition(".")
    tail = tail.lower()
    if tail in _KNOWN_EXTS and tail != path:
        return "jpg" if tail == "jpeg" else tail
    return "jpg"


def hero_object_path(lot_id, *, ext: str = "jpg") -> str | None:
    """Flat hero key ``<key>.<ext>`` — back-compatible with existing rows."""
    base = key_base(lot_id)
    return f"{base}.{ext}" if base else None


def gallery_object_path(lot_id, index: int, *, ext: str = "jpg") -> str | None:
    """Namespaced gallery key ``<key>/NN.<ext>`` (won't collide with the hero)."""
    base = key_base(lot_id)
    return f"{base}/{index:02d}.{ext}" if base else None


def _content_type_for(path: Path) -> str:
    return _EXT_CT.get(path.suffix.lstrip(".").lower(), "image/jpeg")


def optimize_for_web(data: bytes, ext: str) -> tuple[bytes, str, str]:
    """Shrink an image for web delivery: cap the long edge, re-encode JPEG.

    Returns ``(bytes, ext, content_type)``. Best-effort: GIFs (may animate)
    and anything Pillow can't parse pass through untouched, and the re-encode
    is only kept when it's actually smaller than the original — so this can
    never make an upload worse, only cheaper.
    """
    original = (data, ext, _EXT_CT.get(ext, "image/jpeg"))
    if ext == "gif":
        return original
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return original

    if max(img.size) > MAX_IMAGE_DIM:
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)

    # Flatten transparency onto white; JPEG has no alpha channel.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    try:
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception:
        return original
    out = buf.getvalue()
    if not out or len(out) >= len(data):
        return original
    return out, "jpg", "image/jpeg"


def _post_object(client: httpx.Client, *, base, key, bucket, path, data, content_type) -> bool:
    """Idempotent upsert of raw bytes to the Storage object endpoint."""
    endpoint = f"{base.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": content_type or "image/jpeg",
        "Cache-Control": CACHE_CONTROL,
        "x-upsert": "true",
    }
    try:
        up = client.post(endpoint, content=data, headers=headers)
    except httpx.HTTPError as e:
        print(f"[listing_images] upload error for {path!r}: {e}", file=sys.stderr)
        return False
    if up.status_code not in (200, 201):
        print(
            f"[listing_images] upload failed ({up.status_code}) for {path!r}: "
            f"{up.text[:200]}",
            file=sys.stderr,
        )
        return False
    return True


def upload_lot_images(lot_id, paths) -> dict | None:
    """Upload a lot's local image files to the shared bucket.

    Returns ``{"hero_image_url": str, "image_urls": [str, ...]}`` or None when
    storage is unconfigured, there's no lot id, or nothing uploaded. The first
    file becomes the hero (flat key, back-compat); every file also lands under
    the gallery prefix. Idempotent upsert, so re-runs overwrite in place.
    """
    cfg = env_config()
    base_key = key_base(lot_id)
    if not cfg or not base_key:
        return None

    files = [Path(p) for p in (paths or []) if p and Path(p).exists()]
    if not files:
        return None

    base, key, bucket = cfg["base"], cfg["key"], cfg["bucket"]
    hero_url: str | None = None
    gallery: list[str] = []

    with httpx.Client(timeout=30.0) as client:
        for i, fp in enumerate(files):
            try:
                data = fp.read_bytes()
            except OSError as e:
                print(f"[listing_images] read failed for {fp}: {e}", file=sys.stderr)
                continue
            if not data:
                continue
            data, ext, ct = optimize_for_web(data, guess_ext(fp.name))

            gal_path = gallery_object_path(lot_id, i, ext=ext)
            if gal_path and _post_object(client, base=base, key=key, bucket=bucket,
                                         path=gal_path, data=data, content_type=ct):
                gallery.append(public_url(gal_path, base=base, bucket=bucket))

            if i == 0:
                hero_path = hero_object_path(lot_id, ext=ext)
                if hero_path and _post_object(client, base=base, key=key, bucket=bucket,
                                              path=hero_path, data=data, content_type=ct):
                    hero_url = public_url(hero_path, base=base, bucket=bucket)

    if not gallery and not hero_url:
        return None
    # Fall back to the gallery's first image if the flat hero upload failed.
    return {"hero_image_url": hero_url or (gallery[0] if gallery else None),
            "image_urls": gallery}

"""Cloudflare R2 backend for listing images (zero-egress storage).

Why this exists: the shared Supabase project hit `exceed_egress_quota` and
402-restricted Storage, taking every listing photo on black-whole.com offline.
Supabase's free tier bundles ~10 GB/month of egress; R2 bills **zero egress**
forever (10 GB storage free), so serving photos can never again take the site
down or force a plan upgrade.

This module is a drop-in transport swap, not a redesign: it reuses the exact
object-key contract from `listing_images` (hero `<key>.<ext>`, gallery
`<key>/NN.<ext>`), so rows migrated here keep the same shape and the CRM's
`listing-images` conventions still read.

Config (all required, else `is_configured()` is False and callers fall back to
the Supabase path):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
    R2_PUBLIC_BASE   e.g. https://pub-<hash>.r2.dev  (or a custom domain)
"""
from __future__ import annotations

import os
import sys

# Long cache: objects are content-stable (upserts replace in place) and the
# site re-reads URLs from the DB. Matches listing_images.CACHE_CONTROL.
CACHE_CONTROL = "max-age=604800"


def env_config() -> dict | None:
    """R2 config from the environment, or None when not fully configured."""
    account = (os.getenv("R2_ACCOUNT_ID") or "").strip()
    akey = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    skey = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    bucket = (os.getenv("R2_BUCKET") or "").strip()
    public = (os.getenv("R2_PUBLIC_BASE") or "").strip()
    if not (account and akey and skey and bucket and public):
        return None
    return {
        "account": account, "access_key": akey, "secret_key": skey,
        "bucket": bucket, "public_base": public.rstrip("/"),
        "endpoint": f"https://{account}.r2.cloudflarestorage.com",
    }


def is_configured() -> bool:
    return env_config() is not None


def public_url(object_path: str, *, public_base: str) -> str:
    """Durable public URL for an object already in the bucket."""
    return f"{public_base.rstrip('/')}/{object_path.lstrip('/')}"


def client(cfg: dict | None = None):
    """boto3 S3 client pointed at R2. Raises ImportError if boto3 is absent."""
    import boto3  # local import: only the R2 path needs the dependency
    from botocore.config import Config

    cfg = cfg or env_config()
    if not cfg:
        raise RuntimeError("R2 is not configured")
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def put_object(s3, *, bucket: str, path: str, data: bytes, content_type: str) -> bool:
    """Upload bytes to R2 (idempotent overwrite). False on any failure."""
    try:
        s3.put_object(Bucket=bucket, Key=path, Body=data,
                      ContentType=content_type or "image/jpeg",
                      CacheControl=CACHE_CONTROL)
    except Exception as e:  # noqa: BLE001 - never crash a scrape over a photo
        print(f"[r2_images] upload failed for {path!r}: {e}", file=sys.stderr)
        return False
    return True


def upload_lot_images(lot_id, paths) -> dict | None:
    """R2 twin of `listing_images.upload_lot_images` — same keys, same return.

    Returns ``{"hero_image_url": str, "image_urls": [str, ...]}`` or None when
    unconfigured / no lot id / nothing uploaded. Photos are run through the same
    `optimize_for_web` egress guard before upload, so R2 objects are the small
    JPEGs, not the raw multi-MB originals.
    """
    from pathlib import Path

    from automation import listing_images as li  # lazy: avoids an import cycle

    cfg = env_config()
    base_key = li.key_base(lot_id)
    if not cfg or not base_key:
        return None
    files = [Path(p) for p in (paths or []) if p and Path(p).exists()]
    if not files:
        return None

    try:
        s3 = client(cfg)
    except Exception as e:  # noqa: BLE001 - boto3 missing or bad config
        print(f"[r2_images] client init failed: {e}", file=sys.stderr)
        return None

    bucket, public_base = cfg["bucket"], cfg["public_base"]
    hero_url: str | None = None
    gallery: list[str] = []

    for i, fp in enumerate(files):
        try:
            data = fp.read_bytes()
        except OSError as e:
            print(f"[r2_images] read failed for {fp}: {e}", file=sys.stderr)
            continue
        if not data:
            continue
        data, ext, ct = li.optimize_for_web(data, li.guess_ext(fp.name))

        gal_path = li.gallery_object_path(lot_id, i, ext=ext)
        if gal_path and put_object(s3, bucket=bucket, path=gal_path, data=data, content_type=ct):
            gallery.append(public_url(gal_path, public_base=public_base))

        if i == 0:
            hero_path = li.hero_object_path(lot_id, ext=ext)
            if hero_path and put_object(s3, bucket=bucket, path=hero_path, data=data, content_type=ct):
                hero_url = public_url(hero_path, public_base=public_base)

    if not gallery and not hero_url:
        return None
    return {"hero_image_url": hero_url or (gallery[0] if gallery else None),
            "image_urls": gallery}

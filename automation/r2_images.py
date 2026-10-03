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

Scraped data (raw maestro archives, lot archives, snapshot backups) is NOT an
image and must never land in the public `R2_BUCKET`: it goes to the private
`LOT_ARCHIVE_R2_BUCKET` via `private_bucket()` / `put_private_object()`.
"""
from __future__ import annotations

import hashlib
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


def content_version(data: bytes) -> str:
    """Short content hash used to cache-bust a replaced object."""
    return hashlib.sha256(data).hexdigest()[:8]


def public_url(object_path: str, *, public_base: str, version: str | None = None) -> str:
    """Durable public URL for an object already in the bucket.

    `version` appends a `?v=<hash>` cache-buster. Object keys are stable (a
    re-upload replaces in place), so with a week-long Cache-Control a corrected
    image would otherwise stay invisible to anyone who already loaded the old
    one. Keying the query on the *content* hash means a fixed image gets a new
    URL immediately, while re-uploading identical bytes churns nothing.
    """
    url = f"{public_base.rstrip('/')}/{object_path.lstrip('/')}"
    return f"{url}?v={version}" if version else url


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


def object_exists(s3, *, bucket: str, path: str) -> bool:
    """HEAD one object. False on 404 or any error (read-only)."""
    try:
        s3.head_object(Bucket=bucket, Key=path)
    except Exception:  # noqa: BLE001 - absent, or unreachable: either way not servable
        return False
    return True


class PrivateBucketNotConfigured(RuntimeError):
    """LOT_ARCHIVE_R2_BUCKET is unset, or names the public image bucket."""


def private_bucket() -> str:
    """The PRIVATE R2 bucket for scraped data (raw archives, lot archives).

    `R2_BUCKET` is public (served at `R2_PUBLIC_BASE`), so anything written
    there is readable by anyone who guesses the key. Scraped auction data —
    the moat — goes to `LOT_ARCHIVE_R2_BUCKET` instead. This NEVER falls back
    to the public bucket: unset, or set to the same name as `R2_BUCKET`, is a
    hard error so a missing env var can't quietly publish the dataset.
    """
    priv = (os.getenv("LOT_ARCHIVE_R2_BUCKET") or "").strip()
    pub = (os.getenv("R2_BUCKET") or "").strip()
    if not priv:
        raise PrivateBucketNotConfigured(
            "LOT_ARCHIVE_R2_BUCKET is not set — refusing to write scraped data "
            "without a private bucket (never falls back to R2_BUCKET)")
    if priv == pub:
        raise PrivateBucketNotConfigured(
            f"LOT_ARCHIVE_R2_BUCKET == R2_BUCKET ({pub!r}) — that bucket is "
            "public; point LOT_ARCHIVE_R2_BUCKET at a private one")
    return priv


def put_private_object(s3, *, bucket: str, path: str, data: bytes,
                       content_type: str) -> bool:
    """Upload to the private bucket. No CacheControl, no public URL.

    `bucket` must be the value `private_bucket()` returned; passing the public
    bucket name is refused. False on upload failure (callers raise).
    """
    if bucket == (os.getenv("R2_BUCKET") or "").strip():
        raise PrivateBucketNotConfigured(
            f"put_private_object refused the public bucket {bucket!r}")
    try:
        s3.put_object(Bucket=bucket, Key=path, Body=data,
                      ContentType=content_type or "application/octet-stream")
    except Exception as e:  # noqa: BLE001 - callers turn False into a hard stop
        print(f"[r2_images] private upload failed for {path!r}: {e}", file=sys.stderr)
        return False
    return True


def upload_lot_images(lot_id, paths, *, status: str | None = None) -> dict | None:
    """R2 twin of `listing_images.upload_lot_images` — same keys, same return.

    Returns ``{"hero_image_url": str, "image_urls": [str, ...]}`` or None when
    unconfigured / no lot id / nothing uploaded. The URLs are always the
    **clean** variant (`photo_policy`): the actual photo, web-optimised, no
    disguise or watermark, under `p/<hmac>/h.o.jpg` and `p/<hmac>/<tok>.o.jpg`.
    When `status` is `active_bid` the disguised + watermarked twin (see
    `image_disguise`) is also written at `h.jpg` / `<tok>.jpg` — the copy
    black-whole.com shows while we're still bidding. With `IMAGE_DISGUISE=0` it is the legacy
    `optimize_for_web` JPEG under the lot-id key (never watermarked).
    """
    from pathlib import Path

    from automation import image_disguise, photo_policy
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
    # Disguised photos go under opaque keys; the legacy keys spell the lot id.
    opaque = image_disguise.enabled()
    watermark = opaque and photo_policy.keeps_watermark_variant(status)

    for i, fp in enumerate(files):
        try:
            source = fp.read_bytes()
        except OSError as e:
            print(f"[r2_images] read failed for {fp}: {e}", file=sys.stderr)
            continue
        if not source:
            continue
        prepared = li.prepare_for_web(source, li.guess_ext(fp.name), key=base_key,
                                      status=status, channel=None)
        if prepared is None:
            print(f"[r2_images] skipped unreadable image {fp.name}", file=sys.stderr)
            continue
        data, ext, ct = prepared
        marked = None
        if watermark:
            marked = li.prepare_for_web(source, li.guess_ext(fp.name), key=base_key,
                                        status=status, channel=photo_policy.SITE)

        ver = content_version(data)

        if opaque:
            wm_gal = li.opaque_gallery_path(lot_id, source)
            gal_path = photo_policy.clean_path(wm_gal) if wm_gal else None
        else:
            wm_gal, gal_path = None, li.gallery_object_path(lot_id, i, ext=ext)
        if gal_path and put_object(s3, bucket=bucket, path=gal_path, data=data, content_type=ct):
            gallery.append(public_url(gal_path, public_base=public_base, version=ver))
            if marked and wm_gal:
                put_object(s3, bucket=bucket, path=wm_gal, data=marked[0], content_type=marked[2])

        if hero_url is None:  # first photo that made it through is the cover
            if opaque:
                wm_hero = li.opaque_hero_path(lot_id)
                hero_path = photo_policy.clean_path(wm_hero) if wm_hero else None
            else:
                wm_hero, hero_path = None, li.hero_object_path(lot_id, ext=ext)
            if hero_path and put_object(s3, bucket=bucket, path=hero_path, data=data, content_type=ct):
                hero_url = public_url(hero_path, public_base=public_base, version=ver)
                if marked and wm_hero:
                    put_object(s3, bucket=bucket, path=wm_hero, data=marked[0],
                               content_type=marked[2])

    if not gallery and not hero_url:
        return None
    return {"hero_image_url": hero_url or (gallery[0] if gallery else None),
            "image_urls": gallery}

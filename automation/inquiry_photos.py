"""Seller photos attached to a storefront inquiry (sell form, optional).

Flow: the form POSTs /contact as before and gets back ``{id, photo_token}``;
the browser then POSTs the picked files to ``/contact/{id}/photos`` with that
token. Files go to the PRIVATE R2 bucket (a seller's photos are theirs, not
ours to publish) under ``inquiries/<id>/<hex>.<ext>``; the admin reads them
through the session-walled ``/api/inquiry-photos/{key}`` proxy.

Guards: the token is an HMAC of the inquiry id + created_at, so nobody can
attach files to someone else's inquiry; uploads close ``UPLOAD_WINDOW`` after
the inquiry was filed; at most ``MAX_PHOTOS`` per inquiry, ``MAX_BYTES`` each;
only real JPEG / PNG / WebP / HEIC / GIF bytes (sniffed, the client's
content-type is ignored).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

MAX_PHOTOS = 6
MAX_BYTES = 10 * 1024 * 1024
UPLOAD_WINDOW = timedelta(hours=2)

KEY_RE = re.compile(r"^inquiries/\d{1,9}/[0-9a-f]{16}\.(jpg|png|webp|heic|gif)$")

_CTYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
           "heic": "image/heic", "gif": "image/gif"}

_BOOT_KEY = secrets.token_bytes(32)


class PhotoRejected(ValueError):
    """A client error: bad token, window closed, too many, too big, not an image."""


def _key() -> bytes:
    s = (os.getenv("SESSION_SECRET") or "").strip()
    return s.encode() if s else _BOOT_KEY


def _stamp(created_at) -> str:
    if isinstance(created_at, datetime):
        return created_at.astimezone(timezone.utc).isoformat()
    return str(created_at or "")


def upload_token(inquiry_id: int, created_at) -> str:
    msg = f"inquiry-photos|{int(inquiry_id)}|{_stamp(created_at)}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()[:32]


def check_token(inquiry: dict, token: str) -> bool:
    want = upload_token(inquiry["id"], inquiry.get("created_at"))
    return bool(token) and hmac.compare_digest(want, str(token))


def sniff(data: bytes) -> str | None:
    """File extension from the magic bytes, or None when it is not an image we take."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
        return "heic"
    return None


def content_type(key: str) -> str:
    return _CTYPES.get(key.rsplit(".", 1)[-1], "application/octet-stream")


def validate(inquiry: dict | None, token: str, existing: int, files: list[bytes],
             now: datetime | None = None) -> list[str]:
    """Extensions for each file, or raise PhotoRejected. Pure: no I/O."""
    if not inquiry or not check_token(inquiry, token):
        raise PhotoRejected("invalid upload token")
    created = inquiry.get("created_at")
    now = now or datetime.now(timezone.utc)
    if isinstance(created, datetime) and now - created > UPLOAD_WINDOW:
        raise PhotoRejected("photo upload window closed — email the photos instead")
    if not files:
        raise PhotoRejected("no photos")
    if existing + len(files) > MAX_PHOTOS:
        raise PhotoRejected(f"at most {MAX_PHOTOS} photos")
    exts = []
    for data in files:
        if not data:
            raise PhotoRejected("empty file")
        if len(data) > MAX_BYTES:
            raise PhotoRejected(f"each photo must be under {MAX_BYTES // (1024 * 1024)} MB")
        ext = sniff(data)
        if not ext:
            raise PhotoRejected("only JPEG, PNG, WebP, HEIC or GIF photos")
        exts.append(ext)
    return exts


def new_key(inquiry_id: int, ext: str) -> str:
    return f"inquiries/{int(inquiry_id)}/{secrets.token_hex(8)}.{ext}"


def store(inquiry_id: int, files: list[bytes], exts: list[str]) -> list[str]:
    """Upload to the private bucket; returns the keys. Raises on any failure."""
    from automation import r2_images
    cfg = r2_images.env_config()
    if not cfg:
        raise RuntimeError("R2 is not configured")
    s3 = r2_images.client(cfg)
    bucket = r2_images.private_bucket()
    keys = []
    for data, ext in zip(files, exts):
        key = new_key(inquiry_id, ext)
        if not r2_images.put_private_object(s3, bucket=bucket, path=key, data=data,
                                            content_type=_CTYPES[ext]):
            raise RuntimeError(f"upload failed for {key}")
        keys.append(key)
    return keys


def fetch(key: str) -> tuple[bytes, str] | None:
    """(bytes, content type) of one inquiry photo, or None when malformed/absent."""
    if not KEY_RE.match(key or ""):
        return None
    from automation import r2_images
    cfg = r2_images.env_config()
    if not cfg:
        raise RuntimeError("R2 is not configured")
    s3 = r2_images.client(cfg)
    try:
        obj = s3.get_object(Bucket=r2_images.private_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001 - NoSuchKey and friends
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return obj["Body"].read(), content_type(key)

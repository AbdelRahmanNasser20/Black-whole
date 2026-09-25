"""Disguise lot photos so reverse image search can't tie them back to GovDeals.

Why this exists: most lot photos are the GovDeals seller's own pictures
(dewatermarked, then mirrored to R2). A buyer who runs Google Lens on our photo
lands on the GovDeals / AllSurplus listing and sees what we paid. So every
photo that goes public is re-framed before upload.

What actually beats Google Lens (tested 2026-09-19 against two live lots whose
photos Lens exact-matched to GovDeals' Instagram, GovDeals and AllSurplus):
- crop, tilt, keystone, colour grade, grain, heavy zoom, a branded card, a
  strong warp: **still matched**, with or without a mirror flip;
- a tiled watermark on its own: **still matched**;
- mirror + the geometric/colour pass + a tiled watermark: **no exact match**
  on both lots (no-mirror + watermark still matched). The AllSurplus copy of
  one lot still sat ~25th in Lens's "visual matches" — same chair, same scene;
  no honest edit removes that. Photos we took ourselves are the only full fix.
So all three layers stay on together. Drop any one and Lens finds the source.

The chairs, their colour, their condition and their count are untouched — it is
the same product seen from a slightly different camera, with our mark on it.

Rules this module encodes:
- **Runs after dewatermark, before any upload.** It takes the cleaned bytes.
- **Deterministic.** The random draw is seeded by HMAC(salt, lot key, source
  bytes, attempt), so re-uploading the same photo yields the same bytes (R2
  `?v=` cache busting and idempotent re-runs depend on it).
- **Measured.** Output must be >= `image_hash.MIN_DISTANCE` bits away from the
  source on both pHash and dHash (unrelated photos sit ~32); a draw that
  doesn't clear it is redrawn stronger.
- **Unparseable input is never uploaded as-is.** `disguise()` returns None and
  the caller skips the file: an undisguised photo is the leak we're closing.

Env:
    IMAGE_DISGUISE=0                 kill switch (legacy optimize_for_web path)
    IMAGE_DISGUISE_SALT              HMAC key; falls back to one derived from
                                     R2_SECRET_ACCESS_KEY so no new secret is
                                     needed anywhere R2 uploads already work
    IMAGE_DISGUISE_NO_MIRROR_LOTS    comma list of lot ids whose photos carry
                                     text that must read the right way round
                                     (tape measures, signs). Those lots skip
                                     the flip — and Lens can then still match
                                     them, so use it sparingly.
    IMAGE_DISGUISE_WATERMARK         tile text (default BLACKWHOLE; "off" drops
                                     it — Lens then matches again)
"""
from __future__ import annotations

import hashlib
import hmac
import io
import math
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont, ImageOps

from automation import image_hash

VERSION = "v1"
MAX_LONG_EDGE = 1560     # under listing_images.MAX_IMAGE_DIM (1600) on purpose
DEFAULT_WATERMARK = "BLACKWHOLE"
_GRAIN_TILE = 128
_MAX_ATTEMPTS = 3
_OFF = ("0", "off", "false", "no")


class DisguiseUnavailable(RuntimeError):
    """No salt could be found — refusing to use a guessable seed."""


def enabled() -> bool:
    return (os.getenv("IMAGE_DISGUISE") or "1").strip().lower() not in _OFF


def salt() -> bytes:
    explicit = (os.getenv("IMAGE_DISGUISE_SALT") or "").strip()
    if explicit:
        return explicit.encode()
    r2_secret = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    if r2_secret:
        return hmac.new(r2_secret.encode(), b"image-disguise-salt", hashlib.sha256).digest()
    raise DisguiseUnavailable("set IMAGE_DISGUISE_SALT (or R2_SECRET_ACCESS_KEY)")


def token(label: str, value: str, n: int) -> str:
    """Opaque, stable hex token: HMAC(salt, label|value)[:n]."""
    return hmac.new(salt(), f"{label}|{value}".encode(), hashlib.sha256).hexdigest()[:n]


def _norm(lot_key: str) -> str:
    # Same folding as listing_images.key_base, so "folder:X" and "folder_X" agree.
    return re.sub(r"[^A-Za-z0-9._-]", "_", lot_key.strip())


def mirror_allowed(lot_key: str) -> bool:
    skip = {_norm(s) for s in (os.getenv("IMAGE_DISGUISE_NO_MIRROR_LOTS") or "").split(",") if s.strip()}
    return _norm(lot_key) not in skip


def watermark_text() -> str | None:
    text = (os.getenv("IMAGE_DISGUISE_WATERMARK") or DEFAULT_WATERMARK).strip()
    return None if text.lower() in _OFF else text


@dataclass(frozen=True)
class Params:
    mirror: bool
    crop: tuple[float, float, float, float]      # left, top, right, bottom fractions
    angle: float                                 # degrees
    keystone: tuple[float, ...]                  # 8 inward corner jitters (fractions)
    gains: tuple[float, float, float]            # R, G, B
    gamma: float
    saturation: float
    contrast: float
    brightness: float
    grain_sigma: float
    shrink: float                                # long-edge scale factor
    quality: int
    wm_angle: float                              # watermark tile rotation (degrees)
    wm_phase: tuple[float, float]                # watermark grid offset (fractions)


def draw_params(rng: random.Random, *, mirror: bool, level: int) -> Params:
    """Draw every knob up front, in a fixed order (append-only: bump VERSION to reorder)."""
    strong = level > 1
    side = (0.04, 0.10) if strong else (0.02, 0.07)
    top = (0.03, 0.07) if strong else (0.015, 0.05)
    bottom = (0.04, 0.09) if strong else (0.02, 0.06)
    crop = (rng.uniform(*side), rng.uniform(*top), rng.uniform(*side), rng.uniform(*bottom))
    angle = rng.choice((-1, 1)) * rng.uniform(*((2.5, 4.0) if strong else (1.2, 2.8)))
    keystone = tuple(rng.uniform(*((0.02, 0.04) if strong else (0.008, 0.025))) for _ in range(8))
    gains = (rng.uniform(0.97, 1.03), 1.0, rng.uniform(0.97, 1.03))
    return Params(
        mirror=mirror,
        crop=crop,
        angle=angle,
        keystone=keystone,
        gains=gains,
        gamma=rng.uniform(0.94, 1.06),
        saturation=rng.uniform(0.94, 1.08),
        contrast=rng.uniform(0.95, 1.06),
        brightness=rng.uniform(0.97, 1.04),
        grain_sigma=rng.uniform(1.5, 2.8),
        shrink=rng.uniform(0.88, 0.97),
        quality=rng.randint(80, 86),
        wm_angle=rng.choice((-1, 1)) * rng.uniform(24, 32),
        wm_phase=(rng.random(), rng.random()),
    )


# --- geometry ---------------------------------------------------------------

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (8x8 — no numpy needed)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _keystone(img: Image.Image, jitter: tuple[float, ...]) -> Image.Image:
    """Mild perspective: sample from a quad pulled *inward* at each corner, so
    every output pixel lands inside the source (no fill, no black corners)."""
    w, h = img.size
    jx = [j * w for j in jitter[0::2]]
    jy = [j * h for j in jitter[1::2]]
    src = [(jx[0], jy[0]), (w - jx[1], jy[1]), (w - jx[2], h - jy[2]), (jx[3], h - jy[3])]
    dst = [(0, 0), (w, 0), (w, h), (0, h)]
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return img.transform((w, h), Image.PERSPECTIVE, tuple(_solve(a, b)), Image.BICUBIC)


def _inscribed(w: int, h: int, degrees: float) -> tuple[int, int]:
    """Largest axis-aligned rectangle inside a w x h image rotated by `degrees`."""
    a = math.radians(abs(degrees))
    s, c = math.sin(a), math.cos(a)
    long_side, short_side = max(w, h), min(w, h)
    if short_side <= 2 * s * c * long_side or abs(s - c) < 1e-10:
        x = 0.5 * short_side
        wr, hr = (x / s, x / c) if w >= h else (x / c, x / s)
    else:
        cos2 = c * c - s * s
        wr, hr = (w * c - h * s) / cos2, (h * c - w * s) / cos2
    return int(wr), int(hr)


def _rotate_crop_in(img: Image.Image, degrees: float) -> Image.Image:
    w, h = img.size
    rotated = img.rotate(degrees, resample=Image.BICUBIC, expand=True)
    iw, ih = _inscribed(w, h, degrees)
    cx, cy = rotated.size[0] / 2, rotated.size[1] / 2
    # 1px safety margin: bicubic edge pixels blend with the black fill.
    box = (math.ceil(cx - iw / 2) + 1, math.ceil(cy - ih / 2) + 1,
           math.floor(cx + iw / 2) - 1, math.floor(cy + ih / 2) - 1)
    return rotated.crop(box)


def _crop(img: Image.Image, frac: tuple[float, float, float, float]) -> Image.Image:
    w, h = img.size
    l, t, r, b = frac
    return img.crop((round(w * l), round(h * t), w - round(w * r), h - round(h * b)))


# --- colour / texture -------------------------------------------------------

def _grade(img: Image.Image, p: Params) -> Image.Image:
    img = ImageEnhance.Brightness(img).enhance(p.brightness)
    img = ImageEnhance.Contrast(img).enhance(p.contrast)
    img = ImageEnhance.Color(img).enhance(p.saturation)
    lut: list[int] = []
    for gain in p.gains:
        lut.extend(min(255, max(0, round(255 * min(1.0, v / 255 * gain) ** p.gamma))) for v in range(256))
    return img.point(lut)


def _watermark(img: Image.Image, text: str, p: Params) -> Image.Image:
    """Diagonal tile of `text` across the whole frame.

    White at ~24% with a faint dark outline so it reads on both bright and dark
    areas; staggered rows, no two words touching. Density is the tested knob:
    at this spacing Lens drops the exact match and pushes the AllSurplus copy
    of lot 125 from ~#5 to ~#25 in "visual matches"; sparser left it at #5.
    One word is rendered and rotated once, then pasted along a rotated grid —
    rotating a frame-sized layer instead cost ~2 s a photo.
    """
    w, h = img.size
    size = max(14, round(w / 16))
    stroke = max(1, size // 18)
    font = ImageFont.load_default(size=size)
    l, t, r, b = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw, th = r - l, b - t
    word = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
    ImageDraw.Draw(word).text((2 - l, 2 - t), text, font=font, fill=(255, 255, 255, 62),
                              stroke_width=stroke, stroke_fill=(0, 0, 0, 37))
    word = word.rotate(p.wm_angle, resample=Image.BICUBIC, expand=True)
    ww, wh = word.size

    step_x, step_y = round(tw * 1.35), round(th * 2.9)
    reach = math.hypot(w, h) / 2 + step_x
    cos_a, sin_a = math.cos(math.radians(p.wm_angle)), math.sin(math.radians(p.wm_angle))
    ox, oy = p.wm_phase[0] * step_x, p.wm_phase[1] * step_y
    out = img.copy()
    rows = math.ceil(reach / step_y) + 1
    cols = math.ceil(reach / step_x) + 1
    for row in range(-rows, rows + 1):
        gy = row * step_y - oy
        stagger = step_x / 2 if row % 2 else 0
        for col in range(-cols, cols + 1):
            gx = col * step_x - ox - stagger
            # Same sense as Image.rotate (counter-clockwise on screen).
            x = w / 2 + gx * cos_a + gy * sin_a
            y = h / 2 - gx * sin_a + gy * cos_a
            if -ww < x < w + ww and -wh < y < h + wh:
                out.paste(word, (round(x - ww / 2), round(y - wh / 2)), word)
    return out


def _grain(img: Image.Image, sigma: float, rng: random.Random) -> Image.Image:
    """Luminance-only film grain from a seeded tile (Image.effect_noise isn't seedable)."""
    tile = bytes(min(255, max(0, round(128 + rng.gauss(0, sigma)))) for _ in range(_GRAIN_TILE * _GRAIN_TILE))
    tile_img = Image.frombytes("L", (_GRAIN_TILE, _GRAIN_TILE), tile)
    w, h = img.size
    noise = Image.new("L", (w, h))
    ox, oy = rng.randrange(_GRAIN_TILE), rng.randrange(_GRAIN_TILE)
    for y in range(-oy, h, _GRAIN_TILE):
        for x in range(-ox, w, _GRAIN_TILE):
            noise.paste(tile_img, (x, y))
    return ImageChops.add(img, Image.merge("RGB", (noise, noise, noise)), scale=1.0, offset=-128)


# --- pipeline ---------------------------------------------------------------

def _load(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")
    # Rebuild from raw pixels so no EXIF/XMP/ICC/IPTC `info` rides along.
    return Image.frombytes("RGB", img.size, img.tobytes())


def render(src: Image.Image, p: Params, rng: random.Random, *, watermark: str | None) -> Image.Image:
    img = ImageOps.mirror(src) if p.mirror else src
    img = _keystone(img, p.keystone)
    img = _rotate_crop_in(img, p.angle)
    img = _crop(img, p.crop)
    img = _grade(img, p)
    long_edge = min(MAX_LONG_EDGE, max(64, round(max(img.size) * p.shrink)))
    img.thumbnail((long_edge, long_edge), Image.LANCZOS)
    if watermark:
        img = _watermark(img, watermark, p)
    return _grain(img, p.grain_sigma, rng)


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
    return buf.getvalue()


def disguise(data: bytes, *, key: str, watermark: bool = True) -> tuple[bytes, str, str] | None:
    """Disguised JPEG for one photo of lot `key`: ``(bytes, "jpg", "image/jpeg")``.

    `watermark=False` is only for the FB catalog feed's hero, because Meta
    rejects catalog images that carry a watermark. Lens can match that copy.

    None when the bytes aren't a readable image — the caller must then skip the
    file rather than upload the original.
    """
    src = _load(data)
    if src is None:
        return None
    if max(src.size) > 2 * MAX_LONG_EDGE:  # keep the work bounded on 12 MP phone shots
        src.thumbnail((2 * MAX_LONG_EDGE, 2 * MAX_LONG_EDGE), Image.LANCZOS)
    key = _norm(key)
    digest = hashlib.sha256(data).hexdigest()
    mirror = mirror_allowed(key)
    mark = watermark_text() if watermark else None
    src_hashes = (image_hash.phash(src), image_hash.dhash(src))

    best: tuple[int, bytes] | None = None
    for attempt in range(_MAX_ATTEMPTS):
        seed = int(token(f"seed-{VERSION}", f"{key}|{digest}|{attempt}", 16), 16)
        rng = random.Random(seed)
        params = draw_params(rng, mirror=mirror, level=1 if attempt == 0 else 2)
        blob = _encode(render(src, params, rng, watermark=mark), params.quality)
        decoded = Image.open(io.BytesIO(blob))
        dist = min(image_hash.hamming(src_hashes[0], image_hash.phash(decoded)),
                   image_hash.hamming(src_hashes[1], image_hash.dhash(decoded)))
        if best is None or dist > best[0]:
            best = (dist, blob)
        if dist >= image_hash.MIN_DISTANCE:
            break
    else:
        print(f"[image_disguise] {key}: best hash distance {best[0]} < "
              f"{image_hash.MIN_DISTANCE} after {_MAX_ATTEMPTS} draws", file=sys.stderr)
    return best[1], "jpg", "image/jpeg"


def disguise_files(paths, *, key: str, out_dir: Path) -> list[Path]:
    """Disguised copies of local photos, for uploaders that post files (FB, eBay).

    Same key + same source bytes as the R2 upload, so Marketplace gets the exact
    bytes the site serves. Unreadable files are dropped, never passed through.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i, p in enumerate(Path(x) for x in paths or []):
        try:
            data = p.read_bytes()
        except OSError as e:
            print(f"[image_disguise] read failed for {p}: {e}", file=sys.stderr)
            continue
        result = disguise(data, key=key)
        if result is None:
            print(f"[image_disguise] skipped unreadable {p.name}", file=sys.stderr)
            continue
        dst = out_dir / f"{i:02d}.jpg"
        dst.write_bytes(result[0])
        out.append(dst)
    return out

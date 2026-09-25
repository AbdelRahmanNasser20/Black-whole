"""Photos disguised before they go public (automation/image_disguise.py).

Offline: synthetic fixtures, FakeS3 injected. What matters here is the contract
the Lens tests of 2026-09-19 rest on — every public photo is mirrored,
re-framed, watermarked, stripped of metadata, stored under an opaque key, and
never uploaded raw — plus determinism (idempotent re-runs, stable R2 URLs).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import image_disguise as d  # noqa: E402
from automation import image_hash  # noqa: E402
from automation import listing_images as li  # noqa: E402
from automation import r2_images as r2  # noqa: E402

_R2 = {
    "R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "ak", "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET": "blackwhole-images", "R2_PUBLIC_BASE": "https://pub-xyz.r2.dev",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IMAGE_DISGUISE_SALT", "test-salt")
    for var in ("IMAGE_DISGUISE", "IMAGE_DISGUISE_NO_MIRROR_LOTS", "IMAGE_DISGUISE_WATERMARK"):
        monkeypatch.delenv(var, raising=False)


def _photo(size=(1200, 900), *, exif=False) -> bytes:
    """Bright scene with a distinct left/right layout (so a mirror is detectable)."""
    img = Image.new("RGB", size, (235, 228, 210))
    dr = ImageDraw.Draw(img)
    w, h = size
    for i in range(6):  # "chairs": dark blocks, denser on the left
        x = int(w * (0.08 + i * 0.09))
        dr.rectangle((x, int(h * 0.45), x + int(w * 0.05), int(h * 0.85)), fill=(70, 40, 30))
    dr.rectangle((int(w * 0.75), int(h * 0.1), int(w * 0.95), int(h * 0.3)), fill=(30, 90, 140))
    buf = io.BytesIO()
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "Apple"          # Make
        ex[0x8825] = {2: (33, 30, 0)}  # GPS IFD — the storage-unit location leak
        img.save(buf, format="JPEG", quality=92, exif=ex.tobytes())
    else:
        img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _open(blob: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(blob))
    img.load()
    return img


# --- the transform ------------------------------------------------------------

def test_deterministic_for_same_photo_and_lot():
    src = _photo()
    assert d.disguise(src, key="gd-239-31465")[0] == d.disguise(src, key="gd-239-31465")[0]


def test_differs_across_lots_and_salts(monkeypatch):
    src = _photo()
    a = d.disguise(src, key="lot-a")[0]
    assert a != d.disguise(src, key="lot-b")[0]
    monkeypatch.setenv("IMAGE_DISGUISE_SALT", "other-salt")
    assert a != d.disguise(src, key="lot-a")[0]


def test_returns_jpeg_and_moves_the_hashes():
    src = _photo()
    blob, ext, ct = d.disguise(src, key="31225")
    assert (ext, ct) == ("jpg", "image/jpeg")
    p, dh = image_hash.distance(_open(src), _open(blob))
    assert p >= image_hash.MIN_DISTANCE and dh >= image_hash.MIN_DISTANCE


def test_strips_exif_gps_and_icc():
    out = _open(d.disguise(_photo(exif=True), key="31225")[0])
    assert len(out.getexif()) == 0
    assert "icc_profile" not in out.info and "exif" not in out.info


def test_no_black_corners_from_rotation_or_keystone():
    out = _open(d.disguise(_photo(), key="corners")[0]).convert("L")
    w, h = out.size
    for x, y in ((0, 0), (w - 8, 0), (0, h - 8), (w - 8, h - 8)):
        patch = out.crop((x, y, x + 8, y + 8))
        assert sum(patch.tobytes()) / 64 > 60, f"dark corner at {(x, y)}"


def test_mirrors_by_default_and_honours_the_no_mirror_list(monkeypatch):
    def left_heavier(blob):
        g = _open(blob).convert("L")
        w, h = g.size
        left = sum(g.crop((0, 0, w // 2, h)).tobytes())
        right = sum(g.crop((w // 2, 0, w, h)).tobytes())
        return left < right  # dark chairs sit on the left of the source

    src = _photo()
    assert left_heavier(src)
    monkeypatch.setenv("IMAGE_DISGUISE_WATERMARK", "off")  # keep the luminance test clean
    assert not left_heavier(d.disguise(src, key="31225")[0])
    monkeypatch.setenv("IMAGE_DISGUISE_NO_MIRROR_LOTS", "folder:Tape_Measure_Lot, 31225")
    assert left_heavier(d.disguise(src, key="31225")[0])
    assert not d.mirror_allowed("folder_Tape_Measure_Lot")  # same folding as key_base


def test_watermark_can_be_turned_off(monkeypatch):
    src = _photo()
    marked = d.disguise(src, key="31225")[0]
    monkeypatch.setenv("IMAGE_DISGUISE_WATERMARK", "off")
    assert d.watermark_text() is None
    assert d.disguise(src, key="31225")[0] != marked


def test_unreadable_bytes_return_none_never_the_original():
    assert d.disguise(b"not an image", key="31225") is None


def test_missing_salt_refuses(monkeypatch):
    monkeypatch.delenv("IMAGE_DISGUISE_SALT")
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(d.DisguiseUnavailable):
        d.disguise(_photo(), key="31225")


def test_salt_falls_back_to_r2_secret(monkeypatch):
    monkeypatch.delenv("IMAGE_DISGUISE_SALT")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    assert d.disguise(_photo(), key="31225") is not None


# --- the gate every upload passes -------------------------------------------

def test_kill_switch_restores_legacy_optimize(monkeypatch):
    src = _photo()
    monkeypatch.setenv("IMAGE_DISGUISE", "0")
    assert li.prepare_for_web(src, "jpg", key="31225") == li.optimize_for_web(src, "jpg")


def test_opaque_keys_hide_the_lot_id():
    for key in (li.opaque_hero_path("gd-239-31465"), li.opaque_gallery_path("gd-239-31465", b"x")):
        assert key.startswith("p/")
        assert "239" not in key and "31465" not in key
    assert li.opaque_hero_path("gd-239-31465") == li.opaque_hero_path("gd-239-31465")
    assert li.opaque_gallery_path("a", b"x") != li.opaque_gallery_path("a", b"y")


def test_is_disguised_url():
    assert li.is_disguised_url("https://pub-xyz.r2.dev/p/abc/h.jpg?v=1")
    assert li.is_disguised_url("https://img.black-whole.com/p/abc/0d.jpg")
    assert not li.is_disguised_url("https://pub-xyz.r2.dev/31225/00.jpg?v=1")
    assert not li.is_disguised_url(None)


class FakeS3:
    def __init__(self): self.puts = []
    def put_object(self, **kw): self.puts.append(kw)


def test_r2_upload_disguises_and_uses_opaque_keys(tmp_path, monkeypatch):
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    fake = FakeS3()
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(_photo())
    b.write_bytes(_photo(size=(900, 1200)))

    out = r2.upload_lot_images("gd-239-31465", [a, b])

    keys = [p["Key"] for p in fake.puts]
    assert all(k.startswith("p/") and "31465" not in k for k in keys)
    assert out["hero_image_url"].split("?")[0].endswith("/h.jpg")
    assert len(out["image_urls"]) == 2 and all(li.is_disguised_url(u) for u in out["image_urls"])
    sources = {a.read_bytes(), b.read_bytes()}
    assert all(p["Body"] not in sources and p["ContentType"] == "image/jpeg" for p in fake.puts)


def test_r2_upload_skips_unreadable_files_instead_of_publishing_them(tmp_path, monkeypatch):
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    fake = FakeS3()
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)
    bad, good = tmp_path / "bad.heic", tmp_path / "good.jpg"
    bad.write_bytes(b"\x00\x00\x00\x18ftypheic not decodable here")
    good.write_bytes(_photo())

    out = r2.upload_lot_images("31225", [bad, good])

    assert all(p["Body"] != bad.read_bytes() for p in fake.puts)
    assert len(out["image_urls"]) == 1  # the good one, now also the hero
    assert out["hero_image_url"].split("?")[0].endswith("/h.jpg")


def test_public_copies_match_what_r2_serves(tmp_path, monkeypatch):
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    fake = FakeS3()
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)
    src = tmp_path / "a.jpg"
    src.write_bytes(_photo())

    r2.upload_lot_images("31225", [src])
    copies = li.public_copies("31225", [src], out_dir=tmp_path / "pub")

    assert [c.read_bytes() for c in copies] == [fake.puts[0]["Body"]]


def test_public_copies_is_identity_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_DISGUISE", "0")
    src = tmp_path / "a.jpg"
    src.write_bytes(_photo())
    assert li.public_copies("31225", [src]) == [src]


# --- hashes -------------------------------------------------------------------

def test_hash_self_distance_zero_and_reencode_close():
    img = _open(_photo())
    assert image_hash.distance(img, img) == (0, 0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    p, dh = image_hash.distance(img, _open(buf.getvalue()))
    assert p <= 4 and dh <= 4


# --- FB catalog twin (Meta rejects watermarked catalog images) ----------------

def test_catalog_url_maps_only_disguised_heroes():
    hero = "https://pub-xyz.r2.dev/p/abc/h.jpg?v=12345678"
    assert li.catalog_url(hero) == "https://pub-xyz.r2.dev/p/abc/h.c.jpg?v=12345678"
    gallery = "https://pub-xyz.r2.dev/p/abc/0f0f0f.jpg?v=1"
    legacy = "https://pub-xyz.r2.dev/31225.jpg?v=1"
    assert li.catalog_url(gallery) == gallery
    assert li.catalog_url(legacy) == legacy
    assert li.catalog_url(None) is None


def test_r2_upload_puts_a_watermark_free_hero_twin(tmp_path, monkeypatch):
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    fake = FakeS3()
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)
    src = tmp_path / "a.jpg"
    src.write_bytes(_photo())

    r2.upload_lot_images("31225", [src])

    bodies = {p["Key"]: p["Body"] for p in fake.puts}
    hero_key = li.opaque_hero_path("31225")
    twin_key = li.catalog_path(hero_key)
    assert twin_key in bodies and bodies[twin_key] != bodies[hero_key]
    assert bodies[twin_key] == d.disguise(src.read_bytes(), key="31225", watermark=False)[0]


def test_catalog_feed_ships_the_twin(monkeypatch):
    from automation import catalog_feed
    row = {"lot_id": "31225", "title": "300 banquet chairs", "price_per_chair": 12,
           "quantity_remaining": 300, "status": "owned",
           "hero_image_url": "https://pub-xyz.r2.dev/p/abc/h.jpg?v=1", "image_urls": []}
    assert catalog_feed._image_link(row) == "https://pub-xyz.r2.dev/p/abc/h.c.jpg?v=1"

"""BLACKWHOLE-31 — the one lot-photo resolver, and the guard on offerable lots.

The regression these lock down: photos that resolve only on the operator's
laptop. The bot runs on a server with no Desktop folder, so a lot whose photos
live only on disk is a lot the bot silently can't show.
"""
from __future__ import annotations

import pytest

from automation import lot_images as li


# ───────────────────────── precedence: DB over disk ─────────────────────────

def test_gallery_urls_win_over_local_disk(tmp_path):
    """A host with both should still hand out URLs — they work everywhere."""
    (tmp_path / "a.jpg").write_bytes(b"x")
    row = {
        "lot_id": "31225",
        "image_urls": ["https://cdn.example/31225/00.jpg"],
        "hero_image_url": "https://cdn.example/31225.jpg",
        "folder_path": str(tmp_path),
    }
    resolved = li.resolve(row)
    assert resolved.source == "db"
    assert resolved.urls == ["https://cdn.example/31225/00.jpg"]
    # local paths are still exposed for callers that want free local files
    assert len(resolved.local_paths) == 1


def test_hero_only_row_still_resolves():
    row = {"lot_id": "9006", "image_urls": [], "hero_image_url": "https://cdn.example/9006.jpg"}
    assert li.resolve(row).urls == ["https://cdn.example/9006.jpg"]


def test_hero_is_not_appended_to_a_populated_gallery():
    """File 0 is uploaded under both the hero and gallery keys.

    Unioning hero + gallery attaches the same photo twice on every send. The
    hero still surfaces as the cover, just not as an extra gallery entry.
    """
    row = {
        "lot_id": "31225",
        "hero_image_url": "https://cdn.example/31225.jpg?v=abc",
        "image_urls": ["https://cdn.example/31225/00.jpg?v=abc",
                       "https://cdn.example/31225/01.jpg?v=def"],
    }
    resolved = li.resolve(row)
    assert len(resolved.urls) == 2
    assert not any(u.endswith("31225.jpg?v=abc") for u in resolved.urls)
    # the dedicated hero column still names the cover
    assert resolved.hero == "https://cdn.example/31225.jpg?v=abc"


def test_hero_defaults_to_the_first_gallery_url():
    row = {"image_urls": ["https://cdn.example/a/00.jpg"]}
    assert li.resolve(row).hero == "https://cdn.example/a/00.jpg"


def test_falls_back_to_disk_when_no_urls(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"x")
    resolved = li.resolve({"lot_id": "x", "folder_path": str(tmp_path)})
    assert resolved.source == "local"
    assert resolved.urls == []
    assert len(resolved.local_paths) == 1


def test_empty_row_is_falsy_not_an_exception():
    resolved = li.resolve({})
    assert not resolved
    assert resolved.source == "none"
    assert resolved.count == 0


def test_urls_are_deduped_and_non_http_dropped():
    row = {"image_urls": ["https://a/1.jpg", "https://a/1.jpg", "", None, "/local/2.jpg"]}
    assert li.resolve(row).urls == ["https://a/1.jpg"]


# ───────────────────────────── local disk rules ─────────────────────────────

def test_internal_subdirs_never_leak_into_a_listing(tmp_path):
    """`_originals` (pre-dewatermark) and `_screenshots` (debug) are ours only."""
    (tmp_path / "photo.png").write_bytes(b"x")
    for sub in ("_originals", "_screenshots"):
        d = tmp_path / sub
        d.mkdir()
        (d / "leak.png").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    paths = li.local_image_paths({"folder_path": str(tmp_path)})
    assert [p.rsplit("/", 1)[-1] for p in paths] == ["photo.png"]


def test_hero_image_sorts_first(tmp_path):
    for name in ("a.jpg", "b.jpg", "z.jpg"):
        (tmp_path / name).write_bytes(b"x")
    paths = li.local_image_paths({"folder_path": str(tmp_path), "hero_image": "z.jpg"})
    assert [p.rsplit("/", 1)[-1] for p in paths] == ["z.jpg", "a.jpg", "b.jpg"]


def test_folder_name_fallback_when_folder_path_is_null(tmp_path, monkeypatch):
    """Hand-added `owned` rows carry only `folder_name`."""
    lot = tmp_path / "ATL_Grey_blueish_chairs_399"
    lot.mkdir()
    (lot / "img.jpg").write_bytes(b"x")
    monkeypatch.setattr(li, "DEFAULT_PICTURES_DIR", str(tmp_path))
    assert li.lot_folder({"folder_name": "ATL_Grey_blueish_chairs_399"}) == str(lot)


def test_missing_folder_is_not_an_error():
    assert li.local_image_paths({"folder_path": "/nope/does/not/exist"}) == []


# ───────────────────────── the guard's core predicate ─────────────────────────

def test_has_usable_images_ignores_local_only_lots(tmp_path):
    """The whole point: local disk is not 'usable' — the bot isn't on the laptop."""
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert li.has_usable_images({"folder_path": str(tmp_path)}) is False
    assert li.has_usable_images({"image_urls": ["https://cdn/a.jpg"]}) is True


def test_resolve_lot_survives_a_dead_database():
    def boom(sql, params):
        raise RuntimeError("connection refused")

    resolved = li.resolve_lot("31225", fetch_row=boom)
    assert resolved.urls == []
    assert resolved.lot_id == "31225"


def test_resolve_lot_uses_the_injected_reader():
    captured = {}

    def fake(sql, params):
        captured["params"] = params
        return {"lot_id": "28505", "image_urls": ["https://cdn/28505/00.jpg"]}

    assert li.resolve_lot("28505", fetch_row=fake).urls == ["https://cdn/28505/00.jpg"]
    assert captured["params"] == {"lot_id": "28505"}


def test_no_lot_id_is_empty():
    assert li.resolve_lot(None).urls == []


# ────────────────────────── backend identification ──────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://pub-4ac6.r2.dev/31225.jpg?v=abc", "r2"),
    ("https://acct.r2.cloudflarestorage.com/b/31225.jpg", "r2"),
    # Supabase Storage is egress-restricted on this project — a 'supabase'
    # answer means the photo is dead, not merely elsewhere.
    ("https://nihg.supabase.co/storage/v1/object/public/listing-images/28065.png", "supabase"),
    ("https://webassets.lqdt1.com/assets/photos/1/2.jpg", "other"),
    ("", "none"),
    (None, "none"),
])
def test_storage_backend(url, expected):
    assert li.storage_backend(url) == expected


# ─────────────────────────── web-template helpers ───────────────────────────

def test_hero_src_prefers_durable_url():
    row = {"hero_image_url": "https://cdn/9006.jpg", "folder_name": "F", "hero_image": "h.jpg"}
    assert li.hero_src(row) == "https://cdn/9006.jpg"


def test_hero_src_falls_back_to_the_local_route():
    row = {"folder_name": "ATL_399", "hero_image": "IMG_0376.JPG"}
    assert li.hero_src(row) == "/image/ATL_399/IMG_0376.JPG"


def test_gallery_srcs_falls_back_to_local_route(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    row = {"folder_name": "F", "folder_path": str(tmp_path)}
    assert li.gallery_srcs(row) == ["/image/F/a.jpg", "/image/F/b.jpg"]


def test_gallery_srcs_empty_when_nothing_anywhere():
    assert li.gallery_srcs({"folder_name": "F"}) == []

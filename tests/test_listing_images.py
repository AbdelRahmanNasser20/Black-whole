"""BLACKWHOLE-6 — durable listing images storage helper."""
from pathlib import Path

import pytest

from automation import config, listing_images as li


# ───────── key scheme (back-compat with the CRM's existing objects) ─────────

def test_key_base_keeps_real_govdeals_ids():
    assert li.key_base("31225") == "31225"


def test_key_base_folds_unsafe_chars_to_match_existing_folder_keys():
    # Existing row lot_id "folder:Orange_Red_Banquet_Chairs_Cypress_242"
    # was uploaded as "folder_Orange_Red_Banquet_Chairs_Cypress_242.jpg".
    assert li.key_base("folder:Orange_Red_Banquet_Chairs_Cypress_242") == \
        "folder_Orange_Red_Banquet_Chairs_Cypress_242"


def test_key_base_blank_is_none():
    assert li.key_base("") is None
    assert li.key_base(None) is None


def test_hero_path_is_flat_for_backcompat():
    assert li.hero_object_path("31225", ext="png") == "31225.png"


def test_gallery_path_is_namespaced_under_prefix():
    assert li.gallery_object_path("31225", 0, ext="png") == "31225/00.png"
    assert li.gallery_object_path("31225", 3, ext="jpg") == "31225/03.jpg"


def test_public_url_shape():
    url = li.public_url("31225.png", base="https://x.supabase.co", bucket="listing-images")
    assert url == "https://x.supabase.co/storage/v1/object/public/listing-images/31225.png"


def test_guess_ext():
    assert li.guess_ext("a.png") == "png"
    assert li.guess_ext("a.JPEG") == "jpg"
    assert li.guess_ext("noext") == "jpg"
    assert li.guess_ext("", "image/webp") == "webp"


# ───────── configuration gating ─────────

def test_env_config_none_when_unset(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", None)
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", None)
    assert li.env_config() is None
    assert li.is_configured() is False


def test_env_config_present(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", "https://x.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", "k")
    monkeypatch.setattr(config, "LISTING_IMAGES_BUCKET", "listing-images")
    cfg = li.env_config()
    assert cfg == {"base": "https://x.supabase.co", "key": "k", "bucket": "listing-images"}


# ───────── upload_lot_images (no network) ─────────

def test_upload_skipped_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", None)
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", None)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    assert li.upload_lot_images("31225", [f]) is None


def test_upload_skipped_when_no_files(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", "https://x.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", "k")
    assert li.upload_lot_images("31225", []) is None


def test_upload_builds_hero_and_gallery_urls(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", "https://x.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", "k")
    monkeypatch.setattr(config, "LISTING_IMAGES_BUCKET", "listing-images")

    uploaded = []

    def fake_post(client, *, base, key, bucket, path, data, content_type):
        uploaded.append(path)
        return True

    monkeypatch.setattr(li, "_post_object", fake_post)

    paths = []
    for i, ext in enumerate(("png", "jpg")):
        p = tmp_path / f"img_{i}.{ext}"
        p.write_bytes(b"data")
        paths.append(p)

    out = li.upload_lot_images("31225", paths)
    base = "https://x.supabase.co/storage/v1/object/public/listing-images"
    assert out["hero_image_url"] == f"{base}/31225.png"
    assert out["image_urls"] == [f"{base}/31225/00.png", f"{base}/31225/01.jpg"]
    # hero uploaded flat + both gallery objects uploaded under the prefix
    assert "31225.png" in uploaded
    assert "31225/00.png" in uploaded and "31225/01.jpg" in uploaded


def test_upload_hero_falls_back_to_gallery0_when_flat_upload_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SUPABASE_STORAGE_URL", "https://x.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_STORAGE_KEY", "k")
    monkeypatch.setattr(config, "LISTING_IMAGES_BUCKET", "listing-images")

    def fake_post(client, *, base, key, bucket, path, data, content_type):
        return "/" not in path  # flat hero key succeeds? no: fail the flat one

    # Flat hero key has no "/", gallery keys do. Make the FLAT one fail:
    def fake_post2(client, *, base, key, bucket, path, data, content_type):
        return "/" in path

    monkeypatch.setattr(li, "_post_object", fake_post2)
    p = tmp_path / "a.png"
    p.write_bytes(b"d")
    out = li.upload_lot_images("31225", [p])
    base = "https://x.supabase.co/storage/v1/object/public/listing-images"
    assert out["image_urls"] == [f"{base}/31225/00.png"]
    assert out["hero_image_url"] == f"{base}/31225/00.png"

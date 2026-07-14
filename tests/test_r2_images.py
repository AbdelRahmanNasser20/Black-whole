"""Offline tests for the Cloudflare R2 image backend (zero-egress storage).

No network, no boto3 required: the S3 client is injected via monkeypatch, so
these run anywhere. Covers config gating, URL building, the key contract shared
with `listing_images`, and the dispatch that prefers R2 when configured.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import listing_images as li  # noqa: E402
from automation import r2_images as r2  # noqa: E402

_ENV = {
    "R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "ak", "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET": "blackwhole-images", "R2_PUBLIC_BASE": "https://pub-xyz.r2.dev",
}


@pytest.fixture
def r2_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    return _ENV


@pytest.fixture
def no_r2_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


class FakeS3:
    """Records put_object calls instead of hitting the network."""
    def __init__(self): self.puts = []
    def put_object(self, **kw): self.puts.append(kw)


def _png(path: Path, size=(2400, 1800)):
    Image.new("RGB", size, (120, 40, 60)).save(path, format="PNG")
    return path


# --- config gating --------------------------------------------------------

def test_is_configured_true_when_all_vars_present(r2_env):
    assert r2.is_configured()
    cfg = r2.env_config()
    assert cfg["endpoint"] == "https://acct123.r2.cloudflarestorage.com"
    assert cfg["bucket"] == "blackwhole-images"


def test_is_configured_false_when_unset(no_r2_env):
    assert r2.env_config() is None
    assert not r2.is_configured()


def test_is_configured_false_when_partially_set(monkeypatch, no_r2_env):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("R2_BUCKET", "b")
    assert not r2.is_configured()  # missing keys/public base


def test_public_base_trailing_slash_tolerated(monkeypatch, r2_env):
    monkeypatch.setenv("R2_PUBLIC_BASE", "https://pub-xyz.r2.dev/")
    assert r2.env_config()["public_base"] == "https://pub-xyz.r2.dev"


def test_public_url_builds_flat_path():
    assert r2.public_url("31225.jpg", public_base="https://pub-xyz.r2.dev") == \
        "https://pub-xyz.r2.dev/31225.jpg"
    assert r2.public_url("/31225/00.jpg", public_base="https://pub-xyz.r2.dev/") == \
        "https://pub-xyz.r2.dev/31225/00.jpg"


# --- upload: same key contract as the Supabase path -----------------------

def test_upload_uses_shared_key_contract_and_optimizes(tmp_path, monkeypatch, r2_env):
    fake = FakeS3()
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)
    src = _png(tmp_path / "a.png")
    raw_len = src.stat().st_size

    out = r2.upload_lot_images("31225", [src])

    # hero (flat) + gallery (namespaced) — identical to listing_images keys
    assert out["hero_image_url"] == "https://pub-xyz.r2.dev/31225.jpg"
    assert out["image_urls"] == ["https://pub-xyz.r2.dev/31225/00.jpg"]
    keys = sorted(p["Key"] for p in fake.puts)
    assert keys == ["31225.jpg", "31225/00.jpg"]
    # egress guard actually ran: PNG -> smaller JPEG, long cache set
    for p in fake.puts:
        assert p["ContentType"] == "image/jpeg"
        assert p["CacheControl"] == r2.CACHE_CONTROL
        assert len(p["Body"]) < raw_len


def test_upload_returns_none_without_config(tmp_path, no_r2_env):
    assert r2.upload_lot_images("31225", [_png(tmp_path / "a.png")]) is None


def test_upload_returns_none_without_lot_id_or_files(tmp_path, monkeypatch, r2_env):
    monkeypatch.setattr(r2, "client", lambda cfg=None: FakeS3())
    assert r2.upload_lot_images("", [_png(tmp_path / "a.png")]) is None
    assert r2.upload_lot_images("31225", []) is None
    assert r2.upload_lot_images("31225", [tmp_path / "missing.png"]) is None


def test_upload_returns_none_when_every_put_fails(tmp_path, monkeypatch, r2_env):
    class Boom:
        def put_object(self, **kw): raise RuntimeError("r2 down")
    monkeypatch.setattr(r2, "client", lambda cfg=None: Boom())
    assert r2.upload_lot_images("31225", [_png(tmp_path / "a.png")]) is None


# --- dispatch: listing_images prefers R2 when configured -------------------

def test_listing_images_dispatches_to_r2_when_configured(tmp_path, monkeypatch, r2_env):
    monkeypatch.setattr(r2, "client", lambda cfg=None: FakeS3())
    out = li.upload_lot_images("31225", [_png(tmp_path / "a.png")])
    assert out["hero_image_url"].startswith("https://pub-xyz.r2.dev/")


def test_listing_images_falls_back_to_supabase_when_r2_absent(tmp_path, monkeypatch, no_r2_env):
    """No R2 config -> the original Supabase path runs (here: unconfigured -> None)."""
    monkeypatch.setattr(li, "env_config", lambda: None)
    assert li.upload_lot_images("31225", [_png(tmp_path / "a.png")]) is None

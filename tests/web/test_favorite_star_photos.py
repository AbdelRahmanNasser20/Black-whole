"""Starring an auction schedules the clean-photo mirror as a background task.

The mirror must never run inline — it downloads, calls dewatermark.ai and
uploads to R2, which would hold the request open for a minute.
"""
import pytest
from fastapi.testclient import TestClient

from automation import favorite_images, favorites
from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _fav(asset_id="9685/56"):
    return favorites.Favorite(
        asset_id=asset_id, link="https://www.govdeals.com/en/asset/9685/56",
        title="t", quantity=1, end_date_iso=None, end_date_raw=None,
        image_url="https://cdn/raw.jpg", location=None, starred_at=None,
        last_synced_at=None, notes=None, sent_intervals=[])


def _post(monkeypatch, **over):
    monkeypatch.setattr(favorites, "upsert", lambda **kw: _fav())
    seen = []
    monkeypatch.setattr(favorite_images, "mirror_favorite_photos",
                        lambda asset_id, **kw: seen.append(asset_id))
    body = {"link": "https://www.govdeals.com/en/asset/9685/56"}
    body.update(over)
    resp = TestClient(app).post("/api/auctions/favorites", json=body)
    return resp, seen


def test_star_schedules_the_photo_mirror(monkeypatch):
    monkeypatch.delenv("FAVORITE_PHOTOS_ON_STAR", raising=False)
    resp, seen = _post(monkeypatch)
    assert resp.status_code == 200 and resp.json()["asset_id"] == "9685/56"
    assert seen == ["9685/56"], "background task runs after the response"


def test_star_does_not_mirror_when_disabled(monkeypatch):
    monkeypatch.setenv("FAVORITE_PHOTOS_ON_STAR", "0")
    resp, seen = _post(monkeypatch)
    assert resp.status_code == 200 and seen == []

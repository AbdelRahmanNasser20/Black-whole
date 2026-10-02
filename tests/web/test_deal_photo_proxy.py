"""The scraped-photo proxy: session-walled, private bucket only."""
import importlib

from fastapi.testclient import TestClient

from automation.web import auth as auth_svc

KEY = "govdeals/1_2_3/abcdef0123.webp"


def _webapp():
    return importlib.import_module("automation.web.app")


def test_path_is_auth_walled():
    assert auth_svc.path_requires_auth("/api/deal-photos/" + KEY)


def test_blocked_without_a_session(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    auth_svc.reset_caches()
    from deals import archive
    monkeypatch.setattr(archive, "fetch_private_photo",
                        lambda p: (_ for _ in ()).throw(AssertionError("handler reached")))
    try:
        r = TestClient(_webapp().app).get("/api/deal-photos/" + KEY)
        assert r.status_code == 401
    finally:
        auth_svc.reset_caches()


def test_serves_bytes_private_and_noindex(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    from deals import archive
    monkeypatch.setattr(archive, "fetch_private_photo", lambda p: (b"img", "image/webp"))
    r = TestClient(_webapp().app).get("/api/deal-photos/" + KEY)
    assert r.status_code == 200 and r.content == b"img"
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"].startswith("private")
    assert r.headers["x-robots-tag"] == "noindex"


def test_unknown_key_is_404_and_store_down_is_503(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    from deals import archive
    client = TestClient(_webapp().app)
    monkeypatch.setattr(archive, "fetch_private_photo", lambda p: None)
    assert client.get("/api/deal-photos/archive/x.parquet").status_code == 404

    def down(p):
        raise archive.r2_images.PrivateBucketNotConfigured("unset")
    monkeypatch.setattr(archive, "fetch_private_photo", down)
    assert client.get("/api/deal-photos/" + KEY).status_code == 503

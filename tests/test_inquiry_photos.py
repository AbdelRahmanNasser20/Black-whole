"""Optional seller photos on a sell inquiry (private R2 bucket, token-gated)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inquiry_photos as ip
from automation import inventory
from automation.web import rate_limit
from automation.web.app import app

ROOT = Path(__file__).resolve().parents[1]
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client():
    rate_limit.reset()
    return TestClient(app, base_url="https://testserver")


def _inq(**kw):
    row = {"id": 14, "kind": "sell", "name": "Edjim", "created_at": NOW, "message": "chairs"}
    row.update(kw)
    return row


# ───────────────────────────── pure rules ─────────────────────────────

def test_sniff_takes_images_only():
    assert ip.sniff(JPEG) == "jpg"
    assert ip.sniff(PNG) == "png"
    assert ip.sniff(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert ip.sniff(b"\0\0\0\x18ftypheic") == "heic"
    assert ip.sniff(b"%PDF-1.7") is None
    assert ip.sniff(b"<svg></svg>") is None


def test_token_is_bound_to_the_inquiry():
    tok = ip.upload_token(14, NOW)
    assert ip.check_token(_inq(), tok)
    assert not ip.check_token(_inq(id=15), tok)
    assert not ip.check_token(_inq(), "")


def test_validate_accepts_good_photos():
    assert ip.validate(_inq(), ip.upload_token(14, NOW), 0, [JPEG, PNG], now=NOW) == ["jpg", "png"]


@pytest.mark.parametrize("token, existing, files, now, msg", [
    ("bad", 0, [JPEG], NOW, "token"),
    (None, 0, [JPEG], NOW + timedelta(hours=3), "window"),
    (None, 5, [JPEG, JPEG], NOW, "at most"),
    (None, 0, [b"%PDF-1.7"], NOW, "only JPEG"),
    (None, 0, [b""], NOW, "empty"),
    (None, 0, [JPEG + b"0" * ip.MAX_BYTES], NOW, "MB"),
    (None, 0, [], NOW, "no photos"),
])
def test_validate_rejects(token, existing, files, now, msg):
    tok = ip.upload_token(14, NOW) if token is None else token
    with pytest.raises(ip.PhotoRejected, match=msg):
        ip.validate(_inq(), tok, existing, files, now=now)


def test_key_shape_matches_the_proxy_guard():
    key = ip.new_key(14, "jpg")
    assert ip.KEY_RE.match(key)
    assert not ip.KEY_RE.match("deals/1/x.jpg")
    assert not ip.KEY_RE.match("inquiries/14/../secret.jpg")


def test_fetch_refuses_foreign_keys_without_touching_r2():
    assert ip.fetch("raw/2026/archive.json.gz") is None


# ───────────────────────────── storage fallback ─────────────────────────────

def test_photo_keys_read_from_column_and_message_fallback():
    row = {"photo_keys": ["inquiries/1/aa.jpg"],
           "message": "chairs\nPHOTOS: inquiries/1/bb.png inquiries/1/aa.jpg"}
    assert inventory.inquiry_photo_keys(row) == ["inquiries/1/aa.jpg", "inquiries/1/bb.png"]


def test_add_photos_folds_into_message_before_migration(monkeypatch):
    seen = {}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): seen["sql"], seen["params"] = sql, params
        def commit(self): pass
    monkeypatch.setattr(inventory, "connect", lambda: Conn())
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: {"id": i})
    monkeypatch.setattr(inventory, "inquiries_have_photos_column", lambda: False)
    inventory.add_inquiry_photos(14, ["inquiries/14/aa.jpg"])
    assert "message" in seen["sql"]
    assert seen["params"][0] == "\nPHOTOS: inquiries/14/aa.jpg"


# ───────────────────────────── endpoints ─────────────────────────────

def test_sell_contact_returns_a_photo_token(client, monkeypatch):
    monkeypatch.setattr(inventory, "create_inquiry", lambda **kw: _inq())
    r = client.post("/contact", json={"kind": "sell", "name": "Edjim", "phone": "3183669114",
                                      "zip_code": "71101", "message": "chairs"})
    assert r.json()["photo_token"] == ip.upload_token(14, NOW)


def test_buy_contact_has_no_photo_token(client, monkeypatch):
    monkeypatch.setattr(inventory, "create_inquiry", lambda **kw: _inq(kind="buy"))
    r = client.post("/contact", json={"kind": "buy", "name": "A", "phone": "1", "zip_code": "30303"})
    assert "photo_token" not in r.json()


def test_photo_upload_stores_and_records(client, monkeypatch):
    stored = {}
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: _inq(created_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(ip, "store", lambda i, blobs, exts: [f"inquiries/{i}/{n:016x}.{e}" for n, e in enumerate(exts)])
    monkeypatch.setattr(inventory, "add_inquiry_photos", lambda i, keys: stored.update(keys=keys))
    row = inventory.get_inquiry(14)
    tok = ip.upload_token(14, row["created_at"])
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: row)
    r = client.post(f"/contact/14/photos?token={tok}",
                    files=[("photos", ("a.jpg", JPEG, "image/jpeg")), ("photos", ("b.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert stored["keys"][0].endswith(".jpg") and stored["keys"][1].endswith(".png")


def test_photo_upload_bad_token_is_a_400_and_stores_nothing(client, monkeypatch):
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: _inq(created_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(ip, "store", lambda *a: pytest.fail("must not store"))
    r = client.post("/contact/14/photos?token=nope", files=[("photos", ("a.jpg", JPEG, "image/jpeg"))])
    assert r.status_code == 400


def test_photo_upload_refused_on_a_buy_inquiry(client, monkeypatch):
    row = _inq(kind="buy", created_at=datetime.now(timezone.utc))
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: row)
    monkeypatch.setattr(ip, "store", lambda *a: pytest.fail("must not store"))
    tok = ip.upload_token(14, row["created_at"])
    r = client.post(f"/contact/14/photos?token={tok}", files=[("photos", ("a.jpg", JPEG, "image/jpeg"))])
    assert r.status_code == 400


def test_photo_proxy_lives_under_the_auth_walled_api_prefix(client, monkeypatch):
    paths = {r.path for r in app.routes}
    assert "/api/inquiry-photos/{key:path}" in paths
    monkeypatch.setattr(ip, "fetch", lambda k: None)
    r = client.get("/api/inquiry-photos/inquiries/14/0000000000000000.jpg", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403, 404)


# ───────────────────────────── markup ─────────────────────────────

def test_sell_form_has_optional_photos_and_asks_the_price():
    html = (ROOT / "automation/web/templates/sell.html").read_text()
    assert 'name="photos"' in html and 'type="file"' in html
    assert 'name="photos" type="file" accept="image/*" multiple data-max="6">' in html  # not required
    assert "How much are you looking to get" in html


def test_site_js_uploads_photos_after_the_inquiry_is_saved():
    js = (ROOT / "automation/web/static/site/site.js").read_text()
    assert "/photos?token=" in js and "photo_token" in js


def test_admin_card_shows_photos():
    js = (ROOT / "automation/web/static/admin/inquiries.js").read_text()
    assert "/api/inquiry-photos/" in js

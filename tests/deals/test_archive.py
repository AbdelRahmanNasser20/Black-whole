from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from deals.models import Lot
from deals.archive import archive_lot_images, _storage_path

def _lot():
    return Lot(984,6466,2,"t","d","372","Furniture","seating_furniture",
        datetime(2026,7,3,tzinfo=timezone.utc),0,10.0,10.0,"USD",0,False,False,None,False,
        "s","c","st","z",None,None,"https://webassets.lqdt1.com/assets/photos/hero.jpg?cb=1",
        "STA",False,{})

@pytest.fixture
def fullres(monkeypatch):
    monkeypatch.setenv("DEALS_ARCHIVE_IMG_HEIGHT", "0")

def test_storage_path_is_stable_and_namespaced(fullres):
    p = _storage_path(_lot(), 0, "https://x/hero.jpg?cb=1")
    assert p.startswith("govdeals/984_6466_2/")
    assert p.endswith(".jpg")

def test_storage_path_is_idempotent_across_idx(fullres):
    lot = _lot()
    url = "https://webassets.lqdt1.com/assets/photos/x.png?cb=9"
    assert _storage_path(lot, 0, url) == _storage_path(lot, 7, url)   # position must not change the key

def test_storage_path_ignores_cache_buster(fullres):
    lot = _lot()
    a = _storage_path(lot, 0, "https://x/1.jpg?cb=111")
    b = _storage_path(lot, 0, "https://x/1.jpg?cb=999")   # same photo, re-stamped cb
    assert a == b

def test_storage_path_webp_ext_when_resizing(monkeypatch):
    monkeypatch.setenv("DEALS_ARCHIVE_IMG_HEIGHT", "800")
    assert _storage_path(_lot(), 0, "https://x/1.jpg?cb=1").endswith(".webp")

def test_download_appends_resize_params(monkeypatch):
    monkeypatch.setenv("DEALS_ARCHIVE_IMG_HEIGHT", "800")
    from deals import archive
    seen = {}
    class R:
        content = b"x"
        def raise_for_status(self): pass
    monkeypatch.setattr(archive.httpx, "get",
                        lambda url, **kw: seen.update(url=url) or R())
    archive._download("https://x/1.jpg?cb=1")
    assert "h=800" in seen["url"] and "webp=true" in seen["url"]

def test_content_type_from_extension():
    from deals.archive import _content_type
    assert _content_type("govdeals/a/deadbeef.png") == "image/png"
    assert _content_type("govdeals/a/deadbeef.webp") == "image/webp"
    assert _content_type("govdeals/a/deadbeef.jpg") == "image/jpeg"

def test_archive_dedups_hero_and_gallery_and_uploads_each_once(fullres):
    lot = _lot()
    # gallery repeats the hero with a different cache-buster — still one upload
    gallery = [lot.hero_image_url.replace("cb=1", "cb=42"),
               "https://webassets.lqdt1.com/assets/photos/2.jpg?cb=1"]
    uploaded = []
    with patch("deals.archive._download", return_value=b"bytes") as dl, \
         patch("deals.archive._upload", side_effect=lambda path, data: uploaded.append(path) or f"https://store/{path}"):
        meter = {}
        urls = archive_lot_images(lot, gallery, meter)
    assert len(uploaded) == 2                    # hero deduped against gallery
    assert dl.call_count == 2
    assert all(u.startswith("https://store/") for u in urls)
    assert meter == {"bytes": 10, "images": 2}

def test_photo_paths_to_urls():
    from deals.mapping import photo_paths_to_urls
    got = photo_paths_to_urls(["/photos/1980/1980_307_a.jpg?cb=2606", "", None,
                               "https://already.absolute/x.jpg"])
    assert got == ["https://webassets.lqdt1.com/assets/photos/1980/1980_307_a.jpg?cb=2606",
                   "https://already.absolute/x.jpg"]

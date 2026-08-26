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


# ── R2-first upload: the Supabase fallback must never fire when R2 is live ──
# Regression guard for the bug where a transient R2 put failure fell through to
# Supabase. The 402 there is on *serving*, so the write could succeed and stamp
# images_archived=true over a permanently dead URL that is never retried.

R2_ENV = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "ak",
          "R2_SECRET_ACCESS_KEY": "sk", "R2_BUCKET": "listing-images",
          "R2_PUBLIC_BASE": "https://pub-test.r2.dev"}


@pytest.fixture
def r2_on(monkeypatch):
    """R2 fully configured, boto3 client stubbed, module cache cleared."""
    from deals import archive
    monkeypatch.setattr(archive, "_R2", {})
    for k, v in R2_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(archive.r2_images, "client", lambda cfg=None: "s3-stub")
    return archive


@pytest.fixture
def r2_off(monkeypatch):
    from deals import archive
    monkeypatch.setattr(archive, "_R2", {})
    for k in R2_ENV:
        monkeypatch.delenv(k, raising=False)
    return archive


def test_upload_raises_and_never_touches_supabase_when_r2_put_fails(r2_on):
    with patch.object(r2_on.r2_images, "put_object", return_value=False), \
         patch.object(r2_on, "httpx") as fake_httpx:
        with pytest.raises(RuntimeError, match="refusing Supabase fallback"):
            r2_on._upload("govdeals/1_2_3/abc.jpg", b"bytes")
    fake_httpx.post.assert_not_called()          # the whole point of the fix


def test_upload_returns_versioned_r2_url_on_success(r2_on):
    with patch.object(r2_on.r2_images, "put_object", return_value=True):
        url = r2_on._upload("govdeals/1_2_3/abc.jpg", b"bytes")
    assert url.startswith("https://pub-test.r2.dev/govdeals/1_2_3/abc.jpg?v=")
    assert "supabase" not in url


def test_upload_uses_supabase_only_when_r2_unconfigured(r2_off, monkeypatch):
    monkeypatch.setenv("SUPABASE_STORAGE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_STORAGE_KEY", "key")
    with patch.object(r2_off, "httpx") as fake_httpx:
        url = r2_off._upload("govdeals/1_2_3/abc.jpg", b"bytes")
    fake_httpx.post.assert_called_once()
    assert url == ("https://proj.supabase.co/storage/v1/object/public/"
                   "listing-images/govdeals/1_2_3/abc.jpg")


def test_failed_upload_is_isolated_per_lot_and_never_marks_archived(fullres):
    """A raising _upload must be counted and the lot left UNarchived, so
    unarchived_active() retries it next pass. If set_archived_images ran here,
    the lot would be stamped archived over a URL that was never written."""
    from deals.archive import archive_active

    rows = [{"asset_id": 1, "account_id": 2, "auction_id": 3, "hero_image_url": "https://x/a.jpg"},
            {"asset_id": 4, "account_id": 5, "auction_id": 6, "hero_image_url": "https://x/b.jpg"}]

    class FakeAdapter:
        def fetch_gallery(self, asset_id, account_id):
            return []

    with patch("deals.store.unarchived_active", return_value=rows), \
         patch("deals.store.set_archived_images") as marked, \
         patch("deals.archive._download", return_value=b"bytes"), \
         patch("deals.archive._upload", side_effect=RuntimeError("R2 down")):
        meter = archive_active(FakeAdapter(), limit=10, sleep_s=0)

    assert meter["errors"] == 2        # both lots failed, loop did not abort
    assert meter["lots"] == 0
    marked.assert_not_called()         # nothing stamped as archived

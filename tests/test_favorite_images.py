"""Clean (dewatermarked) R2 photos for favorited auctions — no inventory row.

Never calls dewatermark.ai or R2: `clean_and_upload`, `fetch_detail`,
`gallery_urls` and the DB writer are all monkeypatched.
"""
from automation import favorite_images as fi


def test_r2_key():
    import hashlib
    assert fi.r2_key("9685/56") == "fav-" + hashlib.sha256(b"9685/56").hexdigest()[:12]
    assert "9685" not in fi.r2_key("9685/56")
    assert fi.r2_key("ps:123") is None and fi.r2_key("bs:abc") is None and fi.r2_key("") is None


def test_mirror_fetches_cleans_uploads_and_stamps(monkeypatch):
    calls = {}
    monkeypatch.setattr(fi.lot_channels, "fetch_detail", lambda a, b: {"assetId": a, "assetPhotos": ["p"] * 9})
    monkeypatch.setattr(fi.lot_channels, "gallery_urls", lambda d: [f"https://cdn/{i}.jpg" for i in range(9)])

    def fake_clean(key, urls, log=print, *, dewatermark=True, limit=None, strict=False):
        calls["key"], calls["n"], calls["dw"] = key, len(urls), dewatermark
        calls["limit"], calls["strict"] = limit, strict
        return {"hero_image_url": "https://r2/fav-9685-56.jpg", "image_urls": ["https://r2/fav-9685-56/00.jpg"]}

    monkeypatch.setattr(fi.lot_channels, "clean_and_upload", fake_clean)
    monkeypatch.setattr(fi.favorites, "set_clean_images", lambda a, h, u: calls.update(stamped=(a, h, u)))
    out = fi.mirror_favorite_photos("9685/56", log=lambda *a: None, force=True)
    assert calls["key"] == fi.r2_key("9685/56") and calls["n"] == fi.FAVORITE_PHOTO_LIMIT and calls["dw"] is True
    assert calls["strict"] is True, "a watermarked original is never published for a favorite"
    assert calls["stamped"][0] == "9685/56" and out["hero_image_url"].startswith("https://r2/")


def test_mirror_skips_closed_lot(monkeypatch):
    def boom(a, b):
        raise RuntimeError("GovDeals returned nothing")

    monkeypatch.setattr(fi.lot_channels, "fetch_detail", boom)
    monkeypatch.setattr(fi.favorites, "get", lambda a: None)
    monkeypatch.setattr(fi.favorites, "set_clean_images",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not stamp")))
    assert fi.mirror_favorite_photos("1/2", log=lambda *a: None) is None


def test_mirror_reuses_existing_clean_photos_unless_forced(monkeypatch):
    """A second star must not re-spend the dewatermark budget."""
    from automation import favorites as fav_mod
    fav = fav_mod.Favorite(
        asset_id="9685/56", link="x", title="t", quantity=1, end_date_iso=None,
        end_date_raw=None, image_url="raw", location=None, starred_at=None,
        last_synced_at=None, notes=None, sent_intervals=[],
        clean_hero_url="https://r2/fav-abc.jpg", clean_image_urls=["https://r2/fav-abc/00.jpg"])
    monkeypatch.setattr(fi.favorites, "get", lambda a: fav)
    monkeypatch.setattr(fi.lot_channels, "fetch_detail",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not refetch")))
    out = fi.mirror_favorite_photos("9685/56", log=lambda *a: None)
    assert out["hero_image_url"] == "https://r2/fav-abc.jpg"


def test_mirror_returns_none_when_nothing_clean(monkeypatch):
    monkeypatch.setattr(fi.favorites, "get", lambda a: None)
    monkeypatch.setattr(fi.lot_channels, "fetch_detail", lambda a, b: {"assetId": a})
    monkeypatch.setattr(fi.lot_channels, "gallery_urls", lambda d: ["https://cdn/0.jpg"])
    monkeypatch.setattr(fi.lot_channels, "clean_and_upload", lambda *a, **k: None)
    monkeypatch.setattr(fi.favorites, "set_clean_images",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not stamp")))
    assert fi.mirror_favorite_photos("9685/56", log=lambda *a: None) is None


def test_mirror_skips_non_govdeals_favorite(monkeypatch):
    monkeypatch.setattr(fi.lot_channels, "fetch_detail",
                        lambda *a: (_ for _ in ()).throw(AssertionError("GovDeals only")))
    assert fi.mirror_favorite_photos("ps:123", log=lambda *a: None) is None


def test_on_star_enabled_defaults_on_and_respects_env(monkeypatch):
    monkeypatch.delenv("FAVORITE_PHOTOS_ON_STAR", raising=False)
    assert fi.on_star_enabled() is True
    monkeypatch.setenv("FAVORITE_PHOTOS_ON_STAR", "0")
    assert fi.on_star_enabled() is False


def test_mirror_never_uses_raw_image_url():
    """The favorite's own image_url is the watermarked CDN file — never the source of a public photo."""
    src = open(fi.__file__).read()
    assert "image_url" not in src.replace("clean_image_urls", "").replace("hero_image_url", "").replace("image_urls", "")

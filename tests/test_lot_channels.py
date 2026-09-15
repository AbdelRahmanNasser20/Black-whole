"""Pure-logic tests for automation.lot_channels (no DB, no browser, no network)."""
from __future__ import annotations

import json

import pytest

from automation import lot_channels as lc
from automation.catalog_feed import google_category


# ─── URL / ids ───

@pytest.mark.parametrize("url,expect", [
    ("https://www.govdeals.com/en/asset/111/27562", (111, 27562)),
    ("https://www.govdeals.com/asset/420/9312?x=1", (420, 9312)),
    ("govdeals.com/en/asset/53677/357", (53677, 357)),
])
def test_parse_govdeals_url(url, expect):
    assert lc.parse_govdeals_url(url) == expect


def test_parse_govdeals_url_rejects_non_asset():
    with pytest.raises(ValueError):
        lc.parse_govdeals_url("https://www.govdeals.com/en/search?q=chairs")


def test_new_lot_id_is_url_order_and_part_suffixed():
    assert lc.new_lot_id(420, 9312) == "gd-420-9312"
    assert lc.new_lot_id(53677, 357, lc.Part(kind="tables")) == "gd-53677-357-tables"


# ─── split spec ───

def test_parse_split_none_is_one_anonymous_part():
    parts = lc.parse_split(None)
    assert len(parts) == 1 and parts[0].kind == "" and parts[0].suffix == ""


def test_parse_split_two_parts_with_photo_indexes():
    parts = lc.parse_split("chairs:60:20:2-3,tables:14:65:0+1")
    assert [p.kind for p in parts] == ["chairs", "tables"]
    assert parts[0].quantity == 60 and parts[0].price == 20 and parts[0].photos == [2, 3]
    assert parts[1].quantity == 14 and parts[1].price == 65 and parts[1].photos == [0, 1]


def test_parse_split_rejects_missing_fields_and_duplicates():
    with pytest.raises(ValueError):
        lc.parse_split("chairs:60")
    with pytest.raises(ValueError):
        lc.parse_split("chairs:60:20,chairs:10:5")


# ─── copy ───

def test_clean_html_unescapes_and_breaks_paragraphs():
    raw = "<p>Lot of 250 chairs &amp; carts</p><p>Buyer removes</p>"
    assert lc.clean_html(raw) == "Lot of 250 chairs & carts\n\nBuyer removes"


@pytest.mark.parametrize("short,long,expect", [
    ("Lot of 250 padded banquet chairs", "", 250),
    ("LOT: (199) BANQUET CHAIRS", "", 199),
    ("Lot of approximately 362 wood chairs", "", 362),
    ("120 Banquet Stacking Chairs - B6", "", 120),
    ("Tables & Chairs (225244 TM)", "-14 each: Round Tables -60 each: Non rolling chairs", None),
])
def test_quantity_from_detail(short, long, expect):
    assert lc.quantity_from_detail({"assetShortDesc": short, "assetLongDesc": long}) == expect


def test_default_title_uses_state_code_and_kind():
    assert lc.default_title(199, lc.Part(), "Banquet Chairs", "Las Vegas", "Nevada") \
        == "199 Banquet Chairs (Las Vegas, NV)"
    assert lc.default_title(14, lc.Part(kind="tables"), None, "Augusta", "GA") == "14 Tables (Augusta, GA)"


def test_unit_word():
    assert lc.unit_word({"chair_type": "Round Folding Tables", "title": "x"}) == "table"
    assert lc.unit_word({"chair_type": None, "title": "Brown Banquet Chairs"}) == "chair"
    assert lc.unit_word("tables") == "table"


def test_fb_description_shape_matches_relist_plan():
    d = lc.fb_description(blurb="Tan chairs.", city="Las Vegas", state="NV", quantity=199,
                          lot_id="gd-420-9312", unit="chair", profile_id="567516776")
    assert d.startswith("Tan chairs.\n\n📍 Location: Las Vegas, NV (local pickup")
    assert "📦 Quantity available: 199" in d
    assert "More photos and full details: https://black-whole.com/listings/gd-420-9312" in d
    assert "All our chair lots: https://www.facebook.com/marketplace/profile/567516776/" in d
    assert d.endswith("SKU gd-420-9312")


def test_plan_entry_for_tables_uses_table_category_and_tags():
    e = lc.plan_entry(lot_id="gd-53677-357-tables", title="14 Round Tables", price=65.0,
                      city="Augusta", state="GA", zip_code="30901", quantity=14, blurb="Round tables.",
                      photo_urls=["https://r2/a.jpg", "https://r2/b.jpg"], unit="table", profile_id="1")
    assert e["category"].endswith("Tables")
    assert e["cover_url"] == "https://r2/a.jpg" and e["photo_urls"][0] == e["cover_url"]
    assert "round tables" in e["tags"]
    assert e["price"] == 65 and isinstance(e["price"], int)
    assert "All our lots:" in e["description"]


# ─── plan JSON round-trip ───

def test_upsert_plan_entry_keeps_live_url(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"account": {"profile_id": "9"}, "listings": [
        {"sku": "a", "title": "old", "fb_listing_url": "https://www.facebook.com/marketplace/item/1/"},
    ]}))
    lc.upsert_plan_entry({"sku": "a", "title": "new", "fb_listing_url": None}, path)
    lc.upsert_plan_entry({"sku": "b", "title": "b"}, path)
    plan = json.loads(path.read_text())
    a, b = plan["listings"]
    assert a["title"] == "new" and a["fb_listing_url"].endswith("/item/1/")
    assert b["order"] == 2
    assert lc.plan_entry_for("b", path)["title"] == "b"
    assert lc.profile_id(path) == "9"


# ─── remove semantics ───

@pytest.mark.parametrize("status,expect", [
    ("owned", "sold_out"), ("won_pickup", "sold_out"), ("listed", "sold_out"),
    ("active_bid", "lost_sold_out"), ("lost", "lost_sold_out"),
])
def test_sold_status_for(status, expect):
    assert lc.sold_status_for({"status": status}) == expect


# ─── catalog ───

def test_google_category_by_row_words():
    assert google_category({"chair_type": "Banquet Chairs", "title": "x"}) == "Furniture > Chairs"
    assert google_category({"chair_type": None, "title": "14 Round Folding Tables"}) == "Furniture > Tables"


# ─── clean_and_upload (no network: httpx + dewatermark + R2 are faked) ───

class _FakeResp:
    def __init__(self, content=b"jpegbytes"):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _FakeResp()


def _fake_dewatermark(monkeypatch, clean_indexes, *, order=None):
    """Stand in for automation.dewatermark.dewatermark, matching its real contract:
    every input is archived into `_originals/` up front, and ONLY the cleaned
    root-folder outputs come back (a failure is a missing path, never a
    watermarked one). `order` re-orders the returned list the way the three
    cache layers do."""
    from automation import dewatermark as dw

    async def fake(_run, files, folder, lot_label=None):
        originals = folder / "_originals"
        originals.mkdir(parents=True, exist_ok=True)
        out = []
        for i, f in enumerate(files):
            (originals / f.name).write_bytes(f.read_bytes())
            f.unlink()
            if i in clean_indexes:
                cleaned = folder / f"{f.stem}.png"
                cleaned.write_bytes(b"cleanbytes")
                out.append(cleaned)
        return [out[i] for i in order] if order else out

    monkeypatch.setattr(dw, "dewatermark", fake)


@pytest.fixture
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(lc.config, "SCRATCH_DIR", str(tmp_path))
    monkeypatch.setattr("httpx.Client", _FakeClient)
    return tmp_path


@pytest.fixture
def _uploads(monkeypatch):
    """Records what reached R2. Nothing watermarked may ever appear here."""
    seen = {}

    def fake_upload(key, files):
        seen["key"] = key
        seen["names"] = [f.name for f in files]
        assert all(f.parent.name != "_originals" for f in files), "watermarked original uploaded"
        return {"hero_image_url": "h", "image_urls": ["a"]}

    monkeypatch.setattr(lc.listing_images, "upload_lot_images", fake_upload)
    return seen


@pytest.mark.parametrize("strict", [True, False])
def test_clean_and_upload_publishes_only_cleaned_photos(_scratch, _uploads, monkeypatch, strict):
    """Partial API failure: the photo that failed is simply absent — never shipped."""
    _fake_dewatermark(monkeypatch, clean_indexes={0})
    lines = []
    out = lc.clean_and_upload("fav-abc", ["https://cdn/0.jpg", "https://cdn/1.jpg"],
                              log=lines.append, strict=strict)
    assert out == {"hero_image_url": "h", "image_urls": ["a"]}
    assert _uploads["key"] == "fav-abc" and _uploads["names"] == ["00.png"]
    assert any("1/2" in ln and "dropped" in ln for ln in lines), lines


@pytest.mark.parametrize("strict", [True, False])
def test_clean_and_upload_uploads_nothing_when_every_photo_fails(_scratch, monkeypatch, strict):
    _fake_dewatermark(monkeypatch, clean_indexes=set())
    monkeypatch.setattr(lc.listing_images, "upload_lot_images",
                        lambda key, files: pytest.fail("must not upload watermarked files"))
    assert lc.clean_and_upload("fav-abc", ["https://cdn/0.jpg", "https://cdn/1.jpg"],
                               log=lambda *a: None, strict=strict) is None


def test_clean_and_upload_restores_gallery_order_for_the_hero(_scratch, _uploads, monkeypatch):
    """`dewatermark()` returns cache-layer order; file 0 becomes the hero, so
    the first gallery photo must be first again."""
    _fake_dewatermark(monkeypatch, clean_indexes={0, 1, 2}, order=[2, 0, 1])
    lc.clean_and_upload("k", [f"https://cdn/{i}.jpg" for i in range(3)], log=lambda *a: None)
    assert _uploads["names"] == ["00.png", "01.png", "02.png"]


def test_clean_and_upload_strict_refuses_to_skip_dewatermarking(_scratch):
    with pytest.raises(ValueError, match="strict"):
        lc.clean_and_upload("k", ["https://cdn/0.jpg"], log=lambda *a: None,
                            dewatermark=False, strict=True)


def test_clean_and_upload_honours_limit_and_does_not_touch_inventory(_scratch, _uploads, monkeypatch):
    _fake_dewatermark(monkeypatch, clean_indexes=set(range(6)))
    monkeypatch.setattr(lc.inventory, "set_images",
                        lambda *a: pytest.fail("clean_and_upload must not stamp inventory"))
    lc.clean_and_upload("k", [f"https://cdn/{i}.jpg" for i in range(9)], log=lambda *a: None, limit=6)
    assert len(_uploads["names"]) == 6


def test_mirror_photos_stamps_inventory(_scratch, _uploads, monkeypatch):
    _fake_dewatermark(monkeypatch, clean_indexes={0})
    stamped = {}
    monkeypatch.setattr(lc.inventory, "set_images",
                        lambda lot_id, hero, urls: stamped.update(lot_id=lot_id, hero=hero, urls=urls))
    out = lc.mirror_photos("gd-1-2", ["https://cdn/0.jpg"], log=lambda *a: None)
    assert out["hero_image_url"] == "h"
    assert stamped == {"lot_id": "gd-1-2", "hero": "h", "urls": ["a"]}

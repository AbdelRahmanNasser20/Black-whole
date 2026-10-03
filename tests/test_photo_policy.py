"""The watermark rule (automation/photo_policy.py) and everything routed through it.

Rule (operator, 2026-10-03): the tiled watermark appears only on black-whole.com,
only for a lot still `active_bid`. Every other channel, and every lot we own,
won or sold, gets the clean (dewatermarked, unwatermarked) photo. The flip is
automatic because the variant is derived from `status` at read time.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import inventory, lot_images, photo_policy, photo_sync  # noqa: E402
from automation import listing_images as li  # noqa: E402
from automation import r2_images as r2  # noqa: E402

BASE = "https://pub-xyz.r2.dev"
_R2 = {
    "R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "ak", "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET": "blackwhole-images", "R2_PUBLIC_BASE": BASE,
}
STATUSES = inventory.ALL_STATUSES
CHANNELS = photo_policy.CHANNELS


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IMAGE_DISGUISE_SALT", "test-salt")
    for var in ("IMAGE_DISGUISE", "IMAGE_DISGUISE_NO_MIRROR_LOTS", "IMAGE_DISGUISE_WATERMARK"):
        monkeypatch.delenv(var, raising=False)


def _photo(shade=200) -> bytes:
    img = Image.new("RGB", (800, 600), (shade, 220, 210))
    for x in range(50, 300, 40):
        img.paste((60, 40, 30), (x, 300, x + 25, 520))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ─── the matrix ───

@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("channel", CHANNELS + (None, ""))
def test_watermark_only_on_the_site_for_active_bid(status, channel):
    expected = channel == "site" and status == "active_bid"
    assert photo_policy.wants_watermark(status, channel) is expected


def test_unknown_or_missing_status_is_clean():
    assert not photo_policy.wants_watermark(None, "site")
    assert not photo_policy.wants_watermark("", "site")
    assert photo_policy.wants_watermark(" active_bid ", "site")


def test_only_active_bid_keeps_a_watermark_variant():
    assert [s for s in STATUSES if photo_policy.keeps_watermark_variant(s)] == ["active_bid"]


# ─── URL variants ───

CLEAN = f"{BASE}/p/abc/0f0f.c.jpg?v=1"
MARKED = f"{BASE}/p/abc/0f0f.jpg?v=1"
HERO_CLEAN = f"{BASE}/p/abc/h.c.jpg?v=2"
HERO_MARKED = f"{BASE}/p/abc/h.jpg?v=2"
LEGACY = f"{BASE}/31225/00.jpg?v=3"


def test_url_variants_round_trip():
    assert photo_policy.clean_url(MARKED) == CLEAN
    assert photo_policy.clean_url(CLEAN) == CLEAN
    assert photo_policy.watermarked_url(CLEAN) == MARKED
    assert photo_policy.watermarked_url(MARKED) == MARKED
    assert photo_policy.clean_url(HERO_MARKED) == HERO_CLEAN
    assert photo_policy.is_clean(CLEAN) and not photo_policy.is_clean(MARKED)


def test_legacy_and_foreign_urls_are_never_rewritten():
    for url in (LEGACY, "https://example.com/p.jpg", None, ""):
        assert photo_policy.clean_url(url) == url
        assert photo_policy.watermarked_url(url) == url
    assert photo_policy.is_clean(LEGACY)


def test_object_path_variants():
    assert photo_policy.clean_path("p/abc/h.jpg") == "p/abc/h.c.jpg"
    assert photo_policy.watermarked_path("p/abc/h.c.jpg") == "p/abc/h.jpg"
    assert photo_policy.clean_path("p/abc/h.c.jpg") == "p/abc/h.c.jpg"


# ─── the resolver ───

def _row(status, *, hero=HERO_CLEAN, urls=(CLEAN,)):
    return {"lot_id": "gd-1-2", "status": status, "hero_image_url": hero, "image_urls": list(urls)}


@pytest.mark.parametrize("stored", ["clean", "marked"])
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("channel", CHANNELS + (None,))
def test_resolver_serves_the_policy_variant(stored, status, channel):
    row = _row(status, hero=HERO_CLEAN if stored == "clean" else HERO_MARKED,
               urls=[CLEAN if stored == "clean" else MARKED])
    got = lot_images.resolve(row, channel)
    if photo_policy.wants_watermark(status, channel):
        assert got.urls == [MARKED] and got.hero == HERO_MARKED
    else:
        assert got.urls == [CLEAN] and got.hero == HERO_CLEAN


def test_resolver_default_is_clean_even_for_a_bid_lot():
    """A caller that forgets its channel must never be handed a watermark."""
    got = lot_images.resolve(_row("active_bid"))
    assert got.urls == [CLEAN] and got.hero == HERO_CLEAN
    assert lot_images.image_urls(_row("active_bid")) == [CLEAN]


def test_site_helpers_default_to_the_site_channel():
    assert lot_images.hero_src(_row("active_bid")) == HERO_MARKED
    assert lot_images.gallery_srcs(_row("active_bid")) == [MARKED]
    assert lot_images.hero_src(_row("owned")) == HERO_CLEAN


def test_flip_is_automatic_when_a_bid_lot_becomes_ours():
    row = _row("active_bid")
    assert lot_images.hero_src(row) == HERO_MARKED
    row["status"] = "won_pickup"  # nothing else changes: no re-upload, no URL edit
    assert lot_images.hero_src(row) == HERO_CLEAN
    assert lot_images.gallery_srcs(row) == [CLEAN]


@pytest.mark.parametrize("channel", [c for c in CHANNELS if c != "site"])
def test_channel_photo_urls_never_watermarked(channel):
    assert lot_images.channel_photo_urls(_row("active_bid", urls=[MARKED]), channel) == [CLEAN]


def test_row_columns_carry_status():
    assert "status" in lot_images.ROW_COLUMNS


# ─── feeds + writers ───

def test_catalog_and_google_feeds_ship_clean_for_a_bid_lot():
    from automation import catalog_feed, google_feed
    row = {"lot_id": "gd-1-2", "title": "300 banquet chairs", "price_per_chair": 12,
           "quantity_remaining": 300, "status": "active_bid",
           "hero_image_url": HERO_MARKED, "image_urls": [MARKED, f"{BASE}/p/abc/1e1e.jpg?v=1"]}
    assert catalog_feed._image_link(row) == HERO_CLEAN
    fr = google_feed.feed_row(row, base_url="https://black-whole.com")
    assert fr["image_link"] == HERO_CLEAN
    assert all(photo_policy.is_clean(u) for u in fr["additional_image_link"].split(","))


def test_fb_fetch_photos_downloads_the_clean_variant(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    pytest.importorskip("playwright")
    import post_fb_listing as pfl
    seen = []

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        return _Resp(b"x" * 5000)

    monkeypatch.setattr(pfl.urllib.request, "urlopen", fake_urlopen)
    pfl.fetch_photos([MARKED, HERO_MARKED], tmp_path)
    assert seen == [CLEAN, HERO_CLEAN]


class FakeS3:
    def __init__(self, existing=()):
        self.puts, self.existing = [], set(existing)

    def put_object(self, **kw):
        self.puts.append(kw)
        self.existing.add(kw["Key"])

    def head_object(self, Bucket, Key):
        if Key not in self.existing:
            raise KeyError(Key)
        return {}


def _r2(monkeypatch, fake):
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(r2, "client", lambda cfg=None: fake)


@pytest.mark.parametrize("status", STATUSES)
def test_upload_writes_watermark_twin_only_for_active_bid(tmp_path, monkeypatch, status):
    fake = FakeS3()
    _r2(monkeypatch, fake)
    src = tmp_path / "a.jpg"
    src.write_bytes(_photo())
    out = r2.upload_lot_images("gd-1-2", [src], status=status)
    keys = {p["Key"] for p in fake.puts}
    clean_keys = {k for k in keys if k.endswith(".c.jpg")}
    marked_keys = keys - clean_keys
    assert len(clean_keys) == 2  # gallery + hero, always
    assert len(marked_keys) == (2 if status == "active_bid" else 0)
    # what goes to the DB is always clean
    assert all(photo_policy.is_clean(u) for u in [out["hero_image_url"], *out["image_urls"]])
    if marked_keys:
        bodies = {p["Key"]: p["Body"] for p in fake.puts}
        for k in marked_keys:
            assert bodies[k] != bodies[photo_policy.clean_path(k)]


def test_public_copies_are_the_clean_bytes(tmp_path, monkeypatch):
    fake = FakeS3()
    _r2(monkeypatch, fake)
    src = tmp_path / "a.jpg"
    src.write_bytes(_photo())
    r2.upload_lot_images("gd-1-2", [src], status="active_bid")
    copies = li.public_copies("gd-1-2", [src], out_dir=tmp_path / "pub")
    clean_gallery = next(p["Body"] for p in fake.puts
                         if p["Key"].endswith(".c.jpg") and not p["Key"].endswith("/h.c.jpg"))
    assert [c.read_bytes() for c in copies] == [clean_gallery]


# ─── photo_sync: plan + apply ───

def _stored_lot(tmp_path, monkeypatch, *, status, existing_clean=False, n=2, folder=False):
    """An R2 lot as the 2026-09 disguise backfill left it: watermarked URLs in the DB."""
    srcs = [_photo(150 + 30 * i) for i in range(n)]
    gal = [li.opaque_gallery_path("gd-1-2", s) for s in srcs]
    hero = li.opaque_hero_path("gd-1-2")
    existing = set(gal) | {hero, photo_policy.clean_path(hero)}
    if existing_clean:
        existing |= {photo_policy.clean_path(k) for k in gal}
    row = {"lot_id": "gd-1-2", "status": status,
           "hero_image_url": f"{BASE}/{hero}?v=9", "image_urls": [f"{BASE}/{k}?v=9" for k in gal]}
    if folder:
        d = tmp_path / "lot"
        d.mkdir()
        for i, s in enumerate(srcs):
            (d / f"{i:02d}.jpg").write_bytes(s)
        row["folder_path"] = str(d)
    return row, srcs, gal, hero, FakeS3(existing)


def test_plan_finds_folder_sources_and_apply_rewrites_to_clean(tmp_path, monkeypatch):
    row, srcs, gal, hero, fake = _stored_lot(tmp_path, monkeypatch, status="lost", folder=True)
    _r2(monkeypatch, fake)
    plan = photo_sync.plan_inventory_row(row, exists=photo_sync.r2_exists_fn(fake, "b"),
                                         public_base=BASE, legacy={})
    c = plan.counts()
    assert c["photos"] == 3 and c["clean_missing"] == 2 and c["clean_missing_no_source"] == 0
    assert c["db_rewrite"] == 3 and not plan.reupload_from_folder  # `lost` is not ours
    new = photo_sync.apply_plan(plan, s3=fake, cfg=r2.env_config())
    assert {p["Key"] for p in fake.puts} == {photo_policy.clean_path(k) for k in gal}
    assert new[0] == f"{BASE}/{photo_policy.clean_path(hero)}?v=9"
    assert new[1] == [f"{BASE}/{photo_policy.clean_path(k)}?v=9" for k in gal]


def test_plan_uses_legacy_log_sources(tmp_path, monkeypatch):
    row, srcs, gal, hero, fake = _stored_lot(tmp_path, monkeypatch, status="sold_out")
    log = tmp_path / "disguise-backfill-20261002T000000Z.jsonl"
    import json
    log.write_text(json.dumps({"table": "inventory", "id": "gd-1-2",
                               "old": {"hero": f"{BASE}/x.jpg", "urls": [f"{BASE}/x/00.jpg", f"{BASE}/x/01.jpg"]},
                               "new": {"hero": row["hero_image_url"], "urls": row["image_urls"]}}) + "\n")
    legacy = photo_sync.legacy_sources(tmp_path)
    plan = photo_sync.plan_inventory_row(row, exists=photo_sync.r2_exists_fn(fake, "b"),
                                         public_base=BASE, legacy=legacy)
    assert [p.source for p in plan.photos if p.role == "gallery"] == [
        f"legacy:{BASE}/x/00.jpg", f"legacy:{BASE}/x/01.jpg"]


def test_active_bid_plan_needs_both_variants(tmp_path, monkeypatch):
    row, srcs, gal, hero, fake = _stored_lot(tmp_path, monkeypatch, status="active_bid",
                                             existing_clean=True)
    fake.existing.discard(gal[0])  # watermarked twin of photo 0 is gone
    plan = photo_sync.plan_inventory_row(row, exists=photo_sync.r2_exists_fn(fake, "b"),
                                         public_base=BASE, legacy={})
    c = plan.counts()
    assert c["clean_missing"] == 0 and c["wm_missing"] == 1 and c["wm_missing_no_source"] == 1


def test_owned_lot_with_its_own_folder_photos_reuploads_from_the_folder(tmp_path, monkeypatch):
    row, srcs, gal, hero, fake = _stored_lot(tmp_path, monkeypatch, status="won_pickup",
                                             existing_clean=True, folder=True)
    (Path(row["folder_path"]) / "zz_our_own.jpg").write_bytes(_photo(90))
    _r2(monkeypatch, fake)
    plan = photo_sync.plan_inventory_row(row, exists=photo_sync.r2_exists_fn(fake, "b"),
                                         public_base=BASE, legacy={})
    assert plan.reupload_from_folder
    new = photo_sync.apply_plan(plan, s3=fake, cfg=r2.env_config())
    assert len(new[1]) == 3 and all(photo_policy.is_clean(u) for u in [new[0], *new[1]])
    assert not any(k["Key"].endswith(".jpg") and not k["Key"].endswith(".c.jpg")
                   for k in fake.puts), "an owned lot never gets a new watermarked object"


def test_apply_never_drops_without_a_source_unless_asked(tmp_path, monkeypatch):
    row, srcs, gal, hero, fake = _stored_lot(tmp_path, monkeypatch, status="owned")
    plan = photo_sync.plan_inventory_row(row, exists=photo_sync.r2_exists_fn(fake, "b"),
                                         public_base=BASE, legacy={})
    kept = photo_sync.apply_plan(plan, s3=fake, cfg=dict(bucket="b"))
    assert kept[1] == row["image_urls"]  # gallery kept as stored; hero rewritten (its twin exists)
    dropped = photo_sync.apply_plan(plan, s3=fake, cfg=dict(bucket="b"), drop_missing=True)
    assert dropped[1] == []


# ─── the status hook ───

@pytest.mark.parametrize("prior,new,fires", [
    ("active_bid", "won_pickup", True),
    ("active_bid", "lost_sold_out", True),
    ("lost", "active_bid", True),
    ("owned", "sold_out", False),
    ("active_bid", "active_bid", False),
])
def test_status_hook_fires_only_across_active_bid(monkeypatch, prior, new, fires):
    monkeypatch.setenv("PHOTO_POLICY_AUTOSYNC", "1")
    for k, v in _R2.items():
        monkeypatch.setenv(k, v)
    ran = []
    monkeypatch.setattr(photo_sync, "sync_lot", lambda lot_id, **k: ran.append(lot_id))
    assert photo_sync.on_status_change("gd-1-2", prior, new, background=False) is fires
    assert ran == (["gd-1-2"] if fires else [])


def test_status_hook_is_off_in_tests_by_default(monkeypatch):
    ran = []
    monkeypatch.setattr(photo_sync, "sync_lot", lambda lot_id, **k: ran.append(lot_id))
    assert photo_sync.on_status_change("gd-1-2", "active_bid", "owned", background=False) is False


def test_set_fields_status_change_calls_the_hook(monkeypatch):
    calls = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=()): return self
        def commit(self): pass

    rows = iter([{"lot_id": "gd-1-2", "status": "active_bid"},
                 {"lot_id": "gd-1-2", "status": "won_pickup"}])
    monkeypatch.setattr(inventory, "connect", lambda: _Conn())
    monkeypatch.setattr(inventory, "get", lambda lot_id: next(rows))
    monkeypatch.setattr(photo_sync, "on_status_change",
                        lambda lot_id, prior, new, **k: calls.append((lot_id, prior, new)))
    inventory.set_fields("gd-1-2", status="won_pickup")
    assert calls == [("gd-1-2", "active_bid", "won_pickup")]


def test_set_fields_without_status_never_calls_the_hook(monkeypatch):
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=()): return self
        def commit(self): pass

    monkeypatch.setattr(inventory, "connect", lambda: _Conn())
    monkeypatch.setattr(inventory, "get", lambda lot_id: {"lot_id": lot_id, "status": "active_bid"})
    monkeypatch.setattr(photo_sync, "on_status_change",
                        lambda *a, **k: pytest.fail("no status change"))
    inventory.set_fields("gd-1-2", title="x")

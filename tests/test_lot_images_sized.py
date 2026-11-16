"""SITE-SPEED: small photo copies (`lot_images.sized` / `srcset`) and the
variant maker (`listing_images.web_variants`, `r2_images.put_variants`)."""
import io

import pytest
from PIL import Image

from automation import listing_images as li
from automation import lot_images, r2_images

P = "https://pub-abc.r2.dev/p/0123abcd/9f8e7d6c5b4a.jpg"


@pytest.fixture(autouse=True)
def _variants_on(monkeypatch):
    monkeypatch.delenv("LOT_IMAGE_VARIANTS", raising=False)


def test_sized_returns_the_variant_and_keeps_the_version():
    assert lot_images.sized(P + "?v=1a2b3c4d", 320) == \
        "https://pub-abc.r2.dev/p/0123abcd/9f8e7d6c5b4a.w320.jpg?v=1a2b3c4d"
    assert lot_images.sized(P, 800) == "https://pub-abc.r2.dev/p/0123abcd/9f8e7d6c5b4a.w800.jpg"
    assert lot_images.sized("https://pub-abc.r2.dev/p/0123abcd/h.jpg?v=9", 800) == \
        "https://pub-abc.r2.dev/p/0123abcd/h.w800.jpg?v=9"


@pytest.mark.parametrize("url", [
    None, "",
    "/image/Lot 31225/01.jpg",                               # local disk route
    "/static/site/hero/banquet-room.jpg",                    # stock photo
    "https://pub-abc.r2.dev/gd-239-31465/00.jpg",            # legacy lot-id key: no copies
    "https://pub-abc.r2.dev/p/0123abcd/h.c.jpg",             # catalog twin
    "https://pub-abc.r2.dev/p/0123abcd/h.w320.jpg",          # already a copy
    "https://pub-abc.r2.dev/p/0123abcd/a.png",
])
def test_sized_falls_back_to_the_input_when_there_is_no_copy(url):
    assert lot_images.sized(url, 320) == url
    assert lot_images.srcset(url) == ""


def test_sized_ignores_unknown_widths_and_the_kill_switch(monkeypatch):
    assert lot_images.sized(P, 500) == P
    monkeypatch.setenv("LOT_IMAGE_VARIANTS", "0")
    assert lot_images.sized(P, 320) == P
    assert lot_images.srcset(P) == ""


def test_srcset_lists_every_copy():
    assert lot_images.srcset(P + "?v=7") == (
        "https://pub-abc.r2.dev/p/0123abcd/9f8e7d6c5b4a.w320.jpg?v=7 320w, "
        "https://pub-abc.r2.dev/p/0123abcd/9f8e7d6c5b4a.w800.jpg?v=7 800w")


def test_origin():
    assert lot_images.origin(P + "?v=1") == "https://pub-abc.r2.dev"
    assert lot_images.origin("/image/x.jpg") is None
    assert lot_images.origin(None) is None


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 40, 10)).save(buf, format="JPEG")
    return buf.getvalue()


def test_web_variants_downscale_the_public_bytes_without_upscaling():
    out = dict(li.web_variants(_jpeg(1560, 1092)))
    assert sorted(out) == [320, 800]
    assert Image.open(io.BytesIO(out[320])).size == (320, 224)
    assert Image.open(io.BytesIO(out[800])).size == (800, 560)
    small = dict(li.web_variants(_jpeg(600, 400)))
    assert Image.open(io.BytesIO(small[800])).size == (600, 400)   # never upscaled
    assert li.web_variants(b"not an image") == []


def test_put_variants_writes_next_to_a_disguised_key_only(monkeypatch):
    calls = []
    monkeypatch.setattr(r2_images, "put_object",
                        lambda s3, **kw: calls.append(kw["path"]) or True)
    data = _jpeg(1000, 700)
    assert r2_images.put_variants(None, bucket="b", path="p/tok/h.jpg", data=data) == 2
    assert calls == ["p/tok/h.w320.jpg", "p/tok/h.w800.jpg"]
    calls.clear()
    assert r2_images.put_variants(None, bucket="b", path="gd-239-31465/00.jpg", data=data) == 0
    assert calls == []

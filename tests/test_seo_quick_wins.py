"""SEO quick wins (2026-10-07, from the on-page SEO audit): trimmed titles,
BreadcrumbList, Organization NAP, HTML 404 for browsers, noindex on orphan
lots, canonical on the `-sold` twin, versioned assets, image alt/dimensions.

DB-free like tests/test_seo.py: inventory is monkeypatched.
"""

import json
from datetime import datetime, timezone

import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import readcache

web_app = importlib.import_module("automation.web.app")


ROW = {
    "lot_id": "10340",
    "title": "Burgundy Banquet Chairs",
    "description": "Stackable padded banquet chairs from a conference center.",
    "city": "Athens",
    "state": "GA",
    "zip_code": "30601",
    "chair_type": "banquet",
    "quantity_remaining": 100,
    "quantity_original": 120,
    "price_per_chair": 12.0,
    "status": "listed",
    "hero_image_url": "https://cdn.example.com/10340.jpg",
    "image_urls": ["https://cdn.example.com/10340/01.jpg"],
    "folder_name": None,
    "hero_image": None,
    "subtitle": None,
    "dimensions": None,
    "facebook_url": None,
    "ebay_url": None,
    "updated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
}


@pytest.fixture
def client(monkeypatch):
    readcache.invalidate_all()
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [dict(ROW)])
    monkeypatch.setattr(web_app.inventory, "list_sold_showcase", lambda limit=None: [])
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(ROW) if lot_id == "10340" else None)
    monkeypatch.setattr(web_app.inventory, "stats",
                        lambda: {"lots": 1, "chairs": 100, "cities": 1, "moved": 0})
    monkeypatch.setattr(web_app.public_map, "nearby", lambda lot_id, **k: {"origin": None, "items": []})
    monkeypatch.setattr(web_app, "_reserve_enabled", lambda: False)
    return TestClient(web_app.app)


def _jsonld_blocks(html: str) -> list[dict]:
    out = []
    marker = '<script type="application/ld+json">'
    pos = 0
    while True:
        start = html.find(marker, pos)
        if start < 0:
            return out
        start += len(marker)
        end = html.index("</script>", start)
        out.append(json.loads(html[start:end].replace("<\\/", "</")))
        pos = end


def _title(html: str) -> str:
    return html.split("<title>")[1].split("</title>")[0]


def _description(html: str) -> str:
    return html.split('<meta name="description" content="')[1].split('"')[0]


def test_detail_title_is_trimmed_to_the_serp_budget(client, monkeypatch):
    long_row = dict(
        ROW, lot_id="gd-56-9685", quantity_remaining=2500,
        title="~2,500 Wire Frame Stacking Chairs — Chrome Frame, Dark Plum Pad, Linkable (Pittsburgh, PA)",
        city="Pittsburgh", state="PA", description="word " * 120,
    )
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: long_row)
    r = client.get("/listings/gd-56-9685")
    title = _title(r.text)
    assert len(title) <= web_app.SEO_TITLE_MAX, title
    assert title.startswith("2500× Wire Frame Stacking Chairs")
    assert title.endswith("— Pittsburgh, PA | Black Whole")
    assert "(Pittsburgh" not in title  # scraped parenthetical dropped, not doubled
    assert len(_description(r.text)) <= web_app.SEO_DESCRIPTION_MAX + 1


def test_static_titles_fit_the_budget(client):
    for path in ("/", "/listings", "/sell", "/map"):
        title = _title(client.get(path).text)
        assert len(title) <= web_app.SEO_TITLE_MAX, (path, title)


def test_short_title_strips_count_and_parenthetical():
    assert web_app._short_title("Lot of 657 Hard Plastic Yellow Chairs") == "Hard Plastic Yellow Chairs"
    assert web_app._short_title("200 Banquet Chairs — Tan Pattern (Orlando, FL)") == "Banquet Chairs — Tan Pattern"
    assert web_app._short_title("Burgundy Banquet Chairs") == "Burgundy Banquet Chairs"
    assert web_app._short_title("(Orlando, FL)") == "(Orlando, FL)"  # never empty


def test_seo_title_never_overflows_for_absurd_inputs():
    t = web_app._seo_title(12000, "A" * 300, "Somewhere Very Long City Name, XX · Another City, YY")
    assert len(t) <= web_app.SEO_TITLE_MAX
    assert t.endswith("| Black Whole")


def test_detail_has_breadcrumb_list(client):
    r = client.get("/listings/10340")
    crumbs = [b for b in _jsonld_blocks(r.text) if b.get("@type") == "BreadcrumbList"]
    assert len(crumbs) == 1
    items = crumbs[0]["itemListElement"]
    assert [i["item"] for i in items] == [
        "https://black-whole.com/", "https://black-whole.com/listings",
        "https://black-whole.com/listings/10340",
    ]
    assert items[-1]["name"] == "Burgundy Banquet Chairs"
    assert 'aria-label="Breadcrumb"' in r.text


def test_static_pages_have_breadcrumbs(client):
    for path in ("/listings", "/sell", "/map"):
        r = client.get(path)
        assert r.status_code == 200, path
        blocks = [b for b in _jsonld_blocks(r.text) if b.get("@type") == "BreadcrumbList"]
        assert len(blocks) == 1, path
        assert blocks[0]["itemListElement"][-1]["item"] == f"https://black-whole.com{path}"


def test_organization_jsonld_carries_nap(client):
    r = client.get("/")
    org = [b for b in _jsonld_blocks(r.text) if "Organization" in b.get("@type", [])][0]
    assert "LocalBusiness" in org["@type"]
    assert org["email"] == web_app.PUBLIC_CONTACT["email"]
    assert org["address"]["addressLocality"] == web_app.PUBLIC_CONTACT["city"]
    assert org["address"]["postalCode"] == web_app.PUBLIC_CONTACT["postal"]
    if not web_app.PUBLIC_CONTACT.get("phone"):
        assert "telephone" not in org
    assert f'mailto:{web_app.PUBLIC_CONTACT["email"]}' in r.text  # footer NAP


def test_storage_note_never_reaches_the_footer_or_jsonld(client, monkeypatch):
    row = dict(ROW, storage_note="Unit 12, gate code 4455")
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: row)
    html = client.get("/listings/10340").text
    assert "gate code" not in html and "Unit 12" not in html


def test_static_assets_are_versioned_per_process_not_per_request(client):
    v = web_app.templates.env.globals["asset_v"]
    for path in ("/", "/listings", "/listings/10340", "/map", "/sell"):
        html = client.get(path).text
        for ref in ("site.css?v=", "site.js?v="):
            if ref in html:
                assert f"{ref}{v}" in html, (path, ref)
        # every versioned URL on the page uses the per-process stamp
        assert all(seg.startswith(v) for seg in html.split("?v=")[1:]), path


def test_html_404_for_browsers_json_for_everyone_else(client):
    r = client.get("/listings/nope", headers={"accept": "text/html,*/*"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert 'name="robots" content="noindex' in r.text
    assert "/listings/10340" in r.text  # live lots are offered on the way out
    r2 = client.get("/listings/nope")
    assert r2.status_code == 404
    assert r2.json() == {"detail": "listing not found"}


def test_api_404_stays_json_even_for_browsers(client):
    r = client.get("/api/no-such-thing", headers={"accept": "text/html"})
    assert r.status_code in (401, 404)
    assert "text/html" not in r.headers.get("content-type", "")


def test_lost_lot_renders_with_noindex_live_and_sold_without(client, monkeypatch):
    lost = dict(ROW, lot_id="1125", status="lost")
    monkeypatch.setattr(web_app.inventory, "get",
                        lambda lot_id: lost if lot_id == "1125" else dict(ROW))
    r = client.get("/listings/1125")
    assert r.status_code == 200
    assert 'name="robots" content="noindex,follow"' in r.text
    assert 'name="robots" content="noindex' not in client.get("/listings/10340").text
    sold = dict(ROW, lot_id="old", status="sold_out", quantity_remaining=0)
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: sold)
    assert 'name="robots" content="noindex' not in client.get("/listings/old").text
    bare_sold = dict(sold, hero_image_url=None, image_urls=None, quantity_original=0)
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: bare_sold)
    assert 'name="robots" content="noindex,follow"' in client.get("/listings/old").text


def test_sold_twin_canonicalises_to_the_live_lot(client, monkeypatch):
    live = dict(ROW, lot_id="gd-28859-2863", status="won_pickup")
    twin = dict(ROW, lot_id="gd-28859-2863-sold", status="lost_sold_out",
                quantity_remaining=0, quantity_original=210)
    rows = {"gd-28859-2863": live, "gd-28859-2863-sold": twin}
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: rows.get(lot_id))
    r = client.get("/listings/gd-28859-2863-sold")
    assert 'rel="canonical" href="https://black-whole.com/listings/gd-28859-2863"' in r.text
    r2 = client.get("/listings/gd-28859-2863")
    assert 'rel="canonical" href="https://black-whole.com/listings/gd-28859-2863"' in r2.text
    # a `-sold` row with no live twin keeps its own canonical
    monkeypatch.setattr(web_app.inventory, "get",
                        lambda lot_id: twin if lot_id == "gd-28859-2863-sold" else None)
    r3 = client.get("/listings/gd-28859-2863-sold")
    assert 'rel="canonical" href="https://black-whole.com/listings/gd-28859-2863-sold"' in r3.text


def test_gallery_images_carry_alt_and_dimensions(client, monkeypatch):
    row = dict(ROW, image_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: row)
    html = client.get("/listings/10340").text
    assert 'fetchpriority="high"' in html
    assert 'alt="Burgundy Banquet Chairs — photo 2"' in html
    assert 'alt=""' not in html
    assert 'width="800" height="600"' in client.get("/listings").text


def test_sitemap_is_memoised_but_invalidated_on_write(client, monkeypatch):
    first = client.get("/sitemap.xml").text
    assert "/listings/10340" in first
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [dict(ROW, lot_id="new-1")])
    assert "/listings/new-1" not in client.get("/sitemap.xml").text  # memo hit
    readcache.invalidate_all()
    assert "/listings/new-1" in client.get("/sitemap.xml").text

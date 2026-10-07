"""SEO content (2026-10-07): /about, original per-lot copy sections with
FAQPage JSON-LD, the homepage intent line + FAQ, ItemList on /listings.

DB-free: inventory is monkeypatched like tests/test_seo.py. The copy module
is pure, so it is also tested directly.
"""

import json
import re
from datetime import datetime, timezone

import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import readcache, seo_copy

web_app = importlib.import_module("automation.web.app")


ROW = {
    "lot_id": "gd-32876-2",
    "title": "120 Burgundy Padded Stacking Banquet Chairs — Chrome Frame (Nashville, TN)",
    "description": "120 burgundy padded stacking banquet chairs with chrome metal frames, from a convention center.",
    "city": "Nashville",
    "state": "TN",
    "zip_code": "37203",
    "chair_type": "Banquet Chairs",
    "chair_frame": "Chrome steel",
    "subtitle": "Burgundy fabric seats on chrome frames.",
    "quantity_remaining": 120,
    "quantity_original": 120,
    "price_per_chair": 25.0,
    "status": "listed",
    "hero_image_url": "https://cdn.example.com/x.jpg",
    "image_urls": [],
    "folder_name": None,
    "hero_image": None,
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
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(ROW) if lot_id == ROW["lot_id"] else None)
    monkeypatch.setattr(web_app.inventory, "stats",
                        lambda: {"lots": 1, "chairs": 120, "cities": 1, "moved": 7681})
    monkeypatch.setattr(web_app.public_map, "nearby", lambda lot_id, **k: {"origin": None, "items": []})
    monkeypatch.setattr(web_app, "_reserve_enabled", lambda: False)
    return TestClient(web_app.app)


def _jsonld(html: str) -> list[dict]:
    out, marker, pos = [], '<script type="application/ld+json">', 0
    while (start := html.find(marker, pos)) >= 0:
        start += len(marker)
        end = html.index("</script>", start)
        out.append(json.loads(html[start:end].replace("<\\/", "</")))
        pos = end
    return out


def _text(html: str) -> str:
    body = html.split("<main", 1)[1].split("</main>", 1)[0]
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


# ── copy module ──

def test_kind_detection():
    assert seo_copy.kind({"chair_type": "Banquet Chairs"}) == "banquet"
    assert seo_copy.kind({"title": "Lot of 500 Metal Folding Chairs"}) == "folding"
    assert seo_copy.kind({"title": "Wire frame stacking chairs, linkable"}) == "stacking"
    assert seo_copy.kind({"title": "Mystery seats"}) == "event"


def test_specs_only_render_fields_that_exist():
    full = dict(seo_copy.specs(ROW, sold=False))
    assert full["Available"] == "120 chairs"
    assert full["Price per chair"] == "$25"
    assert full["Whole lot"] == "$3,000 (before freight)"
    assert full["Frame"] == "Chrome steel"
    assert full["Pallets"].startswith("≈ 4 at 35")
    assert full["Pickup city"] == "Nashville, TN"
    bare = dict(seo_copy.specs({"lot_id": "x", "title": "Chairs"}, sold=False))
    assert list(bare) == ["Condition"]
    sold = dict(seo_copy.specs(dict(ROW, status="sold_out", quantity_remaining=0), sold=True))
    assert sold["Lot size"] == "120 chairs" and "Price per chair" not in sold
    assert sold["Sourced from"] == "Nashville, TN"


def test_copy_is_stable_per_lot_and_differs_between_lots():
    a1 = seo_copy.good_for(ROW, sold=False)
    a2 = seo_copy.good_for(dict(ROW), sold=False)
    assert a1 == a2
    others = {seo_copy.good_for(dict(ROW, lot_id=f"lot-{i}"), sold=False) for i in range(12)}
    assert len(others) > 1
    assert "Nashville, TN" in a1


def test_pickup_freight_flags_the_unweighed_placeholder():
    text = seo_copy.pickup_freight(ROW, sold=False)
    assert "4 pallets" in text and "estimate until we weigh" in text
    weighed = seo_copy.pickup_freight(dict(ROW, chair_weight_lb=11.5, chairs_per_pallet=40), sold=False)
    assert "3 pallets" in weighed and "1,380 lb" in weighed and "until we weigh" not in weighed


def test_faq_jsonld_is_valid_and_escaped():
    items = seo_copy.faq(dict(ROW, description="</script><b>x"), sold=False)
    raw = seo_copy.faq_jsonld(items)
    assert "</" not in raw
    data = json.loads(raw.replace("<\\/", "</"))
    assert data["@type"] == "FAQPage" and len(data["mainEntity"]) == 4
    assert data["mainEntity"][0]["name"] == "Can I buy fewer than 120 chairs?"


def test_copy_never_touches_storage_note():
    row = dict(ROW, storage_note="Unit 12, gate code 4455")
    blob = json.dumps(seo_copy.build(row, sold=False))
    assert "gate code" not in blob and "Unit 12" not in blob


# ── pages ──

def test_lot_page_gains_original_sections_and_faq_schema(client):
    html = client.get(f"/listings/{ROW['lot_id']}").text
    for heading in ("SPECS", "GOOD FOR", "PICKUP &amp; FREIGHT", "QUESTIONS BUYERS ASK"):
        assert heading in html, heading
    kinds = [b["@type"] for b in _jsonld(html)]
    assert "FAQPage" in kinds and "Product" in kinds and "BreadcrumbList" in kinds
    assert seo_copy.word_count(_text(html)) >= 450
    assert "Unit 12" not in html


def test_sold_lot_copy_is_past_tense(client, monkeypatch):
    sold = dict(ROW, status="sold_out", quantity_remaining=0)
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: sold)
    html = client.get(f"/listings/{ROW['lot_id']}").text
    assert "WHO BOUGHT SETS LIKE THIS" in html
    assert "Can I still buy these banquet chairs?" in html
    assert "Price per chair" not in html.split("specs-table")[1].split("</table>")[0]


def test_about_page(client):
    r = client.get("/about")
    assert r.status_code == 200
    html = r.text
    assert "<title>About Black Whole Liquidation — Bulk Chair Lots</title>" in html
    assert 'rel="canonical" href="https://black-whole.com/about"' in html
    assert "7,681 CHAIRS MOVED" in html
    assert f'mailto:{web_app.PUBLIC_CONTACT["email"]}' in html
    assert seo_copy.word_count(_text(html)) >= 450
    assert any(b.get("@type") == "BreadcrumbList" for b in _jsonld(html))
    assert 'href="/about"' in client.get("/").text  # nav + footer link
    assert "https://black-whole.com/about" in client.get("/sitemap.xml").text


def test_homepage_intent_line_and_faq(client):
    html = client.get("/").text
    assert "Bulk seating from $25 per chair · 120 chairs on the floor · 1 pickup city" in html
    assert "STRAIGHT ANSWERS" in html
    faq = [b for b in _jsonld(html) if b.get("@type") == "FAQPage"]
    assert len(faq) == 1 and len(faq[0]["mainEntity"]) == len(seo_copy.SITE_FAQ)


def test_homepage_without_stock_has_no_intent_line(client, monkeypatch):
    readcache.invalidate_all()
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [])
    monkeypatch.setattr(web_app.inventory, "stats", lambda: {"lots": 0, "chairs": 0, "cities": 0, "moved": 0})
    assert "intent-line" not in client.get("/").text


def test_listings_itemlist(client):
    html = client.get("/listings").text
    assert "CHAIR LOTS FOR SALE" in html
    lst = [b for b in _jsonld(html) if b.get("@type") == "ItemList"][0]
    assert lst["numberOfItems"] == 1
    assert lst["itemListElement"][0]["url"] == f"https://black-whole.com/listings/{ROW['lot_id']}"

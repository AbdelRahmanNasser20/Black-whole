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
    "locations": None,
    "updated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
}


@pytest.fixture
def client(monkeypatch):
    readcache.invalidate_all()
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [dict(ROW), dict(ROW, lot_id="cheap", price_per_chair=8.0)])
    monkeypatch.setattr(web_app.inventory, "list_sold_showcase", lambda limit=None: [])
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(ROW) if lot_id == ROW["lot_id"] else None)
    monkeypatch.setattr(web_app.inventory, "stats",
                        lambda: {"lots": 2, "chairs": 240, "cities": 1, "moved": 7681})
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

def test_kind_detection_and_unit_label():
    assert seo_copy.kind({"chair_type": "Banquet Chairs"}) == "banquet"
    assert seo_copy.kind({"title": "Lot of 500 Metal Folding Chairs"}) == "folding"
    assert seo_copy.kind({"title": "Wire frame stacking chairs, linkable"}) == "stacking"
    assert seo_copy.kind({"title": "Mystery seats"}) == "event"
    assert seo_copy.kind_label({"title": "20 Round Banquet Tables"}) == "banquet tables"
    assert seo_copy.kind_label(ROW) == "banquet chairs"


def test_freight_facts_only_what_the_spec_sheet_lacks():
    facts = dict(seo_copy.freight_facts(ROW, sold=False))
    assert facts["Whole lot"] == "$3,000 (before freight)"
    assert facts["Frame"] == "Chrome steel"
    assert facts["Pallets"].startswith("≈ 4 at 35")
    assert facts["Weight per chair"] == "13 lb (estimate)"
    assert facts["Lot weight"] == "≈ 1,560 lb"
    for dup in ("Available", "Price per chair", "Pickup city", "Condition", "Dimensions"):
        assert dup not in facts
    bare = seo_copy.freight_facts({"lot_id": "x", "title": "Chairs"}, sold=False)
    assert bare == []
    sold = dict(seo_copy.freight_facts(dict(ROW, status="sold_out", quantity_remaining=0), sold=True))
    assert "Whole lot" not in sold and sold["Pallets"].startswith("≈ 4")


def test_calibration_comes_from_freight_estimate():
    weighed = dict(ROW, chair_weight_lb=11.5, chairs_per_pallet=40)
    facts = dict(seo_copy.freight_facts(weighed, sold=False))
    assert facts["Weight per chair"] == "11.5 lb (weighed)"
    assert facts["Pallets"].startswith("≈ 3 at 40")
    text = seo_copy.pickup_freight(weighed, sold=False)
    assert "3 pallets" in text and "1,380 lb" in text and "until we weigh" not in text
    assert "estimate until we weigh" in seo_copy.pickup_freight(ROW, sold=False)


def test_copy_is_stable_per_lot_and_differs_between_lots():
    a1 = seo_copy.good_for(ROW, sold=False)
    a2 = seo_copy.good_for(dict(ROW), sold=False)
    assert a1 == a2
    others = {seo_copy.good_for(dict(ROW, lot_id=f"lot-{i}"), sold=False) for i in range(12)}
    assert len(others) > 1
    assert "Nashville, TN" in a1


def test_small_lot_is_not_called_a_building():
    small = dict(ROW, quantity_remaining=40, quantity_original=40)
    text = seo_copy.good_for(small, sold=False)
    assert "whole-building" not in text and "full room" not in text
    assert "40" in text
    faq = dict(seo_copy.faq(small, sold=False))
    assert "Can I buy fewer than 40 chairs?" in faq


def test_incoming_lot_never_promises_pickup_today():
    for status in ("active_bid", "won_pickup"):
        copy = seo_copy.build(dict(ROW, status=status), sold=False)
        assert copy["incoming"] is True
        assert "on their way to Nashville, TN" in copy["pickup_freight"]
        assert "Local pickup in Nashville, TN is free" not in copy["pickup_freight"]
        assert "They sit in" not in copy["good_for"]
        questions = [q for q, _ in copy["faq"]]
        assert "Do you take reservations before it arrives?" in questions
        assert "Do you hold chairs while we get approval?" not in questions
    live = seo_copy.build(ROW, sold=False)
    assert live["incoming"] is False and "Local pickup in Nashville, TN is free" in live["pickup_freight"]


def test_table_lot_is_described_as_tables():
    tables = dict(ROW, title="20 Round Banquet Tables (Augusta, GA)", chair_type=None,
                  quantity_remaining=20, quantity_original=20)
    copy = seo_copy.build(tables, sold=False)
    assert "Can I buy fewer than 20 tables?" in dict(copy["faq"])
    prose = copy["good_for"] + copy["pickup_freight"] + " ".join(q + a for q, a in copy["faq"])
    assert "chair" not in prose.lower()
    assert "table" in prose.lower()
    sold = seo_copy.build(dict(tables, status="sold_out", quantity_remaining=0), sold=True)
    assert "Can I still buy these banquet tables?" in dict(sold["faq"])


def test_out_of_stock_live_row_advertises_nothing():
    gone = dict(ROW, quantity_remaining=0)
    assert seo_copy.freight_facts(gone, sold=False) == [("Frame", "Chrome steel")]
    assert "Can I buy part of the lot?" in dict(seo_copy.faq(gone, sold=False))
    assert "120" not in seo_copy.good_for(gone, sold=False)


def test_multi_location_lot_names_every_city():
    multi = dict(ROW, locations=[{"city": "Atlanta", "state": "GA"}, {"city": "Nashville", "state": "TN"}])
    text = seo_copy.pickup_freight(multi, sold=False)
    assert "Atlanta, GA and Nashville, TN" in text
    assert "from Atlanta, GA and Nashville, TN" in dict(seo_copy.faq(multi, sold=False)).keys().__str__()


def test_faq_jsonld_is_valid_and_escaped():
    items = seo_copy.faq(dict(ROW, description="</script><b>x"), sold=False)
    raw = seo_copy.faq_jsonld(items)
    assert "</" not in raw
    data = json.loads(raw.replace("<\\/", "</"))
    assert data["@type"] == "FAQPage" and len(data["mainEntity"]) == 4
    assert data["mainEntity"][0]["name"] == "Can I buy fewer than 120 chairs?"
    assert seo_copy.jsonld({"a": "</script>"}) == '{"a": "<\\/script>"}'


def test_copy_never_touches_storage_note():
    row = dict(ROW, storage_note="Unit 12, gate code 4455")
    blob = json.dumps(seo_copy.build(row, sold=False))
    assert "gate code" not in blob and "Unit 12" not in blob


# ── pages ──

def test_lot_page_gains_original_sections_and_faq_schema(client):
    html = client.get(f"/listings/{ROW['lot_id']}").text
    for heading in ("GOOD FOR", "PICKUP &amp; FREIGHT", "QUESTIONS BUYERS ASK"):
        assert heading in html, heading
    assert "Whole lot" in html and "specs-table" in html
    kinds = [b["@type"] for b in _jsonld(html)]
    assert "FAQPage" in kinds and "Product" in kinds and "BreadcrumbList" in kinds
    assert seo_copy.word_count(_text(html)) >= 400
    assert "Unit 12" not in html


def test_incoming_lot_page_says_arrival(client, monkeypatch):
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(ROW, status="won_pickup"))
    html = client.get(f"/listings/{ROW['lot_id']}").text
    assert "ARRIVAL &amp; FREIGHT" in html
    assert "on their way to Nashville, TN" in html


def test_sold_lot_copy_is_past_tense(client, monkeypatch):
    sold = dict(ROW, status="sold_out", quantity_remaining=0)
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: sold)
    html = client.get(f"/listings/{ROW['lot_id']}").text
    assert "WHO BOUGHT SETS LIKE THIS" in html
    assert "Can I still buy these banquet chairs?" in html
    assert "Whole lot" not in html


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


def test_homepage_intent_line_uses_the_floor_price_of_every_lot(client):
    html = client.get("/").text
    # $8 is on the second public lot, not the first featured one.
    assert "Bulk seating from $8 per chair · 240 chairs on the floor · 1 pickup city" in html
    assert "STRAIGHT ANSWERS" in html
    faq = [b for b in _jsonld(html) if b.get("@type") == "FAQPage"]
    assert len(faq) == 1 and len(faq[0]["mainEntity"]) == len(seo_copy.SITE_FAQ)
    # The sentence sits under the section header, not between the title and its nav.
    assert html.index('class="see-all') < html.index('class="intent-line"')


def test_homepage_without_stock_has_no_intent_line(client, monkeypatch):
    readcache.invalidate_all()
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [])
    monkeypatch.setattr(web_app.inventory, "stats", lambda: {"lots": 0, "chairs": 0, "cities": 0, "moved": 0})
    assert "intent-line" not in client.get("/").text


def test_listings_itemlist(client):
    html = client.get("/listings").text
    assert "CHAIR LOTS FOR SALE" in html
    lst = [b for b in _jsonld(html) if b.get("@type") == "ItemList"][0]
    assert lst["numberOfItems"] == 2
    assert lst["itemListElement"][0]["url"] == f"https://black-whole.com/listings/{ROW['lot_id']}"

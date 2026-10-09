"""Buyer guides (AI SEO, 2026-10-09): /guides, /guides/{slug}, FAQPage JSON-LD,
sitemap + llms.txt listing, live-only numbers, visit tracking.

DB-free: inventory and public_map are monkeypatched like tests/test_seo_city.py.
"""

import importlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from automation.alerts import geo
from automation.web import guides, public_map, readcache, visits

web_app = importlib.import_module("automation.web.app")


def _row(**over):
    base = {
        "lot_id": "31225-atl", "slug": "1200-brown-convention-chairs-atlanta-ga",
        "title": "1,200 Brown Convention Chairs (Atlanta, GA)", "description": "x",
        "city": "Atlanta", "state": "GA", "zip_code": "30336", "chair_type": "Banquet Chairs",
        "quantity_remaining": 1200, "quantity_original": 1200, "price_per_chair": 25.0,
        "status": "listed", "hero_image_url": "https://cdn.example.com/p.jpg", "image_urls": [],
        "folder_name": None, "hero_image": None, "subtitle": None, "dimensions": None,
        "facebook_url": None, "ebay_url": None, "locations": None,
        "storage_note": "SECRET-GATE-4321 — 123 Private Warehouse Rd",
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


ATL = _row()
PHX = _row(lot_id="9006", slug="300-mauve-banquet-chairs-phoenix-az", title="Mauve Banquet Chairs",
           city="Phoenix", state="AZ", quantity_remaining=300, quantity_original=300, price_per_chair=20.0)


@pytest.fixture
def client(monkeypatch):
    readcache.invalidate_all()
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [dict(ATL), dict(PHX)])
    monkeypatch.setattr(web_app.inventory, "list_sold_showcase", lambda limit=None: [])
    monkeypatch.setattr(web_app.inventory, "stats",
                        lambda: {"lots": 2, "chairs": 1500, "cities": 2, "moved": 0})
    monkeypatch.setattr(public_map, "all_points", lambda: [])
    monkeypatch.setattr(geo, "resolve_place", lambda city, state, zip_code: (None, None, None))
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


def test_index_lists_every_guide(client):
    r = client.get("/guides")
    assert r.status_code == 200
    for g in guides.GUIDES:
        assert f'href="{g["path"]}"' in r.text
    assert 'rel="canonical" href="https://black-whole.com/guides"' in r.text


@pytest.mark.parametrize("slug", [g["slug"] for g in guides.GUIDES])
def test_every_guide_renders_with_faq_jsonld(client, slug):
    r = client.get(f"/guides/{slug}")
    assert r.status_code == 200, slug
    blocks = _jsonld(r.text)
    faq = [b for b in blocks if b.get("@type") == "FAQPage"]
    assert len(faq) == 1
    qs = faq[0]["mainEntity"]
    assert len(qs) >= 4
    for q in qs:
        assert q["@type"] == "Question" and q["acceptedAnswer"]["text"]
        assert q["name"] in r.text  # the markup and the structured data agree
    crumbs = [b for b in blocks if b.get("@type") == "BreadcrumbList"][0]["itemListElement"]
    assert crumbs[1]["item"] == "https://black-whole.com/guides"
    assert "noindex" not in r.text
    assert "SECRET-GATE" not in r.text and "Private Warehouse" not in r.text


def test_unknown_guide_is_404(client):
    assert client.get("/guides/nope").status_code == 404


def test_city_guide_answers_from_live_inventory(client):
    r = client.get("/guides/used-church-chairs-near-me")
    html = r.text
    faq = [b for b in _jsonld(html) if b.get("@type") == "FAQPage"][0]["mainEntity"]
    by_q = {q["name"]: q["acceptedAnswer"]["text"] for q in faq}
    atl = by_q["Are there used church chairs for sale near Atlanta?"]
    assert "1,200 chairs" in atl and "$25 per chair" in atl
    boise = by_q["Are there used church chairs for sale near Boise?"]
    assert "Nothing is staged in Boise, ID" in boise
    assert 'href="/chairs/atlanta-ga"' in html and 'href="/chairs/phoenix-az"' in html
    assert "1,500 CHAIRS ON THE FLOOR" in html
    assert "FROM $20 / CHAIR" in html


def test_guide_renders_without_db(client, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(web_app.inventory, "list_public", boom)
    readcache.invalidate_all()
    r = client.get("/guides/used-church-chairs-near-me")
    assert r.status_code == 200
    assert "Nothing is on the floor right now" in r.text
    assert "CHAIRS ON THE FLOOR" not in r.text  # no number is invented


def test_guides_in_sitemap_and_llms_txt(client):
    xml = client.get("/sitemap.xml").text
    llms = client.get("/llms.txt").text
    assert "<loc>https://black-whole.com/guides</loc>" in xml
    assert "## Buyer guides" in llms
    for g in guides.GUIDES:
        assert f"<loc>https://black-whole.com{g['path']}</loc>" in xml
        assert f"(https://black-whole.com{g['path']})" in llms


def test_guide_visits_are_tracked():
    assert visits.should_track("/guides/where-to-buy-banquet-chairs-in-bulk", "Mozilla/5.0 (X11) Chrome/120")
    assert visits.should_track("/guides", "Mozilla/5.0 (X11) Chrome/120")


def test_copy_has_no_fake_reviews_or_hand_typed_stock_numbers():
    for g in guides.GUIDES:
        text = " ".join(p for _, paras in g["sections"] for p in paras) + " ".join(a for _, a in g["faq"])
        low = text.lower()
        assert "review" not in low and "stars" not in low and "testimonial" not in low
        assert "$" not in text  # every dollar figure on a guide is rendered from the ledger

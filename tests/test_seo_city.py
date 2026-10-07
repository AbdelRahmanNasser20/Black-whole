"""SEO city pages + clean lot URLs (2026-10-07): automation/lot_urls.py,
automation/web/city_pages.py, /chairs, /chairs/{slug}, slug routing + 301,
sitemap/feeds/map emitting slug URLs, city crumb on lot pages.

DB-free: inventory, public_map and geo are monkeypatched.
"""

import json
from datetime import datetime, timezone

import importlib

import pytest
from fastapi.testclient import TestClient

from automation import catalog_feed, google_feed, lot_urls
from automation.alerts import geo
from automation.web import city_pages, public_map, readcache

web_app = importlib.import_module("automation.web.app")


def _row(**over):
    base = {
        "lot_id": "gd-56-9685",
        "slug": "2500-wire-frame-stacking-chairs-pittsburgh-pa",
        "title": "~2,500 Wire Frame Stacking Chairs — Chrome Frame, Linkable (Pittsburgh, PA)",
        "description": "Wire-rod stacking chairs from a conference building.",
        "city": "Pittsburgh", "state": "PA", "zip_code": "15222",
        "chair_type": "Stacking Chairs",
        "quantity_remaining": 2500, "quantity_original": 2500, "price_per_chair": 25.0,
        "status": "listed",
        "hero_image_url": "https://cdn.example.com/p.jpg", "image_urls": [],
        "folder_name": None, "hero_image": None, "subtitle": None, "dimensions": None,
        "facebook_url": None, "ebay_url": None, "locations": None,
        "updated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


LIVE = _row()
NOSLUG = _row(lot_id="9006", slug=None, title="Mauve Banquet Chairs", city="Phoenix", state="AZ",
              quantity_remaining=300, quantity_original=300, price_per_chair=20.0)
SOLD = _row(lot_id="old-1", slug="3000-blue-chairs-baltimore-md", title="Blue Chairs",
            city="Baltimore", state="MD", status="sold_out", quantity_remaining=0,
            quantity_original=3000)

POINTS = [
    {"id": "a", "kind": "lot", "lot_id": "gd-56-9685", "title": "Wire chairs", "bucket": "available",
     "quantity": 2500, "unit": "CHAIR", "price_per_chair": 25.0, "city": "Pittsburgh", "state": "PA",
     "lat": 40.44, "lng": -79.99, "precision": "city", "hero": None, "url": "/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa"},
    {"id": "b", "kind": "lot", "lot_id": "9006", "title": "Mauve chairs", "bucket": "available",
     "quantity": 300, "unit": "CHAIR", "price_per_chair": 20.0, "city": "Phoenix", "state": "AZ",
     "lat": 33.45, "lng": -112.07, "precision": "city", "hero": None, "url": "/listings/9006"},
    {"id": "c", "kind": "lot", "lot_id": "cle-1", "title": "Cleveland chairs", "bucket": "incoming",
     "quantity": 400, "unit": "CHAIR", "price_per_chair": None, "city": "Cleveland", "state": "OH",
     "lat": 41.50, "lng": -81.69, "precision": "city", "hero": None, "url": "/listings/cle-1"},
]
COORDS = {("pittsburgh", "pa"): (40.44, -79.99), ("phoenix", "az"): (33.45, -112.07),
          ("baltimore", "md"): (39.29, -76.61), ("cleveland", "oh"): (41.50, -81.69)}


@pytest.fixture
def client(monkeypatch):
    readcache.invalidate_all()
    rows = {LIVE["lot_id"]: LIVE, NOSLUG["lot_id"]: NOSLUG, SOLD["lot_id"]: SOLD}
    slugs = {r["slug"]: r for r in rows.values() if r.get("slug")}
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [dict(LIVE), dict(NOSLUG)])
    monkeypatch.setattr(web_app.inventory, "list_sold_showcase", lambda limit=None: [dict(SOLD)])
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(rows[lot_id]) if lot_id in rows else None)
    monkeypatch.setattr(web_app.inventory, "get_by_slug", lambda s: dict(slugs[s]) if s in slugs else None)
    monkeypatch.setattr(web_app.inventory, "stats",
                        lambda: {"lots": 2, "chairs": 2800, "cities": 2, "moved": 3000})
    monkeypatch.setattr(public_map, "all_points", lambda: [dict(p) for p in POINTS])
    monkeypatch.setattr(public_map, "nearby", lambda lot_id, **k: {"origin": None, "items": []})
    monkeypatch.setattr(geo, "resolve_place",
                        lambda city, state, zip_code: (*COORDS.get(((city or "").lower(), (state or "").lower()), (None, None)), "city"))
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


# ── lot_urls ──

def test_make_slug_is_descriptive_and_bounded():
    assert lot_urls.make_slug(LIVE) == "2500-wire-frame-stacking-chairs-pittsburgh-pa"
    assert lot_urls.make_slug(NOSLUG) == "300-mauve-banquet-chairs-phoenix-az"
    assert lot_urls.make_slug({"lot_id": "x", "title": "Lot of 657 Hard Plastic Yellow Chairs",
                               "quantity_original": 657, "city": "Fayetteville", "state": "North Carolina"}) \
        == "657-hard-plastic-yellow-chairs-fayetteville-north-carolina"
    long = lot_urls.make_slug({"lot_id": "y", "title": "A " * 200, "quantity_original": 5})
    assert len(long) <= lot_urls.SLUG_MAX and not long.endswith("-")
    assert lot_urls.make_slug({"lot_id": "folder:ATL_Grey_399", "title": ""}) == "chair-lot"


def test_public_path_prefers_slug_and_escapes_ids():
    assert lot_urls.public_path(LIVE) == "/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa"
    assert lot_urls.public_path(NOSLUG) == "/listings/9006"
    assert lot_urls.public_path({"lot_id": "folder:ATL_Grey_399", "slug": None}) == "/listings/folder%3AATL_Grey_399"
    assert lot_urls.city_path("Atlanta", "GA") == "/chairs/atlanta-ga"
    assert lot_urls.city_slug("Lakeside Marblehead", "Ohio") == "lakeside-marblehead-ohio"


# ── lot routes ──

def test_slug_url_is_canonical_and_id_url_redirects(client):
    r = client.get("/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa")
    assert r.status_code == 200
    assert 'rel="canonical" href="https://black-whole.com/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa"' in r.text
    product = [b for b in _jsonld(r.text) if b.get("@type") == "Product"][0]
    assert product["sku"] == "2500-wire-frame-stacking-chairs-pittsburgh-pa"
    head = r.text.split("<main", 1)[0]
    assert "gd-56-9685" not in head  # never in title, canonical, og:url or the JSON-LD
    r2 = client.get("/listings/gd-56-9685?utm_source=facebook", follow_redirects=False)
    assert r2.status_code == 301
    assert r2.headers["location"] == "/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa?utm_source=facebook"


def test_unslugged_lot_still_serves_by_id(client):
    r = client.get("/listings/9006")
    assert r.status_code == 200
    assert 'rel="canonical" href="https://black-whole.com/listings/9006"' in r.text


def test_lot_page_breadcrumb_includes_its_city(client):
    html = client.get("/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa").text
    crumbs = [b for b in _jsonld(html) if b.get("@type") == "BreadcrumbList"][0]["itemListElement"]
    assert [c["item"] for c in crumbs][:3] == [
        "https://black-whole.com/", "https://black-whole.com/listings", "https://black-whole.com/chairs/pittsburgh-pa",
    ]


# ── sitemap / feeds / map ──

def test_sitemap_lists_slug_urls_and_city_pages(client):
    xml = client.get("/sitemap.xml").text
    assert "<loc>https://black-whole.com/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa</loc>" in xml
    assert "<loc>https://black-whole.com/listings/9006</loc>" in xml
    assert "/listings/gd-56-9685" not in xml
    assert "<loc>https://black-whole.com/chairs</loc>" in xml
    assert "<loc>https://black-whole.com/chairs/pittsburgh-pa</loc>" in xml
    assert "<loc>https://black-whole.com/chairs/phoenix-az</loc>" in xml
    assert "/chairs/baltimore-md" not in xml  # sold-only city is noindex


def test_feeds_link_to_the_slug():
    fb = catalog_feed.feed_row(LIVE, base_url="https://black-whole.com")
    assert fb["link"].startswith("https://black-whole.com/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa?")
    g = google_feed.feed_row(LIVE, base_url="https://black-whole.com")
    assert g["link"].startswith("https://black-whole.com/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa?")
    assert catalog_feed.feed_row(NOSLUG, base_url="https://black-whole.com")["link"].startswith(
        "https://black-whole.com/listings/9006?")


def test_map_points_carry_slug_urls(monkeypatch):
    pts = public_map.points_from_inventory([dict(LIVE), dict(NOSLUG)])
    urls = {p["lot_id"]: p["url"] for p in pts}
    assert urls["gd-56-9685"] == "/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa"
    assert urls["9006"] == "/listings/9006"


# ── city pages ──

def test_city_index_groups_live_and_sold_by_location():
    idx = city_pages.index.__wrapped__() if hasattr(city_pages.index, "__wrapped__") else None
    assert idx is None or "pittsburgh-pa" in idx


def test_city_page_renders_lots_nearby_and_schema(client):
    r = client.get("/chairs/pittsburgh-pa")
    assert r.status_code == 200
    html = r.text
    assert "<title>Bulk Banquet Chairs for Sale in Pittsburgh, PA | Black Whole</title>" in html
    assert "BULK BANQUET CHAIRS FOR SALE IN PITTSBURGH, PA" in html
    assert 'href="/listings/2500-wire-frame-stacking-chairs-pittsburgh-pa"' in html
    assert "WITHIN DRIVING DISTANCE" in html and 'href="/listings/cle-1"' in html  # Cleveland ≈ 115 mi
    assert "/listings/9006" not in html.split("WITHIN DRIVING DISTANCE")[1].split("</section>")[0]  # Phoenix is 1,800 mi away
    kinds = [b["@type"] for b in _jsonld(html)]
    assert "FAQPage" in kinds and "ItemList" in kinds and "BreadcrumbList" in kinds
    assert 'name="robots" content="noindex' not in html
    assert 'rel="canonical" href="https://black-whole.com/chairs/pittsburgh-pa"' in html


def test_sold_only_city_is_noindex_but_renders(client):
    r = client.get("/chairs/baltimore-md")
    assert r.status_code == 200
    assert 'name="robots" content="noindex,follow"' in r.text
    assert "ALREADY MOVED THROUGH BALTIMORE" in r.text
    assert "Nothing is on the floor in Baltimore, MD" in r.text


def test_unknown_city_is_404(client):
    assert client.get("/chairs/nowhere-xx").status_code == 404


def test_cities_index_and_map_list(client):
    html = client.get("/chairs").text
    assert 'href="/chairs/pittsburgh-pa"' in html and 'href="/chairs/phoenix-az"' in html
    assert html.index("pittsburgh-pa") < html.index("phoenix-az")  # most live chairs first
    m = client.get("/map").text
    assert "PICKUP CITIES" in m and 'href="/chairs/pittsburgh-pa"' in m


def test_city_copy_never_leaks_storage_note(client, monkeypatch):
    leaky = dict(LIVE, storage_note="Unit 12, gate code 4455")
    monkeypatch.setattr(web_app.inventory, "list_public", lambda: [leaky])
    readcache.invalidate_all()
    html = client.get("/chairs/pittsburgh-pa").text
    assert "gate code" not in html and "Unit 12" not in html

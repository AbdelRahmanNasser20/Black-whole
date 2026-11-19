"""/deals: ZIP + miles ("near"), source photos on feed rows, the filter panel at the top,
saved + starter searches on the feed."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import deals_sources, lot_pages, public_deals
from automation.web.app import app

WEB = Path("automation/web")
TEMPLATE = (WEB / "templates/deals_public.html").read_text()
FEED_JS = (WEB / "static/deals/feed.js").read_text()
CARD_JS = (WEB / "static/ui/card.js").read_text()


@pytest.fixture(autouse=True)
def _anon(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


# ── resolve_origin ────────────────────────────────────────────────────────

def test_resolve_origin_shapes(monkeypatch):
    from automation.alerts import geo
    monkeypatch.setattr(geo, "resolve_place", lambda c, s, z: (33.4, -112.0, "zip") if z == "85054" else (None, None, None))
    assert public_deals.resolve_origin(None, None) is None
    assert public_deals.resolve_origin("  ", 50) is None
    o = public_deals.resolve_origin("85054", 50)
    assert o == {"label": "85054", "radius": 50.0, "lat": 33.4, "lng": -112.0, "resolved": True}
    bad = public_deals.resolve_origin("nowhere", None)
    assert bad["resolved"] is False and bad["lat"] is None and bad["radius"] == 100.0
    assert public_deals.resolve_origin("85054", 99999)["radius"] == public_deals.RADIUS_MAX


def test_where_gets_a_haversine_clause_only_with_a_resolved_origin():
    where, args = public_deals.build_public_where(origin=(33.4, -112.0, 50.0))
    assert "asin(sqrt(" in where and "lat IS NOT NULL" in where and "state = ANY(%s)" in where
    assert args[-5:-1] == [33.4, 33.4, -112.0, 50.0]
    states = args[-1]
    assert "AZ" in states and "NV" in states and "IN" not in states and "PA" not in states
    where2, _ = public_deals.build_public_where(origin={"resolved": False, "lat": None})
    assert "asin(" not in where2


def test_snapshot_rows_filter_by_distance():
    rows = [
        {"source": "gsa", "title": "a", "canonical_category": None, "state": "AZ", "bid_count": 0,
         "end_utc": None, "current_bid": 1, "lat": 33.45, "lng": -112.07},     # Phoenix
        {"source": "gsa", "title": "b", "canonical_category": None, "state": "GA", "bid_count": 0,
         "end_utc": None, "current_bid": 1, "lat": 33.75, "lng": -84.39},      # Atlanta
        {"source": "gsa", "title": "c", "canonical_category": None, "state": "AZ", "bid_count": 0,
         "end_utc": None, "current_bid": 1, "lat": None, "lng": None},         # unmapped
        {"source": "gsa", "title": "d", "canonical_category": None, "state": "IN", "bid_count": 0,
         "end_utc": None, "current_bid": 1, "lat": 33.4526, "lng": -112.207},  # Indiana lot, Phoenix point: bad data
    ]
    out = deals_sources.apply_filters(rows, origin=(33.4, -112.0, 100.0))
    assert [r["title"] for r in out] == ["a"]


def test_unresolved_place_answers_nothing_not_everything(monkeypatch):
    from automation.alerts import geo
    monkeypatch.setattr(geo, "resolve_place", lambda c, s, z: (None, None, None))
    called = {"db": False}
    monkeypatch.setattr(public_deals.db, "connect", lambda *a, **k: called.__setitem__("db", True) or (_ for _ in ()).throw(AssertionError("no DB read")))
    out = public_deals.fetch_page(origin=public_deals.resolve_origin("xyz", 25))
    assert out["total"] == 0 and out["rows"] == [] and out["near"] == {"label": "xyz", "resolved": False, "radius": 25.0}
    assert called["db"] is False


# ── the route ─────────────────────────────────────────────────────────────

def _page(**kw):
    rows = [{"title": "Lot", "source": "govdeals", "photo_url": "https://files.lqdt1.com/photos/1/a.jpg", "lot_key": "1"}]
    return {"rows": rows, "total": 1, "page": 1, "per_page": 25, "pages": 1, **kw}


def test_route_passes_near_and_radius_and_keeps_photo_for_everyone(monkeypatch):
    seen = {}
    def fake_fetch(**kw):
        seen.update(kw); return _page(near={"label": "85054", "resolved": True, "radius": 50.0, "lat": 33.4, "lng": -112.0})
    monkeypatch.setattr(public_deals, "fetch_page", fake_fetch)
    monkeypatch.setattr(public_deals, "resolve_origin", lambda near, radius: {"label": near, "radius": float(radius), "lat": 33.4, "lng": -112.0, "resolved": True})
    monkeypatch.setattr(lot_pages, "photos_mode", lambda: lot_pages.PHOTOS_ALL)
    r = TestClient(app).get("/deals/api/lots?near=85054&radius=50")
    assert r.status_code == 200
    assert seen["origin"]["label"] == "85054" and seen["origin"]["radius"] == 50.0
    body = r.json()
    assert body["near"]["resolved"] is True
    assert body["rows"][0]["photo_url"].startswith("https://files.lqdt1.com/")


def test_route_strips_photos_when_the_setting_says_signed_in_only(monkeypatch):
    monkeypatch.setattr(public_deals, "fetch_page", lambda **kw: _page())
    monkeypatch.setattr(lot_pages, "photos_mode", lambda: lot_pages.PHOTOS_SIGNED_IN)
    body = TestClient(app).get("/deals/api/lots").json()
    assert "photo_url" not in body["rows"][0]
    assert body["rows"][0]["title"] == "Lot"          # the rest of the row is untouched


def test_radius_bounds_are_enforced():
    c = TestClient(app)
    assert c.get("/deals/api/lots?near=85054&radius=0").status_code == 422
    assert c.get("/deals/api/lots?near=85054&radius=5000").status_code == 422


def test_enrich_public_turns_hero_into_a_safe_photo_url():
    from deals.fees import fee_model_from_env
    row = {"asset_id": 1, "account_id": 2, "auction_id": 3, "title": "x", "site": "govdeals",
           "hero_image_url": "https://files.lqdt1.com/photos/2/a.jpg", "current_bid": 10, "bid_count": 0}
    out = public_deals.enrich_public(public_deals._with_source(dict(row)), fee_model_from_env())
    assert out["photo_url"] == "https://files.lqdt1.com/photos/2/a.jpg" and "hero_image_url" not in out
    row["hero_image_url"] = "https://black-whole.com/our-copy.jpg"       # never our own host
    out = public_deals.enrich_public(public_deals._with_source(dict(row)), fee_model_from_env())
    assert out["photo_url"] is None


def test_public_cols_never_select_our_archived_copies():
    assert "archived_hero_url" not in public_deals.PUBLIC_COLS and "gallery_urls" not in public_deals.PUBLIC_COLS


# ── the page ──────────────────────────────────────────────────────────────

def test_filters_sit_under_the_controls_not_in_a_side_rail():
    assert "{% block rail %}" not in TEMPLATE
    body = TEMPLATE.split("{% block content %}", 1)[1]
    assert body.index('id="feed-controls"') < body.index('id="feed-rail"') < body.index('id="feed-filters"')
    assert 'data-filters-toggle' in body and 'data-rail-open' not in TEMPLATE
    for el in ('id="feed-near"', 'id="feed-radius"', 'id="feed-saved"', 'id="feed-bids-label"', 'id="feed-ending"'):
        assert el in body, el


def test_page_renders_without_a_rail_and_with_the_panel(monkeypatch):
    html = TestClient(app).get("/deals").text
    assert 'class="has-rail' not in html and 'id="rail"' not in html
    assert 'id="feed-rail" class="feed-rail feed-filter-panel" hidden' in html
    assert 'id="feed-saved"' in html


def test_feed_js_keeps_near_in_the_url_so_it_survives_a_view_switch():
    assert "'near', 'radius'" in FEED_JS
    assert "p.delete('radius')" in FEED_JS                 # miles never travel without a place
    assert "showPanel(" in FEED_JS and "localStorage.setItem('feed.filters'" in FEED_JS


def test_card_photo_is_a_lazy_no_referrer_hotlink_only_over_https():
    assert 'referrerpolicy="no-referrer"' in CARD_JS and 'loading="lazy"' in CARD_JS
    assert re.search(r"https:\\\/\\\/|\^https:", CARD_JS)


def test_starter_searches_use_real_category_keys():
    store = (WEB / "static/site/platform/finds_store.js").read_text()
    block = store.split("export const STARTERS", 1)[1].split("];", 1)[0]
    for key in re.findall(r"category: '([a-z_]+)'", block):
        assert key in ("seating_furniture", "vehicles", "kitchen_restaurant", "medical_equipment", "tools_shop",
                       "computers_electronics", "general_merchandise", "lab_test_equipment", "fitness_equipment",
                       "lawn_landscaping", "av_equipment", "comms_radios", "musical_instruments", "collectibles_jewelry", "other"), key
    assert "STARTERS" in (WEB / "static/deals/save_search.js").read_text()
    assert 'id="finds-starters"' in (WEB / "templates/platform_finds.html").read_text()

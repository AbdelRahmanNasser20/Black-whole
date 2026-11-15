"""/deals has three views over the same filters: cards, a paged table, and a map (MapLibre GL)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _html():
    return TestClient(app).get("/deals").text


def test_three_view_controls_and_their_targets():
    html = _html()
    seg = html.split('id="feed-view-toggle"', 1)[1].split("</div>", 1)[0]
    for view in ("cards", "table", "map"):
        assert f'data-view="{view}"' in seg, view
    for el_id in ("feed-grid", "feed-table", "feed-pager", "feed-map", "feed-map-panel"):
        assert f'id="{el_id}"' in html, el_id


def test_slot_for_the_save_search_button_ships_empty():
    assert '<div id="feed-actions"></div>' in _html()


def test_map_style_url_reaches_the_page(monkeypatch):
    assert 'data-style-url="https://tiles.openfreemap.org/styles/positron"' in _html()
    web = __import__("automation.web.app", fromlist=["x"])
    monkeypatch.setitem(web.templates.env.globals, "map_style_url", "https://example.test/style.json")
    assert 'data-style-url="https://example.test/style.json"' in _html()


def test_views_module_exports_the_renderers_for_reuse():
    js = (STATIC / "deals/views.js").read_text()
    for name in ("renderCards", "renderTable", "renderPager", "mountMap"):
        assert f"export function {name}" in js or f"export const {name}" in js, name
    assert TestClient(app).get("/static/deals/views.js").status_code == 200


def test_glmap_is_a_pinned_maplibre_with_the_shared_map_handle():
    js = (STATIC / "ui/glmap.js").read_text()
    assert "maplibre-gl/4.7.1/" in js and "cdnjs.cloudflare.com" in js
    for method in ("setPoints", "fit", "onViewport", "inBounds", "bboxParam", "invalidateSize", "visibleCount"):
        assert method in js, method
    assert "cluster: true" in js
    assert TestClient(app).get("/static/ui/glmap.js").status_code == 200


def test_deals_map_uses_glmap_not_leaflet():
    js = (STATIC / "deals/map.js").read_text()
    assert "../ui/glmap.js" in js
    assert "admin_map.js" not in js and "AdminMap" not in js


def test_map_failure_leaves_a_plain_message():
    js = (STATIC / "deals/map.js").read_text()
    assert "The map could not load. The cards and the table still work." in js


def test_feed_keeps_the_view_in_the_url_and_in_local_storage():
    js = (STATIC / "deals/feed.js").read_text()
    assert "'feed.view'" in js
    assert "list: 'cards'" in js          # old ?view=list links still open the cards
    assert "renderTable" in js and "renderPager" in js


# --- the table pager and the anonymous depth cap (scrape guard) ---

def _lots(monkeypatch, viewer=None, per_page=50, total=20000):
    web = __import__("automation.web.app", fromlist=["x"])
    shared = {"rows": [], "total": total, "page": 1, "per_page": per_page, "pages": -(-total // per_page)}
    monkeypatch.setattr(web.public_deals, "fetch_page", lambda **kw: shared)
    monkeypatch.setattr(web.public_deals, "add_platform_extras", lambda rows, uid: None)
    monkeypatch.setattr(web, "_platform_viewer", lambda request: viewer)
    return web, shared


def test_anonymous_lots_payload_names_the_last_page_it_may_read(monkeypatch):
    web, shared = _lots(monkeypatch)
    body = TestClient(app).get("/deals/api/lots?per_page=50").json()
    assert body["max_page"] == web.scrape_guard.ANON_MAX_ROWS // 50 == 20
    assert "max_page" not in shared          # the page dict is shared: never written to


def test_max_page_follows_the_served_page_size(monkeypatch):
    web, _ = _lots(monkeypatch, per_page=25)
    assert TestClient(app).get("/deals/api/lots").json()["max_page"] == 40


def test_signed_in_viewer_and_guard_off_get_no_cap(monkeypatch):
    web, _ = _lots(monkeypatch, viewer={"user_id": "u"})
    assert "max_page" not in TestClient(app).get("/deals/api/lots?per_page=50").json()
    web, _ = _lots(monkeypatch)
    monkeypatch.setenv("SCRAPE_GUARD_ENABLED", "0")
    assert "max_page" not in TestClient(app).get("/deals/api/lots?per_page=50").json()


def test_pager_and_feed_honour_the_cap_and_the_last_page():
    views = (STATIC / "deals/views.js").read_text()
    feed = (STATIC / "deals/feed.js").read_text()
    assert "max_page" in views                  # the pager stops at the cap
    assert "lastGoodPage" in feed               # a 401 goes back to the last page that loaded
    assert "body.page > body.pages" in feed     # a page past the end clamps to the last page
    assert "st.view !== view" in feed           # a late answer from the old view is dropped


def test_map_popup_lists_every_lot_at_the_point_and_survives_tile_errors():
    js = (STATIC / "ui/glmap.js").read_text()
    assert "POPUP_MAX" in js and "more" in js
    assert "(e.features || []).map((f) => f.properties.popup)" in js   # all pins under the click, not the first
    assert "gl-popup-item" in js
    assert "styledata" in js and "sourceId" in js

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

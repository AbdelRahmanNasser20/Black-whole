"""Public /deals feed shell (plan §6): the ids Workstream D's map builds against, the shared-shell assets,
the server-shipped skeleton, and the hard rules (noindex, no <img>, no storefront chrome, no storage_note)."""
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_deals_page_shell():
    html = TestClient(app).get("/deals").text
    assert "<title>Surplus Radar" in html
    # §6 "Produces" contract — D's map.js queries these ids
    for el_id in ("feed-q", "feed-chips", "feed-controls", "feed-sort", "feed-state", "feed-view-toggle",
                  "feed-count", "feed-filters", "feed-grid", "feed-more", "feed-rail", "feed-map", "feed-map-panel"):
        assert f'id="{el_id}"' in html, el_id
    assert 'name="robots" content="noindex' in html
    assert "/static/deals/feed.js" in html and "/static/ui/state.js" in html
    assert "/static/deals_public.js" not in html and "/static/deals_public.css" not in html
    # server-shipped skeleton so the first paint is the grid's twin, not an empty box
    assert 'class="sk-card"' in html
    assert 'id="feed-grid" class="grid feed-grid" data-state="loading"' in html
    # portfolio "what this is" block stays, collapsed
    assert 'id="feed-about"' in html
    # hard rules: no chair-storefront chrome, no photos, no private fields, no "Loading…" text
    assert "Sell Your Chairs" not in html and "<img" not in html
    assert "storage_note" not in html
    assert "Loading…" not in html


def test_deals_feed_assets_are_served():
    c = TestClient(app)
    assert c.get("/static/deals/feed.js").status_code == 200
    assert c.get("/static/deals/feed.css").status_code == 200
    assert c.get("/static/deals_public.js").status_code == 404
    assert c.get("/static/deals_public.css").status_code == 404

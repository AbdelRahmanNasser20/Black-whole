"""/deals v2: smaller cards, the results box scrolls on its own, a radius slider, browser stars,
similar lots as a card row, gzip at the origin."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from automation.web.app import app

WEB = Path("automation/web")
TEMPLATE = (WEB / "templates/deals_public.html").read_text()
FEED_CSS = (WEB / "static/deals/feed.css").read_text()
FEED_JS = (WEB / "static/deals/feed.js").read_text()
CARD_JS = (WEB / "static/ui/card.js").read_text()
STARS_JS = (WEB / "static/ui/stars.js").read_text()
LOT_TEMPLATE = (WEB / "templates/deal_lot.html").read_text()
DETAIL_JS = (WEB / "static/deals/detail.js").read_text()


def test_results_scroll_in_their_own_box_under_the_controls():
    body = TEMPLATE.split("{% block content %}", 1)[1]
    assert body.index('id="feed-controls"') < body.index('id="feed-scroll"') < body.index('id="feed-grid"')
    assert "--feed-scroll-top" in FEED_CSS and "overflow-y: auto" in FEED_CSS.split(".feed-scroll {", 1)[1][:200]
    assert "sizeScrollBox" in FEED_JS and "ResizeObserver" in FEED_JS


def test_cards_are_denser():
    rule = FEED_CSS.split(".feed-grid {", 1)[1].split("}", 1)[0]
    assert "auto-fill" in rule and "minmax(200px" in rule


def test_radius_is_a_slider_with_a_live_label():
    assert 'type="range" id="feed-radius"' in TEMPLATE and 'id="feed-radius-out"' in TEMPLATE
    assert "feed-radius-out" in FEED_JS


def test_every_card_gets_a_star_and_carries_its_row():
    assert 'class="card-star"' in CARD_JS and "data-star-id" in CARD_JS and "data-row=" in CARD_JS
    assert "pf.stars.v1" in STARS_JS                 # the same key merge.js imports after sign-in
    assert 'id="feed-fav-toggle"' in TEMPLATE and "setFavMode" in FEED_JS
    assert "/platform/signup" in TEMPLATE.split('id="feed-fav-note"', 1)[1][:300]   # email path = an account


def test_star_id_is_stable_from_the_lot_url():
    # The id is "<source>:<key>" with the key read off the lot page path, the one stable public handle.
    assert "lastIndexOf('-' + source + '-')" in STARS_JS


def test_similar_lots_are_a_horizontal_card_row_with_a_crawler_copy():
    assert 'id="similar" class="similar-row"' in LOT_TEMPLATE and "<noscript>" in LOT_TEMPLATE
    assert 'data-self="{{ lot.path }}"' in LOT_TEMPLATE
    assert "scroll-snap-type: x mandatory" in (WEB / "static/deals/detail.css").read_text()
    assert "r.viewer_url !== self" in DETAIL_JS and "slice(0, 12)" in DETAIL_JS


def test_origin_gzips_large_json(monkeypatch):
    from automation.web import platform_api
    monkeypatch.setattr(platform_api, "sites", lambda rows: {"ok": True, "sites": [{"key": f"s{i}", "name": "x" * 40} for i in range(60)]})
    r = TestClient(app).get("/platform/api/sites", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(json.loads(r.content)["sites"]) == 60   # the test client inflates; the body is intact


def test_page_still_renders():
    html = TestClient(app).get("/deals").text
    assert 'id="feed-scroll"' in html and 'id="feed-fav-toggle"' in html
    # stars.js is imported by feed.js and card.js only: a second <script> tag would make a second module
    # instance, two click listeners, and every star click would toggle twice (seen 2026-11-19).
    assert "stars.js" not in html and "from '../ui/stars.js'" in FEED_JS

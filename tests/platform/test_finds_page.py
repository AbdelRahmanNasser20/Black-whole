"""/platform/finds renders for a visitor with no account and for a signed-in user; the Save button is wired."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from automation.web import platform_auth

WEB = Path(__file__).resolve().parents[2] / "automation" / "web"
ME = {"user_id": "11111111-1111-1111-1111-111111111111", "email": "me@x.test", "onboarded_at": "2026-10-01"}


def _client():
    from automation.web.app import app
    return TestClient(app)


def test_finds_page_works_with_no_account(monkeypatch):
    monkeypatch.setattr(platform_auth, "maybe_user", lambda request: None)
    r = _client().get("/platform/finds", follow_redirects=False)
    assert r.status_code == 200                                   # never a redirect to sign-in
    html = r.text
    assert 'data-signed-in="0"' in html and "Kept in this browser" in html
    for el in ('id="finds-add"', 'id="finds-tabs"', 'id="finds-cards"', 'id="finds-table"', 'id="finds-map"',
               'data-view="cards"', 'data-view="table"', 'data-view="map"', 'id="finds-alert"', 'id="finds-delete"'):
        assert el in html, el
    assert 'content="noindex,nofollow"' in html
    assert "/static/site/platform/finds.js?v=" in html and "data-style-url=" in html
    assert "private" not in r.headers.get("cache-control", "")
    assert "me@x.test" not in html


def test_finds_page_signed_in_is_private(monkeypatch):
    monkeypatch.setattr(platform_auth, "maybe_user", lambda request: dict(ME))
    r = _client().get("/platform/finds")
    assert r.status_code == 200 and 'data-signed-in="1"' in r.text and "me@x.test" in r.text
    assert r.headers["cache-control"] == "private, no-store"


def test_finds_page_survives_an_auth_lookup_failure(monkeypatch):
    def boom(request):
        raise RuntimeError("db down")
    monkeypatch.setattr(platform_auth, "maybe_user", boom)
    r = _client().get("/platform/finds")
    assert r.status_code == 200 and 'data-signed-in="0"' in r.text


def test_not_onboarded_user_gets_the_browser_mode(monkeypatch):
    monkeypatch.setattr(platform_auth, "maybe_user", lambda request: {**ME, "onboarded_at": None})
    assert 'data-signed-in="0"' in _client().get("/platform/finds").text


def test_deals_page_mounts_the_save_button_module(monkeypatch):
    html = (WEB / "templates" / "deals_public.html").read_text()
    assert '<div id="feed-actions"></div>' in html
    assert "/static/deals/save_search.js?v=" in html
    js = (WEB / "static" / "deals" / "save_search.js").read_text()
    assert "feed:params" in js and "feed-actions" in js and "/platform/api/finds" in js and "addPending" in js


def test_platform_deals_page_has_a_save_button_and_nav_link():
    html = (WEB / "templates" / "platform_deals.html").read_text()
    assert 'id="pd-save"' in html and "/static/site/platform/save_feed.js?v=" in html
    assert 'href="/platform/finds"' in (WEB / "templates" / "_platform_nav.html").read_text()


def test_browser_store_contract():
    js = (WEB / "static" / "site" / "platform" / "finds_store.js").read_text()
    assert "'pf.pending_searches.v1'" in js and "'pf.stars.v1'" in js
    # the same filter names the server model and the public feed take
    from automation.web import platform_finds
    names = re.search(r"export const FILTERS = \[(.*?)\]", js).group(1)
    assert tuple(n.strip().strip("'") for n in names.split(",")) == platform_finds.FILTERS
    # the star key is the one the /platform page writes
    assert "const STAR_KEY = 'pf.stars.v1'" in (WEB / "static" / "site" / "platform" / "auctions.js").read_text()


def test_signed_in_pages_run_the_merge():
    merge = (WEB / "static" / "site" / "platform" / "merge.js").read_text()
    assert "/platform/api/finds" in merge and "/platform/api/saved/import" in merge
    for page in ("platform_alerts.html", "platform_saved.html"):
        assert "/static/site/platform/merge.js" in (WEB / "templates" / page).read_text(), page
    assert "mergeAll" in (WEB / "static" / "site" / "platform" / "finds.js").read_text()


def test_anonymous_alert_goes_to_sign_in_and_reads_only_public_endpoints():
    js = (WEB / "static" / "site" / "platform" / "finds.js").read_text()
    assert "location.href = '/platform/signup'" in js and "want_alert" in js
    assert "/deals/api/lots?" in js                               # the public feed, policy applied server-side
    assert "innerHTML" in js and "esc(" in js                     # every scraped field goes through esc()

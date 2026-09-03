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
    assert 'id="sr-q"' in html and 'id="sr-pager-top"' in html and 'id="sr-side"' in html
    assert 'name="robots" content="noindex' in html
    assert "/static/deals_public.js" in html and "/static/admin_map.js" in html
    # portfolio landing block + no chair-storefront chrome
    assert 'id="sr-about"' in html
    assert "Sell Your Chairs" not in html and "<img" not in html

"""GET /platform/how-it-works — the pipeline page. No database on the request path,
runtime numbers only, honest copy, linked from the platform nav."""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import auth as auth_svc
from automation.web import platform_api, public_deals, public_sold
from automation.web.app import app

app_module = sys.modules["automation.web.app"]
TEMPLATE = (Path("automation/web") / "templates/platform_how.html").read_text()


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def no_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GET /platform/how-it-works must not touch the database")
    for mod, name in ((public_deals, "fetch_facets"), (public_deals, "fetch_page"), (inventory, "list_public"),
                      (app_module.db, "fetch_all"), (app_module.db, "fetch_one")):
        monkeypatch.setattr(mod, name, boom)
    monkeypatch.setattr(platform_api, "stats", lambda: None)
    monkeypatch.setattr(public_sold, "headline", lambda: None)


def _client():
    return TestClient(app, base_url="https://testserver")


def test_renders_without_db_and_without_numbers(no_db):
    r = _client().get("/platform/how-it-works")
    assert r.status_code == 200
    html = r.text
    assert "<h1 class=\"pf-headline\">How it works.</h1>" in html
    assert 'href="/platform#app"' in html and 'href="/platform/signup"' in html
    assert "closed auctions with the final price on record" not in html   # stats None: no number
    assert f"{len(platform_api.SITES)}<span class=\"pf-fact-of\"> of " in html


def test_stats_render_when_present(no_db, monkeypatch):
    monkeypatch.setattr(platform_api, "stats", lambda: {"sold_outcomes": 222000, "open_now": 23758})
    html = _client().get("/platform/how-it-works").text
    assert "222,000" in html and "23,758" in html


def test_no_number_is_baked_into_the_template():
    body = TEMPLATE.split("{% block content %}", 1)[1]
    words = re.sub(r"{{.*?}}|{%.*?%}|<[^>]+>|style=\"[^\"]*\"", " ", body)
    words = words.replace("R2", "")        # a product name, not a figure
    assert not re.search(r"\d", words), "every figure must come from the context"


def test_honest_copy():
    text = TEMPLATE.lower()
    for banned in ("all sites", "every auction site in the country", "customers", "testimonial", "guarantee"):
        assert banned not in text, banned
    assert "plain-english search" in text and "does not do yet" in text


def test_linked_from_the_platform_nav(no_db):
    assert 'href="/platform/how-it-works"' in _client().get("/platform").text
    assert 'href="/platform/how-it-works"' in (Path("automation/web") / "templates/_platform_nav.html").read_text()


def test_not_in_storefront_nav(no_db):
    assert "/platform/how-it-works" not in (Path("automation/web") / "templates/_public_base.html").read_text()

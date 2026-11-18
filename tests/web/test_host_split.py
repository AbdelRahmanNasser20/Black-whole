"""Host split: the chair storefront and the liquidator platform on two hosts, one app.

Guards: off by default (no PLATFORM_HOST → every URL serves as before); on, the
platform host serves the landing at "/", chair URLs on it bounce to the chair host,
platform URLs on the chair host bounce to the platform host, local hosts never
bounce, each host gets its own robots/sitemap, and the query string survives."""
import pytest
from fastapi.testclient import TestClient

from automation import config
from automation.web import host_split
from automation.web.app import app


@pytest.fixture
def split(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_HOST", "surpluslens.test")
    monkeypatch.setattr(config, "PLATFORM_BASE_URL", "https://surpluslens.test")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://black-whole.com")
    yield


def _client(host: str) -> TestClient:
    return TestClient(app, base_url=f"https://{host}", follow_redirects=False)


def test_off_by_default_passes_everything(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_HOST", "")
    assert host_split.decide("black-whole.com", "/deals") == ("pass", "/deals")
    assert host_split.decide("anything.example", "/") == ("pass", "/")


def test_decisions(split):
    d = host_split.decide
    assert d("surpluslens.test", "/") == ("rewrite", "/platform")
    assert d("www.surpluslens.test", "/") == ("rewrite", "/platform")
    assert d("surpluslens.test", "/deals/lot/abc") == ("pass", "/deals/lot/abc")
    assert d("surpluslens.test", "/static/site/x.css") == ("pass", "/static/site/x.css")
    assert d("surpluslens.test", "/BingSiteAuth.xml") == ("pass", "/BingSiteAuth.xml")
    assert d("surpluslens.test", "/robots.txt") == ("rewrite", "/platform/robots.txt")
    assert d("surpluslens.test", "/sitemap.xml") == ("rewrite", "/sitemap-index.xml")
    assert d("surpluslens.test", "/listings") == ("redirect", "https://black-whole.com/listings")
    assert d("surpluslens.test", "/admin") == ("redirect", "https://black-whole.com/admin")
    assert d("black-whole.com", "/platform/finds") == ("redirect", "https://surpluslens.test/platform/finds")
    assert d("black-whole.com", "/deals") == ("redirect", "https://surpluslens.test/deals")
    assert d("black-whole.com", "/listings") == ("pass", "/listings")
    assert d("black-whole.com", "/dealsx") == ("pass", "/dealsx")   # prefix means a path segment
    assert d("127.0.0.1", "/deals") == ("pass", "/deals")
    assert d("localhost", "/listings") == ("pass", "/listings")


def test_chair_host_redirects_platform_paths_with_query(split):
    r = _client("black-whole.com").get("/deals?category=vehicles&state=GA")
    assert r.status_code == 301
    assert r.headers["location"] == "https://surpluslens.test/deals?category=vehicles&state=GA"


def test_platform_host_redirects_chair_paths(split):
    r = _client("surpluslens.test").get("/chairs/atlanta-ga")
    assert r.status_code == 301
    assert r.headers["location"] == "https://black-whole.com/chairs/atlanta-ga"


def test_platform_host_serves_landing_at_root(split, monkeypatch):
    from automation.web import platform_api, public_sold
    monkeypatch.setattr(platform_api, "stats", lambda: None)
    monkeypatch.setattr(public_sold, "headline", lambda: None)
    r = _client("surpluslens.test").get("/")
    assert r.status_code == 200
    assert "pf-hero" in r.text


def test_platform_host_robots_points_at_its_own_sitemap(split):
    r = _client("surpluslens.test").get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap: https://surpluslens.test/sitemap-index.xml" in r.text
    assert "black-whole.com" not in r.text
    assert "Disallow: /admin" not in r.text          # nothing of the chair site is on this host


def test_chair_host_robots_unchanged(split):
    r = _client("black-whole.com").get("/robots.txt")
    assert "Sitemap: https://black-whole.com/sitemap.xml" in r.text


def test_sitemap_index_on_platform_host_omits_storefront(split, monkeypatch):
    from automation.web import deal_sitemap
    monkeypatch.setattr(deal_sitemap, "urls", lambda: [("/deals/lot/x", None)])
    xml = deal_sitemap.index_xml("https://surpluslens.test", storefront_url="https://black-whole.com")
    assert "https://surpluslens.test/sitemaps/deals-1.xml" in xml
    assert "sitemap.xml" not in xml.replace("sitemaps/deals-1.xml", "")
    same = deal_sitemap.index_xml("https://black-whole.com", storefront_url="https://black-whole.com")
    assert "https://black-whole.com/sitemap.xml" in same


def test_brand_global_renders(split, monkeypatch):
    from automation.web import platform_api, public_sold
    from automation.web.app import templates
    monkeypatch.setattr(platform_api, "stats", lambda: None)
    monkeypatch.setattr(public_sold, "headline", lambda: None)
    monkeypatch.setitem(templates.env.globals, "platform_brand", "Surplus Lens")
    r = _client("surpluslens.test").get("/")
    assert "<title>Auction intelligence for liquidators | Surplus Lens</title>" in r.text
    assert "◉ Surplus Lens" in r.text

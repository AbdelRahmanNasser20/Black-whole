"""Typed short links (`/cl`, `/cl/<lot>`) redirect to a UTM-tagged URL so
Craigslist/phone/print traffic lands in site_visits with a source. No DB."""
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from automation.web import short_links
from automation.web.app import app

client = TestClient(app)


def _qs(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.mark.parametrize("code", list(short_links.CHANNELS))
def test_landing_redirect_carries_utm(code):
    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert urlsplit(loc).path == "/"
    src, medium = short_links.CHANNELS[code]
    assert _qs(loc) == {"utm_source": src, "utm_medium": medium, "utm_campaign": f"short_{code}"}


def test_lot_redirect_goes_to_lot_page():
    r = client.get("/cl/gd-28859-2863", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert urlsplit(loc).path == "/listings/gd-28859-2863"
    assert _qs(loc)["utm_source"] == "craigslist"


def test_unknown_code_is_not_a_route():
    assert client.get("/zz", follow_redirects=False).status_code == 404


def test_short_url_text_is_typeable():
    assert short_links.short_url("cl").endswith("black-whole.com/cl")
    assert "://" not in short_links.short_url("cl")
    assert short_links.short_url("cl", "31225").endswith("/cl/31225")
    with pytest.raises(KeyError):
        short_links.short_url("nope")


def test_craigslist_body_includes_short_link():
    from automation.craigslist import CraigslistListing, deterministic_varier
    _, body = deterministic_varier(
        CraigslistListing(chair_type="banquet chairs", quantity="300", price=20), "atlanta", "Atlanta")
    assert "black-whole.com/cl" in body

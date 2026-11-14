"""GET /BingSiteAuth.xml — Bing Webmaster Tools ownership file."""
from fastapi.testclient import TestClient

from automation.web.app import app


def test_bing_site_auth_served():
    r = TestClient(app, base_url="https://testserver").get("/BingSiteAuth.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<user>F1622AF247E88937142BFA0511079194</user>" in r.text
    assert r.text.startswith('<?xml version="1.0"?>')

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


ROW = {"lot_id": "9006", "title": "Mauve Banquet Chairs", "status": "owned",
       "hero_image_url": "https://cdn.example.com/9006.jpg", "image_urls": [],
       "locations": None, "govdeals_password": "secret", "buyer_cert_path": None}


def test_inventory_with_stats_is_one_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(inventory, "list_with_stats",
                        lambda status=None: (calls.append(status) or
                                             {"items": [dict(ROW)], "stats": {"lots": 1, "chairs": 442, "cities": 1, "moved": 0}}))
    monkeypatch.setattr(inventory, "list_all", lambda status=None: pytest.fail("must not use list_all"))
    body = TestClient(app).get("/api/inventory?with_stats=1&status=owned").json()
    assert calls == ["owned"]
    assert body["stats"]["chairs"] == 442
    assert body["items"][0]["govdeals_password_set"] is True
    assert "govdeals_password" not in body["items"][0]


def test_inventory_without_flag_is_unchanged(monkeypatch):
    monkeypatch.setattr(inventory, "list_all", lambda status=None: [dict(ROW)])
    body = TestClient(app).get("/api/inventory").json()
    assert set(body) == {"items"}

"""Every chair inquiry carries a ZIP (migration 035).

A sell lead = where the chairs sit, a buy lead = where they would go. Without
it the lead cannot be routed (no freight lane, no nearest lot), so the forms
require it and `create_inquiry` enforces it — except for the site-tagged
platform / liquidator request-access leads, which never asked for one.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web.app import app


@pytest.fixture
def client():
    return TestClient(app, base_url="https://testserver")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "automation" / "web" / "templates"


class _FakeCur:
    def fetchone(self):
        return {"id": 1}


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur()

    def commit(self):
        pass


@pytest.fixture
def sink(monkeypatch):
    seen = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(seen))
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: {"id": i})
    monkeypatch.setattr(inventory.attribution_mod, "columns_ready", lambda table: False)
    return seen


# ───────────────────────────── clean_zip ─────────────────────────────

@pytest.mark.parametrize("raw, want", [
    ("85054", "85054"), (" 85054 ", "85054"), ("85054-1234", "85054"),
    ("3003", None), ("", None), (None, None), ("abcde", None),
])
def test_clean_zip(raw, want):
    assert inventory.clean_zip(raw) == want


# ───────────────────────────── create_inquiry ────────────────────────

@pytest.mark.parametrize("zip_code", [None, "", "3003", "zip"])
def test_chair_inquiry_without_zip_is_rejected(sink, zip_code):
    with pytest.raises(ValueError, match="ZIP"):
        inventory.create_inquiry(kind="sell", name="Brenda", email="b@x.com",
                                 message="Gold metal banquet chairs", zip_code=zip_code)
    assert "sql" not in sink


def test_zip_stored_in_column_once_migrated(sink, monkeypatch):
    monkeypatch.setattr(inventory, "inquiries_have_zip_column", lambda: True)
    inventory.create_inquiry(kind="sell", name="Brenda", phone="928-710-4336",
                             message="Gold metal banquet chairs", zip_code="86301-1234")
    assert "zip_code" in sink["sql"]
    assert sink["params"][sink["sql"].split("(")[1].split(")")[0].split(", ").index("zip_code")] == "86301"
    assert "ZIP" not in sink["params"][6]            # message untouched
    assert sink["sql"].count("%s") == len(sink["params"])


def test_zip_folded_into_message_before_migration(sink, monkeypatch):
    monkeypatch.setattr(inventory, "inquiries_have_zip_column", lambda: False)
    inventory.create_inquiry(kind="buy", name="Annie", email="a@x.com",
                             message="Need 10 tables ASAP", zip_code="30318")
    assert "zip_code" not in sink["sql"]
    assert sink["params"][6] == "ZIP 30318\n\nNeed 10 tables ASAP"
    assert sink["sql"].count("%s") == len(sink["params"])


@pytest.mark.parametrize("tag", ["[PLATFORM PAGE]", "[LIQUIDATORS PAGE]"])
def test_site_tagged_leads_do_not_need_a_zip(sink, tag):
    inventory.create_inquiry(kind="buy", name="Test Person", email="t@x.com",
                             message=f"{tag} I am: liquidator | Company: Example")
    assert "zip_code" not in sink["sql"]
    assert sink["params"][6].startswith(tag)


def test_zip_column_probe_caches_only_positive(monkeypatch):
    inventory._inquiries_zip_ready = False
    calls = {"n": 0}

    def fetch_one(sql, *a):
        calls["n"] += 1
        return None if calls["n"] == 1 else {"?column?": 1}
    monkeypatch.setattr(inventory.db, "fetch_one", fetch_one)
    assert inventory.inquiries_have_zip_column() is False   # absent → not cached
    assert inventory.inquiries_have_zip_column() is True    # present → cached
    assert inventory.inquiries_have_zip_column() is True
    assert calls["n"] == 2
    inventory._inquiries_zip_ready = False


# ───────────────────────────── endpoint ──────────────────────────────

def test_contact_passes_zip(client, monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 1, "kind": "sell", "name": "Brenda", "zip_code": "86301"}
    monkeypatch.setattr(inventory, "create_inquiry", fake_create)
    r = client.post("/contact", json={"kind": "sell", "name": "Brenda", "email": "b@x.com",
                                      "zip_code": " 86301 ", "message": "150 gold banquet chairs"})
    assert r.status_code == 200
    assert captured["zip_code"] == "86301"


def test_contact_missing_zip_is_a_400(client, monkeypatch):
    real = inventory.create_inquiry

    def real_rules(**kw):
        return real(**kw)   # hits the ValueError before any DB call
    monkeypatch.setattr(inventory, "create_inquiry", real_rules)
    r = client.post("/contact", json={"kind": "sell", "name": "Brenda", "email": "b@x.com",
                                      "message": "150 gold banquet chairs"})
    assert r.status_code == 400
    assert "ZIP" in r.json()["detail"]


def test_lead_ping_shows_zip():
    src = (ROOT / "automation" / "web" / "app.py").read_text()
    body = src.split("async def _notify_new_inquiry", 1)[1].split("async def ", 1)[0]
    assert "ZIP {row['zip_code']}" in body


# ───────────────────────────── forms ─────────────────────────────────

@pytest.mark.parametrize("template", ["sell.html", "about.html", "landing.html", "listing_detail.html"])
def test_every_chair_form_requires_a_zip(template):
    html = (TEMPLATES / template).read_text()
    form = html.split('id="contact-form"', 1)[1].split("</form>", 1)[0]
    assert 'name="zip_code"' in form
    zip_input = form.split('name="zip_code"', 1)[1].split(">", 1)[0]
    assert "required" in zip_input
    assert 'pattern="[0-9]{5}"' in zip_input
    assert 'inputmode="numeric"' in zip_input
    assert 'autocomplete="postal-code"' in zip_input


def test_site_js_blocks_a_bad_zip_client_side():
    js = (ROOT / "automation" / "web" / "static" / "site" / "site.js").read_text()
    assert "[name=\"zip_code\"]" in js
    assert "zip.length !== 5" in js


def test_admin_card_shows_zip():
    js = (ROOT / "automation" / "web" / "static" / "admin" / "inquiries.js").read_text()
    assert "q.zip_code" in js

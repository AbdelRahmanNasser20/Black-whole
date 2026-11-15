"""The "early access" email strip: one email field on every public page, stored as a
`site_updates` subscriber. Nothing is sent, so the copy never promises a schedule."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import auth as auth_svc
from automation.web import rate_limit
from automation.web.app import app

STATIC = Path("automation/web/static")
TEMPLATES = Path("automation/web/templates")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    rate_limit.reset()
    yield
    auth_svc.reset_caches()
    rate_limit.reset()


class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    """Answers the duplicate lookup with `existing`, and records every statement."""
    def __init__(self, log, existing):
        self.log, self.existing = log, existing

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT"):
            return _Cur(self.existing)
        return _Cur({"id": 42})

    def commit(self):
        pass


def _patch(monkeypatch, existing):
    log = []
    monkeypatch.setattr(inventory, "connect", lambda: _Conn(log, existing))
    monkeypatch.setattr(inventory, "_subscribers_has_unsub_token", lambda: False)
    monkeypatch.setattr(inventory, "get_subscriber", lambda i: {"id": i, "email": "a@b.co"})
    return log


def test_email_only_signup_is_stored_with_its_source(monkeypatch):
    log = _patch(monkeypatch, existing=None)
    row = inventory.create_subscriber(email="A@B.co", source="site_updates")
    assert row["id"] == 42 and not row.get("duplicate")
    insert = [(s, p) for s, p in log if s.lstrip().upper().startswith("INSERT")]
    assert len(insert) == 1 and "site_updates" in insert[0][1]


def test_same_email_twice_keeps_one_row(monkeypatch):
    # Also keeps an unsubscribed person off the list: their old row comes back untouched.
    log = _patch(monkeypatch, existing={"id": 7, "email": "a@b.co", "status": "unsubscribed"})
    row = inventory.create_subscriber(email="A@B.co", source="site_updates")
    assert row["id"] == 7 and row["duplicate"] is True and row["status"] == "unsubscribed"
    assert not [s for s, _ in log if s.lstrip().upper().startswith("INSERT")]
    assert "lower(email) = lower(%s)" in log[0][0] and log[0][1] == ("A@B.co",)


def test_other_sources_are_not_deduped(monkeypatch):
    # The chair alert form is a standing order with its own details; a repeat is a new row, as before.
    log = _patch(monkeypatch, existing={"id": 7})
    inventory.create_subscriber(email="a@b.co", source="site_listings")
    assert [s for s, _ in log if s.lstrip().upper().startswith("INSERT")]


def _web(monkeypatch, rows):
    web = __import__("automation.web.app", fromlist=["x"])
    pings = []

    async def fake_notify(row):
        pings.append(row["id"])
    monkeypatch.setattr(web, "_notify_new_subscriber", fake_notify)
    it = iter(rows)
    monkeypatch.setattr(inventory, "create_subscriber", lambda **kw: next(it))
    return web, pings


def test_strip_signup_is_rate_limited_per_ip_and_a_repeat_sends_no_ping(monkeypatch):
    web, pings = _web(monkeypatch, [{"id": 1}] + [{"id": 1, "duplicate": True}] * 50)
    c = TestClient(app)
    codes = [c.post("/subscribe", json={"email": "a@b.co", "source": "site_updates"},
                    headers={"cf-connecting-ip": "203.0.113.9"}).status_code
             for _ in range(web.SUBSCRIBE_PER_IP_LIMIT + 1)]
    assert codes[:-1] == [200] * web.SUBSCRIBE_PER_IP_LIMIT and codes[-1] == 429
    assert pings == [1]
    # another address is not affected
    assert c.post("/subscribe", json={"email": "c@d.co", "source": "site_updates"},
                  headers={"cf-connecting-ip": "203.0.113.10"}).status_code == 200


def test_chair_alert_form_is_not_rate_limited(monkeypatch):
    # A church office or a carrier NAT shares one IP: the chair form keeps its old behaviour.
    web, _ = _web(monkeypatch, [{"id": n} for n in range(1, 60)])
    c = TestClient(app)
    codes = {c.post("/subscribe", json={"email": f"p{n}@b.co", "zip_code": "30301", "source": "site_listings"},
                    headers={"cf-connecting-ip": "203.0.113.20"}).status_code
             for n in range(web.SUBSCRIBE_PER_IP_LIMIT + 5)}
    assert codes == {200}
    # and those posts did not use up the strip's budget for the same IP
    assert c.post("/subscribe", json={"email": "a@b.co", "source": "site_updates"},
                  headers={"cf-connecting-ip": "203.0.113.20"}).status_code == 200


def test_a_rejected_strip_post_does_not_use_the_budget(monkeypatch):
    web, _ = _web(monkeypatch, [{"id": 1}] * 50)
    c = TestClient(app)
    h = {"cf-connecting-ip": "203.0.113.30"}
    for bad in ("", "not-an-email", "   "):
        for _ in range(web.SUBSCRIBE_PER_IP_LIMIT):
            assert c.post("/subscribe", json={"email": bad, "source": "site_updates"}, headers=h).status_code == 400
    assert c.post("/subscribe", json={"email": "a@b.co", "source": "site_updates"}, headers=h).status_code == 200


def test_strip_answer_is_the_same_for_new_repeat_and_unsubscribed(monkeypatch):
    # No id: a returned row id would tell a caller whether an address is already on file.
    web, _ = _web(monkeypatch, [{"id": 900}, {"id": 7, "duplicate": True},
                                {"id": 3, "duplicate": True, "status": "unsubscribed"}])
    c = TestClient(app)
    bodies = [c.post("/subscribe", json={"email": e, "source": "site_updates"},
                     headers={"cf-connecting-ip": "203.0.113.40"}).json()
              for e in ("new@b.co", "old@b.co", "gone@b.co")]
    assert bodies == [{"ok": True}] * 3


def test_chair_alert_form_still_gets_its_row_id(monkeypatch):
    _web(monkeypatch, [{"id": 55}])
    r = TestClient(app).post("/subscribe", json={"email": "a@b.co", "zip_code": "30301", "source": "site_listings"})
    assert r.json() == {"ok": True, "id": 55}


def test_strip_success_text_does_not_claim_list_membership():
    js = (STATIC / "site/updates_strip.js").read_text().lower()
    assert "on the list" not in js and "we will be in touch" in js


@pytest.mark.parametrize("path", ["/", "/deals", "/deals/sold", "/platform", "/platform/signup"])
def test_strip_is_on_every_public_page(path, monkeypatch):
    web = __import__("automation.web.app", fromlist=["x"])
    monkeypatch.setattr(web.inventory, "list_public", lambda: [], raising=False)
    monkeypatch.setattr(web.inventory, "stats", lambda: {"lots": 0, "chairs": 0, "cities": 0}, raising=False)
    html = TestClient(app).get(path).text
    assert 'id="updates-strip"' in html, path
    assert html.count('id="updates-strip"') == 1
    assert "/static/site/updates_strip.js" in html


def test_strip_is_on_the_sources_page():
    # /sources reads the DB, so the include is checked on the template.
    assert '{% include "_updates_strip.html" %}' in (TEMPLATES / "sources.html").read_text()


def test_strip_is_not_on_the_admin():
    assert 'id="updates-strip"' not in (TEMPLATES / "index.html").read_text()
    assert "_updates_strip.html" not in (TEMPLATES / "_base.html").read_text()


def test_strip_copy_promises_no_schedule():
    text = (TEMPLATES / "_updates_strip.html").read_text() + (STATIC / "site/updates_strip.js").read_text()
    low = text.lower()
    assert "get early access" in low and "notify me" in low and 'type="email"' in low
    for word in ("weekly", "every week", "daily", "each week", "newsletter"):
        assert word not in low, word
    assert 'value="site_updates"' in text or "'site_updates'" in text
    assert "<img" not in low


def test_strip_assets_are_served():
    c = TestClient(app)
    assert c.get("/static/site/updates_strip.js").status_code == 200
    assert c.get("/static/site/updates_strip.css").status_code == 200

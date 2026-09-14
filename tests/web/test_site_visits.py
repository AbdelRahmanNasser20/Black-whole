"""site_visits: public page views are logged with UTM attribution, bots and
admin paths are skipped, nothing ever blocks a page, and the admin rollup
endpoint returns the documented shape. No DB required."""
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import visits
from automation.web.app import app
import sys
app_module = sys.modules["automation.web.app"]


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SITE_VISITS", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def captured(monkeypatch):
    rows = []
    monkeypatch.setattr(visits, "_runner", lambda fn, row: rows.append(row))
    return rows


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(app_module.inventory, "stats", lambda: {"lots": 0, "chairs": 0, "cities": 0, "moved": 0})
    monkeypatch.setattr(app_module.inventory, "list_public", lambda: [])
    monkeypatch.setattr(app_module.inventory, "list_sold_showcase", lambda: [])


UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


@pytest.mark.parametrize("path,ua,ok", [
    ("/", UA, True),
    ("/listings", UA, True),
    ("/listings/31225", UA, True),
    ("/admin", UA, False),
    ("/api/visits/summary", UA, False),
    ("/static/site/public.css", UA, False),
    ("/listings/31225", "Googlebot/2.1", False),
    ("/listings/31225", "python-requests/2.31", False),
    ("/listings/31225", "", False),
])
def test_should_track(path, ua, ok):
    assert visits.should_track(path, ua) is ok


def test_should_track_only_get():
    assert visits.should_track("/listings", UA, method="POST") is False


def test_build_row_parses_utm_referer_lot_and_hashes_visitor():
    row = visits.build_row(
        "/listings/31225",
        {"utm_source": "apollo", "utm_medium": "email", "utm_campaign": "atl-churches", "x": "1"},
        {"user-agent": UA, "referer": "https://mail.google.com/mail/u/0/", "x-forwarded-for": "8.8.8.8, 10.0.0.1",
         "cf-ipcountry": "US"},
        "10.0.0.1",
    )
    assert row["lot_id"] == "31225"
    assert (row["utm_source"], row["utm_medium"], row["utm_campaign"]) == ("apollo", "email", "atl-churches")
    assert row["referer_host"] == "mail.google.com"
    assert row["country"] == "US"
    assert len(row["visitor"]) == 16 and "8.8.8.8" not in row["visitor"]
    # same person, same day → same hash; different IP → different hash
    again = visits.build_row("/listings/31225", {}, {"user-agent": UA, "x-forwarded-for": "8.8.8.8"}, None)
    other = visits.build_row("/listings/31225", {}, {"user-agent": UA, "x-forwarded-for": "9.9.9.9"}, None)
    assert again["visitor"] == row["visitor"] != other["visitor"]


def test_build_row_clips_long_utm():
    row = visits.build_row("/", {"utm_campaign": "x" * 500}, {"user-agent": UA}, None)
    assert len(row["utm_campaign"]) == 64


def test_public_pages_record_a_view(captured, no_db):
    c = TestClient(app)
    r = c.get("/listings?utm_source=apollo&utm_campaign=nc-churches", headers={"user-agent": UA})
    assert r.status_code == 200
    assert captured and captured[-1]["utm_campaign"] == "nc-churches" and captured[-1]["path"] == "/listings"


def test_bots_and_admin_are_not_recorded(captured, no_db):
    c = TestClient(app)
    c.get("/listings", headers={"user-agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"})
    c.get("/admin", headers={"user-agent": UA})
    assert captured == []


def test_off_switch(captured, no_db, monkeypatch):
    monkeypatch.setenv("SITE_VISITS", "off")
    TestClient(app).get("/listings", headers={"user-agent": UA})
    assert captured == []


def test_insert_failure_backs_off_and_never_raises(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("pooler down")

    monkeypatch.setattr(visits.db, "execute", boom)
    visits._state["down_until"] = 0.0
    row = visits.build_row("/", {}, {"user-agent": UA}, None)
    visits._insert(row)          # swallowed
    visits._insert(row)          # skipped — backoff
    assert len(calls) == 1 and visits._state["down_until"] > 0
    visits._state["down_until"] = 0.0


def test_summary_endpoint_shape(monkeypatch):
    monkeypatch.setattr(visits.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(visits.db, "fetch_one", lambda sql, p=None: {"views": 0, "visitors": 0, "from_email": 0})
    r = TestClient(app).get("/api/visits/summary?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 7
    assert set(body) == {"days", "total", "by_campaign", "by_day", "by_lot", "referers"}

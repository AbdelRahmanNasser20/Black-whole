"""First-touch lead attribution (docs/analytics_leads_plan.md).

DB-free: every write path is monkeypatched, TestClient is built without a
`with` block so the lifespan never runs, Telegram is stubbed. What is
defended here:

  1. `attribution.channel()` buckets raw utm/referrer the way the plan says.
  2. `from_payload()` never raises and clips to 200 chars.
  3. Every lead endpoint hands `attribution=` to its writer.
  4. Writers degrade: no attr_* columns in the INSERT until 022 is applied.
  5. `/event` stores tel:/mailto: clicks as `/_event/<kind>` rows, drops bots,
     rejects unknown kinds and rate-limits.
  6. The report's pure aggregation produces the per-channel funnel.
"""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from automation import attribution, config as config_mod, deposits, freight_log, inventory
from automation import site_settings, stripe_gateway, telegram_alerts
from automation.web import auth as auth_svc
from automation.web import rate_limit, visits
from automation.web.app import app

app_module = sys.modules["automation.web.app"]
report = importlib.import_module("scripts.lead_funnel_report")

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"
ATTR = {"source": "facebook", "medium": "marketplace", "campaign": "short_fb",
        "referrer": "https://m.facebook.com/x/y?z=1", "landing": "/listings/31225"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    rate_limit.reset()
    attribution.reset_schema_cache()
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET", "SITE_VISITS"):
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()

    async def fake_send(text, *, topic=None):
        return True, None
    monkeypatch.setattr(telegram_alerts, "send_message", fake_send)
    yield
    rate_limit.reset()
    attribution.reset_schema_cache()
    auth_svc.reset_caches()


@pytest.fixture
def client():
    return TestClient(app, base_url="https://testserver")


# ───────────────────────────── channel() ─────────────────────────────────────

@pytest.mark.parametrize("source,medium,referrer,expected", [
    ("google", "feed", None, "google_feed"),
    ("google", "shopping", None, "google_feed"),
    ("google", "cpc", None, "google_ads"),
    ("google", None, None, "google_organic"),
    (None, None, "www.google.com", "google_organic"),
    (None, None, "https://www.google.co.uk/search", "google_organic"),
    (None, None, "m.facebook.com", "facebook"),
    (None, None, "l.facebook.com", "facebook"),
    ("facebook", "marketplace", None, "facebook"),
    ("craigslist", "listing", None, "craigslist"),
    (None, None, "phoenix.craigslist.org", "craigslist"),
    (None, None, "www.ebay.com", "ebay"),
    ("apollo", "email", None, "email"),
    ("foo-newsletter", "email", None, "email"),
    (None, None, "black-whole.com", "direct"),
    (None, None, "www.bwliquidation.com", "direct"),
    (None, None, None, "direct"),
    (None, None, "", "direct"),
    (None, None, "chatgpt.com", "ai_assistant"),
    (None, None, "some-blog.example.org", "referral"),
    ("foo", None, None, "utm:foo"),
    ("FaceBook", None, None, "facebook"),
    ("'>\"></script><svg/onload=confirm(1)>", None, None, "utm:scriptsvgonloadconfirm1"),
    ("'\"<>", None, None, "utm:other"),
])
def test_channel_buckets(source, medium, referrer, expected):
    assert attribution.channel(source, medium, referrer) == expected


# ───────────────────────────── from_payload() ────────────────────────────────

def test_from_payload_nested_object():
    attr = attribution.from_payload({"name": "x", "attribution": dict(ATTR)})
    assert attr == {"source": "facebook", "medium": "marketplace", "campaign": "short_fb",
                    "referrer": "m.facebook.com", "landing": "/listings/31225"}


def test_from_payload_flat_keys():
    attr = attribution.from_payload({"utm_source": "Google", "utm_medium": "FEED",
                                     "referrer": "www.google.com", "landing_path": "/"})
    assert attr["source"] == "google" and attr["medium"] == "feed"
    assert attr["referrer"] == "www.google.com" and attr["landing"] == "/"


@pytest.mark.parametrize("payload", [None, "garbage", 42, {}, {"attribution": "nope"},
                                     {"attribution": None}, {"attribution": []}])
def test_from_payload_garbage_is_empty(payload):
    attr = attribution.from_payload(payload)
    assert set(attr) == set(attribution.FIELDS)
    assert attribution.is_empty(attr)


def test_from_payload_clips_and_validates():
    attr = attribution.from_payload({"attribution": {
        "source": "x" * 500, "campaign": " c ", "landing": "listings/1", "referrer": "  ",
    }})
    assert len(attr["source"]) == attribution.MAX_LEN
    assert attr["campaign"] == "c"
    assert attr["landing"] is None            # must start with "/"
    assert attr["referrer"] is None


# ───────────────────────────── schema detection ──────────────────────────────

def test_insert_columns_empty_attr():
    assert attribution.insert_columns("inquiries", None) == ([], [])
    assert attribution.insert_columns("inquiries", {"source": None}) == ([], [])


def test_insert_columns_when_db_down(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no db")
    monkeypatch.setattr(attribution.db, "fetch_one", boom)
    assert attribution.insert_columns("inquiries", {"source": "facebook"}) == ([], [])


def test_insert_columns_when_ready(monkeypatch):
    monkeypatch.setattr(attribution.db, "fetch_one", lambda *a, **k: {"?column?": 1})
    cols, vals = attribution.insert_columns("inquiries", attribution.from_payload({"attribution": ATTR}))
    assert cols == list(attribution.COLUMNS)
    assert vals == ["facebook", "marketplace", "short_fb", "m.facebook.com", "/listings/31225"]


def test_columns_ready_caches_only_positive(monkeypatch):
    answers = iter([None, {"?column?": 1}, None])
    monkeypatch.setattr(attribution.db, "fetch_one", lambda *a, **k: next(answers))
    assert attribution.columns_ready("deposits") is False     # not applied yet
    assert attribution.columns_ready("deposits") is True      # applied
    assert attribution.columns_ready("deposits") is True      # cached, no third probe
    assert attribution.columns_ready("inquiries") is False    # per-table


# ───────────────────────────── writers degrade ───────────────────────────────

class _FakeCur:
    def fetchone(self):
        return {"id": 42}


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


@pytest.mark.parametrize("ready", [True, False])
def test_create_inquiry_attribution_columns(monkeypatch, ready):
    sink = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(sink))
    monkeypatch.setattr(inventory, "get_inquiry", lambda i: {"id": i})
    monkeypatch.setattr(attribution, "columns_ready", lambda table: ready)
    inventory.create_inquiry(kind="buy", name="Jane", email="j@x.com", lot_id="31225",
                             attribution=attribution.from_payload({"attribution": ATTR}))
    assert ("attr_source" in sink["sql"]) is ready
    assert sink["params"][:3] == ("buy", "31225", "Jane")
    if ready:
        assert sink["params"][-5:] == ("facebook", "marketplace", "short_fb", "m.facebook.com", "/listings/31225")


@pytest.mark.parametrize("ready", [True, False])
def test_create_subscriber_attribution_columns(monkeypatch, ready):
    sink = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(sink))
    monkeypatch.setattr(inventory, "_subscribers_has_unsub_token", lambda: True)
    monkeypatch.setattr(inventory, "get_subscriber", lambda i: {"id": i})
    monkeypatch.setattr(attribution, "columns_ready", lambda table: ready)
    inventory.create_subscriber(email="buyer@x.com",
                                attribution=attribution.from_payload({"attribution": ATTR}))
    assert ("attr_source" in sink["sql"]) is ready
    assert sink["sql"].count("%s") == len(sink["params"])


@pytest.mark.parametrize("ready", [True, False])
def test_freight_insert_attribution_columns(monkeypatch, ready):
    seen = {}
    monkeypatch.setattr(freight_log, "schema_ready", lambda: True)
    monkeypatch.setattr(attribution, "columns_ready", lambda table: ready)

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"id": 9}
    monkeypatch.setattr(freight_log.db, "fetch_one", fake_fetch_one)
    qid = freight_log.insert_storefront_quote(
        lot_id="31225", origin_zip="83702", dest_zip="01608", quantity=150, quote=None,
        buyer_email="b@x.com", buyer_phone="4045550100", unquotable_reason="x",
        attribution=attribution.from_payload({"attribution": ATTR}),
    )
    assert qid == 9
    assert ("attr_landing" in seen["sql"]) is ready
    assert seen["sql"].count("%s") == len(seen["params"])


@pytest.mark.parametrize("ready", [True, False])
def test_deposit_insert_attribution_columns(monkeypatch, ready):
    seen = {}
    monkeypatch.setattr(attribution, "columns_ready", lambda table: ready)

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"id": 7}
    monkeypatch.setattr(deposits.db, "fetch_one", fake_fetch_one)
    quote = deposits.quote(quantity=10, price_per_chair=10, kind="deposit", pct=0.15, min_cents=20000)
    deposits.create_pending(lot_id="31225", quantity=10, price_per_chair=10, quote=quote,
                            buyer_name="J", buyer_email="j@x.com",
                            attribution=attribution.from_payload({"attribution": ATTR}))
    assert ("attr_source" in seen["sql"]) is ready
    assert seen["sql"].count("%s") == len(seen["params"])


# ───────────────────────────── endpoints pass it on ──────────────────────────

def test_contact_passes_attribution(client, monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 1, "kind": "buy", "name": "Jane", "email": "j@x.com", "lot_id": "31225"}
    monkeypatch.setattr(inventory, "create_inquiry", fake_create)
    r = client.post("/contact", json={"kind": "buy", "name": "Jane", "email": "j@x.com",
                                      "lot_id": "31225", "attribution": ATTR})
    assert r.status_code == 200
    assert captured["attribution"]["source"] == "facebook"
    assert captured["attribution"]["referrer"] == "m.facebook.com"


def test_contact_without_attribution_still_works(client, monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 1, "kind": "buy", "name": "Jane", "email": "j@x.com"}
    monkeypatch.setattr(inventory, "create_inquiry", fake_create)
    r = client.post("/contact", json={"kind": "buy", "name": "Jane", "email": "j@x.com"})
    assert r.status_code == 200
    assert attribution.is_empty(captured["attribution"])


def test_subscribe_passes_attribution(client, monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": 7, "name": "Jane", "email": "j@x.com"}
    monkeypatch.setattr(inventory, "create_subscriber", fake_create)
    r = client.post("/subscribe", json={"email": "j@x.com", "source": "site_listings",
                                        "attribution": ATTR})
    assert r.status_code == 200
    assert captured["attribution"]["source"] == "facebook"
    assert captured["source"] == "site_listings"     # the form-location field is untouched


def test_freight_estimate_passes_attribution(client, monkeypatch):
    lot = {"lot_id": "31225", "title": "Mity-Lite", "status": "listed", "price_per_chair": 12,
           "quantity_remaining": 400, "quantity_original": 945, "city": "Boise", "state": "ID",
           "zip_code": "83702"}
    monkeypatch.setattr(inventory, "get", lambda lot_id: dict(lot) if lot_id == "31225" else None)
    inserts = []

    def fake_insert(**kw):
        inserts.append(kw)
        return 4242
    monkeypatch.setattr(freight_log, "insert_storefront_quote", fake_insert)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(app_module, "_notify_freight_estimate", lambda *a, **k: _noop())
    monkeypatch.setattr(app_module, "_carrier_check_task", lambda *a, **k: _noop())
    app_module._carrier_reset()
    monkeypatch.delenv("WARP_API_KEY", raising=False)

    r = client.post("/freight-estimate", json={
        "lot_id": "31225", "dest_zip": "01608", "email": "b@x.com", "phone": "(404) 555-0100",
        "attribution": ATTR,
    })
    assert r.status_code == 200, r.text
    assert inserts and inserts[0]["attribution"]["source"] == "facebook"


def test_reserve_checkout_passes_attribution(client, monkeypatch):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(config_mod, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(site_settings, "get_all", lambda: {"deposit_pct": 0.15, "deposit_min_usd": 200})
    lot = {"lot_id": "31225", "title": "Mity-Lite", "status": "listed", "price_per_chair": 100,
           "quantity_remaining": 100, "quantity_original": 945, "city": "Baltimore", "state": "MD"}
    monkeypatch.setattr(inventory, "get", lambda lot_id: dict(lot) if lot_id == "31225" else None)
    pending = []

    def fake_create_pending(**kw):
        pending.append(kw)
        return {"id": 7, "lot_id": "31225", "kind": kw["quote"].kind, "quantity": kw["quantity"],
                "amount_cents": kw["quote"].amount_cents, "subtotal_cents": kw["quote"].subtotal_cents,
                "status": "pending", "buyer_name": "J", "buyer_email": "j@x.com", "buyer_phone": None}
    monkeypatch.setattr(deposits, "create_pending", fake_create_pending)
    monkeypatch.setattr(deposits, "attach_session", lambda d, s: {"id": d, "stripe_session_id": s})
    monkeypatch.setattr(deposits, "transition", lambda *a, **k: ({"status": "canceled"}, True))
    monkeypatch.setattr(stripe_gateway, "create_checkout_session",
                        lambda *, deposit, lot: SimpleNamespace(id="cs_x", url="https://checkout.stripe/x"))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(app_module, "_notify_deposit", lambda *a, **k: _noop())

    r = client.post("/reserve/31225/checkout", json={
        "quantity": 30, "kind": "deposit", "name": "J", "email": "j@x.com", "attribution": ATTR,
    })
    assert r.status_code == 200, r.text
    assert pending and pending[0]["attribution"]["source"] == "facebook"


# ───────────────────────────── /event ────────────────────────────────────────

@pytest.fixture
def captured(monkeypatch):
    rows = []
    monkeypatch.setattr(visits, "_runner", lambda fn, row: rows.append(row))
    return rows


def test_event_tel_click_is_stored(client, captured):
    r = client.post("/event", json={"kind": "tel_click", "lot_id": "31225", "attribution": ATTR},
                    headers={"user-agent": UA, "cf-ipcountry": "US"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(captured) == 1
    row = captured[0]
    assert row["path"] == "/_event/tel_click"
    assert row["lot_id"] == "31225"
    assert row["utm_source"] == "facebook" and row["utm_medium"] == "marketplace"
    assert row["referer_host"] == "m.facebook.com"
    assert row["country"] == "US" and len(row["visitor"]) == 16


def test_event_bot_is_dropped(client, captured):
    r = client.post("/event", json={"kind": "mailto_click"}, headers={"user-agent": "Googlebot/2.1"})
    assert r.status_code == 200 and r.json() == {"ok": False}
    assert captured == []


def test_event_unknown_kind_is_400(client, captured):
    r = client.post("/event", json={"kind": "pageview"}, headers={"user-agent": UA})
    assert r.status_code == 400
    assert captured == []


def test_event_off_switch(client, captured, monkeypatch):
    monkeypatch.setenv("SITE_VISITS", "off")
    r = client.post("/event", json={"kind": "tel_click"}, headers={"user-agent": UA})
    assert r.json() == {"ok": False} and captured == []


def test_event_rate_limited(client, captured):
    for _ in range(app_module.EVENT_PER_IP_LIMIT):
        assert client.post("/event", json={"kind": "tel_click"}, headers={"user-agent": UA}).status_code == 200
    r = client.post("/event", json={"kind": "tel_click"}, headers={"user-agent": UA})
    assert r.status_code == 429


def test_event_is_public_even_with_admin_auth_on(client, captured, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")
    auth_svc.reset_caches()
    r = client.post("/event", json={"kind": "tel_click"}, headers={"user-agent": UA})
    assert r.status_code == 200


def test_summary_excludes_event_rows(monkeypatch):
    sqls = []

    def fake_all(sql, params=None):
        sqls.append(sql)
        return []
    monkeypatch.setattr(visits.db, "fetch_all", fake_all)
    monkeypatch.setattr(visits.db, "fetch_one", lambda sql, params=None: sqls.append(sql) or {})
    visits.summary(7)
    assert sqls and all("NOT LIKE '/_event/%%'" in s for s in sqls)


# ───────────────────────────── report aggregation ────────────────────────────

def test_report_merge_builds_the_funnel():
    visits_rows = [
        {"source": None, "medium": None, "referrer": "www.google.com", "views": 100, "visitor_days": 40, "lot_views": 60},
        {"source": "google", "medium": "feed", "referrer": None, "views": 20, "visitor_days": 10, "lot_views": 20},
        {"source": None, "medium": None, "referrer": "m.facebook.com", "views": 30, "visitor_days": 20, "lot_views": 10},
        {"source": None, "medium": None, "referrer": None, "views": 500, "visitor_days": 100, "lot_views": 5},
    ]
    leads = {
        "contact": [{"source": None, "medium": None, "referrer": "www.google.com", "n": 2}],
        "subscribe": [{"channel": "unattributed", "n": 1}],
        "freight": [{"source": "google", "medium": "feed", "referrer": None, "n": 1}],
        "clicks": [{"source": None, "medium": None, "referrer": "m.facebook.com", "n": 3}],
        "checkout": [],
    }
    paid = [{"source": None, "medium": None, "referrer": "www.google.com", "n": 1, "usd": 250.0}]
    table = report.merge(visits_rows, leads, paid)

    g = table["google_organic"]
    assert (g["visits"], g["visitor_days"], g["lot_views"]) == (100, 40, 60)
    assert g["contact"] == 2 and g["leads"] == 2 and g["conv_pct"] == 5.0
    assert g["paid"] == 1 and g["paid_usd"] == 250.0
    assert table["google_feed"]["freight"] == 1 and table["google_feed"]["conv_pct"] == 10.0
    assert table["facebook"]["clicks"] == 3 and table["facebook"]["conv_pct"] == 15.0
    assert table["direct"]["leads"] == 0 and table["direct"]["conv_pct"] == 0.0
    assert table["unattributed"]["subscribe"] == 1 and table["unattributed"]["visits"] == 0

    tot = report.totals(table)
    assert tot["leads"] == 7 and tot["visitor_days"] == 170 and tot["paid_usd"] == 250.0
    assert [name for name, _ in report.ordered(table)][:2] == ["facebook", "google_organic"]

    text = report.render({"days": 7, "channels": table, "total": tot, "top_lots": [{"lot_id": "31225", "leads": 4}],
                          "migration_missing": ["subscribers"]})
    assert "google_organic" in text and "TOTAL" in text and "31225" in text
    assert attribution.MIGRATION_HINT in text

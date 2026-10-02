"""Admin API behind the Sales tab's Quotes view (`/api/freight-quotes*`, `/api/sales/counts`).

Offline: `freight_log` is replaced by in-memory doubles, the carrier check by a
spy. What is pinned: the routes are auth-walled, the view adds the price check
and the over-stock flag, a missing migration is a 409 with the apply command
(never a 500), and no route can hand back `raw_response`.
"""
import sys

import pytest
from fastapi.testclient import TestClient

from automation import freight_log, inventory, warp_rates
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

app_module = sys.modules["automation.web.app"]


def _row(**over):
    row = {
        "id": 9, "source": "storefront", "quoted_at": "2026-09-17T12:20:15+00:00",
        "lot_id": "folder:ATL", "lot_title": "Blue Banquet Chairs", "lot_quantity_now": 100,
        "origin_zip": "30318", "dest_zip": "30033", "quantity": 160, "mode": "ltl",
        "ltl_low": 570.0, "ltl_high": 810.0, "partial_low": None, "partial_high": None,
        "miles": 15, "transit_days": 2, "provider": "estimator",
        "buyer_email": "megan@example.org", "buyer_phone": "4045550100",
        "thread_url": None, "valid_until": "2026-09-24",
        "status": "new", "status_changed_at": None, "note": None,
        "unquotable_reason": None, "lot_quantity_remaining": 100,
        "carrier_status": "ok", "carrier_low": 589.51, "carrier_name": "Averitt Express",
        "carrier_count": 17, "carrier_options": [], "carrier_checked_at": "2026-10-02T07:00:00+00:00",
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()


@pytest.fixture
def store(monkeypatch):
    state = {"rows": [_row()], "ready": True, "list_args": [], "updates": []}

    def list_quotes(status=None, limit=200):
        state["list_args"].append(status)
        if status == "bogus":
            raise ValueError("invalid status: bogus")
        return [dict(r) for r in state["rows"]]

    def get_quote(quote_id):
        return next((dict(r) for r in state["rows"] if r["id"] == quote_id), None)

    def set_quote_status(quote_id, *, status=None, note=None):
        if status is not None and status not in freight_log.QUOTE_STATUSES:
            raise ValueError(f"invalid status: {status}")
        if not state["ready"]:
            raise freight_log.SchemaNotReady(freight_log.MIGRATION_HINT)
        state["updates"].append((quote_id, status, note))
        row = next((r for r in state["rows"] if r["id"] == quote_id), None)
        if row is None:
            return None
        if status is not None:
            row["status"] = status
        if note is not None:
            row["note"] = note or None
        return dict(row)

    monkeypatch.setattr(freight_log, "list_quotes", list_quotes)
    monkeypatch.setattr(freight_log, "get_quote", get_quote)
    monkeypatch.setattr(freight_log, "set_quote_status", set_quote_status)
    monkeypatch.setattr(freight_log, "schema_ready", lambda: state["ready"])
    monkeypatch.setattr(freight_log, "count_new", lambda: 3)
    return state


def _client():
    return TestClient(app, base_url="https://testserver")


# ──────────────────────────────────── list ──────────────────────────────────

def test_list_returns_rows_with_the_derived_fields(store):
    body = _client().get("/api/freight-quotes?status=open").json()
    assert store["list_args"] == ["open"]
    assert body["schema_ready"] is True
    assert body["statuses"] == ["new", "answered", "won", "lost", "junk"]
    (item,) = body["items"]
    assert (item["shown_low"], item["shown_high"]) == (570.0, 810.0)
    assert item["price_check"] == "ok"          # Averitt $590 sits inside $570–$810
    assert item["over_stock"] is True           # asked 160, lot had 100
    assert item["buyer_phone"] == "4045550100"
    assert "raw_response" not in item


def test_old_rows_compare_against_the_lots_current_stock(store):
    """Rows from before migration 021 have no stock snapshot (Megan's 160 on a
    100-chair lot): fall back to what the lot holds now, and say which it is."""
    store["rows"] = [_row(lot_quantity_remaining=None, lot_quantity_now=100)]
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert item["over_stock"] is True
    assert item["stock_compared"] == 100 and item["stock_is_current"] is True

    store["rows"] = [_row(lot_quantity_remaining=100, lot_quantity_now=40)]
    readcache.invalidate_all()
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert item["stock_compared"] == 100 and item["stock_is_current"] is False

    store["rows"] = [_row(lot_quantity_remaining=None, lot_quantity_now=None, lot_id=None)]
    readcache.invalidate_all()
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert item["over_stock"] is False and item["stock_compared"] is None


def test_list_flags_a_quote_the_site_underpriced(store):
    store["rows"] = [_row(ltl_low=1080.0, ltl_high=1520.0, carrier_low=2308.76,
                          quantity=200, lot_quantity_remaining=2500)]
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert item["price_check"] == "site_low"
    assert item["over_stock"] is False


def test_partial_mode_rows_compare_against_the_partial_range(store):
    store["rows"] = [_row(mode="partial", ltl_low=None, ltl_high=None,
                          partial_low=4640.0, partial_high=6550.0, carrier_low=None,
                          carrier_status="too_big")]
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert (item["shown_low"], item["shown_high"]) == (4640.0, 6550.0)
    assert item["price_check"] is None


def test_gray_zone_rows_compare_against_the_cheaper_range(store):
    """mode='both': the estimator shows the range with the cheaper midpoint
    first. `recommended_mode` is not stored, so the view re-derives it."""
    store["rows"] = [_row(mode="both", ltl_low=2000.0, ltl_high=2800.0,
                          partial_low=1500.0, partial_high=2100.0, carrier_low=1600.0)]
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert (item["shown_low"], item["shown_high"]) == (1500.0, 2100.0)
    assert item["price_check"] == "ok"


def test_unquotable_row_has_no_price_and_no_check(store):
    store["rows"] = [_row(mode=None, ltl_low=None, ltl_high=None, carrier_low=None,
                          carrier_status=None, unquotable_reason="international/offshore destination 'K1A 0B1'")]
    (item,) = _client().get("/api/freight-quotes").json()["items"]
    assert item["shown_low"] is None and item["price_check"] is None


def test_list_before_the_migration_says_how_to_apply_it(store):
    store["ready"] = False
    body = _client().get("/api/freight-quotes").json()
    assert body["schema_ready"] is False
    assert "021_sales_quotes.sql" in body["migration_hint"]


def test_unknown_status_filter_is_a_400(store):
    assert _client().get("/api/freight-quotes?status=bogus").status_code == 400


# ─────────────────────────────────── update ─────────────────────────────────

def test_status_and_note_update(store):
    r = _client().patch("/api/freight-quotes/9", json={"status": "answered", "note": "emailed 09-25"})
    assert r.status_code == 200
    assert store["updates"] == [(9, "answered", "emailed 09-25")]
    assert r.json()["status"] == "answered" and r.json()["price_check"] == "ok"


def test_note_can_be_cleared(store):
    _client().patch("/api/freight-quotes/9", json={"note": None})
    assert store["updates"] == [(9, None, "")]


@pytest.mark.parametrize("body, code", [
    ({"status": "sold"}, 400),
    ({}, 400),
    ({"lot_id": "x"}, 400),
])
def test_bad_updates_are_400(store, body, code):
    assert _client().patch("/api/freight-quotes/9", json=body).status_code == code
    assert store["updates"] == []


def test_update_of_a_missing_row_is_404(store):
    assert _client().patch("/api/freight-quotes/404", json={"status": "won"}).status_code == 404


def test_update_before_the_migration_is_a_409_with_the_command(store):
    store["ready"] = False
    r = _client().patch("/api/freight-quotes/9", json={"status": "won"})
    assert r.status_code == 409
    assert "apply_sql.py scripts/sql/021_sales_quotes.sql" in r.json()["detail"]


# ───────────────────────────────── carrier check ────────────────────────────

def test_manual_carrier_check_runs_and_returns_the_row(store, monkeypatch):
    ran = []
    monkeypatch.setattr(app_module, "_run_carrier_check",
                        lambda qid: ran.append(qid) or {"carrier_status": "ok"})
    r = _client().post("/api/freight-quotes/9/carrier-check")
    assert r.status_code == 200 and ran == [9]
    assert r.json()["carrier_name"] == "Averitt Express"


def test_manual_carrier_check_over_budget_is_a_429(store, monkeypatch):
    def spent(qid):
        raise app_module.CarrierBudgetExceeded("40 carrier checks used this hour")

    monkeypatch.setattr(app_module, "_run_carrier_check", spent)
    r = _client().post("/api/freight-quotes/9/carrier-check")
    assert r.status_code == 429 and "next hour" in r.json()["detail"]


def test_chair_field_edit_before_the_migration_says_which_command_to_run(monkeypatch):
    class UndefinedColumn(Exception):
        pass

    def boom(lot_id, **fields):
        raise UndefinedColumn('column "chair_weight_lb" of relation "inventory" does not exist')

    monkeypatch.setattr(inventory, "set_fields", boom)
    r = _client().patch("/api/inventory/31225", json={"chair_weight_lb": 13})
    assert r.status_code == 409
    assert "021_sales_quotes.sql" in r.json()["detail"]


@pytest.mark.parametrize("setup, code", [
    ("missing", 404), ("no_lane", 409), ("no_schema", 409), ("switched_off", 409),
])
def test_manual_carrier_check_refusals(store, monkeypatch, setup, code):
    ran = []
    monkeypatch.setattr(app_module, "_run_carrier_check", lambda qid: ran.append(qid))  # → None
    quote_id = 9
    if setup == "missing":
        quote_id = 404
    elif setup == "no_schema":
        store["ready"] = False
    elif setup == "switched_off":
        monkeypatch.setattr(warp_rates, "enabled", lambda: False)
    r = _client().post(f"/api/freight-quotes/{quote_id}/carrier-check")
    assert r.status_code == code
    if setup != "no_lane":
        assert ran == []


# ──────────────────────────────────── counts ────────────────────────────────

def test_sales_counts(store, monkeypatch):
    monkeypatch.setattr(inventory, "list_inquiries", lambda status=None: [{"id": 1}, {"id": 2}])
    assert _client().get("/api/sales/counts").json() == {"quotes_new": 3, "inquiries_new": 2}


# ───────────────────────────────────── auth ─────────────────────────────────

def test_routes_are_behind_the_admin_wall(store, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    client = _client()
    assert client.get("/api/freight-quotes").status_code == 401
    assert client.patch("/api/freight-quotes/9", json={"status": "won"}).status_code == 401
    assert client.post("/api/freight-quotes/9/carrier-check").status_code == 401
    assert client.get("/api/sales/counts").status_code == 401
    assert store["updates"] == []

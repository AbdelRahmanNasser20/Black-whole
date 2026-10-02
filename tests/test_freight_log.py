"""Offline tests for the `freight_quotes` ledger (`automation/freight_log.py`).

No DB: `db.fetch_one` / `fetch_all` / `execute` are replaced by recorders, and
the "is migration 021 applied" answer is set per test. What is pinned here is
the contract the endpoint and the Sales tab lean on:

  * every request is written — an unquotable one too, with no fake price;
  * before 021 the insert still works through the old columns;
  * a write failure is a `None`, never an exception;
  * the read side never hands back `raw_response` (calibration + caller IP).
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from automation import db, freight_log

QUOTE = {
    "mode": "ltl", "recommended_mode": "ltl",
    "ltl_low": 570, "ltl_high": 810, "partial_low": None, "partial_high": None,
    "miles": 15, "transit_days": 2, "provider": "estimator",
    "valid_until": "2026-10-09",
    "accessorials": {"residential": True, "liftgate": True},
    "raw": {"provider": "estimator", "nmfc_class": 175},
}


@pytest.fixture
def calls(monkeypatch):
    """Record every statement; `ready` flips the schema answer."""
    state = {"ready": True, "sql": [], "fetch_all": [], "fetch_one": {"id": 77}}

    def fetch_one(sql, params=None):
        if "information_schema.columns" in sql:
            return {"?column?": 1} if state["ready"] else None
        state["sql"].append((sql, params))
        return state["fetch_one"]

    def fetch_all(sql, params=None):
        state["sql"].append((sql, params))
        return state["fetch_all"]

    def execute(sql, params=None):
        state["sql"].append((sql, params))
        return 1

    freight_log.reset_schema_cache()
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "execute", execute)
    yield state
    freight_log.reset_schema_cache()


def _insert(**over):
    kw = dict(
        lot_id="31225", origin_zip="30318", dest_zip="30033", quantity=160,
        quote=QUOTE, buyer_email="megan@example.org", buyer_phone="4045550100",
        client_ip="203.0.113.7", lot_quantity_remaining=100,
    )
    kw.update(over)
    return freight_log.insert_storefront_quote(**kw)


# ─────────────────────────────────── insert ─────────────────────────────────

def test_insert_writes_contact_and_stock_on_the_new_schema(calls):
    assert _insert() == 77
    sql, params = calls["sql"][-1]
    assert "buyer_phone" in sql and "lot_quantity_remaining" in sql
    assert "megan@example.org" in params
    assert "4045550100" in params
    assert 100 in params
    assert "ltl" in params
    # priced row ⇒ no unquotable reason
    assert params[-2] is None


def test_unquotable_request_is_stored_without_a_price_or_a_mode(calls):
    assert _insert(quote=None, origin_zip=None, unquotable_reason="international") == 77
    _sql, params = calls["sql"][-1]
    assert params[1] is None                # origin_zip
    assert params[4] is None                # mode — never a fake 'ltl'
    assert params[5:9] == (None, None, None, None)   # the four range bounds
    assert params[-2] == "international"
    assert "megan@example.org" in params and "4045550100" in params


def test_legacy_schema_insert_keeps_phone_in_raw(calls):
    """Before migration 021: old columns only, phone merged into raw_response."""
    calls["ready"] = False
    assert _insert() == 77
    sql, params = calls["sql"][-1]
    assert "buyer_phone" not in sql.split("VALUES")[0]
    assert "unquotable_reason" not in sql
    raw = next(p for p in params if isinstance(p, str) and "client_ip" in p)
    assert '"buyer_phone": "4045550100"' in raw


def test_legacy_schema_cannot_store_an_unquotable_request(calls):
    calls["ready"] = False
    assert _insert(quote=None, unquotable_reason="offshore") is None
    assert calls["sql"] == []          # nothing written, and nothing faked


def test_insert_failure_is_none_not_an_exception(calls, monkeypatch):
    def boom(sql, params=None):
        if "information_schema" in sql:
            return {"x": 1}
        raise RuntimeError("pooler down")

    monkeypatch.setattr(db, "fetch_one", boom)
    assert _insert() is None


def test_schema_answer_is_cached_only_when_positive(calls):
    calls["ready"] = False
    assert freight_log.schema_ready() is False
    calls["ready"] = True                      # operator applies 021, no redeploy
    assert freight_log.schema_ready() is True
    calls["ready"] = False                     # a column never disappears
    assert freight_log.schema_ready() is True


# ─────────────────────────────────── status ─────────────────────────────────

def test_status_change_stamps_the_time(calls):
    calls["fetch_one"] = {"id": 9, "status": "answered"}
    freight_log.set_quote_status(9, status="answered", note="  called her  ")
    update_sql, params = next(c for c in calls["sql"] if c[0].lstrip().startswith("UPDATE"))
    assert "status_changed_at = now()" in update_sql
    assert params == ["answered", "called her", 9]


def test_bad_status_is_a_value_error(calls):
    with pytest.raises(ValueError):
        freight_log.set_quote_status(9, status="sold")
    assert calls["sql"] == []


def test_status_change_before_the_migration_says_so(calls):
    calls["ready"] = False
    with pytest.raises(freight_log.SchemaNotReady) as err:
        freight_log.set_quote_status(9, status="won")
    assert "021" in str(err.value)


def test_note_is_capped(calls):
    calls["fetch_one"] = {"id": 9}
    freight_log.set_quote_status(9, note="x" * 5000)
    _sql, params = next(c for c in calls["sql"] if c[0].lstrip().startswith("UPDATE"))
    assert len(params[0]) == freight_log.NOTE_MAX_LEN


# ─────────────────────────────── carrier result ─────────────────────────────

def test_carrier_result_stores_at_most_five_options(calls):
    options = [{"carrier": f"C{i}", "price_usd": 500 + i, "transit_days": 2} for i in range(9)]
    ok = freight_log.set_carrier_result(9, {
        "carrier_status": "ok", "carrier_low": 500, "carrier_name": "C0",
        "carrier_count": 9, "carrier_options": options,
    })
    assert ok is True
    _sql, params = calls["sql"][-1]
    assert params[0] == "ok" and params[1] == 500 and params[3] == 9
    assert params[4].count('"carrier"') == freight_log.CARRIER_OPTIONS_MAX


def test_carrier_result_is_skipped_before_the_migration(calls):
    calls["ready"] = False
    assert freight_log.set_carrier_result(9, {"carrier_status": "ok"}) is False
    assert calls["sql"] == []


# ──────────────────────────────────── read ──────────────────────────────────

def test_list_never_selects_raw_response_as_a_column(calls):
    calls["fetch_all"] = [{
        "id": 9, "quoted_at": datetime(2026, 9, 17, 12, 20, tzinfo=timezone.utc),
        "ltl_low": Decimal("570.00"), "status": "new",
    }]
    rows = freight_log.list_quotes("open")
    sql, params = calls["sql"][-1]
    assert "q.raw_response," not in sql and "q.*" not in sql
    assert params[0] == ["new", "answered"]
    assert rows[0]["ltl_low"] == 570.0
    assert rows[0]["quoted_at"].startswith("2026-09-17T12:20")


def test_list_on_the_legacy_schema_fills_follow_up_defaults(calls):
    calls["ready"] = False
    calls["fetch_all"] = [{"id": 9, "buyer_phone": None}]
    (row,) = freight_log.list_quotes()
    assert row["status"] == "new"
    assert row["carrier_low"] is None
    assert "q.status" not in calls["sql"][-1][0]
    # a triaged-only filter has nothing to show before the migration
    assert freight_log.list_quotes("won") == []


def test_list_rejects_an_unknown_status(calls):
    with pytest.raises(ValueError):
        freight_log.list_quotes("everything")


def test_count_new_is_storefront_only(calls):
    calls["fetch_one"] = {"n": 3}
    assert freight_log.count_new() == 3
    sql = calls["sql"][-1][0]
    assert "status = 'new'" in sql and "source = 'storefront'" in sql


def test_count_new_is_zero_before_the_migration(calls):
    """Nothing can be marked answered on the old schema — a count there would
    be a badge that never clears."""
    calls["ready"] = False
    assert freight_log.count_new() == 0
    assert calls["sql"] == []


def test_schema_check_keys_on_the_last_column_the_migration_adds(monkeypatch):
    """`apply_sql.py` runs the file statement by statement. A run that dies
    half-way must not flip the code onto inserts naming columns that are not
    there yet — so the sentinel is the LAST freight_quotes column, and the
    migration must keep adding it last."""
    asked = []
    freight_log.reset_schema_cache()
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: asked.append(sql) or None)
    assert freight_log.schema_ready() is False
    assert "column_name = 'carrier_checked_at'" in asked[0]

    from pathlib import Path
    sql = Path("scripts/sql/021_sales_quotes.sql").read_text()
    added = [c for c in ("buyer_phone", "status", "unquotable_reason", "lot_quantity_remaining",
                         "carrier_status", "carrier_options", "carrier_checked_at")]
    positions = [sql.index(f"ADD COLUMN IF NOT EXISTS {c} ") for c in added]
    assert positions == sorted(positions) and positions[-1] == max(positions)
    # …and the two DROP NOT NULLs the unquotable insert needs come before it too
    assert sql.index("ALTER COLUMN mode DROP NOT NULL") < positions[-1]
    assert sql.index("ALTER COLUMN origin_zip DROP NOT NULL") < positions[-1]

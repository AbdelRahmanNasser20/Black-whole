from datetime import datetime, timezone
from decimal import Decimal

import pytest

from recorder import store
from recorder.models import Observation

AWARE = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 7, 31, 12, 0)


# --- observation_row: Observation -> INSERT_SQL param tuple ----------------

def test_observation_row_maps_all_fields_in_order():
    obs = Observation(
        source="govdeals", source_lot_id="1/2/3", status="active", raw={"a": 1},
        current_bid=Decimal("12.50"), bid_count=3, end_date=AWARE, observed_at=AWARE,
    )
    row = store.observation_row(obs)
    assert row == ("govdeals", "1/2/3", "active", Decimal("12.50"), 3, AWARE, '{"a": 1}', AWARE)


def test_observation_row_allows_none_optional_fields():
    obs = Observation(source="gsa", source_lot_id="x", status="gone", raw={})
    row = store.observation_row(obs)
    assert row[3] is None   # current_bid
    assert row[4] is None   # bid_count
    assert row[5] is None   # end_date
    assert row[7] is None   # observed_at -> None so the DB default (now()) applies


def test_observation_row_rejects_naive_end_date():
    obs = Observation(source="gsa", source_lot_id="x", status="active", raw={}, end_date=NAIVE)
    with pytest.raises(ValueError):
        store.observation_row(obs)


def test_observation_row_rejects_naive_observed_at():
    obs = Observation(source="gsa", source_lot_id="x", status="active", raw={}, observed_at=NAIVE)
    with pytest.raises(ValueError):
        store.observation_row(obs)


def test_observation_row_raw_survives_decimal_via_default_str():
    # raw dicts can carry Decimal-typed source fields; json.dumps(default=str)
    # must not blow up on them.
    obs = Observation(source="gsa", source_lot_id="x", status="active",
                       raw={"price": Decimal("9.99")})
    row = store.observation_row(obs)
    assert row[6] == '{"price": "9.99"}'


# --- insert_observations -----------------------------------------------

def test_insert_observations_calls_executemany_and_returns_count(monkeypatch):
    captured = {}

    def fake_executemany(sql, seq):
        captured["sql"] = sql
        captured["rows"] = list(seq)

    monkeypatch.setattr(store.db, "executemany", fake_executemany)
    obs = [
        Observation(source="gsa", source_lot_id="1", status="active", raw={}),
        Observation(source="gsa", source_lot_id="2", status="active", raw={}),
    ]
    n = store.insert_observations(obs)
    assert n == 2
    assert len(captured["rows"]) == 2
    assert "::jsonb" in captured["sql"]
    assert "COALESCE(%s, now())" in captured["sql"]


def test_insert_observations_empty_iterable_skips_db_call(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("must not call db.executemany for an empty batch")

    monkeypatch.setattr(store.db, "executemany", fail)
    assert store.insert_observations([]) == 0


# --- tracked_active: DISTINCT ON subquery with outer status filter --------

def test_tracked_active_sql_filters_status_outside_distinct_on(monkeypatch):
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    store.tracked_active()

    sql = captured["sql"]
    assert "DISTINCT ON (source, source_lot_id)" in sql
    assert sql.count("DISTINCT ON") == 1
    # the status filter must sit AFTER the DISTINCT ON subquery closes —
    # filtering inside DISTINCT ON would surface a stale 'active' row for a
    # lot whose latest snapshot has since closed.
    subquery_close = sql.index(") latest")
    status_filter = sql.index("status = 'active'")
    distinct_on = sql.index("DISTINCT ON")
    assert distinct_on < subquery_close < status_filter


def test_tracked_active_sql_tie_breaks_same_batch_observed_at_with_id_desc(monkeypatch):
    # CRITICAL 2: a discover+sold_sweep batch inserts via one executemany()
    # transaction, so Postgres now() (observed_at) is identical for every
    # row in that batch. Without an `id DESC` tie-break, "latest snapshot"
    # is nondeterministic among same-batch rows for the same lot.
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    store.tracked_active()
    assert "ORDER BY source, source_lot_id, observed_at DESC, id DESC" in captured["sql"]


def test_tracked_active_no_source_passes_no_params(monkeypatch):
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    store.tracked_active()
    assert captured["params"] is None
    assert "source = %s" not in captured["sql"]


def test_tracked_active_filters_by_source_and_returns_rows(monkeypatch):
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "source": "govdeals", "source_lot_id": "1/2/3", "observed_at": AWARE,
            "end_date": None, "current_bid": None, "bid_count": None,
        }]

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    rows = store.tracked_active(source="govdeals")

    assert captured["params"] == ("govdeals",)
    assert "source = %s" in captured["sql"]
    assert rows == [{
        "source": "govdeals", "source_lot_id": "1/2/3", "observed_at": AWARE,
        "end_date": None, "current_bid": None, "bid_count": None,
    }]


# --- table_size_pretty (IMPORTANT 6: storage-size visibility) --------------

def test_table_size_pretty_returns_value_from_row(monkeypatch):
    captured = {}

    def fake_fetch_one(sql, params=None):
        captured["sql"] = sql
        return {"size": "128 MB"}

    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    assert store.table_size_pretty() == "128 MB"
    assert "pg_total_relation_size('listing_snapshots')" in captured["sql"]
    assert "pg_size_pretty" in captured["sql"]


def test_table_size_pretty_returns_unknown_when_no_row(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: None)
    assert store.table_size_pretty() == "unknown"


# --- newest_observed_at --------------------------------------------------

def test_newest_observed_at_returns_value_from_row(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: {"max_observed_at": AWARE})
    assert store.newest_observed_at("govdeals") == AWARE


def test_newest_observed_at_returns_none_when_no_row(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: None)
    assert store.newest_observed_at("govdeals") is None


# --- coverage --------------------------------------------------------------

def test_coverage_computes_pct_and_all_rollup(monkeypatch):
    def fake_fetch_all(sql, params=None):
        assert params == (7,)
        return [
            {"source": "govdeals", "closed_lots": 10, "covered": 9, "missed": 1},
            {"source": "gsa", "closed_lots": 4, "covered": 4, "missed": 0},
        ]

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    rows = store.coverage()

    by_source = {r["source"]: r for r in rows}
    assert by_source["govdeals"]["pct"] == 90.0
    assert by_source["gsa"]["pct"] == 100.0
    assert by_source["_all"]["closed_lots"] == 14
    assert by_source["_all"]["covered"] == 13
    assert by_source["_all"]["missed"] == 1
    assert by_source["_all"]["pct"] == round(100.0 * 13 / 14, 1)


def test_coverage_sql_tie_breaks_latest_cte_with_id_desc(monkeypatch):
    # CRITICAL 2: the coverage view's `latest` CTE needs the same id DESC
    # tie-break as tracked_active(), for the same same-batch-observed_at
    # reason.
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    store.coverage(days=7)
    assert "ORDER BY source, source_lot_id, observed_at DESC, id DESC" in captured["sql"]


def test_coverage_handles_zero_closed_lots_without_dividing_by_zero(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [])
    rows = store.coverage(days=1)
    assert rows == [{"source": "_all", "closed_lots": 0, "covered": 0, "missed": 0, "pct": 0.0}]

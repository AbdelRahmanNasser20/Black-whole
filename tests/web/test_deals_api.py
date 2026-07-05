import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch, rows):
    # NB: `automation.web.__init__` re-exports the FastAPI instance as `app`,
    # shadowing the submodule on attribute access — go through importlib.
    webapp = importlib.import_module("automation.web.app")

    captured = {}

    def fake_fetch_all(sql, params=()):
        captured.setdefault("sqls", []).append(sql)
        if "FROM deal_lots WHERE" in sql and "count(*)" not in sql and "GROUP BY" not in sql:
            captured["rows_sql"] = sql
            captured["rows_params"] = params
            return [dict(r) for r in rows]
        return []  # facet queries

    def fake_fetch_one(sql, params=()):
        if "count(*) AS c" in sql:
            return {"c": len(rows)}
        return {"total_lots": 456, "candidates": 25, "ending_24h": 25}

    monkeypatch.setattr(webapp.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(webapp.db, "fetch_one", fake_fetch_one)
    return TestClient(webapp.app), captured


ROW = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Desk",
       "canonical_category": "Furniture", "city": "Houston", "state": "TX",
       "bid_count": 0, "current_bid": 100.0, "currency_code": "USD",
       "end_utc": None, "outcome": None, "final_bid": None,
       "outcome_complete": False, "first_seen_at": None,
       "hero_image_url": None, "archived_hero_url": None}


def test_deals_endpoint_shape_and_enrichment(monkeypatch):
    client, cap = _client(monkeypatch, [ROW])
    r = client.get("/api/deals?max_bids=0&ending_within=48&sort=landed")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["stats"]["candidates"] == 25
    row = body["rows"][0]
    assert row["landed_cost"] == 112.5
    assert row["govdeals_url"].endswith("/asset/305/10340")
    assert "ORDER BY current_bid DESC" in cap["rows_sql"]
    assert 0 in cap["rows_params"] and 48 in cap["rows_params"]


def test_deals_endpoint_rejects_bad_status(monkeypatch):
    client, _ = _client(monkeypatch, [])
    assert client.get("/api/deals?status=bogus").status_code == 400


def test_deals_endpoint_passes_native_filter(monkeypatch):
    client, cap = _client(monkeypatch, [ROW])
    r = client.get("/api/deals?native=47b")
    assert r.status_code == 200
    assert "native_category_id = %s" in cap["rows_sql"]
    assert "47B" in cap["rows_params"]


def test_deals_tree_groups_twigs_under_branches(monkeypatch):
    webapp = importlib.import_module("automation.web.app")
    monkeypatch.setattr(webapp.db, "fetch_all", lambda sql, params=(): [
        {"canonical_category": "tools_shop", "native_category_id": "375",
         "native_category_name": "Power Tools", "n": 73, "zero_bid": 33, "ending_24h": 5},
        {"canonical_category": "tools_shop", "native_category_id": "90",
         "native_category_name": "Tools, All Types", "n": 83, "zero_bid": 27, "ending_24h": 2},
        {"canonical_category": "av_equipment", "native_category_id": "22",
         "native_category_name": "Audio/Visual Equipment", "n": 205, "zero_bid": 124, "ending_24h": 9},
    ])
    client = TestClient(webapp.app)
    body = client.get("/api/deals/tree").json()
    assert body["total"] == 361
    assert [b["category"] for b in body["branches"]] == ["av_equipment", "tools_shop"]
    tools = body["branches"][1]
    assert tools["n"] == 156 and tools["zero_bid"] == 60
    # twigs sorted by volume within the branch
    assert [t["native_id"] for t in tools["twigs"]] == ["90", "375"]


def test_deals_tree_rejects_bad_status(monkeypatch):
    client, _ = _client(monkeypatch, [])
    assert client.get("/api/deals/tree?status=bogus").status_code == 400

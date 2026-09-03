# tests/deals/test_profiles_api.py
import importlib
from fastapi.testclient import TestClient
from deals.profiles import Profile


def _profile(slug="desks", **over):
    kw = dict(slug=slug, name="Desks", keywords=["desk"], exclude_terms=[], search_terms=["desks"],
              native_category_ids=["372"], canonical_categories=[], min_quantity=1, item_noun="desks",
              states=[], min_price=None, max_price=None, enabled=True, is_default=False)
    kw.update(over)
    return Profile(**kw)


_FAKE_PROFILES = {
    "desks": _profile(),
    "medical": _profile("medical", name="Medical", keywords=["dental"]),
    "chairs": _profile("chairs", name="Chairs", is_default=True, min_quantity=50),
}


def _client(monkeypatch, rows=()):
    webapp = importlib.import_module("automation.web.app")
    cap = {}

    def fake_fetch_all(sql, params=()):
        cap.setdefault("sqls", []).append((sql, params))
        if "row_to_json(v.*) AS verdict" in sql:
            cap["rows_sql"], cap["rows_params"] = sql, params
            return [dict(r) for r in rows]
        return []

    def fake_fetch_one(sql, params=()):
        if "count(*) AS c" in sql:
            return {"c": len(rows)}
        return {"total_lots": 1, "candidates": 0, "ending_24h": 0}

    monkeypatch.setattr(webapp.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(webapp.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webapp.profiles, "load", lambda slug: _FAKE_PROFILES.get(slug))
    monkeypatch.setattr(webapp.profiles, "list_all",
                        lambda include_disabled=False: [_FAKE_PROFILES["chairs"], _profile()])
    return TestClient(webapp.app), cap


def test_deals_profile_param_filters_sql(monkeypatch):
    client, cap = _client(monkeypatch)
    r = client.get("/api/deals?profile=desks&status=closed")
    assert r.status_code == 200
    assert "title ILIKE ANY(%s)" in cap["rows_sql"]
    assert ["%desk%"] in list(cap["rows_params"])


def test_deals_unknown_profile_404(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.get("/api/deals?profile=nope").status_code == 404


def test_deals_no_profile_no_filter(monkeypatch):
    client, cap = _client(monkeypatch)
    client.get("/api/deals")
    assert "ILIKE ANY" not in cap["rows_sql"]


def test_deals_geo_and_tree_take_profile(monkeypatch):
    client, cap = _client(monkeypatch)
    assert client.get("/api/deals/geo?profile=desks").status_code == 200
    assert client.get("/api/deals/tree?profile=desks").status_code == 200
    geo = [s for s, _ in cap["sqls"] if "lat IS NOT NULL" in s]
    tree = [s for s, _ in cap["sqls"] if "GROUP BY canonical_category, native_category_id" in s]
    assert geo and "title ILIKE ANY(%s)" in geo[0]
    assert tree and "title ILIKE ANY(%s)" in tree[0]
    assert client.get("/api/deals/tree?profile=nope").status_code == 404


# ── /api/profiles CRUD + outcomes (Task 4) ───────────────────────────────────

def test_list_profiles(monkeypatch):
    client, _ = _client(monkeypatch)
    body = client.get("/api/profiles").json()
    assert body["default"] == "chairs" and [p["slug"] for p in body["profiles"]] == ["chairs", "desks"]


def test_create_profile_coerces_comma_lists(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    saved = {}
    monkeypatch.setattr(webapp.profiles, "upsert", lambda p: saved.setdefault("p", p) or p)
    r = client.post("/api/profiles", json={"slug": "Office-Desks", "name": "Office desks",
                                           "keywords": "desk, credenza", "min_quantity": "5"})
    assert r.status_code == 200
    assert saved["p"].slug == "office-desks" and saved["p"].keywords == ["desk", "credenza"]
    assert saved["p"].min_quantity == 5


def test_create_profile_bad_slug_400(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.post("/api/profiles", json={"slug": "no way", "name": "x"}).status_code == 400


def test_create_profile_table_absent_503(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    def boom(p):
        raise webapp.profiles.ProfilesUnavailable("apply 006 first")
    monkeypatch.setattr(webapp.profiles, "upsert", boom)
    r = client.post("/api/profiles", json={"slug": "desks", "name": "Desks"})
    assert r.status_code == 503 and "006" in r.json()["detail"]


def test_delete_default_409(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    def boom(slug):
        raise ValueError("default")
    monkeypatch.setattr(webapp.profiles, "delete", boom)
    assert client.delete("/api/profiles/chairs").status_code == 409


def test_outcomes_rollup_uses_profile_filter(monkeypatch):
    client, cap = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    monkeypatch.setattr(webapp.db, "fetch_one", lambda sql, params=(): {
        "closed": 10, "no_bid": 4, "sold": 6, "median_final_bid": 120.0, "last_closed_at": None})
    r = client.get("/api/profiles/desks/outcomes")
    assert r.status_code == 200
    body = r.json()
    assert body["closed"] == 10 and body["no_bid_pct"] == 40.0 and body["comps"] == []
    comps_sql = [s for s, _ in cap["sqls"] if "deal_verdicts" in s]
    assert comps_sql and "title ILIKE ANY(%s)" in comps_sql[0]

"""One combined Auctions list: /api/auctions defaults to source=all, accepts
tx (TXAuction), and every card says which site it came from."""
import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation import auctions_supabase as aus
from automation.web import auth as auth_svc
from deals.profiles import SEED_PROFILES


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _row(link, qty, end):
    return {"asset_id": link[-6:], "link": link, "title": f"({qty}) banquet chairs", "description": "",
            "quantity": qty, "quantity_source": "llm", "quantity_confidence": "high", "price": "$10.00",
            "location": "Austin, TX, United States", "pickup_zip": "78741", "contact_email": None,
            "contact_phone": None, "end_date": end, "time_left": "", "image_url": "",
            "last_seen_at": datetime.now(timezone.utc)}


def test_source_of_link():
    assert aus.source_of_link("https://www.govdeals.com/en/asset/1/2") == "gd"
    assert aus.source_of_link("https://www.publicsurplus.com/sms/auction/view?auc=1") == "ps"
    assert aus.source_of_link("https://www.bidspotter.com/en-us/auction-catalogues/x/lot-y") == "bs"
    assert aus.source_of_link("https://www.txauction.com/auctions/31431/lot/57702") == "tx"
    assert aus.source_of_link("") == "other"


def test_all_sources_drop_the_link_filter_and_badge_each_item(monkeypatch):
    sqls = []
    future = datetime.now(timezone.utc) + timedelta(days=2)
    rows = [_row("https://www.txauction.com/auctions/31431/lot/57702", 500,
                 future.strftime("%Y-%m-%dT%H:%M:%SZ")),
            _row("https://www.govdeals.com/en/asset/9/8", 200, future.strftime("%Y-%m-%dT%H:%M:%S"))]

    def fetch_all(sql, params=()):
        sqls.append((sql, params))
        return [] if "quantity IS NULL OR quantity <" in sql else [dict(r) for r in rows]

    monkeypatch.setattr(aus.db, "fetch_all", fetch_all)
    items = aus.get_top_lots(SEED_PROFILES["chairs"], source="all", n=5, include_condition=False)
    assert all("link ILIKE" not in s for s, _ in sqls)
    assert [(i["source"], i["source_name"]) for i in items] == [("tx", "TXAuction"), ("gd", "GovDeals")]
    assert items[0]["end_utc"] == future.strftime("%Y-%m-%dT%H:%M:%SZ")
    # naive GovDeals end_date = US/Eastern → converted to UTC, never read as UTC
    gd_utc = datetime.strptime(items[1]["end_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert gd_utc - future.replace(microsecond=0) in (timedelta(hours=4), timedelta(hours=5))

    sqls.clear()
    aus.get_top_lots(SEED_PROFILES["chairs"], source="tx", n=5, include_condition=False)
    assert all("link ILIKE" in s and "%txauction.com%" in p for s, p in sqls)
    with pytest.raises(ValueError):
        aus.get_top_lots(SEED_PROFILES["chairs"], source="zz", include_condition=False)


def test_api_auctions_default_all_and_validates(monkeypatch):
    webapp = importlib.import_module("automation.web.app")
    seen = []
    monkeypatch.setattr(webapp, "get_top_lots", lambda prof, **kw: seen.append(kw["source"]) or [])
    monkeypatch.setattr(webapp.profiles, "resolve", lambda slug: SEED_PROFILES["chairs"])
    webapp._AUCTIONS_CACHE.clear()
    c = TestClient(webapp.app)
    assert c.get("/api/auctions?n=3").status_code == 200
    assert c.get("/api/auctions?n=3&source=tx").status_code == 200
    assert c.get("/api/auctions?n=3&source=gd").status_code == 200
    assert c.get("/api/auctions?n=3&source=nope").status_code == 400
    assert seen == ["all", "tx", "gd"]
    webapp._AUCTIONS_CACHE.clear()


def test_cache_stats_counts_tx(monkeypatch):
    cap = {}
    monkeypatch.setattr(aus.db, "fetch_one", lambda sql, params=(): {"n": 3, "newest": None, "oldest": None})

    def fetch_all(sql, params=()):
        cap["sql"], cap["params"] = sql, params
        return [{"src": "tx", "n": 3, "newest": None}]

    monkeypatch.setattr(aus.db, "fetch_all", fetch_all)
    stats = aus.cache_stats()
    assert "THEN 'tx'" in cap["sql"] and "%txauction.com%" in cap["params"]
    assert stats["by_source"]["tx"]["count"] == 3


def test_asset_ids_for_txauction_links():
    webapp = importlib.import_module("automation.web.app")
    from auction_extractors.listings_db import extract_asset_id
    for link in ("https://www.txauction.com/auctions/31431/lot/57702-lot-11",
                 "https://www.txauction.com/auctions/31431/lot/57702"):
        assert webapp._asset_id_from_link(link) == "tx:57702"
        assert extract_asset_id(link) == "tx:57702"
    assert webapp._asset_id_from_link("https://www.govdeals.com/en/asset/9/8") == "9/8"


def test_tx_favorites_are_never_sent_to_govdeals():
    from deals.bidders import parse_favorite_key
    assert parse_favorite_key("tx:57702") is None
    assert parse_favorite_key("17/28505") == (17, 28505)

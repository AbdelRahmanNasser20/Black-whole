"""Auctions tab loader: a starred lot must not vanish because its count was a
low-confidence LLM guess, and the Listings DB tab must read Supabase.

Orlando 28859/2863 ("Two Hundred Ten (210) Banquet Hall Chairs") was in
`auction_listings` with quantity=1 / confidence=low. The 50-chair floor in SQL
dropped it, so it showed in Favorites and nowhere else.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from automation import auctions_supabase as al
from automation.favorites import _parse_end_date
from auction_extractors import end_dates
from deals.profiles import SEED_PROFILES


def _row(asset, title, qty, conf="high", src="llm", end=None):
    end = end or (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"asset_id": asset, "link": f"https://www.govdeals.com/en/asset/{asset}",
            "title": title, "description": "", "quantity": qty, "quantity_source": src,
            "quantity_confidence": conf, "price": "USD 440.0", "location": "Orlando, FL",
            "pickup_zip": "32816", "contact_email": None, "contact_phone": None,
            "end_date": end, "time_left": "", "image_url": "",
            "last_seen_at": datetime.now(timezone.utc)}


@pytest.fixture
def fake_db(monkeypatch):
    calls: list[str] = []
    verified = [_row("28860/2863", "Two Hundred  (200) Banquet Hall Chairs", 200, "medium")]
    unsure = [
        _row("28859/2863", "Two Hundred Ten (210) Banquet Hall Chairs", 1, "low"),
        _row("1/1", "Banquet chairs, set of (12)", 3, "low"),          # claim under floor
        _row("2/2", "Lot of (300) banquet chairs", None, "unknown", "llm_failed"),
    ]

    def fetch_all(sql, params=()):
        calls.append(sql)
        return [dict(r) for r in (unsure if "quantity IS NULL OR quantity <" in sql else verified)]

    monkeypatch.setattr(al.db, "fetch_all", fetch_all)
    return calls


def test_low_confidence_lot_with_title_count_reaches_the_tab(fake_db):
    items = al.get_top_lots(SEED_PROFILES["chairs"], source="gd", n=15,
                            min_quantity=50, include_condition=False)
    by_link = {i["link"].rsplit("/asset/", 1)[1]: i for i in items}
    assert set(by_link) == {"28860/2863", "28859/2863", "2/2"}
    assert by_link["28859/2863"]["quantity"] == 210
    assert by_link["28859/2863"]["quantity_unverified"] is True
    assert by_link["28859/2863"]["llm_quantity"] == 1
    assert by_link["28860/2863"]["quantity_unverified"] is False
    assert [i["quantity"] for i in items] == [300, 210, 200]  # ranked by quantity


def test_favorites_uses_the_shared_parser():
    assert _parse_end_date("2026-10-05T11:02:00") == end_dates.parse_end_date("2026-10-05T11:02:00")
    assert _parse_end_date("2026-10-05T11:02:00") == datetime(2026, 10, 5, 15, 2, tzinfo=timezone.utc)


def test_listings_browser_reads_supabase_not_sqlite(monkeypatch):
    seen: dict = {}

    def fetch_one(sql, params=()):
        seen["count_sql"] = sql
        return {"n": 1}

    def fetch_all(sql, params=()):
        seen["sql"], seen["params"] = sql, params
        return [{"asset_id": "28860/2863", "last_seen_at": datetime(2026, 10, 5, tzinfo=timezone.utc)}]

    monkeypatch.setattr(al.db, "fetch_one", fetch_one)
    monkeypatch.setattr(al.db, "fetch_all", fetch_all)
    total, rows = al.browse_listings(source="gd", q="Orlando", status="active", limit=10)
    assert total == 1 and rows[0]["last_seen_at"].startswith("2026-10-05")
    assert "FROM auction_listings" in seen["sql"]
    assert "America/New_York" in seen["sql"], "naive end dates are Eastern here too"
    assert seen["params"][-2:] == (10, 0)
    assert "%govdeals.com%" in seen["params"] and "%Orlando%" in seen["params"]


def test_listings_endpoint_does_not_open_sqlite():
    import inspect
    import importlib
    webapp = importlib.import_module("automation.web.app")
    src = inspect.getsource(webapp.list_raw_listings)
    assert "sqlite" not in src.lower() and "browse_listings" in src

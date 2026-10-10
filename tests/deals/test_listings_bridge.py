"""deals/listings_bridge.py — TXAuction deal_lots → auction_listings (the
Auctions tab's table). No DB, no LLM: db + the quantity refiner are faked."""
from datetime import datetime, timedelta, timezone

import pytest

from deals import listings_bridge as lb
from deals.profiles import SEED_PROFILES

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
CHAIRS = SEED_PROFILES["chairs"]


def _deal(native="31431/57702", title="(500) MTS Seating Omega Stacker event chairs",
          bid=5645.0, desc="(500) MTS Seating Omega Stacker event chairs. Dimensions: 22.5”"):
    return {"site": "txauction", "native_id": native, "title": title, "description": desc,
            "current_bid": bid, "city": "Austin", "state": "TX", "zip": "78741",
            "end_utc": NOW + timedelta(days=3, hours=4), "hero_image_url": "https://cdn/x.jpeg",
            "lot_number": "11"}


def test_listing_row_shape():
    r = lb.listing_row(_deal(), NOW)
    assert r["asset_id"] == "tx:57702"
    assert r["link"] == "https://www.txauction.com/auctions/31431/lot/57702"
    assert r["price"] == "$5,645.00"
    assert r["location"] == "Austin, TX, United States"
    assert r["end_date"] == "2026-10-13T12:00:00Z"
    assert (r["quantity"], r["quantity_source"]) == (500, "regex_title")     # seed only, untrusted
    assert r["pickup_zip"] == "78741" and r["image_url"] == "https://cdn/x.jpeg"
    assert set(r) == set(lb.COLS)


def test_listing_row_without_title_count_and_without_price():
    r = lb.listing_row(_deal(title="Stacking chairs, blue"), NOW)
    assert (r["quantity"], r["quantity_source"]) == (None, "llm_missing")
    with pytest.raises(ValueError):
        lb.listing_row(_deal(bid=None), NOW)                                 # never "$0.00"


def test_end_date_reads_back_through_the_one_parser():
    from auction_extractors.end_dates import parse_end_date
    r = lb.listing_row(_deal(), NOW)
    assert parse_end_date(r["end_date"]) == NOW + timedelta(days=3, hours=4)


def test_verify_quantities_batches_of_12_and_failure_is_never_the_seed():
    rows = [lb.listing_row(_deal(native=f"1/{i}"), NOW) for i in range(14)]
    calls = []

    def refine(items):
        calls.append(len(items))
        if len(calls) == 2:
            raise RuntimeError("groq 429")
        return [{"quantity": 480, "quantity_source": "llm", "quantity_confidence": "high"}
                if i % 2 == 0 else
                {"quantity": None, "quantity_source": "llm_missing", "quantity_confidence": "unknown"}
                for i, _ in enumerate(items)]

    ok, bad = lb.verify_quantities(rows, refine)
    assert calls == [12, 2]
    assert (ok, bad) == (6, 8)
    assert rows[0]["quantity"] == 480 and rows[0]["quantity_source"] == "llm"
    assert rows[1]["quantity"] is None and rows[1]["quantity_source"] == "llm_missing"
    assert all(r["quantity"] is None and r["quantity_source"] == "llm_failed" for r in rows[12:])


def test_upsert_keeps_first_seen_and_trusted_quantity():
    sql = lb.UPSERT
    assert "ON CONFLICT (asset_id) DO UPDATE" in sql
    assert "first_seen_at = EXCLUDED" not in sql
    for c in ("quantity", "quantity_source", "quantity_confidence"):
        assert f"{c} = CASE WHEN auction_listings.quantity_source IN ('llm', 'structured') " \
               f"THEN auction_listings.{c} ELSE EXCLUDED.{c} END" in sql
    assert "last_seen_at = EXCLUDED.last_seen_at" in sql


def test_mirror_filters_by_profile_and_skips_llm_for_trusted_rows(monkeypatch):
    art = _deal(native="31534/60733", title="ARTWORK: still life", bid=170.0,
                desc="oil on canvas; cracking on white of chair")
    wheel = _deal(native="1/2", title="(3) wheelchairs", bid=20.0, desc="")
    deals = [_deal(), _deal(native="31431/57700", title="(50) MTS Seating Omega Stacker event chairs",
                            bid=350.0), art, wheel]
    executed, sqls = [], []

    def fetch_all(sql, params=()):
        sqls.append((sql, params))
        if "FROM deal_lots" in sql:
            return [dict(d) for d in deals]
        return [{"asset_id": "tx:57702"}]                 # already llm-verified

    monkeypatch.setattr(lb.db, "fetch_all", fetch_all)
    monkeypatch.setattr(lb.db, "execute", lambda sql, params=(): executed.append(params))
    seen = []
    rep = lb.mirror("txauction", CHAIRS, now=NOW,
                    refine=lambda items: seen.extend(i["title"] for i in items) or
                    [{"quantity": 50, "quantity_source": "llm", "quantity_confidence": "high"}] * len(items))
    deal_sql, deal_params = sqls[0]
    assert deal_params[0] == "txauction" and "end_utc > now()" in deal_sql
    # "chair" sits in the artwork's description, so the profile keeps it (same rule as the
    # Auctions tab's SQL); the wheelchair is vetoed by the title exclusion.
    assert rep.candidates == 3 and rep.written == 3
    assert seen == ["(50) MTS Seating Omega Stacker event chairs", "ARTWORK: still life"]
    assert {p[0] for p in executed} == {"tx:57702", "tx:57700", "tx:60733"}


def test_mirror_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(lb.db, "fetch_all", lambda sql, params=(): [_deal()] if "deal_lots" in sql else [])
    monkeypatch.setattr(lb.db, "execute", lambda *a: (_ for _ in ()).throw(AssertionError("dry-run wrote")))
    rep = lb.mirror("txauction", CHAIRS, dry_run=True, now=NOW,
                    refine=lambda items: (_ for _ in ()).throw(AssertionError("dry-run called the LLM")))
    assert rep.candidates == 1 and rep.written == 0
    assert "tx:57702" in capsys.readouterr().out


def test_unknown_site_has_no_prefix():
    with pytest.raises(ValueError):
        lb.asset_key("publicsurplus", "123")

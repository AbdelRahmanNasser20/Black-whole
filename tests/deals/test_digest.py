from deals.digest import format_digest
from deals.fees import FeeModel

def test_digest_lists_lots_with_landed_cost_and_link():
    rows = [dict(asset_id=984, account_id=6466, auction_id=2, title="9 chairs",
                 current_bid=10.0, bid_count=0, city="Warren", state="ME",
                 end_utc="2026-07-03T13:00:00Z")]
    out = format_digest(rows, fees=FeeModel(buyer_premium_pct=0.125))
    assert "9 chairs" in out
    assert "/asset/984/6466" in out               # correct URL order
    assert "0 bids" in out

def test_digest_empty_is_friendly():
    assert "no " in format_digest([], fees=FeeModel()).lower()

def test_digest_label_and_profile_rows(monkeypatch):
    from deals import digest
    cap = {}
    monkeypatch.setattr(digest.db, "fetch_all", lambda sql, params=(): cap.update(sql=sql, params=params) or [])
    assert digest.candidate_rows(("title ILIKE ANY(%s)", [["%desk%"]])) == []
    assert "title ILIKE ANY(%s)" in cap["sql"] and ["%desk%"] in list(cap["params"])
    assert "bid_count = 0" in cap["sql"] and "interval '24 hours'" in cap["sql"]
    out = format_digest([], fees=FeeModel(), label="Desks")
    assert "Desks" in out

def test_digest_no_profile_reads_the_view(monkeypatch):
    from deals import digest
    cap = {}
    monkeypatch.setattr(digest.db, "fetch_all", lambda sql, params=(): cap.update(sql=sql) or [])
    digest.candidate_rows(None)
    assert cap["sql"] == "SELECT * FROM deal_candidates"

# tests/deals/test_saved_search_alerts.py
from deals.saved_search_alerts import format_search_alert

def test_format_lists_lots_with_urls():
    rows = [{"title": "40 chairs", "current_bid": 5.0, "bid_count": 0,
             "asset_id": 1, "account_id": 2, "city": "Richmond", "state": "VA"}]
    text = format_search_alert("cheap chairs", rows)
    assert "cheap chairs" in text and "40 chairs" in text
    assert "govdeals.com/en/asset/1/2" in text

def test_format_caps_at_ten():
    rows = [{"title": f"lot {i}", "current_bid": 1, "bid_count": 0,
             "asset_id": i, "account_id": 1, "city": "X", "state": "Y"} for i in range(25)]
    text = format_search_alert("s", rows)
    assert "lot 9" in text and "lot 11" not in text and "+15 more" in text

import pathlib
from deals.ebay_parse import parse_sold_page

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ebay_sold_sample_2026-07-17.html"

def test_parses_real_sold_page():
    data = parse_sold_page(FIXTURE.read_text())
    assert data["count"] >= 20                      # page had ~50 real cards
    assert data["median"] and data["median"] > 0
    first = data["items"][0]
    assert first["listing_id"] and first["title"] and first["price"] > 0
    assert first["url"].startswith("https://")

def test_placeholder_card_is_dropped():
    data = parse_sold_page(FIXTURE.read_text())
    assert all(i["title"].lower() != "shop on ebay" for i in data["items"])

def test_empty_html_yields_zero():
    assert parse_sold_page("<html></html>") == {
        "count": 0, "median": None, "mean": None, "items": []}

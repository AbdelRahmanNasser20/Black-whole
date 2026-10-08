from automation import retail_anchor


def test_chair_lot_gets_full_new_range_with_sources():
    a = retail_anchor.anchor({"title": "Mauve Banquet Chairs", "price_per_chair": 28})
    assert a["low"] == 24.49 and a["high"] == 749.00
    assert "savings_pct" not in a  # no "you save" claim: new can undercut used
    assert all(s["url"].startswith("https://") for s in a["sources"])


def test_tables_and_unpriced_lots_get_none():
    assert retail_anchor.anchor({"title": "60in round tables", "price_per_chair": 20}) is None
    assert retail_anchor.anchor({"title": "chairs", "price_per_chair": None}) is None
    assert retail_anchor.anchor({"title": "chairs", "price_per_chair": "garbage"}) is None

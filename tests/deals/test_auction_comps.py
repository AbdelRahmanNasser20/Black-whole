"""deals/auction_comps.py — Flip Score from closed auctions. Pure, no DB."""
from deals import auction_comps as ac


def _matcher(bid, title="2018 Ford Transit Cargo Van", sales=None, category="vehicles"):
    m = ac.Matcher()
    m.add_lot("lot", title=title, category=category, bid=bid, premium_pct=0.125)
    for i, (t, price) in enumerate(sales or []):
        m.add_sale(title=t, category=category, price=price, age_days=10 + i)
    return m


SALES = [(f"201{n % 4 + 6} Ford Transit Cargo Van", 9000 + 100 * n) for n in range(10)]


def test_tokens_drop_filler_and_split_the_year():
    words, year = ac.tokens("Lot of (30) 2018 Dell Latitude Laptops - See Description")
    assert year == 2018
    assert words == {"dell", "latitude", "laptop"}


def test_score_curve():
    assert ac.flip_score(100, 100, 25) == 50          # cost at the comp value
    assert ac.flip_score(50, 100, 25) == 80           # half the comp value
    assert ac.flip_score(300, 100, 25) == 10
    assert ac.flip_score(1, 100, 6) == 75             # weak evidence is clamped
    assert ac.flip_score(1, 100, 25, damaged=True) == 50
    assert ac.flip_score(10, 0, 25) == 0


def test_cheap_lot_scores_high_and_cost_includes_the_premium():
    res = _matcher(1000, sales=SALES).results()["lot"]
    assert res.comp_count == 10
    assert 9000 <= res.comp_value <= 9900
    assert res.cost == 1125.0
    assert res.score >= 95


def test_bid_at_comp_value_scores_near_50():
    res = _matcher(8400, sales=SALES).results()["lot"]
    assert 45 <= res.score <= 55


def test_too_few_comps_is_no_score():
    assert _matcher(1000, sales=SALES[:4]).results() == {}


def test_unrelated_and_other_category_sales_are_not_comps():
    m = _matcher(1000, sales=[("Steel Filing Cabinet 4 Drawer", 50)] * 10)
    assert m.results() == {}
    m = ac.Matcher()
    m.add_lot("lot", title="2018 Ford Transit Cargo Van", category="vehicles", bid=1000)
    for t, p in SALES:
        m.add_sale(title=t, category="other", price=p, age_days=5)
    assert m.results() == {}


def test_vehicle_comp_must_be_within_the_year_span():
    old = [("1999 Ford Transit Cargo Van", 800)] * 10
    assert _matcher(1000, sales=old).results() == {}


def test_scattered_comps_are_discarded():
    wild = [("2018 Ford Transit Cargo Van", p) for p in (100, 120, 150, 9000, 9500, 9800, 200, 9900)]
    assert _matcher(1000, sales=wild).results() == {}


def test_bulk_lot_is_priced_per_unit():
    sales = [("Lot of 2 Dell Latitude 5490 Laptops", 200 + 2 * n) for n in range(8)]
    res = _matcher(200, title="Lot of 10 Dell Latitude 5490 Laptops", sales=sales,
                   category="computers_electronics").results()["lot"]
    assert res.quantity == 10
    assert 1000 <= res.comp_value <= 1080


def test_damaged_lot_is_capped_and_damaged_sales_are_ignored():
    res = _matcher(100, title="2018 Ford Transit Cargo Van - Does not run", sales=SALES).results()["lot"]
    assert res.damaged and res.score <= ac.DAMAGE_CAP
    broken = [(t + " for parts", 300) for t, _ in SALES]
    assert _matcher(1000, sales=broken).results() == {}


def test_old_and_bad_sales_are_skipped():
    m = ac.Matcher()
    m.add_lot("lot", title="2018 Ford Transit Cargo Van", category="vehicles", bid=1000)
    for t, p in SALES:
        m.add_sale(title=t, category="vehicles", price=p, age_days=ac.WINDOW_DAYS + 1)
        m.add_sale(title=t, category="vehicles", price=0, age_days=5)
    assert m.results() == {}


def test_deal_rank_prefers_real_bids_and_a_near_close():
    assert ac.deal_rank(120, 100, 5, 9) == 0.0
    assert ac.deal_rank(100, 1000, 5, 9) > ac.deal_rank(100, 1000, 40, 9)
    assert ac.deal_rank(100, 1000, 5, 9) > ac.deal_rank(100, 1000, 5, 1)
    assert ac.deal_rank(1000, 10000, 5, 9) > ac.deal_rank(10, 100, 5, 9)


def test_weighted_median():
    assert ac.weighted_median([(1, 1), (2, 1), (100, 1)]) == 2
    assert ac.weighted_median([(1, 10), (2, 1), (100, 1)]) == 1


def test_bulk_lot_ignores_sales_with_no_stated_count():
    """A whole-lot price is not a unit price: 25 x $450 would read $11,000."""
    sales = [("Dell Latitude 5490 Laptops", 400 + 10 * n) for n in range(10)]
    m = _matcher(500, title="Lot of 25 Dell Latitude 5490 Laptops", sales=sales,
                 category="computers_electronics")
    assert m.results() == {}

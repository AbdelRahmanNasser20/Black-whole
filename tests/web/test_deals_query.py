from automation.web import deals_query
from deals.fees import FeeModel


def test_default_active_filter():
    where, args = deals_query.build_where()
    assert "outcome_complete IS NOT TRUE" in where
    assert "end_utc > now()" in where
    assert args == []


def test_status_closed_and_all():
    where, _ = deals_query.build_where(status="closed")
    assert where == "outcome_complete IS TRUE"
    where, _ = deals_query.build_where(status="all")
    assert where == "TRUE"


def test_search_matches_title_and_description():
    where, args = deals_query.build_where(q="chair", status="all")
    assert "title ILIKE %s" in where and "description ILIKE %s" in where
    assert args == ["%chair%", "%chair%"]


def test_combined_filters_order_and_args():
    where, args = deals_query.build_where(
        q="desk", category="Furniture", state="tx",
        max_bids=0, ending_within=48, status="active")
    assert where.count("%s") == len(args) == 6
    assert "canonical_category = %s" in where
    assert "state = %s" in where
    assert "bid_count <= %s" in where
    assert "make_interval(hours => %s)" in where
    assert "TX" in args  # state upper-cased
    assert 0 in args and 48 in args


def test_native_category_filter_uppercased():
    where, args = deals_query.build_where(native="47b", status="all")
    assert "native_category_id = %s" in where
    assert args == ["47B"]


def test_native_and_canonical_compose():
    where, args = deals_query.build_where(category="tools_shop", native="375", status="all")
    assert "canonical_category = %s" in where and "native_category_id = %s" in where
    assert args == ["tools_shop", "375"]


def test_order_clause_whitelist():
    assert deals_query.order_clause("ends", None) == "ORDER BY end_utc ASC NULLS LAST"
    assert deals_query.order_clause("bids", None) == "ORDER BY bid_count DESC NULLS LAST"
    assert deals_query.order_clause("landed", "asc") == "ORDER BY current_bid ASC NULLS LAST"
    # unknown sort / dir fall back safely — never raw user input
    assert deals_query.order_clause("evil; DROP TABLE", "x") == "ORDER BY end_utc ASC NULLS LAST"


def test_enrich_landed_cost_and_urls():
    fees = FeeModel(buyer_premium_pct=0.125, tax_pct=0.0, freight=0.0)
    row = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "current_bid": 100.0}
    out = deals_query.enrich(dict(row), fees)
    assert out["landed_cost"] == 112.5
    assert out["govdeals_url"] == "https://www.govdeals.com/en/asset/305/10340"
    assert out["viewer_url"] == "/deals/305/10340/1"


def test_enrich_null_bid():
    fees = FeeModel()
    out = deals_query.enrich({"asset_id": 1, "account_id": 2, "auction_id": 3,
                              "current_bid": None}, fees)
    assert out["landed_cost"] == 0.0

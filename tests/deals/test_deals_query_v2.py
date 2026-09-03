# tests/deals/test_deals_query_v2.py
from automation.web.deals_query import build_where, order_clause, SORTS


def test_min_margin_adds_verdict_filter():
    where, args = build_where(status="active", min_margin=150.0)
    assert "margin_pct" in where and 150.0 in args


def test_list_filter_uses_exists_with_binding():
    where, args = build_where(status="active", list_id=7)
    assert "deal_list_items" in where and 7 in args


def test_tag_filter_uses_exists_with_binding():
    where, args = build_where(status="active", tag="pallet")
    assert "deal_lot_tags" in where and "pallet" in args


def test_margin_sort_available():
    assert "margin" in SORTS
    assert "v.margin_pct" in order_clause("margin", "desc")


def test_no_new_filters_no_new_sql():
    where, args = build_where(status="active")
    assert "deal_list_items" not in where and "margin_pct" not in where
    assert "current_bid >=" not in where and "current_bid <=" not in where
    assert "BETWEEN" not in where


def test_bbox_tuple_emits_between_sql():
    # bbox = (south, west, north, east); args = lat pair then lng pair
    where, args = build_where(status="active", bbox=(33.0, -112.5, 34.0, -111.5))
    assert "lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s" in where
    assert args == [33.0, 34.0, -112.5, -111.5]


def test_min_price_filter():
    where, args = build_where(status="active", min_price=100.0)
    assert "current_bid >= %s" in where and 100.0 in args
    assert "current_bid <= %s" not in where


def test_max_price_filter():
    where, args = build_where(status="active", max_price=500.0)
    assert "current_bid <= %s" in where and 500.0 in args
    assert "current_bid >= %s" not in where


def test_min_max_price_filters():
    where, args = build_where(status="active", min_price=100.0, max_price=500.0)
    assert "current_bid >= %s" in where and "current_bid <= %s" in where
    assert args == [100.0, 500.0]


def test_combined_bbox_and_price():
    where, args = build_where(
        status="active", q="chair",
        min_price=50.0, max_price=250.0,
        bbox=(33.0, -112.5, 34.0, -111.5),
    )
    assert "(title ILIKE %s OR description ILIKE %s)" in where
    assert "current_bid >= %s" in where and "current_bid <= %s" in where
    assert "lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s" in where
    # args follow fragment order: q, q, min, max, then lat pair + lng pair
    assert args == ["%chair%", "%chair%", 50.0, 250.0, 33.0, 34.0, -112.5, -111.5]
    assert where.count(" AND ") >= 4


def test_profile_where_is_spliced_with_binding():
    where, args = build_where(status="active", profile_where=("(title ILIKE ANY(%s))", [["%desk%"]]))
    assert "(title ILIKE ANY(%s))" in where and [["%desk%"]][0] in args


def test_profile_where_none_adds_nothing():
    where, _ = build_where(status="active", profile_where=None)
    assert "ILIKE ANY" not in where

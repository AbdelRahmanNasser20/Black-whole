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

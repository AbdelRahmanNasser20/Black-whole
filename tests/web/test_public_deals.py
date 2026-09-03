"""Chair-buyer isolation + paging contract for the public /deals surface.
Pure SQL-building tests; fetch_* are covered via the endpoint tests in
tests/web/test_public_deals_api.py with the DB monkeypatched."""
import re

import pytest

from automation.web import public_deals as pd


def test_exclusion_where_names_every_operator_table():
    where, args = pd.exclusion_where()
    assert "canonical_category <> ALL(%s)" in where
    assert "COALESCE(title, '') !~* %s" in where
    for tbl in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert f"FROM {tbl}" in where
    assert args[0] == sorted(pd.EXCLUDED_CATEGORIES)
    assert args[1] == pd.EXCLUDED_TITLE_SQL_RE


def test_sql_regex_uses_postgres_word_boundary():
    # Postgres reads `\b` as backspace, so the Python-dialect regex would match
    # nothing in SQL and every chair lot would leak (live smoke, 2026-09-04).
    assert "\\b" not in pd.EXCLUDED_TITLE_SQL_RE and "\\y" in pd.EXCLUDED_TITLE_SQL_RE
    assert pd.EXCLUDED_TITLE_SQL_RE.replace("\\y", "\\b") == pd.EXCLUDED_TITLE_RE


def test_title_regex_blocks_seating_only():
    rx = re.compile(pd.EXCLUDED_TITLE_RE, re.I)
    assert rx.search("Lot of (199) Banquet Chairs")
    assert rx.search("Church pews - 40 sections")
    assert rx.search("Bar Stools, set of 12")
    assert not rx.search("Lot of (30) Lenovo Thinkpads T460")
    assert not rx.search("Wheelchair accessible van")  # 'chair' must be a whole word


def test_is_excluded_row():
    assert pd.is_excluded({"canonical_category": "seating_furniture", "title": "Desk"})
    assert pd.is_excluded({"canonical_category": "other", "title": "Stacking chairs x 200"})
    assert not pd.is_excluded({"canonical_category": "other", "title": "Vulcan fryer"})
    assert not pd.is_excluded({"canonical_category": None, "title": "Water plant pumps"})


def test_build_public_where_is_title_only_search():
    where, args = pd.build_public_where(q="laptop", status="active")
    assert "title ILIKE %s" in where and "description" not in where
    assert args.count("%laptop%") == 1


def test_public_order_rejects_private_sorts():
    assert pd.public_order("margin", None) == "ORDER BY end_utc ASC NULLS LAST"
    assert pd.public_order("bid", "asc") == "ORDER BY current_bid ASC NULLS LAST"
    assert pd.public_order("newest", None) == "ORDER BY first_seen_at DESC NULLS LAST"


def test_public_cols_never_leak_private_fields():
    for col in ("hero_image_url", "archived_hero_url", "gallery_urls", "description",
                "seller", "high_bidder", "raw"):
        assert col not in pd.PUBLIC_COLS


@pytest.mark.parametrize("page,per_page,expect", [(0, 25, (1, 25)), (5, 33, (5, 25)), (9999, 100, (400, 100))])
def test_page_clamping(page, per_page, expect):
    assert pd.clamp_page(page, per_page) == expect

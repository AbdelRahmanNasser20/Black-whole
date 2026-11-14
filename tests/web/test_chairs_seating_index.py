"""Migration 036's partial index is only used while its predicate matches
public_deals.seating_where() exactly. Change the seating constants and this test
fails: ship a new migration that drops and recreates the index with them."""
from pathlib import Path

from automation.web import public_deals

SQL = Path("scripts/sql/036_deal_lots_live_seating_index.sql").read_text()


def test_index_predicate_matches_seating_where():
    sql, (categories, title_re) = public_deals.seating_where()
    assert "COALESCE(canonical_category = ANY(%s), false) OR COALESCE(title, '') ~* %s" in sql
    assert "'{" + ",".join(categories) + "}'::text[]" in SQL
    assert "~* '" + title_re + "'" in SQL

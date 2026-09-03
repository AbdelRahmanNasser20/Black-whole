# tests/deals/test_profiles.py
import pytest
from deals import profiles
from deals.profiles import Profile, SEED_PROFILES


def _p(**over):
    base = dict(slug="x", name="X", keywords=["chair"], exclude_terms=["stool"],
                search_terms=["chairs"], native_category_ids=["372"],
                canonical_categories=[], min_quantity=50, item_noun="chairs",
                states=[], min_price=None, max_price=None, enabled=True, is_default=False)
    base.update(over)
    return Profile(**base)


def test_seeds_match_today_defaults():
    c = SEED_PROFILES["chairs"]
    assert c.is_default and c.min_quantity == 50 and "chair" in c.keywords
    assert "372" in c.native_category_ids and "266" in c.native_category_ids
    assert "chair cover" in c.exclude_terms
    m = SEED_PROFILES["medical"]
    assert m.min_quantity == 1 and "dental" in m.keywords and m.native_category_ids == ["67", "301"]


def test_matches_keyword_in_title_or_description():
    p = _p()
    assert profiles.matches(p, "Lot of 200 CHAIRS", "")
    assert profiles.matches(p, "Furniture lot", "banquet chair x 40")
    assert not profiles.matches(p, "Desks", "tables")


def test_exclude_term_on_title_wins():
    p = _p()
    assert not profiles.matches(p, "Chair scale stool", "")
    assert profiles.matched_keyword(p, "50 chairs", "") == "chair"
    assert profiles.matched_keyword(p, "desk", "") == ""


def test_empty_keywords_match_everything():
    assert profiles.matches(_p(keywords=[], exclude_terms=[]), "anything", None)


def test_deal_lots_where_binds_arrays_and_band():
    p = _p(states=["AZ", "nv"], min_price=10, max_price=500, canonical_categories=["seating_furniture"])
    where, args = profiles.deal_lots_where(p)
    assert "title ILIKE ANY(%s) OR description ILIKE ANY(%s)" in where
    assert "NOT (title ILIKE ANY(%s))" in where
    assert "canonical_category = ANY(%s)" in where
    assert "state = ANY(%s)" in where and "current_bid >= %s" in where and "current_bid <= %s" in where
    assert ["%chair%"] in args and ["%stool%"] in args
    assert ["AZ", "NV"] in args and 10.0 in args and 500.0 in args


def test_deal_lots_where_empty_profile_is_true():
    where, args = profiles.deal_lots_where(_p(keywords=[], exclude_terms=[]))
    assert where == "TRUE" and args == []


def test_auction_listings_where_uses_min_quantity_default_and_override():
    p = _p()
    where, args = profiles.auction_listings_where(p)
    assert "quantity >= %s" in where and 50 in args
    _, args2 = profiles.auction_listings_where(p, min_quantity=5)
    assert 5 in args2 and 50 not in args2


def test_validate_slug():
    assert profiles.validate_slug(" Office-Desks ") == "office-desks"
    with pytest.raises(ValueError):
        profiles.validate_slug("bad slug!")


def test_from_row_and_to_row_roundtrip():
    row = _p().to_row()
    assert row["keywords"] == ["chair"] and row["min_price"] is None
    assert profiles.from_row(row) == _p()


def test_resolve_none_returns_default(monkeypatch):
    monkeypatch.setattr(profiles, "list_all", lambda include_disabled=False: [_p(slug="a"), _p(slug="b", is_default=True)])
    assert profiles.resolve(None).slug == "b"


def test_resolve_unknown_raises(monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda slug: None)
    with pytest.raises(KeyError):
        profiles.resolve("nope")


def test_delete_refuses_default(monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda slug: _p(slug="chairs", is_default=True))
    with pytest.raises(ValueError):
        profiles.delete("chairs")


def test_table_absent_falls_back_to_seeds(monkeypatch):
    """research_profiles not yet applied to prod: every reader degrades to the
    built-in seeds instead of raising, so the default path keeps working."""
    def boom(sql, params=None):
        raise RuntimeError('relation "research_profiles" does not exist')
    monkeypatch.setattr(profiles.db, "fetch_all", boom)
    monkeypatch.setattr(profiles.db, "fetch_one", boom)
    assert [p.slug for p in profiles.list_all()] == ["chairs", "medical"]
    assert profiles.load("medical").min_quantity == 1
    assert profiles.load("nope") is None
    assert profiles.resolve(None).slug == "chairs"
    assert profiles.default_slug() == "chairs"

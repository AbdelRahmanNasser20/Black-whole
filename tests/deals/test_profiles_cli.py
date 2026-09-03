# tests/deals/test_profiles_cli.py
import sys
from datetime import datetime, timezone
from pathlib import Path

AE = str(Path(__file__).resolve().parents[2] / "auction_extractors")
if AE not in sys.path:
    sys.path.insert(0, AE)


def test_search_terms_env_override():
    import govdeals_chairs_extraction as gd
    assert gd.search_terms_from_env(["chairs"], {}) == ["chairs"]
    assert gd.search_terms_from_env(["chairs"], {"SCRAPE_SEARCH_TERMS": "desks, office desks ,"}) == ["desks", "office desks"]


def test_ps_search_terms_env_override():
    import public_surplus_automation as ps
    assert ps.search_terms_from_env(["chairs"], {"SCRAPE_SEARCH_TERMS": "lockers"}) == ["lockers"]


def test_quantity_prompt_uses_item_noun(monkeypatch):
    import quantity_llm
    monkeypatch.setenv("SCRAPE_ITEM_NOUN", "desks")
    assert "DESKS" in quantity_llm._quantity_prompt_header()
    monkeypatch.delenv("SCRAPE_ITEM_NOUN")
    # unset = today's prompt, byte-for-byte
    assert quantity_llm._quantity_prompt_header() == \
        "You estimate how many CHAIRS (individual chair units) are in each auction lot."


# ── deals CLI --profile (Task 7) ─────────────────────────────────────────────

def test_cli_profile_arg_defaults_from_env(monkeypatch):
    import argparse
    import deals.cli as cli
    monkeypatch.setenv("DEALS_PROFILE", "desks")
    ap = argparse.ArgumentParser(); cli.profile_arg(ap)
    assert ap.parse_args([]).profile == "desks"
    assert ap.parse_args(["--profile", "chairs"]).profile == "chairs"
    monkeypatch.delenv("DEALS_PROFILE")
    ap2 = argparse.ArgumentParser(); cli.profile_arg(ap2)
    assert ap2.parse_args([]).profile is None


def test_discover_with_profile_uses_native_ids(monkeypatch, capsys, make_lot):
    import deals.cli as cli
    import deals.profiles as profiles
    from deals.profiles import Profile
    prof = Profile(slug="desks", name="Desks", keywords=["desk"], native_category_ids=["372", "47B"])
    monkeypatch.setattr(profiles, "resolve", lambda slug: prof)
    seen = {}
    monkeypatch.setattr(cli, "run_discovery",
                        lambda adapter, categories, max_pages, archive_predicate=None:
                            seen.update(categories=categories, pred=archive_predicate) or "ok")
    monkeypatch.setattr(cli.sites, "get_adapter", lambda key: object())
    monkeypatch.setattr(sys, "argv", ["deals.cli", "discover", "--profile", "desks"])
    cli.main()
    assert seen["categories"] == ["372", "47B"]
    assert seen["pred"](make_lot(title="10 desks")) and not seen["pred"](make_lot(title="10 chairs"))


def test_discover_explicit_categories_beat_profile(monkeypatch, capsys):
    import deals.cli as cli
    import deals.profiles as profiles
    from deals.profiles import Profile
    prof = Profile(slug="desks", name="Desks", keywords=["desk"], native_category_ids=["372"])
    monkeypatch.setattr(profiles, "resolve", lambda slug: prof)
    seen = {}
    monkeypatch.setattr(cli, "run_discovery",
                        lambda adapter, categories, max_pages, archive_predicate=None:
                            seen.update(categories=categories) or "ok")
    monkeypatch.setattr(cli.sites, "get_adapter", lambda key: object())
    monkeypatch.setattr(sys, "argv", ["deals.cli", "discover", "--profile", "desks", "--categories", "22"])
    cli.main()
    assert seen["categories"] == ["22"]


def test_discover_without_profile_is_unchanged(monkeypatch, capsys):
    import deals.cli as cli
    monkeypatch.delenv("DEALS_PROFILE", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "run_discovery",
                        lambda adapter, categories, max_pages, archive_predicate=None:
                            seen.update(categories=categories, pred=archive_predicate) or "ok")
    monkeypatch.setattr(cli.sites, "get_adapter", lambda key: object())
    monkeypatch.setattr(sys, "argv", ["deals.cli", "discover"])
    cli.main()
    assert seen["categories"] == cli.DEFAULT_CATEGORIES and seen["pred"] is None


def test_store_extra_where_is_bound(monkeypatch):
    from deals import store
    cap = {}
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): cap.update(sql=sql, params=params) or [])
    store.bidder_targets(limit=5, category=None, extra_where=("title ILIKE ANY(%s)", [["%desk%"]]))
    assert "title ILIKE ANY(%s)" in cap["sql"] and ["%desk%"] in list(cap["params"])
    store.due_for_poll(datetime.now(timezone.utc), extra_where=("state = ANY(%s)", [["AZ"]]))
    assert "state = ANY(%s)" in cap["sql"] and ["AZ"] in list(cap["params"])

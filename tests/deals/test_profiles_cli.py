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

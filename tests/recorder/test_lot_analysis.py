"""Lot analysis — offline: fake identity/classify/comps, a LocalStore, no LLM, no DB."""
import json

import pytest

from deals.llm_provider import LlmUnavailable
from deals.llm_steps import LlmStepError, LotIdentity
from recorder import lot_analysis, lot_archive

DOC = {
    "lot_key": "5282/3780/2",
    "summary": {"title": "Lot of Approx. 150 Stacking Chairs", "description": "Used. 150 chairs.",
                "final_price": 1725.0, "bid_count": 54, "outcome": "sold",
                "category_name": "Furniture and Furnishings", "canonical_category": "seating_furniture",
                "condition_code": "SD"},
}


def _ident(lot):
    return LotIdentity(brand=None, model=None, item_type="Stacking Chair", quantity=150,
                       condition="Used", queries=['"stacking chair" bulk lot'], est_resale_per_unit=20.0)


def _classify(title, desc):
    return "seating_furniture", 0.97


def _comps(words, lot_key):
    rows = [{"title": f"Lot of ({n}) Stacking Chairs", "price": p, "asset_id": i, "account_id": 1,
             "auction_id": 1, "closed_at": "2026-08-01", "origin": "deal_lots"}
            for i, (n, p) in enumerate([(10, 100.0), (20, 300.0), (40, 400.0), (8, 96.0)])]
    rows.append({"title": "Bulk Stacking Chairs", "price": 20.0, "asset_id": 99, "account_id": 1,
                 "auction_id": 1, "origin": "deal_lots"})     # count unknown → excluded
    return rows, words[:2]


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr("deals.llm_provider.active_provider", lambda env=None: ("groq", "gsk_SECRET"))


def _run(**kw):
    kw.setdefault("identity_fn", _ident)
    kw.setdefault("classify_fn", _classify)
    kw.setdefault("comps_fn", _comps)
    return lot_analysis.analyze(DOC, **kw)


def test_keywords_are_specific_and_singular():
    assert lot_analysis.keywords("Banquet Chairs", ['"banquet chair" bulk lot']) == ["banquet", "chair"]
    assert lot_analysis.keywords("", [], "Lot of 30 Batteries") == ["battery"]


@pytest.mark.parametrize("final,verdict", [(5.0, "steal"), (8.0, "good deal"), (10.0, "market price"),
                                           (13.0, "above market")])
def test_deal_verdict_thresholds(final, verdict):
    assert lot_analysis.deal_verdict(final, [10.0, 10.0, 10.0], "sold")["verdict"] == verdict


def test_deal_verdict_needs_three_comps():
    v = lot_analysis.deal_verdict(5.0, [10.0, 10.0], "sold")
    assert v["verdict"] is None and "not enough comps" in v["verdict_label"]


def test_deal_verdict_labels_non_sales():
    assert "not a sale" in lot_analysis.deal_verdict(5.0, [10.0] * 3, "reserve_not_met")["verdict_label"]


def test_unknown_count_comps_only_count_for_single_units():
    assert lot_analysis.usable_for_per_unit(150, "default") is False
    assert lot_analysis.usable_for_per_unit(1, "default") is True
    assert lot_analysis.usable_for_per_unit(150, "title") is True


def test_analysis_ok_path():
    a = _run()
    assert a["status"] == "ok"
    assert a["category"]["llm"] == "seating_furniture"
    assert a["quantity"]["value"] == 150 and a["quantity"]["source"] == "title"
    assert a["condition"] == {"llm": "Used", "govdeals_code": "SD"}
    d = a["deal"]
    assert d["comps"] == 4 and d["excluded_unknown_count"] == 1
    assert d["final_per_unit"] == 11.5 and d["median_per_unit"] == 11.0
    assert d["verdict"] == "market price"
    assert a["resale"]["method"] == "llm_estimate" and a["resale"]["confidence"] == "low"
    assert a["resale"]["low"] == 2250.0 and a["resale"]["high"] == 3750.0
    assert [c["used"] for c in a["comps"]][-1] is False       # unused sorted last


def test_provider_key_is_never_stored():
    a = _run()
    assert a["provider"] == "groq"
    assert "gsk_SECRET" not in json.dumps(a, default=str)


def test_identity_failure_is_unavailable_not_a_default():
    def boom(lot):
        raise LlmStepError("unparseable identity response: ''")
    a = _run(identity_fn=boom)
    assert a["status"] == "unavailable" and "unparseable" in a["error"]
    assert "deal" not in a and "resale" not in a and "category" not in a


def test_category_failure_leaves_llm_null():
    def boom(t, d):
        raise LlmUnavailable("groq HTTP 429")
    a = _run(classify_fn=boom)
    assert a["status"] == "ok" and a["category"]["llm"] is None and "429" in a["category"]["error"]


def test_no_estimate_and_no_comps_says_unavailable():
    def ident(lot):
        i = _ident(lot)
        i.est_resale_per_unit = None
        return i
    a = _run(identity_fn=ident, comps_fn=lambda w, k: ([], []))
    assert a["resale"]["method"] == "unavailable"
    assert a["deal"]["verdict"] is None


def test_comps_db_error_is_reported():
    def boom(w, k):
        raise RuntimeError("pooler full")
    a = _run(comps_fn=boom)
    assert a["comps_error"] == "pooler full" and a["deal"]["verdict"] is None


def test_analyze_and_store_caches(tmp_path, monkeypatch):
    store = lot_archive.LocalStore(tmp_path)
    store.put(lot_archive.doc_key("5282/3780/2"), lot_archive.serialize(DOC), "application/gzip")
    monkeypatch.setattr(lot_archive, "summarize", lambda *a, **k: DOC["summary"])
    a = lot_analysis.analyze_and_store(store, "5282/3780/2", identity_fn=_ident, classify_fn=_classify,
                                       comps_fn=_comps, ebay_provider=None)
    assert a["status"] == "ok"
    assert lot_analysis.load(store, "5282/3780/2")["deal"]["verdict"] == "market price"


def test_analyze_unarchived_lot(tmp_path):
    a = lot_analysis.analyze_and_store(lot_archive.LocalStore(tmp_path), "1/2/3")
    assert a["status"] == "unavailable" and a["error"] == "lot is not archived"

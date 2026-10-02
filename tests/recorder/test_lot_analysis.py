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


def _comps(plan, lot_key):
    rows = [{"title": f"Lot of ({n}) Stacking Chairs", "price": p, "asset_id": i, "account_id": 1,
             "auction_id": 1, "closed_at": "2026-08-01", "origin": "deal_lots"}
            for i, (n, p) in enumerate([(10, 100.0), (20, 300.0), (40, 400.0), (8, 96.0)])]
    rows.append({"title": "Bulk Stacking Chairs", "price": 20.0, "asset_id": 99, "account_id": 1,
                 "auction_id": 1, "origin": "deal_lots"})     # count unknown → excluded
    return rows


def _keep_all(ident, comps):
    return list(range(len(comps)))


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr("deals.llm_provider.active_provider", lambda env=None: ("groq", "gsk_SECRET"))


def _run(**kw):
    kw.setdefault("identity_fn", _ident)
    kw.setdefault("classify_fn", _classify)
    kw.setdefault("comps_fn", _comps)
    kw.setdefault("judge_fn", _keep_all)
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
    a = _run(identity_fn=ident, comps_fn=lambda w, k: [])
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
                                       comps_fn=_comps, judge_fn=_keep_all, ebay_provider=None)
    assert a["status"] == "ok"
    assert lot_analysis.load(store, "5282/3780/2")["deal"]["verdict"] == "market price"


def test_analyze_unarchived_lot(tmp_path):
    a = lot_analysis.analyze_and_store(lot_archive.LocalStore(tmp_path), "1/2/3")
    assert a["status"] == "unavailable" and a["error"] == "lot is not archived"


# --- v2 comp matching (2026-10-02) ------------------------------------------------

CAR_COVER_DOC = {
    "lot_key": "77/88/1",
    "summary": {"title": "Lot of (2) Range Rover car covers", "description": "Two fitted covers.",
                "final_price": 60.0, "bid_count": 4, "outcome": "sold",
                "category_name": "Automotive Parts", "canonical_category": "general_merchandise"},
}


def _car_cover_ident(lot):
    return LotIdentity(brand="Range Rover", model=None, item_type="car cover", quantity=2,
                       condition="Used", queries=["range rover car cover"], est_resale_per_unit=40.0)


def _row(title, price, cat="other", cur="USD", i=1):
    return {"title": title, "price": price, "asset_id": i, "account_id": 1, "auction_id": 1,
            "origin": "deal_lots", "canonical_category": cat, "currency_code": cur}


CAR_COVER_ROWS = [
    _row("2016 Range Rover Sport", 21000.0, "vehicles", i=1),
    _row("Car cover fitted sedan", 35.0, "general_merchandise", i=2),
    _row("Toyota car", 4000.0, "vehicles", i=3),
    _row("Hard cover notebook", 5.0, "other", i=4),
]


def test_range_rover_car_covers_only_match_the_car_cover_row():
    judge_calls = []
    a = lot_analysis.analyze(CAR_COVER_DOC, identity_fn=_car_cover_ident,
                             classify_fn=lambda t, d: ("other", 0.5),
                             comps_fn=lambda plan, key: list(CAR_COVER_ROWS),
                             judge_fn=lambda i, c: judge_calls.append(c) or [])
    used = [c["title"] for c in a["comps"] if c["used"]]
    assert used == ["Car cover fitted sedan"]
    assert [c["title"] for c in a["comps"]] == ["Car cover fitted sedan"]
    assert a["deal"]["verdict"] is None
    assert "not enough comps" in a["deal"]["verdict_label"]
    assert a["deal"]["match_method"] == "phrase"
    assert judge_calls == []            # 1 candidate < MIN_COMPS: no LLM call
    assert a["version"] == lot_analysis.ANALYSIS_VERSION == 2


def test_match_plan_and_matcher():
    plan = lot_analysis.match_plan("car cover", "Range Rover", None, [], None)
    assert plan.phrase == ["car", "cover"] and plan.head == "cover"
    assert set(plan.discriminators) == {"car", "range", "rover"}
    assert plan.match("CAR COVERS (lot of 3)") == "phrase"
    assert plan.match("Cover for Range Rover, grey") == "head+discriminator"
    assert plan.match("Hard cover notebook") is None
    assert plan.match("2016 Range Rover Sport") is None
    assert plan.match("Carcover") is None                      # word boundaries, not substrings
    assert lot_analysis.match_plan("unknown", queries=["stacking chairs"]).phrase == ["stacking", "chair"]
    assert lot_analysis.match_plan(None, queries=[], title="Lot of 30") is None


def test_sql_uses_postgres_word_boundaries_never_backslash_b():
    plan = lot_analysis.match_plan("battery charger", None, None, [], None)
    phrase, disc, head, disc2 = plan.sql_params()
    assert phrase == r"\ybatter(?:y|ies)(?:\s|-)+chargers?\y"
    assert head == r"\ychargers?\y" and disc == disc2 == r"\y(?:batter(?:y|ies))\y"
    for p in (phrase, disc, head):
        assert r"\b" not in p
    assert "ILIKE" not in lot_analysis._DEAL_LOT_COMPS_SQL
    assert "ILIKE" not in lot_analysis._SOLD_COMPS_SQL
    assert "currency_code = 'USD'" in lot_analysis._DEAL_LOT_COMPS_SQL


def test_currency_and_category_gates():
    plan = lot_analysis.match_plan("stacking chair", None, None, [], None)
    rows = [_row("Stacking chairs (10)", 100.0, "seating_furniture", i=1),
            dict(_row("Stacking chairs (10)", 90.0, "seating_furniture", cur="EUR", i=2), account_id=2),
            _row("Stacking chairs toy set", 5.0, "collectibles_jewelry", i=3),
            _row("Stacking chairs misc", 50.0, "general_merchandise", i=4)]
    kept, counts = lot_analysis.filter_comps(plan, rows, "seating_furniture")
    assert [r["asset_id"] for r in kept] == [1, 4]
    assert counts["currency"] == 1 and counts["category"] == 1


def test_judge_decides_and_unavailable_judge_keeps_nothing():
    a = _run(judge_fn=lambda ident, comps: [0, 1, 2])
    assert a["deal"]["judged"] is True
    assert sum(1 for c in a["comps"] if c["used"]) == 3
    assert sum(1 for c in a["comps"] if c["kept"] is False) == 2

    def down(ident, comps):
        raise LlmUnavailable("groq HTTP 503")
    b = _run(judge_fn=down)
    assert b["deal"]["judged"] is False and "503" in b["deal"]["judge_error"]
    assert not any(c["used"] for c in b["comps"])
    assert "not enough comps" in b["deal"]["verdict_label"]


def test_relists_of_the_same_asset_are_never_its_comps_and_titles_dedupe():
    plan = lot_analysis.match_plan("stacking chair", None, None, [], None)
    relist = dict(_row("Lot of Approx. 150 Stacking Chairs", 823.0, "seating_furniture", i=5282),
                  account_id=3780, auction_id=1, closed_at="2026-09-01T00:00:00+00:00")
    a1 = dict(_row("Stacking chairs (10)", 100.0, "seating_furniture", i=11), account_id=77,
              closed_at="2026-08-01T00:00:00+00:00")
    a2 = dict(a1, asset_id=12, price=120.0, closed_at="2026-09-15T00:00:00+00:00")   # same seller+title
    other = dict(_row("Stacking chairs (10)", 90.0, "seating_furniture", i=13), account_id=78)
    sc_relist = {"title": "Stacking Chairs lot", "price": 50.0, "source": "govdeals",
                 "source_lot_id": "5282/3780/1", "origin": "sold_comps:govdeals", "currency_code": "USD"}
    kept, counts = lot_analysis.filter_comps(plan, [relist, a1, a2, other, sc_relist],
                                             "seating_furniture", "5282/3780/2")
    assert [(r["asset_id"], r["price"]) for r in kept] == [(12, 120.0), (13, 90.0)]
    assert counts["same_asset"] == 2 and counts["duplicate"] == 1


def test_comp_sql_excludes_every_auction_run_of_the_asset():
    assert "NOT (asset_id = %s AND account_id = %s)" in lot_analysis._DEAL_LOT_COMPS_SQL
    assert "auction_id = %s" not in lot_analysis._DEAL_LOT_COMPS_SQL
    assert "split_part(c.source_lot_id, '/', 1)" in lot_analysis._SOLD_COMPS_SQL


def test_old_version_cache_is_not_a_cache_hit(tmp_path, monkeypatch):
    from recorder import cli
    store = lot_archive.LocalStore(tmp_path)
    store.put(lot_archive.analysis_key("5282/3780/2"),
              json.dumps({"status": "ok", "version": 1}).encode(), "application/json")
    ran = []
    monkeypatch.setattr(lot_analysis, "analyze_and_store",
                        lambda st, key, *a, **k: ran.append(key) or {"status": "ok"})
    cli.cmd_archive_analyze(5, lot="5282/3780/2", archive_store=store)
    assert ran == ["5282/3780/2"]
    store.put(lot_archive.analysis_key("5282/3780/2"),
              json.dumps({"status": "ok", "version": lot_analysis.ANALYSIS_VERSION}).encode(),
              "application/json")
    cli.cmd_archive_analyze(5, lot="5282/3780/2", archive_store=store)
    assert ran == ["5282/3780/2"]

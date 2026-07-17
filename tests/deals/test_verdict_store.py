# tests/deals/test_verdict_store.py
from deals.verdict_store import VERDICT_COLUMNS, verdict_row

def _v():
    return {c: None for c in VERDICT_COLUMNS} | {
        "asset_id": 1, "account_id": 2, "auction_id": 3,
        "identity": {"brand": "Steelcase"}, "queries": ["steelcase leap v2"],
        "method": "comps", "comps": [{"title": "x", "price": 100.0}],
        "comp_count": 5, "per_unit": 100.0, "recovery_tier": 0.4,
        "est_resale": 400.0, "landed_cost": 50.0,
        "margin": 350.0, "margin_pct": 700.0, "confidence": "medium"}

def test_row_matches_columns_order_and_serializes_json():
    row = verdict_row(_v())
    assert len(row) == len(VERDICT_COLUMNS)
    i_identity = VERDICT_COLUMNS.index("identity")
    assert isinstance(row[i_identity], str)          # json.dumps'd
    i_comps = VERDICT_COLUMNS.index("comps")
    assert isinstance(row[i_comps], str)

def test_queries_stays_a_list_for_text_array_binding():
    row = verdict_row(_v())
    assert row[VERDICT_COLUMNS.index("queries")] == ["steelcase leap v2"]

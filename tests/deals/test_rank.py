# tests/deals/test_rank.py
from deals.rank import build_rank_prompt, parse_rank_response

VERDICTS = [{"asset_id": 1, "account_id": 2, "auction_id": 3,
             "identity": {"brand": "Steelcase", "item_type": "chair", "quantity": 40},
             "est_resale": 1600.0, "margin_pct": 700.0, "landed_cost": 100.0,
             "confidence": "medium", "comp_count": 5,
             "comps": [{"title": "leap v2", "price": 100.0, "url": "u"}]}]

def test_prompt_contains_lot_facts_and_json_contract():
    p = build_rank_prompt(VERDICTS)
    assert "Steelcase" in p and "700" in p and '"index"' in p

def test_parse_happy_path():
    out = parse_rank_response('[{"index": 0, "score": 7.5, "notes": "solid comps"}]')
    assert out[0]["score"] == 7.5

def test_parse_fenced_and_garbage():
    assert parse_rank_response('```json\n[{"index":0,"score":1,"notes":""}]\n```')[0]["index"] == 0
    assert parse_rank_response("sorry") == []

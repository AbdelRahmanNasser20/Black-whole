import pytest
from deals.comps import Comp
from deals.llm_steps import LotIdentity, parse_identity_response, parse_judge_response

def test_parse_identity_happy_path():
    text = '''```json
    {"brand": "Steelcase", "model": "Leap V2", "item_type": "office chair",
     "quantity": 40, "condition": "used",
     "queries": ["steelcase leap v2 chair", "steelcase leap chair used"],
     "est_resale_per_unit": 150}
    ```'''
    ident = parse_identity_response(text)
    assert ident.brand == "Steelcase" and ident.quantity == 40
    assert ident.queries[0] == "steelcase leap v2 chair"

def test_parse_identity_defaults_quantity_to_1():
    ident = parse_identity_response('{"item_type": "chair", "queries": ["chair"]}')
    assert ident.quantity == 1 and ident.brand is None

def test_parse_identity_garbage_raises():
    from deals.llm_steps import LlmStepError
    with pytest.raises(LlmStepError):
        parse_identity_response("I cannot help with that")

def test_parse_judge_keeps_by_index():
    comps = [Comp("1", "leap v2", 100, None, ""), Comp("2", "aeron", 900, None, ""),
             Comp("3", "leap v2 headrest only", 40, None, "")]
    kept = parse_judge_response('{"keep": [0]}', comps)
    assert [c.listing_id for c in kept] == ["1"]

def test_parse_judge_bad_indices_ignored():
    comps = [Comp("1", "x", 10, None, "")]
    assert parse_judge_response('{"keep": [0, 7, -2]}', comps) == [comps[0]]

def test_parse_judge_garbage_returns_empty():
    assert parse_judge_response("no json here", [Comp("1", "x", 10, None, "")]) == []

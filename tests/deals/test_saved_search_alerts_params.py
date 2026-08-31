# tests/deals/test_saved_search_alerts_params.py
"""Unit tests for deals.saved_search_alerts._sanitize_params — pure, no DB."""
from deals.saved_search_alerts import _sanitize_params


def test_bbox_string_becomes_float_tuple():
    out = _sanitize_params({"bbox": "33.0,-112.5,34.0,-111.0"})
    assert out["bbox"] == (33.0, -112.5, 34.0, -111.0)
    assert all(isinstance(x, float) for x in out["bbox"])


def test_bbox_string_with_spaces():
    out = _sanitize_params({"bbox": " 33.0 , -112.5 , 34.0 , -111.0 "})
    assert out["bbox"] == (33.0, -112.5, 34.0, -111.0)


def test_bbox_jsonb_array_becomes_float_tuple():
    out = _sanitize_params({"bbox": [33, -112.5, "34.0", -111]})
    assert out["bbox"] == (33.0, -112.5, 34.0, -111.0)


def test_bad_bbox_dropped_rest_kept():
    for bad in ("a,b,c,d", "1,2,3", "1,2,3,4,5", "", None, {"x": 1}, 42,
                [1, 2, 3], [1, 2, 3, None]):
        out = _sanitize_params({"bbox": bad, "state": "AZ"})
        assert "bbox" not in out, f"bbox={bad!r} should be dropped"
        assert out == {"state": "AZ"}


def test_prices_coerced_to_float():
    out = _sanitize_params({"min_price": "100", "max_price": 250})
    assert out == {"min_price": 100.0, "max_price": 250.0}
    assert isinstance(out["min_price"], float)
    assert isinstance(out["max_price"], float)


def test_bad_price_dropped_other_kept():
    out = _sanitize_params({"min_price": "abc", "max_price": "50", "q": "chair"})
    assert "min_price" not in out
    assert out == {"max_price": 50.0, "q": "chair"}


def test_none_price_dropped():
    out = _sanitize_params({"min_price": None})
    assert out == {}


def test_unknown_keys_stripped_allowed_kept():
    out = _sanitize_params({"q": "chair", "state": "AZ", "sort": "price",
                            "page": 2, "max_bids": 0})
    assert out == {"q": "chair", "state": "AZ", "max_bids": 0}


def test_empty_and_none_input():
    assert _sanitize_params({}) == {}
    assert _sanitize_params(None) == {}

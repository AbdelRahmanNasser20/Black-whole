from datetime import datetime, timezone
from deals.models import Lot
from deals.classify import apply_classification, CANONICAL_LABELS

def _lot(cat_id, canon):
    return Lot(1,2,3,"9 stackable banquet chairs","chairs", cat_id,"General Merchandise",canon,
        datetime(2026,7,3,tzinfo=timezone.utc),0,10.0,10.0,"USD",0,False,False,None,False,
        "s","c","st","z",None,None,"",("STA"),False,{})

def test_labels_include_seating_and_general():
    assert "seating_furniture" in CANONICAL_LABELS and "general_merchandise" in CANONICAL_LABELS

def test_apply_records_llm_and_agreement_true():
    lot = apply_classification(_lot("372","seating_furniture"),
                               classifier=lambda t,d: ("seating_furniture", 0.95))
    assert lot.llm_category == "seating_furniture"
    assert lot.llm_category_confidence == 0.95
    assert lot.category_agreement is True

def test_disagreement_flagged_on_general_merch_catchall():
    # native code said general_merchandise, but the LLM reads the text as furniture
    lot = apply_classification(_lot("266","general_merchandise"),
                               classifier=lambda t,d: ("seating_furniture", 0.9))
    assert lot.category_agreement is False
    assert lot.llm_category == "seating_furniture"

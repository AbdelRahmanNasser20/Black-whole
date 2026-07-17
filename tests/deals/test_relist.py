from deals.relist import title_similarity, find_relist

def test_similarity_ignores_order_and_case():
    assert title_similarity("Steelcase Leap V2 Chairs (40)",
                            "chairs steelcase LEAP v2 40") > 0.8

def test_similarity_disjoint_is_zero():
    assert title_similarity("forklift", "office chairs") == 0.0

CLOSED = [{"asset_id": 9, "account_id": 5, "auction_id": 100,
           "title": "Lot of 40 Steelcase Leap V2 Chairs", "final_bid": 0.0,
           "closed_at": "2026-07-10"}]

def test_find_relist_matches_same_account_similar_title():
    new = {"asset_id": 77, "account_id": 5, "auction_id": 200,
           "title": "40 Steelcase Leap V2 Chairs — RELISTED"}
    m = find_relist(new, CLOSED)
    assert m and m["auction_id"] == 100

def test_no_match_across_accounts():
    new = {"asset_id": 77, "account_id": 6, "auction_id": 200,
           "title": "Lot of 40 Steelcase Leap V2 Chairs"}
    assert find_relist(new, CLOSED) is None

def test_same_auction_is_not_a_relist():
    new = {"asset_id": 9, "account_id": 5, "auction_id": 100,
           "title": "Lot of 40 Steelcase Leap V2 Chairs"}
    assert find_relist(new, CLOSED) is None

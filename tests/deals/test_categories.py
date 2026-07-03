from deals.categories import canonical_category, SEATING_FURNITURE_CODES, GENERAL_MERCH_CODE

def test_furniture_cluster_maps_to_one_supercategory():
    for code in ("372", "47B", "47C", "47A", "46", "47D", "28E"):
        assert canonical_category(code) == "seating_furniture"

def test_general_merchandise_is_its_own_bucket_not_furniture():
    assert canonical_category("266") == "general_merchandise"
    assert GENERAL_MERCH_CODE == "266"

def test_unknown_code_falls_back_to_other():
    assert canonical_category("ZZZ") == "other"

def test_vehicle_codes_map_to_vehicles():
    assert canonical_category("94A") == "vehicles"   # Automobiles/Cars
    assert canonical_category("94D") == "vehicles"   # Vans

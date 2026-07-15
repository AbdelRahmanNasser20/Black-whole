"""Map GovDeals native category codes to our canonical supercategories.
Native codes verified from a full day's sweep (2026-07-03). The seating/
furniture inventory we resell is scattered across several native codes;
"General Merchandise" (266) is a ~37% catch-all that tells us nothing, so it
is its OWN bucket (never folded into furniture) and must be LLM-scanned."""

SEATING_FURNITURE_CODES = {"372", "47B", "47C", "47A", "46", "47D", "28E"}
GENERAL_MERCH_CODE = "266"
AV_EQUIPMENT_CODE = "22"   # Audio/Visual Equipment — projectors, screens, sound gear
# Resale-vetted verticals added 2026-07-03 (supply from a 14.4k-lot firehose sample,
# demand from flipper-market research — see docs/deals-market-research-2026-07.md):
TOOLS_SHOP_CODES = {"90", "249", "375", "28I", "153", "159", "95"}      # tools, power tools, generators, compressors, welding
KITCHEN_RESTAURANT_CODES = {"287", "21", "632", "631", "630", "25U"}    # commercial food service + kitchen
COMMS_RADIO_CODES = {"28", "28S"}                                       # two-way radios / comms equipment
LAB_TEST_CODES = {"57", "57M", "326", "57I", "57D", "575"}              # laboratory + test & measurement
MEDICAL_CODES = {"67", "301"}                                           # Class I medical + hospital equipment
FITNESS_CODES = {"147", "208"}                                          # exercise + fitness/recreation
MUSIC_CODES = {"70"}                                                    # musical instruments (school band)
LAWN_LANDSCAPING_CODES = {"71", "373", "40"}                            # mowing, parks/grounds, nursery
_VEHICLE_PREFIXES = ("94", "64", "95", "645", "643", "644", "648", "649", "642", "646", "655", "656", "657")

def canonical_category(native_id: str) -> str:
    code = (native_id or "").strip().upper()
    if code in SEATING_FURNITURE_CODES:
        return "seating_furniture"
    if code == GENERAL_MERCH_CODE:
        return "general_merchandise"
    if code == AV_EQUIPMENT_CODE:
        return "av_equipment"
    # exact-set checks must run before the vehicle prefix scan: welding is "95",
    # which the "95" vehicle prefix would otherwise swallow
    if code in TOOLS_SHOP_CODES:
        return "tools_shop"
    if code in KITCHEN_RESTAURANT_CODES:
        return "kitchen_restaurant"
    if code in COMMS_RADIO_CODES:
        return "comms_radios"
    if code in LAB_TEST_CODES:
        return "lab_test_equipment"
    if code in MEDICAL_CODES:
        return "medical_equipment"
    if code in FITNESS_CODES:
        return "fitness_equipment"
    if code in MUSIC_CODES:
        return "musical_instruments"
    if code in LAWN_LANDSCAPING_CODES:
        return "lawn_landscaping"
    if any(code.startswith(p) for p in _VEHICLE_PREFIXES):
        return "vehicles"
    if code in {"56", "180", "171", "56B", "149"}:
        return "collectibles_jewelry"
    if code in {"29", "217", "219", "220", "221", "222", "218", "244", "291"}:
        return "computers_electronics"
    return "other"

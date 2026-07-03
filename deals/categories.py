"""Map GovDeals native category codes to our canonical supercategories.
Native codes verified from a full day's sweep (2026-07-03). The seating/
furniture inventory we resell is scattered across several native codes;
"General Merchandise" (266) is a ~37% catch-all that tells us nothing, so it
is its OWN bucket (never folded into furniture) and must be LLM-scanned."""

SEATING_FURNITURE_CODES = {"372", "47B", "47C", "47A", "46", "47D", "28E"}
GENERAL_MERCH_CODE = "266"
_VEHICLE_PREFIXES = ("94", "64", "95", "645", "643", "644", "648", "649", "642", "646", "655", "656", "657")

def canonical_category(native_id: str) -> str:
    code = (native_id or "").strip().upper()
    if code in SEATING_FURNITURE_CODES:
        return "seating_furniture"
    if code == GENERAL_MERCH_CODE:
        return "general_merchandise"
    if any(code.startswith(p) for p in _VEHICLE_PREFIXES):
        return "vehicles"
    if code in {"56", "180", "171", "56B", "149"}:
        return "collectibles_jewelry"
    if code in {"29", "217", "219", "220", "221", "222", "218", "244", "291"}:
        return "computers_electronics"
    return "other"

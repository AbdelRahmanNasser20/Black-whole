"""
Shared helper: upgrade/correct listing quantities using the regex on
title + description together.

Extracted from govdeals_chairs_extraction.py so public_surplus_automation.py
can reuse the same behavior. The logic is source-agnostic — both scrapers
seed listings with ``quantity_source = "regex_title"`` and want the same
fulltext-refine semantics before the optional LLM pass.
"""

from __future__ import annotations

from quantity_infer import infer_chair_quantity_from_title


# Any title-only quantity above this is almost certainly an auction ID /
# lot number / year that slipped through — if the fulltext-regex returns a
# much smaller number for the same row, trust the fulltext.
_SUSPICIOUS_HIGH = 9999


def refine_quantities_with_regex_fulltext(listings: list[dict]) -> list[dict]:
    """Re-run the regex over ``title + description`` and update in place.

    Rules:
      - Only rows tagged ``regex_title`` are touched. LLM-tagged rows are
        left alone (the LLM is assumed to be the stronger signal).
      - If the cached title-regex value was obviously-wrong (>9999, i.e. an
        auction ID or lot number) and fulltext returns a smaller number,
        **correct** it (don't just upgrade).
      - If fulltext returns a larger number than title-only, upgrade.
      - If fulltext corroborates the title-only value (equal, >1), promote
        the source to ``regex_fulltext``.
    """
    if not listings:
        return listings
    upgraded = corrected = 0
    for item in listings:
        src = item.get("quantity_source") or "regex_title"
        if src != "regex_title":
            continue
        title = item.get("title") or ""
        desc = item.get("description") or ""
        if not desc:
            continue
        old_qty = int(item.get("quantity") or 1)
        new_qty = infer_chair_quantity_from_title(f"{title}\n{desc}")
        if old_qty > _SUSPICIOUS_HIGH and new_qty < old_qty:
            item["quantity"] = new_qty
            item["quantity_source"] = "regex_fulltext"
            item["quantity_confidence"] = "medium"
            corrected += 1
        elif new_qty > old_qty:
            item["quantity"] = new_qty
            item["quantity_source"] = "regex_fulltext"
            item["quantity_confidence"] = "medium"
            upgraded += 1
        elif new_qty == old_qty and old_qty > 1:
            item["quantity_source"] = "regex_fulltext"
            item["quantity_confidence"] = "medium"
    print(f"   → regex(fulltext) upgraded {upgraded}/{len(listings)} listings")
    if corrected:
        print(f"   → regex(fulltext) corrected {corrected} cached-garbage rows (>{_SUSPICIOUS_HIGH})")
    return listings

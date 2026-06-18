"""
Shared helper: upgrade/correct listing quantities using the regex on
title + description together.

Extracted from govdeals_chairs_extraction.py so public_surplus_automation.py
can reuse the same behavior. The logic is source-agnostic — both scrapers
seed listings with ``quantity_source = "regex_title"`` and want the same
fulltext-refine semantics before the optional LLM pass.
"""

from __future__ import annotations

import re

from quantity_infer import infer_chair_quantity_from_title


# Any title-only quantity above this is almost certainly an auction ID /
# lot number / year that slipped through — if the fulltext-regex returns a
# much smaller number for the same row, trust the fulltext.
_SUSPICIOUS_HIGH = 9999

# Sellers splitting one batch across several auctions repeat the GRAND TOTAL
# in every description ("105 chairs total across our listings, sold in groups
# of 1, 4, and 12") or point at sibling lots ("(2) other sets of 8 chairs are
# also being posted"). Any number in such a sentence is more likely the
# cross-listing total than this lot's count, so those sentences are removed
# before the fulltext regex runs. Sentence-level (not row-level) because the
# same phrases appear in harmless footers ("Check out our other auctions!")
# on rows whose real count lives in a different sentence. Each phrase below
# was seen in a real description that produced a wrong quantity in
# state/listings.db.
_CROSS_LISTING_RE = re.compile(
    r"(?i)\b(?:"
    r"other\s+(?:listing|auction|lot|set)s?"
    r"|separate\s+(?:listing|auction|lot)s?"
    r"|sister\s+(?:listing|auction)s?"
    r"|also\s+(?:being\s+)?(?:posted|listed)"
    r"|posted\s+separately"
    r"|listed\s+separately"
    r"|across\s+(?:multiple|several|many|our|all)\s+(?:listing|auction|lot)s?"
    r"|total\s+across"
    r"|sold\s+in\s+(?:groups|sets|quantities|batches)\s+of"
    # "(This is part of 105 chairs being sold in varying numbers and
    # individually.)" — UAB Courtside split-lot phrasing (2026-06-18).
    r"|part\s+of\s+(?:the\s+)?\d+\s+chairs?"
    r"|sold\s+in\s+varying\s+(?:numbers|quantities|amounts|sizes)"
    r"|being\s+sold\s+(?:in\s+varying|individually)"
    r"|(?:more|additional|remaining)\s+(?:\S+\s+){0,3}?available\s+in"
    r"|see\s+our\s+other"
    r"|multiple\s+(?:listings|auctions)"
    r")"
)

# Sentence boundary: ./!/? followed by whitespace (so "38.25 inches" stays
# whole), or a newline. Kept spans are reassembled byte-for-byte so the
# surviving text is untouched for the noise-stripping regexes downstream.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)|\n")


def _drop_cross_listing_sentences(desc: str) -> tuple[str, int]:
    """Return ``desc`` minus any sentence that references other listings,
    plus how many sentences were dropped."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(desc):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(desc):
        spans.append((start, len(desc)))
    kept: list[str] = []
    dropped = 0
    for s, e in spans:
        if _CROSS_LISTING_RE.search(desc[s:e]):
            dropped += 1
        else:
            kept.append(desc[s:e])
    if not dropped:
        return desc, 0
    return "".join(kept).strip(), dropped


def refine_quantities_with_regex_fulltext(listings: list[dict]) -> list[dict]:
    """Re-run the regex over ``title + description`` and update in place.

    Rules:
      - Only rows tagged ``regex_title`` are touched. LLM-tagged rows are
        left alone (the LLM is assumed to be the stronger signal).
      - Sentences referencing chairs in OTHER listings (split-lot sellers
        repeating the cross-listing total) are removed from the description
        first; if nothing else remains, the title-derived value stands and
        confidence drops to ``low``.
      - If the cached title-regex value was obviously-wrong (>9999, i.e. an
        auction ID or lot number) and fulltext returns a smaller number,
        **correct** it (don't just upgrade).
      - If fulltext returns a larger number than title-only, upgrade.
      - If fulltext corroborates the title-only value (equal, >1), promote
        the source to ``regex_fulltext``.
    """
    if not listings:
        return listings
    upgraded = corrected = cross_listing = 0
    for item in listings:
        src = item.get("quantity_source") or "regex_title"
        if src != "regex_title":
            continue
        title = item.get("title") or ""
        desc = item.get("description") or ""
        if not desc:
            continue
        desc, dropped = _drop_cross_listing_sentences(desc)
        if dropped:
            cross_listing += 1
            if not desc:
                item["quantity_confidence"] = "low"
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
    if cross_listing:
        print(f"   → regex(fulltext) ignored other-listing sentences on {cross_listing} rows")
    return listings

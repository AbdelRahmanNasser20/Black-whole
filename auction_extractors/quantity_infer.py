"""
Infer chair *quantity* from listing titles when the site does not expose a dedicated field.

Sites often mix auction IDs (#12345), lot numbers, years, and zip codes with real counts.
Strategy: strip known ID prefixes, then match phrases that usually mean quantity
("lot of (200)", "approx 50", "120 chairs"), and only then use a guarded fallback.
"""

from __future__ import annotations

import re

# Tried in order; first match wins.
_QUANTITY_PATTERNS: tuple[tuple[str, str], ...] = (
    # GovDeals-style
    (r"(?i)lot\s+of\s*\(\s*(\d+)\s*\)", "lot of (N)"),
    (r"(?i)lot\s+of\s+(\d+)\b", "lot of N"),
    (r"(?i)qty\.?\s*(\d+)", "qty N"),
    (r"(?i)quantity\s*:?\s*(\d+)", "quantity N"),
    (r"(?i)approx\.?\s*(\d+)", "approx N"),
    (r"(?i)approximately\s+(\d+)", "approximately N"),
    (r"(?i)lot\s+size\s*:?\s*(\d+)", "lot size"),
    # Parentheses often hold counts: "(370)" near chairs
    (r"(?i)\(\s*(\d+)\s*\)\s*(?:total|chair|stack|banquet|padded|metal)", "paren before chair words"),
    (r"(?i)\(\s*(\d+)\s*\)\s*$", "paren at end of title"),
    # Units
    (r"(?i)\b(\d+)\s*(?:pcs|pieces|ea|each)\b", "N pcs"),
    # Any-length number IMMEDIATELY before a chair word (0-5 char gap).
    # Catches "1200 chairs", "Lot of 14 chairs", "37 chair".
    (r"(?i)\b(\d+)[^\d\n]{0,5}chairs?\b", "N chairs (immediate)"),
    # Short numbers (1-3 digits) near a chair word — tolerates extra adjectives
    # between ("16- Stackable Chairs", "107 stacking plastic chairs"). Four+
    # digit numbers require the immediate match above; otherwise they're almost
    # always model/serial numbers, not counts (UMF 8678, GLAVAL 2017, etc.).
    (r"(?i)\b(\d{1,3})[^\d\n]{0,30}chairs?\b", "short N <nearby> chairs"),
)


def _strip_noise(text: str) -> str:
    """Remove auction / asset / internal-code noise that pollutes quantity detection.

    Auction sites pepper titles with IDs, lot numbers, auction numbers, and
    product model numbers. Without stripping those, quantity regex happily
    returns 218321 as a "chair count". Every pattern here was added in
    response to a false positive we actually saw in ``state/listings.db``.
    """
    t = text.strip()
    # Leading "#12345 - title"
    t = re.sub(r"^#\d+\s*[-–—]\s*", "", t)
    # Leading long numeric prefix + dash
    t = re.sub(r"^\d{5,}\s*[-–—]\s*", "", t)
    # GovDeals internal codes: "(218321 BT)", "(217377 DC)", "(12345 AJ)", etc.
    t = re.sub(r"\(\s*\d{4,}\s+[A-Z]{1,4}\s*\)", "", t)
    # Inline model numbers like "SMR APEX S241000" — letter+digits globs.
    t = re.sub(r"\b[A-Z]+\d{4,}\b", "", t)
    # Lot identifiers: "Lot # 4589", "Lot 1049", "LOT#: 12345".
    t = re.sub(r"\b[Ll]ot\s*#?\s*:?\s*\d+\b", "", t)
    # Auction numbers: "Auction#262", "Auction 160".
    t = re.sub(r"\b[Aa]uction\s*#?\s*\d+\b", "", t)
    # Model/serial codes following context words: "Medical 8678", "Model 614",
    # "Series 2017", "No. 3977".
    t = re.sub(
        r"\b(?:Medical|Model|Series|No\.?|Number|Cat\.?|SKU)\s*#?\s*\d+\b",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Hyphenated / spaced model codes: "OS-712", "OS 712", "JAZZY 614", "BR75".
    # Letters (2+) followed by - or space then 3+ digits. 3+ digit cutoff
    # preserves real counts like "4 chairs" that share the letter-space-digit
    # shape but with <3 digits.
    t = re.sub(r"\b[A-Z]{2,}[-\s]\d{3,}\b", "", t)
    # Hyphenated alphanumeric model/part codes where the stem mixes letters and
    # digits: "424L-115", "OS-712", "AB12-90". The trailing number is a model
    # suffix, not a count ("MTI 424L-115 Power Exam Chair" used to → 115). Stem
    # must contain a letter, so pure numeric ranges ("115-200") are left alone.
    t = re.sub(r"\b[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*-\d{2,}\b", "", t)
    # Title-case brand + model number sitting directly before a *singular*
    # seating noun: "Winco 653 Chair", "Invacare 9153 Chair", "Jazzy 614
    # Recliner". The number is a product model, not a count, so strip
    # "<brand> <model>" before the proximity pattern below reads it
    # ("Winco 653 Chair" → 653). Guarded three ways so real counts survive:
    #   (a) the digits must butt right up against the seating noun;
    #   (b) the noun must be SINGULAR (`chair`, not `chairs`) — genuine
    #       multi-item lots say "32 Chairs"/"Table and 4 Chairs" (plural),
    #       whereas a model designation reads "653 Chair Recliner" (singular);
    #   (c) quantity connectors that legitimately precede a count
    #       (of / lot / qty / approx / total / set / pack / pallet / box / case)
    #       are excluded, so "Lot of 653 Chairs" / "Approx 50 Chairs" survive.
    t = re.sub(
        r"\b(?!(?:of|lot|qty|quantity|approx|approximately|about|around|roughly|"
        r"est|estimated|total|set|sets|pack|packs|case|cases|box|boxes|pallet|"
        r"pallets|up|to|over|plus)\b)"
        r"[A-Za-z]{2,}\s+\d{1,4}\b"
        r"(?=\s+(?:chair|recliner|geri|sofa|stool|bench|couch|seat|lounge|"
        r"sleeper|glider|rocker|loveseat|settee)\b)",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Dimension / angle / weight / percentage contexts — NEVER chair counts.
    # "The chair rotates 360 degrees", "45 inches tall", "Weighs 25 lbs", "50%".
    # Remove the digits so the downstream proximity pattern doesn't pick them
    # up (e.g. "rotates 360 degrees" within 30 chars of "Chair" used to → 360).
    t = re.sub(r"\b\d+\s*(?:°|degrees?)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d+\s*(?:lbs?\.?|pounds?|kg|kilos?|oz|ounces?)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:inch(?:es)?|in\.?|ft\.?|feet|cm|mm|meters?)\b",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Inches shorthand (``22"`` / ``18.5"``). Separate pattern because ``"`` is
    # a non-word char so ``\b`` after it wouldn't fire.
    t = re.sub(r'\b\d+(?:\.\d+)?\s*"', "", t)
    t = re.sub(r"\b\d+(?:\.\d+)?\s*%", "", t)
    # Rotation verbs: "rotates 360", "swivels 180", "turns 90".
    t = re.sub(r"\b(?:rotate[sd]?|swivel[sd]?|turns?|spin[sd]?)\s+\d+\b", "", t, flags=re.IGNORECASE)
    return t.strip()


# Backwards-compat alias
_strip_leading_noise = _strip_noise


def infer_chair_quantity_from_title(title: str) -> int:
    """
    Best-effort quantity. Returns >= 1.

    Relies on explicit chair-count patterns; returns 1 when none match. The
    blind integer fallback was removed because it produced catastrophic false
    positives ("UMF Medical 8678 Power Phlebotomy Chair" → 8678, "2017 Ford"
    → 2017). When the title alone is ambiguous, the description-based
    ``refine_quantities_with_regex_fulltext`` pass and the downstream LLM
    refinement pick up the real count.
    """
    if not title or not str(title).strip():
        return 1

    t = _strip_noise(str(title))

    for pattern, _label in _QUANTITY_PATTERNS:
        m = re.search(pattern, t)
        if m:
            n = int(m.group(1))
            if n > 0:
                return n

    return 1

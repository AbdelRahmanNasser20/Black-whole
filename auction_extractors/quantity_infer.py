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

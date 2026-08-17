"""Explicit-count extraction — the safety net under the BLACKWHOLE-4 trust gate.

The trust gate (``top_chairs.trusted_quantity``) is right to refuse a regex
count: the regex path really does read model and fleet numbers as chairs. But
refusing the count and *dropping the lot* are two different things, and the
pipeline was doing the second. Lot 420/9312 — "LOT: (199) BANQUET CHAIRS", 199
chairs in Las Vegas — was scraped, cached, and then discarded from the alert
because the quantity LLM's chunk failed that run.

``explicit_title_quantity`` is the narrow read used to *flag* such a lot for the
operator, never to feed it back in as a trusted count. It answers only for
titles that name their count in a shape a model/lot/fleet number cannot take.

Run standalone (no pytest needed):
    python auction_extractors/tests/test_explicit_quantity.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantity_infer import explicit_title_quantity


def test_the_lot_we_actually_lost() -> None:
    """The regression case. 199 banquet chairs, dropped for 11 days."""
    got = explicit_title_quantity("LOT: (199) BANQUET CHAIRS")
    assert got is not None, "the lot that started this must be readable"
    qty, why = got
    assert qty == 199, got
    assert why, "every answer carries the pattern that produced it"


def test_explicit_phrasings_are_read() -> None:
    cases = [
        ("Lot of (250) Stacking Chairs", 250),
        ("Lot of 250 banquet chairs", 250),
        ("Banquet chairs, QTY 120", 120),
        ("Quantity: 300 folding chairs", 300),
        ("Stacking chairs approx 75", 75),
        ("Approximately 450 church chairs", 450),
        ("Blue Conference/Banquet Chairs (370)", 370),
        ("(96) padded stack chairs", 96),
        ("1200 chairs on pallets", 1200),
        ("Lot of 2,100 chairs", 2100),
    ]
    for title, want in cases:
        got = explicit_title_quantity(title)
        assert got is not None, f"should read a count from {title!r}"
        assert got[0] == want, f"{title!r} -> {got[0]}, want {want}"


def test_model_lot_and_fleet_numbers_are_refused() -> None:
    """The BLACKWHOLE-4 false positives. None of these may produce a count.

    Each one is a real misread from ``state/listings.db`` — this is the exact
    reason the trust gate exists, and the explicit tier must not reopen it.
    """
    for title in (
        "Jazzy 614 Power Wheelchair",
        "Winco 653 Chair",
        "Invacare 9153 Chair",
        "MTI 424L-115 Power Exam Chair",
        "SMR APEX S241000 Dental Chair",
        "06-316  UAB Courtside Chairs (4)",       # lot code 06-316, only 4 chairs
        "2014 Ford ACCESSIBLE VAN (5173)",        # fleet number
        "Chair rotates 360 degrees",
        "Office chair, 45 inches tall",
        "Executive chair holds 300 lbs",
        "Chair, 50% polyester",
        "Auction#262 Stacking Chair",
        "Lot # 4589 Chair",
    ):
        got = explicit_title_quantity(title)
        assert got is None or got[0] < 50, f"{title!r} must not yield a bulk count, got {got}"


def test_public_surplus_bin_codes_are_not_counts() -> None:
    """Found by replaying 8,131 LLM-scored rows: "PW-26-103-CHAIRS-PLASTIC" is
    bin 103, and the LLM says the lot holds 7 chairs."""
    for title in ("#4020145 - PW-26-103-CHAIRS-PLASTIC",
                  "#4021906 - PW-26-109-CHAIRS-ASSORTED"):
        got = explicit_title_quantity(title)
        assert got is None or got[0] < 50, f"{title!r} -> {got}"


def test_an_implausible_positional_count_is_an_id() -> None:
    """Public Surplus writes "#4066544 - Chairs (9124)" — asset 9124. The
    biggest real lot in 8,131 verified rows is 2,100."""
    assert explicit_title_quantity("#4066544 - Chairs (9124)") is None
    # …but an explicit English claim is the seller's word, not a stray number.
    assert explicit_title_quantity("Lot of 9124 chairs") == (9124, "lot of N")


def test_no_count_means_no_answer() -> None:
    for title in ("Banquet Reception Chairs", "Stacking chairs", "", "   "):
        assert explicit_title_quantity(title) is None, title
    assert explicit_title_quantity(None) is None


def test_a_singular_chair_word_is_not_a_count() -> None:
    """'653 Chair' is a model designation; genuine lots say 'Chairs'."""
    got = explicit_title_quantity("Herman Miller 653 Chair")
    assert got is None or got[0] < 50, got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("all explicit-quantity tests passed")

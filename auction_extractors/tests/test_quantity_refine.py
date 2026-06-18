"""Fulltext quantity-refine guards.

Regression net for `refine_quantities_with_regex_fulltext`. The headline bug
this locks down: a seller splits one batch of chairs across several auctions
and repeats the GRAND TOTAL in every description ("105 chairs total across our
listings, sold in groups of 1, 4, and 12"). The fulltext pass used to read the
105 out of the description and stamp it on every sibling listing, so five
small lots all ranked as 105-chair lots. A description that references chairs
in OTHER listings must never upgrade this listing's quantity.

Seen live in state/listings.db: lots 98+100/31024 ("Lot of (6)/(8) stackable
padded armchairs") stored quantity 22 because the description added
"(2) other sets of 8 chairs are also being posted."

Run standalone (no pytest needed):  python tests/test_quantity_refine.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantity_refine import refine_quantities_with_regex_fulltext


def _row(title: str, desc: str, qty: int, source: str = "regex_title") -> dict:
    return {
        "title": title,
        "description": desc,
        "quantity": qty,
        "quantity_source": source,
        "quantity_confidence": "low" if qty == 1 else "medium",
    }


# (row, expected_quantity, expected_source)
CASES = [
    # ── cross-listing totals must NOT become this listing's quantity ─────
    (
        _row(
            "Chorus Chairs",
            "105 chairs total across our listings. Sold in groups of 1, 4, "
            "and 12. See our other auctions for the remaining lots.",
            1,
        ),
        1,
        "regex_title",
    ),
    (
        _row(
            "Lot of (6) stackable padded armchairs",
            "Each Chair 24” x 24” x 19” seat height\n"
            "Red padded seat. Black plastic back.\n\n"
            "(2) other sets of 8 chairs are also being posted.",
            6,
        ),
        6,
        "regex_fulltext",
    ),
    # UAB Courtside (2026-06-18): title says "(4)" but the shared description
    # repeats the grand total — "(This is part of 105 chairs being sold in
    # varying numbers and individually.)" — and the fulltext pass upgraded
    # every sibling lot to 105. The "part of N chairs … individually" sentence
    # must be dropped so the title's 4 stands.
    (
        _row(
            "06-316  UAB Courtside Chairs  (4)",
            "Four UAB basketball courtside chairs. (This is part of 105 "
            "chairs being sold in varying numbers and individually.) Send "
            "all inquiries to MATT WILDT at mwildt@uab.edu.",
            4,
        ),
        4,
        "regex_title",
    ),
    # the per-listing count in another sentence must still be picked up
    (
        _row(
            "Padded Banquet Chairs",
            "Set of 4 chairs. There are 96 more chairs available in our "
            "other listings, posted separately.",
            1,
        ),
        4,
        "regex_fulltext",
    ),
    # ── harmless boilerplate footers must NOT suppress real upgrades ─────
    (
        _row(
            "Dining Chairs - Local Pick-Up",
            "Lot of 70 dining chairs with padded seats. Check out our other "
            "auctions for more great items!",
            1,
        ),
        70,
        "regex_fulltext",
    ),
    (
        _row(
            "Office chairs rolling and stationary",
            "Lot of 60 assorted rolling and stationary chairs. The yellow "
            "stacking chairs in the photo are not part of this auction; they "
            "are in a separate listing.",
            1,
        ),
        60,
        "regex_fulltext",
    ),
    # ── legit description-sourced counts must still upgrade ─────────────
    (
        _row("Banquet Chairs", "Lot of (12) banquet chairs, sold as is.", 1),
        12,
        "regex_fulltext",
    ),
    (
        _row("Stackable Conference Chairs", "Approximately 325 chairs.", 1),
        325,
        "regex_fulltext",
    ),
    # ── corroboration promotes the source ────────────────────────────────
    (
        _row("Approx 50 Chairs", "This lot consists of 50 chairs.", 50),
        50,
        "regex_fulltext",
    ),
    # ── LLM-tagged rows are never touched ────────────────────────────────
    (
        _row("Banquet Chairs", "Lot of (12) banquet chairs.", 7, source="llm"),
        7,
        "llm",
    ),
]


def run() -> int:
    failures = []
    for row, want_qty, want_src in CASES:
        got = refine_quantities_with_regex_fulltext([dict(row)])[0]
        if got["quantity"] != want_qty or got["quantity_source"] != want_src:
            failures.append((row["title"], want_qty, want_src, got["quantity"], got["quantity_source"]))

    for title, want_qty, want_src, got_qty, got_src in failures:
        print(f"FAIL  {title!r}: expected {want_qty}/{want_src}, got {got_qty}/{got_src}")
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


def test_quantity_refine():
    bad = []
    for row, want_qty, want_src in CASES:
        got = refine_quantities_with_regex_fulltext([dict(row)])[0]
        if got["quantity"] != want_qty or got["quantity_source"] != want_src:
            bad.append((row["title"], want_qty, want_src, got["quantity"], got["quantity_source"]))
    assert not bad, f"quantity refine regressions: {bad}"


if __name__ == "__main__":
    raise SystemExit(run())

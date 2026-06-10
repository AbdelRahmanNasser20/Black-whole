"""Quantity-from-title inference guards.

Regression net for `infer_chair_quantity_from_title`. The headline bug this
locks down: a brand + model number that sits right before a seating noun
("Winco 653 Chair Recliner") must NOT be read as a count of 653 chairs.

Run standalone (no pytest needed):  python tests/test_quantity_infer.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantity_infer import infer_chair_quantity_from_title as infer


# (title, expected_quantity)
CASES = [
    # ── model numbers that must NOT be mistaken for counts ──────────────
    ("Winco 653 Chair Recliner", 1),
    ("Winco 652 Chair Recliner", 1),
    ("WINCO 653 Chair", 1),
    ("Jazzy 614 Recliner", 1),
    ("Drive Medical 802 Geri Chair", 1),
    ("Invacare 9153 Chair", 1),
    ("Ritter 204 Power Exam Table", 1),
    ("UMF Medical 8678 Power Phlebotomy Chair", 1),
    ("MTI 424L-115 Power Medical Exam Chair", 1),
    ("Pedigo OS-712 Procedure Chair", 1),

    # ── vehicle fleet/unit numbers in trailing parens must NOT be counts ─
    # (2026-06-10: wheelchair-accessible vans ranked top of the Auctions tab
    # with "quantity" 5173 because "(5173)" matched "paren at end of title")
    ("2014 Ford Metrolite Wheelchair  ACCESSIBLE VAN (5173)", 1),
    ("2010 Ford/Coach Phoenix DRW Wheelchair ACCESSIBLE VAN (5001)", 1),
    ("2018 Ford Econoline Wheel Chair Accesible Van (5244)", 1),

    # ── genuine counts that must survive unchanged ──────────────────────
    ("653 Stackable Chairs", 653),
    ("Lot of 653 Chairs", 653),
    ("Lot of 100 Virco Kinder Height Student Chairs", 100),
    ("Approx 50 Chairs", 50),
    ("Approximately 200 Banquet Chairs", 200),
    ("Qty 75 Folding Chairs", 75),
    ("(370) Padded Chairs", 370),
    ("37 chair", 37),
    ("1200 chairs", 1200),
    ("16- Stackable Chairs", 16),
    ("Pallet of 200 Office Chairs", 200),
    # plural counts where a common word precedes the number — must NOT be
    # mistaken for a brand+model strip ("and 32 Chairs", "with 2 Chairs").
    ("8 Library Tables and 32 Chairs Lot 1 Russwood Library", 32),
    ("Table and 4 Chairs", 4),
    ("Children's Furniture - Table with 2 Chairs", 2),
    ("Dinette Sets one table 3 chairs", 3),
]


def run() -> int:
    failures = []
    for title, expected in CASES:
        got = infer(title)
        if got != expected:
            failures.append((title, expected, got))

    for title, expected, got in failures:
        print(f"FAIL  {title!r}: expected {expected}, got {got}")
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


def test_quantity_inference():
    bad = [(t, e, infer(t)) for t, e in CASES if infer(t) != e]
    assert not bad, f"quantity inference regressions: {bad}"


if __name__ == "__main__":
    raise SystemExit(run())

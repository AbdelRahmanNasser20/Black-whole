"""The held-back path — the fix for lot 420/9312.

The scraper never failed to *find* that lot. It found it, stored it, and then
dropped it at the last gate because the quantity LLM's chunk had failed, so
``trusted_quantity`` returned None and the ``q > MIN_CHAIR_QUANTITY`` filter
read that as "not a bulk lot". 199 banquet chairs in Las Vegas, gone from the
alert for eleven days while the auction ran and closed.

``partition_for_alert`` splits that decision in two. A lot with a trusted
count is ranked as before. A lot with no trusted count but a title that states
one is *held back* — surfaced separately, labelled unverified, never fed back
in as a quantity. The invariant these tests defend: **no trusted count is ever
a reason to make a lot disappear.**

Run standalone (no pytest needed):
    python auction_extractors/tests/test_held_back_alert.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import govdeals_chairs_extraction as gd

LOST_LOT = {
    "title": "LOT: (199) BANQUET CHAIRS",
    "link": "https://www.govdeals.com/en/asset/420/9312",
    "quantity": None,
    "quantity_source": "llm_failed",
    "price": "USD 1,900.00",
    "location": "Las Vegas, Nevada, USA",
    "end_date": "2026-08-21",
    "time_left": "4d",
}


def _trusted(title: str, qty: int, **extra) -> dict:
    return {"title": title, "quantity": qty, "quantity_source": "llm",
            "link": "https://www.govdeals.com/en/asset/1/1", **extra}


def test_the_lot_we_lost_is_held_back_not_dropped() -> None:
    kept, held = gd.partition_for_alert([LOST_LOT])
    assert kept == [], "an unverified count must never be ranked as verified"
    assert len(held) == 1, "…but it must not vanish either — that was the bug"
    assert held[0]["claimed_quantity"] == 199
    assert held[0]["claimed_by"], "the operator gets told which pattern read it"


def test_held_back_never_forges_a_trusted_quantity() -> None:
    """The claim rides in its own key. Anything downstream reading
    ``quantity`` / ``quantity_source`` must still see the failure."""
    _, held = gd.partition_for_alert([LOST_LOT])
    assert held[0].get("quantity") is None
    assert held[0].get("quantity_source") == "llm_failed"


def test_trusted_lots_still_rank_normally() -> None:
    rows = [
        _trusted("Lot of 300 banquet chairs", 300),
        _trusted("Two stacking chairs", 2),          # below the floor
        LOST_LOT,
    ]
    kept, held = gd.partition_for_alert(rows)
    assert [k["quantity"] for k in kept] == [300]
    assert len(held) == 1


def test_an_untrusted_lot_with_no_stated_count_stays_out() -> None:
    """No verified count and no claim in the title = nothing to say about it.
    Held-back is a shortlist for the operator, not a dumping ground."""
    row = dict(LOST_LOT, title="Banquet Reception Chairs")
    kept, held = gd.partition_for_alert([row])
    assert kept == [] and held == []


def test_a_small_stated_count_is_not_worth_holding() -> None:
    row = dict(LOST_LOT, title="Lot of (6) banquet chairs")
    _, held = gd.partition_for_alert([row])
    assert held == [], "6 chairs is not a bulk lot in any provenance"


def test_non_chair_lots_are_refused_before_the_count_is_read() -> None:
    """"Lot of 500 White Spandex Chair Covers" reads as 500 chairs to any
    count-from-title path. It is 500 pieces of fabric."""
    row = dict(LOST_LOT, title="Lot of 500 White Spandex Chair Covers")
    kept, held = gd.partition_for_alert([row])
    assert kept == [] and held == []


def test_medical_lots_follow_the_existing_flag() -> None:
    row = dict(LOST_LOT, title="Lot of (199) Medical Exam Chairs, hospital")
    _, held = gd.partition_for_alert([row], include_medical=False)
    assert held == [], "medical stays out unless INCLUDE_MEDICAL is set"


def test_held_back_sorts_biggest_claim_first() -> None:
    rows = [
        dict(LOST_LOT, title="Lot of 80 stacking chairs"),
        dict(LOST_LOT, title="Lot of 900 banquet chairs"),
        dict(LOST_LOT, title="Lot of 300 folding chairs"),
    ]
    _, held = gd.partition_for_alert(rows)
    assert [h["claimed_quantity"] for h in held] == [900, 300, 80]


def test_junk_rows_cannot_crash_the_split() -> None:
    kept, held = gd.partition_for_alert([None, "nonsense", 42, LOST_LOT])
    assert len(held) == 1 and kept == []


# ── message rendering ────────────────────────────────────────────────────────

def test_alert_is_sent_even_when_nothing_is_verified() -> None:
    """The run that lost 420/9312 had zero trusted bulk lots, so it printed
    "No banquet chairs" and sent nothing at all."""
    _, held = gd.partition_for_alert([LOST_LOT])
    body = gd._compose_alert([], held)
    assert "9312" in body, "the link has to reach the operator"
    assert "199" in body
    assert "unverified" in body.lower()


def test_held_back_survives_the_telegram_length_cap() -> None:
    """Truncation must eat the ranked section, never the held-back one —
    cutting it off would reproduce the original bug one layer down."""
    ranked = [_trusted(f"Lot of {300 + i} banquet chairs {'x' * 60}", 300 + i,
                       rank=i, price="USD 1.00", end_date="2026-09-01",
                       time_left="10d")
              for i in range(120)]
    _, held = gd.partition_for_alert([LOST_LOT])
    body = gd._compose_alert(ranked, held, max_len=4000)
    assert len(body) <= 4000
    assert "9312" in body, "the held-back lot must not be the part that gets cut"


def test_no_held_back_leaves_the_old_message_untouched() -> None:
    ranked = [_trusted("Lot of 300 banquet chairs", 300, rank=1)]
    assert gd._compose_alert(ranked, []) == gd._format_output(ranked)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("all held-back alert tests passed")

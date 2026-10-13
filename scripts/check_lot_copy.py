#!/usr/bin/env python
"""Guard: a sellable lot's description must not quote a stale chair count.

Lot 31225 (2026-10-13): title said 2,500, body still said "2,100 ... plus
1,600 more" from the GovDeals auction, while cold emails pointed churches at
the page. This reads the first number in each sellable lot's `description`
and flags it when it is more than 10% off `quantity_remaining`.

Read-only. Uses the one sellable-lots query (`inventory.list_catalog_feed`).
Exit 1 if any lot mismatches, so it works as a cron canary.

    ./.venv/bin/python scripts/check_lot_copy.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `config` loads the repo-root .env (BLACKWHOLE_DB_URL) as a side effect.
from automation import config, inventory  # noqa: E402,F401

TOLERANCE = 0.10
# "2,100", "1200×", "about 2,500" -> first run of digits (commas allowed).
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")


def first_number(text: str | None) -> int | None:
    m = NUMBER.search(text or "")
    return int(m.group().replace(",", "")) if m else None


def mismatch(said: int | None, qty: int) -> bool:
    return said is not None and abs(said - qty) > TOLERANCE * qty


def main() -> int:
    bad = []
    for row in inventory.list_catalog_feed():
        qty = int(row["quantity_remaining"])
        said = first_number(row.get("description"))
        if mismatch(said, qty):
            bad.append((row["lot_id"], row["status"], qty, said, (row["description"] or "")[:60]))
    if not bad:
        print("OK: every sellable lot's description agrees with quantity_remaining")
        return 0
    print(f"{'lot_id':<14} {'status':<11} {'remaining':>9} {'copy says':>9}  description")
    for lot_id, status, qty, said, desc in bad:
        print(f"{lot_id:<14} {status:<11} {qty:>9} {said:>9}  {desc}")
    print(f"\n{len(bad)} lot(s) with a stale count in the description")
    return 1


if __name__ == "__main__":
    sys.exit(main())

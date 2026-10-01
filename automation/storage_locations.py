"""Where every lot we hold physically sits — the operator's storage view.

`inventory.storage_note` is PRIVATE (facility address + gate code). It never
renders on the storefront, never reaches a channel adapter, never goes into a
bot draft. The only readers are this module's callers: the auth-walled admin
Locations tab (`/api/locations`) and `scripts/lot_location.py` (terminal).

Gate / entry codes are masked by default; the raw note is returned only when a
caller asks for it explicitly (`raw=True`) — the admin's edit box and
`lot_location.py --show-codes`.
"""
from __future__ import annotations

import re

from automation import db

# Lots we physically hold or are about to (won, not yet picked up). Sold/lost lots have no location worth tracking.
HELD_STATUSES = ("owned", "won_pickup", "in_transit", "listed")

# keyword + the rest of its clause (up to the next . ; / or newline, max 40 chars) → "[code hidden]"
_CODE_RE = re.compile(r"(?i)\b(gate|entry|access|door|lock|pin|code)\b[^.;/\n]{0,40}")

_COLS = ("lot_id, title, city, state, zip_code, quantity_remaining, status, "
         "coalesce(fake_sold_out, false) AS fake_sold_out, storage_note, updated_at")


def mask_codes(note: str | None) -> str:
    if not note:
        return ""
    return _CODE_RE.sub(lambda m: f"{m.group(1)} [code hidden]", note)


def _shape(row: dict, raw: bool) -> dict:
    note = (row.get("storage_note") or "").strip()
    out = dict(row)
    out["storage_note"] = note if raw else mask_codes(note)
    out["recorded"] = bool(note)
    return out


def list_held(raw: bool = False, missing_only: bool = False) -> list[dict]:
    """Every held lot with its storage note (codes masked unless raw). fake_sold_out lots are included
    — the chairs are still sitting somewhere — and flagged so the UI can say so."""
    sql = f"SELECT {_COLS} FROM inventory WHERE status = ANY(%s)"
    if missing_only:
        sql += " AND coalesce(trim(storage_note), '') = ''"
    rows = db.fetch_all(sql + " ORDER BY state, city, lot_id", (list(HELD_STATUSES),))
    return [_shape(r, raw) for r in rows]


def search(q: str, raw: bool = False) -> list[dict]:
    rows = db.fetch_all(
        f"SELECT {_COLS} FROM inventory WHERE lot_id = %s OR title ILIKE %s ORDER BY updated_at DESC",
        (q, f"%{q}%"))
    return [_shape(r, raw) for r in rows]


def get(lot_id: str, raw: bool = False) -> dict | None:
    row = db.fetch_one(f"SELECT {_COLS} FROM inventory WHERE lot_id = %s", (lot_id,))
    return _shape(row, raw) if row else None


def set_note(lot_id: str, note: str) -> dict | None:
    """Record where a lot sits. Empty string clears it. Returns the masked row, or None if no such lot."""
    n = db.execute("UPDATE inventory SET storage_note = %s, updated_at = now() WHERE lot_id = %s",
                   ((note or "").strip() or None, lot_id))
    return get(lot_id) if n else None

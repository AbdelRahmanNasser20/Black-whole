"""Where is a lot physically stored? Operator-only lookup.

    .venv/bin/python scripts/lot_location.py <lot_id or text>   # one lot (matches lot_id/title)
    .venv/bin/python scripts/lot_location.py --all               # every held lot + its storage
    .venv/bin/python scripts/lot_location.py --missing           # held lots with NO storage_note (fix these)
    .venv/bin/python scripts/lot_location.py --set <lot_id> "<storage note>"

Same data as the admin Locations tab (/admin?tab=locations). `inventory.storage_note`
is PRIVATE (address + gate code) — never paste it into a listing, a bot draft or a
channel. Gate/entry codes are masked unless --show-codes is passed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation import config  # noqa: F401  (loads .env)
from automation import storage_locations as sl


def show(rows):
    for r in rows:
        where = ", ".join(x for x in (r["city"], r["state"], r["zip_code"]) if x) or "?"
        flag = "  (fake sold out)" if r["fake_sold_out"] else ""
        print(f"{r['lot_id']}  |  {r['title']}  |  qty {r['quantity_remaining']}  |  {r['status']}{flag}  |  public: {where}")
        print(f"    storage: {r['storage_note'] or '⚠️  NOT RECORDED — set it with --set'}")


def main(argv):
    raw = "--show-codes" in argv
    argv = [a for a in argv if a != "--show-codes"]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    if argv[0] == "--set":
        row = sl.set_note(argv[1], argv[2])
        print(f"updated {argv[1]}" if row else f"no lot {argv[1]}"); return 0 if row else 1
    if argv[0] in ("--all", "--missing"):
        rows = sl.list_held(raw=raw, missing_only=argv[0] == "--missing")
    else:
        rows = sl.search(argv[0], raw=raw)
    show(rows) if rows else print("no matching lots")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

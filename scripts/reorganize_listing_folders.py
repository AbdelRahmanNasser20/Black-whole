"""Reorganize ~/Desktop/Banquet chiars Pictures/ into status buckets.

Dry-run by default; --apply moves folders AND updates inventory.folder_path in
one transaction. folder_name (the /image/ URL segment + Supabase Storage key
basename) is NEVER changed — only the physical location and the folder_path
column. The web app's _resolve_folder_dir() finds folders in either the flat
location or a bucket, so serving keeps working through the move.

    .venv/bin/python scripts/reorganize_listing_folders.py            # dry-run
    .venv/bin/python scripts/reorganize_listing_folders.py --apply
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from automation import inventory                      # noqa: E402
from automation.config import DOWNLOAD_ROOT           # noqa: E402

LEGACY_SKIP = {"General listing", "Lost biddings", "Listing Automations",
               "Listing_automation_html"}
BUCKETS = ("_active", "_sold", "_lost", "_archive", "_docs")

_STATUS_BUCKET = {
    "listed": "_active", "draft": "_active", "owned": "_active", "won_pickup": "_active",
    "sold_out": "_sold",
    "lost_sold_out": "_lost", "lost": "_lost",
    "hidden": "_archive", "active_bid": "_archive",
}


def bucket_for_status(status: str | None) -> str:
    return _STATUS_BUCKET.get((status or "").strip(), "_archive")


def plan_moves(rows, on_disk):
    """Return (moves, unmatched).

    moves = [(folder_name, bucket)] for ledger rows whose folder is on disk.
    unmatched = disk folders (excluding buckets) with no ledger row — left in
    place for the operator to place by hand.
    """
    on_disk_set = set(on_disk)
    by_folder = {r["folder_name"]: r for r in rows if r.get("folder_name")}
    moves = [
        (name, bucket_for_status(row.get("status")))
        for name, row in by_folder.items()
        if name in on_disk_set
    ]
    matched = {m[0] for m in moves}
    unmatched = [d for d in on_disk if d not in matched and d not in BUCKETS]
    return moves, unmatched


def _disk_lot_names(root: Path) -> list[str]:
    """Top-level dirs that look like lots (skip buckets + known non-lots)."""
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith(".") or p.name in BUCKETS:
            continue
        if p.name in LEGACY_SKIP:
            continue
        out.append(p.name)
    return out


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    root = Path(DOWNLOAD_ROOT)
    rows = inventory.list_all()
    on_disk = _disk_lot_names(root)
    moves, unmatched = plan_moves(rows, on_disk)

    print(f"root: {root}")
    print(f"planned moves: {len(moves)}   unmatched (left in place): {len(unmatched)}")
    for name, bucket in sorted(moves, key=lambda m: (m[1], m[0])):
        print(f"  {bucket:9s} <- {name}")
    if unmatched:
        print("UNMATCHED (no ledger row — place by hand):")
        for u in sorted(unmatched):
            print(f"  ? {u}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to move folders + update folder_path.")
        return 0

    for bucket in BUCKETS:
        (root / bucket).mkdir(exist_ok=True)

    moved = 0
    with inventory.connect() as conn:
        for name, bucket in moves:
            src = root / name
            dst = root / bucket / name
            if not src.is_dir():
                continue                 # already moved (idempotent)
            if dst.exists():
                print(f"  skip (exists): {dst}")
                continue
            shutil.move(str(src), str(dst))
            conn.execute(
                "UPDATE inventory SET folder_path = %s, updated_at = now() "
                "WHERE folder_name = %s",
                (str(dst), name),
            )
            moved += 1
        conn.commit()
    print(f"applied {moved} moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

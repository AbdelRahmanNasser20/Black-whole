"""Transfer-source path resolution guard.

The discovery cron sets ``LISTINGS_DB_PATH=/tmp/listings.db`` (the container's
filesystem is ephemeral, so the scrape writes there). The transfer step must
read the SAME path — it used to hardcode ``auction_extractors/state/listings.db``
and crash every cron run with ``source not found`` (exit 1), so Supabase was
never updated. ``_sqlite_path()`` must honor ``LISTINGS_DB_PATH`` and fall back
to the state-dir default, matching ``listings_db.py``.

Pure / no DB. Run standalone:  python tests/test_transfer_sqlite_path.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.transfer_listings_to_supabase import _sqlite_path


def main() -> int:
    checks = []
    saved = os.environ.pop("LISTINGS_DB_PATH", None)

    # default: repo's auction_extractors/state/listings.db
    default = _sqlite_path()
    ok = default.parts[-3:] == ("auction_extractors", "state", "listings.db")
    print(f"  [{'ok ' if ok else 'FAIL'}] default -> {default}")
    checks.append(ok)

    # env override is honored verbatim
    os.environ["LISTINGS_DB_PATH"] = "/tmp/listings.db"
    overridden = _sqlite_path()
    ok = str(overridden) == "/tmp/listings.db"
    print(f"  [{'ok ' if ok else 'FAIL'}] LISTINGS_DB_PATH -> {overridden}")
    checks.append(ok)

    if saved is None:
        os.environ.pop("LISTINGS_DB_PATH", None)
    else:
        os.environ["LISTINGS_DB_PATH"] = saved

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} ({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_transfer_sqlite_path():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

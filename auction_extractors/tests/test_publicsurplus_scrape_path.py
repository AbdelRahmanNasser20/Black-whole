"""Scrape-path default guard for Public Surplus.

PS migrated to the ps-v2 site and now anti-bot-gates search results: a plain
``requests`` GET reliably returns only the empty shell (``noAuctionsFound``),
so the HTTP fast path returns 0 listings and the scraper wastes a round-trip
before falling back to the browser. The browser path is now primary; the HTTP
fast path is opt-in via ``PUBLICSURPLUS_USE_API=1``.

Pure / no network. Run standalone:  python tests/test_publicsurplus_scrape_path.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from public_surplus_automation import _use_http_fast_path


def _check(name: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    return ok


def main() -> int:
    checks = []
    saved = os.environ.pop("PUBLICSURPLUS_USE_API", None)
    checks.append(_check("unset -> browser primary (off)", _use_http_fast_path(), False))
    os.environ["PUBLICSURPLUS_USE_API"] = "1"
    checks.append(_check("=1 -> http fast path on", _use_http_fast_path(), True))
    os.environ["PUBLICSURPLUS_USE_API"] = "0"
    checks.append(_check("=0 -> off", _use_http_fast_path(), False))
    if saved is None:
        os.environ.pop("PUBLICSURPLUS_USE_API", None)
    else:
        os.environ["PUBLICSURPLUS_USE_API"] = saved

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} ({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_publicsurplus_scrape_path():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

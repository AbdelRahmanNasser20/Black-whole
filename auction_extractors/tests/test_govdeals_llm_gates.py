"""Gate guard for the GovDeals quantity pipeline.

The title-only regex misreads model numbers, fleet numbers, and (since
2026-06-18) thousands-separated counts as quantities; the LLM pass is the
trustworthy signal. It must therefore default ON — matching Public Surplus —
so a scrape run without the flag explicitly set still gets LLM quantities
instead of silently shipping the brittle regex fallback (which is exactly how
"Lot of 2,100 Banquet Chairs" landed as quantity 2 and every UAB Courtside lot
as 105).

Pure / no network. Run standalone:  python tests/test_govdeals_llm_gates.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from govdeals_chairs_extraction import _llm_quantity_enabled


def _check(name: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    return ok


def main() -> int:
    checks = []
    var = "USE_LLM_QUANTITY"
    saved = os.environ.pop(var, None)
    checks.append(_check(f"{var} unset -> on", _llm_quantity_enabled(), True))
    os.environ[var] = "0"
    checks.append(_check(f"{var}=0 -> off", _llm_quantity_enabled(), False))
    os.environ[var] = "1"
    checks.append(_check(f"{var}=1 -> on", _llm_quantity_enabled(), True))
    if saved is None:
        del os.environ[var]
    else:
        os.environ[var] = saved

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} "
          f"({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_govdeals_llm_gates():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

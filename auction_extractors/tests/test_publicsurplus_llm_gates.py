"""Gate guard for the Public Surplus quantity pipeline.

Quantity is LLM-inferred from title + description (regex is untrusted — it
reads lot numbers like "LOT #142" as counts; see GitHub issue #10). Both the
description fetch and the LLM pass must therefore default ON; the title-regex
seed survives only as a tagged fallback when the LLM call fails.

Pure / no network. Run standalone:  python tests/test_publicsurplus_llm_gates.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from public_surplus_automation import _descriptions_enabled, _llm_quantity_enabled


def _check(name: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    return ok


def main() -> int:
    checks = []
    for var, fn in (
        ("FETCH_PUBLIC_SURPLUS_DESCRIPTION", _descriptions_enabled),
        ("USE_LLM_QUANTITY", _llm_quantity_enabled),
    ):
        saved = os.environ.pop(var, None)
        checks.append(_check(f"{var} unset -> on", fn(), True))
        os.environ[var] = "0"
        checks.append(_check(f"{var}=0 -> off", fn(), False))
        os.environ[var] = "1"
        checks.append(_check(f"{var}=1 -> on", fn(), True))
        if saved is None:
            del os.environ[var]
        else:
            os.environ[var] = saved

    # The regex-fulltext refine step is gone from this pipeline — the LLM
    # replaces it (it used to run before the LLM and get overwritten anyway).
    import public_surplus_automation as ps
    checks.append(_check(
        "regex_fulltext_dropped",
        hasattr(ps, "refine_quantities_with_regex_fulltext"), False))

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} "
          f"({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_publicsurplus_llm_gates():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

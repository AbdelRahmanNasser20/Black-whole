#!/usr/bin/env python
"""Local entry point for the classification backfill.

The logic lives in `deals/backfill_classify.py` so the Render cron can reach it
as `python -m deals.cli backfill-classify` — see render.yaml's
`deals-backfill-classify` service, which drains the backlog hourly.

    python scripts/backfill_classifications.py --reset-fakes
    python scripts/backfill_classifications.py --limit 1000
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  — imported for the .env side effect
from deals.backfill_classify import run  # noqa: E402
from deals.classify import active_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-fakes", action="store_true",
                    help="blank the provably-fake other/0.0 rows, then exit")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--rpm", type=int, default=18,
                    help="requests per minute; 18 keeps us inside Groq's 6k tokens/min")
    a = ap.parse_args()

    resolved = active_provider()
    if resolved is None:
        print("no LLM provider configured — see scripts/check_llm_provider.py", file=sys.stderr)
        return 2
    print(f"provider: {resolved[0]}")
    print(run(limit=a.limit, rpm=a.rpm, reset=a.reset_fakes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

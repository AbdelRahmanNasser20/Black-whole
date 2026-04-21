"""
python -m auction_extractors — language-agnostic entry point.

Subcommands:

  top    Print top-N chair listings as JSON on stdout.

Examples:

  python -m auction_extractors top --source gd --n 15 --min-qty 50
  python -m auction_extractors top --source ps --n 10 --no-condition > top.json

Any language can shell out to this and parse the JSON. The schema matches
the Python function ``auction_extractors.get_top_chairs``.
"""

from __future__ import annotations

import argparse
import json
import sys

from top_chairs import get_top_chairs


def _cmd_top(args: argparse.Namespace) -> int:
    listings = get_top_chairs(
        source=args.source,
        n=args.n,
        min_quantity=args.min_qty,
        include_condition=not args.no_condition,
        active_only=not args.include_expired,
        max_stale_days=args.max_stale_days,
    )
    json.dump(listings, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m auction_extractors")
    sub = ap.add_subparsers(dest="cmd", required=True)

    top = sub.add_parser("top", help="top-N chair listings as JSON")
    top.add_argument("--source", choices=("gd", "ps"), default="gd")
    top.add_argument("--n", type=int, default=15)
    top.add_argument("--min-qty", type=int, default=50)
    top.add_argument(
        "--no-condition",
        action="store_true",
        help="skip the condition-scoring LLM (faster, no condition fields)",
    )
    top.add_argument(
        "--include-expired",
        action="store_true",
        help="include ended / not-recently-seen listings (archive mode)",
    )
    top.add_argument(
        "--max-stale-days",
        type=int,
        default=2,
        help="with --include-expired off, drop listings not re-seen in N days",
    )
    top.set_defaults(func=_cmd_top)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

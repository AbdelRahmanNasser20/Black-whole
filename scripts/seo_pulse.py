#!/usr/bin/env python3
"""SEO Pulse — audit the live site, read Search Console, rank what to fix.

    .venv/bin/python scripts/seo_pulse.py run                       # audit + keyword ideas, no GSC
    .venv/bin/python scripts/seo_pulse.py run --gsc ~/Downloads/black-whole.com-Performance-on-Search-2026-11-05.zip
    .venv/bin/python scripts/seo_pulse.py run --gsc api --draft     # service account + Groq drafts
    .venv/bin/python scripts/seo_pulse.py run --telegram             # weekly cron form
    .venv/bin/python scripts/seo_pulse.py audit [--limit 10]         # just the crawl
    .venv/bin/python scripts/seo_pulse.py keywords --seed "church chairs"

Read-only on the web and the DB. Writes only `SEO_PULSE_DIR` (report + snapshot).
Design: docs/superpowers/specs/2026-11-05-seo-pulse-design.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config  # noqa: E402,F401  (loads .env)
from automation import seo_pulse  # noqa: E402


def _http():
    import httpx
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=20),
                        headers={"User-Agent": seo_pulse.USER_AGENT, "Accept-Language": "en-US,en"})


def _base(args) -> str:
    return (args.base or os.getenv("PUBLIC_BASE_URL") or "https://black-whole.com").rstrip("/")


def cmd_run(args) -> int:
    log = (lambda s: print(s, file=sys.stderr)) if not args.quiet else None
    with _http() as http:
        res = seo_pulse.run(
            base=_base(args), http=http, gsc_spec=args.gsc, site_url=os.getenv("GSC_SITE_URL", ""),
            days=args.days, keywords=not args.no_keywords, draft=args.draft, limit=args.limit,
            directory=Path(args.out).expanduser() if args.out else None, log=log,
        )
    print(res["report"])
    print(f"\nreport: {res['report_path']}\nsnapshot: {res['snapshot_path']}", file=sys.stderr)
    if res["drafted"]:
        print(f"drafts: {res['drafted']}", file=sys.stderr)
    if args.telegram:
        from automation import telegram_alerts
        text = seo_pulse.telegram_lines(res["site"], res["gsc"], res["recs"], res["trend"])
        ok, err = telegram_alerts.send_message_sync(text, topic="ops")
        print(f"telegram: {'sent' if ok else 'FAILED ' + str(err)}", file=sys.stderr)
    return 0


def cmd_audit(args) -> int:
    with _http() as http:
        site = seo_pulse.crawl(_base(args), http, limit=args.limit,
                               log=lambda s: print(s, file=sys.stderr))
    print(seo_pulse.render_report(site, None, seo_pulse.recommend(site, None), None, None))
    return 0


def cmd_keywords(args) -> int:
    with _http() as http:
        ideas = seo_pulse.keyword_ideas(http, seeds=args.seed or seo_pulse.DEFAULT_SEEDS,
                                        cities=args.city or [], max_requests=args.max_requests)
    for i in ideas:
        print(f"{i['term']}\t← {i['from']}")
    print(f"{len(ideas)} suggestions", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="site origin (default PUBLIC_BASE_URL or https://black-whole.com)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="audit + GSC + keywords + recommendations + snapshot")
    r.add_argument("--gsc", help="GSC export zip/folder, or 'api' (GSC_SERVICE_ACCOUNT_JSON + GSC_SITE_URL)")
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--draft", action="store_true", help="Groq drafts for the top recommendations")
    r.add_argument("--telegram", action="store_true", help="send the 5-line summary to Telegram (ops topic)")
    r.add_argument("--no-keywords", action="store_true")
    r.add_argument("--limit", type=int, help="crawl only the first N sitemap URLs")
    r.add_argument("--out", help="directory for report + snapshot (default SEO_PULSE_DIR)")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("audit", help="crawl + page issues only")
    a.add_argument("--limit", type=int)
    a.set_defaults(fn=cmd_audit)

    k = sub.add_parser("keywords", help="Google autocomplete ideas")
    k.add_argument("--seed", action="append")
    k.add_argument("--city", action="append")
    k.add_argument("--max-requests", type=int, default=40)
    k.set_defaults(fn=cmd_keywords)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

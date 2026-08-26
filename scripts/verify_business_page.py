#!/usr/bin/env python
"""Guard: everything the FB Business-Page path needs that is checkable WITHOUT
Facebook credentials.

Companion to ``verify_catalog_feed.py`` (which validates feed rows against
Meta's product-feed spec). This one answers the operator question "is the
Business-Page/Commerce-Manager path ready on OUR side?" — the parts Meta pulls
from us. It cannot see inside Commerce Manager (that needs the operator's
login); it proves every input Meta's scheduled fetch will touch:

  1. **Feed reachable** — ``/catalog/facebook.csv`` answers 200 with the exact
     9-column header. A cache-buster query string is appended by default
     because Cloudflare fronts the URL with ``max-age=14400``.
  2. **Row count** — at least one sellable row (Commerce Manager rejects an
     empty feed source).
  3. **Every ``link`` live** — each ``https://black-whole.com/listings/{lot}``
     answers a ranged GET 200/206. These are the click-through targets from
     the Page Shop.
  4. **Every ``image_link`` live** — ranged GET 200/206. **GET, never HEAD**:
     the r2.dev public bucket 403s HEAD while serving the identical GET fine.
  5. **No dead storage** — any Supabase Storage URL fails (that backend 402s).

Exit code is 1 on any FAIL, so this works as a CI gate or cron canary.

    ./.venv/bin/python scripts/verify_business_page.py
    ./.venv/bin/python scripts/verify_business_page.py --url http://127.0.0.1:8765/catalog/facebook.csv
    ./.venv/bin/python scripts/verify_business_page.py --no-bust   # let Cloudflare serve its cache
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Imported for the .env side effect (SITE_BASE_URL) + the canonical column list.
from automation import catalog_feed, config  # noqa: E402,F401

import httpx  # noqa: E402

EXPECTED_HEADER = list(catalog_feed.FEED_COLUMNS)

# Meta rejects a feed missing any of these; everything else in their template is
# optional. Checking "required present" instead of "header == our snapshot" means
# adding an optional column locally does not make this script fail against the
# still-deployed feed — it only fails on something Meta would actually reject.
REQUIRED_COLUMNS = [
    "id", "title", "description", "availability",
    "condition", "link", "image_link", "brand",
]


def ranged_get(client: httpx.Client, url: str) -> int | str:
    """Status of a 1-byte ranged GET. A string means the request itself blew up."""
    try:
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
            return resp.status_code
    except Exception as exc:  # noqa: BLE001 - a network blip is a finding
        return type(exc).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="feed URL (default: <SITE_BASE_URL>/catalog/facebook.csv)")
    ap.add_argument("--no-bust", action="store_true",
                    help="don't append a cache-buster query string")
    args = ap.parse_args()

    base = args.url or f"{catalog_feed.site_base_url()}/catalog/facebook.csv"
    url = base if args.no_bust else f"{base}{'&' if '?' in base else '?'}vb={int(time.time())}"

    failures = 0

    def report(ok: bool, name: str, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28} {detail}")

    # 1. feed reachable + header
    try:
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        body = resp.text
        report(resp.status_code == 200, "feed reachable",
               f"HTTP {resp.status_code} {url}")
    except Exception as exc:  # noqa: BLE001
        report(False, "feed reachable", f"{type(exc).__name__}: {exc}")
        print(f"\n1 check failed — feed unreachable, nothing else to verify.")
        return 1

    reader = csv.DictReader(io.StringIO(body))
    got = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in got]
    report(not missing, "feed header",
           f"{got}" if not missing else f"MISSING REQUIRED {missing} — got {got}")
    if got != EXPECTED_HEADER:
        print(f"NOTE  live header differs from this checkout's catalog_feed.FEED_COLUMNS.\n"
              f"      live  : {got}\n      local : {EXPECTED_HEADER}\n"
              f"      That is expected until the local change is deployed.")
    rows = list(reader)

    # 2. row count
    report(len(rows) > 0, "row count",
           f"{len(rows)} sellable rows (Commerce Manager rejects an empty feed)")

    # 3+4+5. per-row liveness + dead-storage sweep
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for row in rows:
            rid = (row.get("id") or "?").strip()
            for col in ("link", "image_link"):
                target = (row.get(col) or "").strip()
                if not target:
                    report(False, f"{col} [{rid}]", "missing")
                    continue
                if "supabase.co/storage/" in target:
                    report(False, f"{col} [{rid}]",
                           f"dead Supabase Storage URL: {target[:90]}")
                    continue
                code = ranged_get(client, target)
                report(code in (200, 206), f"{col} [{rid}]",
                       f"{code} {target[:100]}")

    print(f"\nsource: {url}")
    if failures:
        print(f"{failures} check(s) FAILED — Meta's next scheduled pull would "
              f"ingest a broken or empty catalog.", file=sys.stderr)
        return 1
    print(f"All checks passed: feed live, {len(rows)} rows, every link and "
          f"image answers a ranged GET.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

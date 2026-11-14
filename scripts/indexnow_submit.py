#!/usr/bin/env python
"""Submit every sitemap URL to IndexNow (Bing, and so ChatGPT search).

The web process pings single lots on add / remove / restore
(`lot_channels._ping_indexnow`); this is the one-shot bulk push for the whole
site — run it once after the key goes live, and after a big catalog change.

    .venv/bin/python scripts/indexnow_submit.py --dry-run
    .venv/bin/python scripts/indexnow_submit.py

Reads `{PUBLIC_BASE_URL}/sitemap.xml` over HTTP (no DB). `--dry-run` prints
the URL count and the URLs and sends nothing.

Exit code is 1 when `INDEXNOW_KEY` is unset or the submit failed
(403 = key file unreachable, 422 = host mismatch, 429 = slow down).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from automation import config  # noqa: E402,F401  (loads .env)
from automation import indexnow  # noqa: E402
from automation.config import PUBLIC_BASE_URL  # noqa: E402

# Cloudflare 403s the default python-httpx User-Agent.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the sitemap URLs, send nothing")
    args = ap.parse_args()

    resp = httpx.get(f"{PUBLIC_BASE_URL}/sitemap.xml", timeout=20.0,
                     headers={"User-Agent": UA}, follow_redirects=True)
    resp.raise_for_status()
    urls = re.findall(r"<loc>([^<]+)</loc>", resp.text)

    if args.dry_run:
        print(f"{len(urls)} urls")
        for u in urls:
            print(u)
        return 0

    if not indexnow.key():
        print("INDEXNOW_KEY unset — nothing sent")
        return 1
    ok, detail = indexnow.submit(urls)
    print(f"submitted {len(urls)} urls → {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

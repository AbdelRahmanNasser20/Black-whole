#!/usr/bin/env python
"""Guard: the Facebook catalog feed must be something Meta will actually ingest.

Companion to ``check_offerable_images.py`` — same shape, different surface.
The failure this exists to catch is silent in the same way: Commerce Manager
pulls ``/catalog/facebook.csv`` on a schedule, and a bad row doesn't crash
anything here — it just quietly fails FB's import (or worse, imports with a
dead photo or a false "in stock" promise). Checks, all cheap:

  1. **Spec** — every row carries the 9 required columns, non-empty, with a
     valid ``availability`` / ``condition`` enum, ``"<amount> USD"`` price,
     absolute https ``link`` / ``image_link``, and within FB's field limits
     (id 100, title 200, description 9999). Duplicate ids fail too.
  2. **No dead storage** — any Supabase Storage URL anywhere in a row fails.
     That backend 402s on every public object on this project (egress-
     restricted); only R2 / other live hosts are acceptable.
  3. (removed 2026-08-25 — no per-lot availability rule; see note below on
     earliest. If it appears it must NOT be "in stock" and its description
     must say mid-September. This promise being wrong has already cost real
     buyers, hence the hardcoded check.
  4. **Liveness** (default on) — every ``link`` and ``image_link`` answers a
     ranged GET with 200/206. A GET, not a HEAD: the r2.dev public bucket
     403s HEAD while serving the identical GET fine.

Exit code is 1 on any failure, so this works as a CI gate or a cron canary.

    ./.venv/bin/python scripts/verify_catalog_feed.py            # live feed
    ./.venv/bin/python scripts/verify_catalog_feed.py --local    # render from DB, no deploy needed
    ./.venv/bin/python scripts/verify_catalog_feed.py --no-http  # spec checks only
    ./.venv/bin/python scripts/verify_catalog_feed.py --url http://127.0.0.1:8765/catalog/facebook.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `config` is imported for its side effect: it loads the repo-root .env
# (BLACKWHOLE_DB_URL for --local, SITE_BASE_URL for the default feed URL).
from automation import catalog_feed, config  # noqa: E402,F401

REQUIRED_COLUMNS = tuple(catalog_feed.FEED_COLUMNS)
VALID_AVAILABILITY = {
    "in stock", "out of stock", "preorder", "available for order", "discontinued",
}
# Availability values that promise the buyer can have it now. Lot 31225 must
# never carry one of these while pickup is still pending.
AVAILABLE_NOW = {"in stock"}
VALID_CONDITION = {"new", "refurbished", "used"}
PRICE_RE = re.compile(r"^\d+\.\d{2} [A-Z]{3}$")
IDAHO_LOT = "31225"

_LIMITS = {"id": 100, "title": 200, "description": 9999}


def fetch_feed(url: str) -> str:
    import httpx

    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def render_local() -> str:
    """Build the CSV straight from the DB — proves the code, not the deploy."""
    from automation import inventory

    return catalog_feed.rows_to_csv(inventory.list_catalog_feed())


def check_http(urls: list[str]) -> list[tuple[int | str, str]]:
    """Ranged GET each URL; return the ones that don't come back 200/206."""
    import httpx

    bad: list[tuple[int | str, str]] = []
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for url in urls:
            try:
                with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
                    code: int | str = resp.status_code
            except Exception as exc:  # noqa: BLE001 - a network blip is a finding
                code = type(exc).__name__
            if code not in (200, 206):
                bad.append((code, url))
    return bad


def validate_row(row: dict, seen_ids: set[str]) -> list[str]:
    """Spec problems with one feed row. Empty list = FB would accept it."""
    problems: list[str] = []
    for col in REQUIRED_COLUMNS:
        if not (row.get(col) or "").strip():
            problems.append(f"missing required field `{col}`")
    rid = (row.get("id") or "").strip()
    if rid in seen_ids:
        problems.append(f"duplicate id `{rid}`")
    seen_ids.add(rid)
    for col, limit in _LIMITS.items():
        val = row.get(col) or ""
        if len(val) > limit:
            problems.append(f"`{col}` is {len(val)} chars (limit {limit})")
    if (av := row.get("availability", "")) not in VALID_AVAILABILITY:
        problems.append(f"invalid availability `{av}`")
    if (cond := row.get("condition", "")) not in VALID_CONDITION:
        problems.append(f"invalid condition `{cond}`")
    if not PRICE_RE.match(row.get("price", "")):
        problems.append(f"price `{row.get('price')}` not in `25.00 USD` form")
    for col in ("link", "image_link"):
        url = (row.get(col) or "").strip()
        if url and not url.startswith("https://"):
            problems.append(f"`{col}` is not absolute https: {url[:80]}")
    for col, val in row.items():
        if "supabase.co/storage/" in (val or ""):
            problems.append(f"dead Supabase Storage URL in `{col}` (backend 402s)")
    # No per-lot availability rule. Operator decision 2026-08-25: every row is
    # "in stock", and any caveat a lot needs lives in `inventory.description`
    # so the feed, the site, and the Marketplace listing read the same field.
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="feed URL (default: <SITE_BASE_URL>/catalog/facebook.csv)")
    ap.add_argument("--local", action="store_true",
                    help="render the feed from the DB instead of fetching a URL")
    ap.add_argument("--no-http", action="store_true",
                    help="skip the liveness GETs on link/image_link")
    args = ap.parse_args()

    if args.local:
        body, source = render_local(), "local render"
    else:
        url = args.url or f"{catalog_feed.site_base_url()}/catalog/facebook.csv"
        try:
            body, source = fetch_feed(url), url
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL could not fetch {url}: {exc}", file=sys.stderr)
            return 1

    reader = csv.DictReader(io.StringIO(body))
    if reader.fieldnames != list(REQUIRED_COLUMNS):
        print(f"FAIL header mismatch: {reader.fieldnames}", file=sys.stderr)
        return 1
    rows = list(reader)
    if not rows:
        print("FAIL feed has a header but zero rows", file=sys.stderr)
        return 1

    failures = 0
    seen_ids: set[str] = set()
    for row in rows:
        rid = (row.get("id") or "?").strip()
        label = f"{rid:<46} {row.get('availability', ''):<20}"
        problems = validate_row(row, seen_ids)
        if not args.no_http and not problems:
            bad = check_http([row["link"], row["image_link"]])
            problems += [f"{code} on {url[:100]}" for code, url in bad]
        if problems:
            failures += 1
            print(f"FAIL {label} {'; '.join(problems)}")
        else:
            suffix = " (urls verified)" if not args.no_http else ""
            print(f"ok   {label}{suffix}")

    print(f"\nsource: {source}")
    if failures:
        print(f"{failures}/{len(rows)} feed rows would fail or mislead — "
              f"fix before Meta's next scheduled pull.", file=sys.stderr)
        return 1
    print(f"All {len(rows)} feed rows pass the Meta product-feed checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

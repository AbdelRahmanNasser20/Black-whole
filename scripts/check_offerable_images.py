#!/usr/bin/env python
"""Guard: every CRM-offerable lot must have photos the bot can actually send.

The failure this exists to catch is silent. A lot flips `crm_offerable = true`,
the bot starts pitching it, a buyer asks "what do they look like" — and the
answer resolves to nothing because the photos only ever existed on the
operator's laptop, or because the URLs on the row point at a storage backend
that has since gone dark. That happened: five buyers on lot 31225 (~945 chairs).

Two checks, both cheap:

  1. **Reachability** — the row has at least one durable URL (not local disk).
  2. **Liveness** (``--http``) — those URLs really return 200. Supabase Storage
     on this project is egress-restricted and 402s on every public object, so a
     row can look populated and still be broken.

Exit code is 1 when any offerable lot fails, so this works as a CI gate or a
cron canary.

    ./.venv/bin/python scripts/check_offerable_images.py
    ./.venv/bin/python scripts/check_offerable_images.py --http
    ./.venv/bin/python scripts/check_offerable_images.py --all --http
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `config` is imported for its side effect: it loads the repo-root .env, which
# is where BLACKWHOLE_DB_URL lives.
from automation import config, db, lot_images  # noqa: E402,F401

QUERY = f"""
    SELECT {', '.join(lot_images.ROW_COLUMNS)}, title, status, crm_offerable
    FROM inventory
    {{where}}
    ORDER BY lot_id
"""


def check_http(urls: list[str], limit: int) -> list[tuple[int | str, str]]:
    """Fetch the first byte of each URL; return the ones that don't come back.

    A ranged GET, NOT a HEAD: the r2.dev public bucket answers HEAD with 403
    while serving the identical GET with 200. This used to HEAD, so it failed
    every lot it checked — and a canary that always cries wolf is one you stop
    reading, which defeats the point of the script.

    ``Range: bytes=0-0`` keeps it to a single byte per URL, so proving liveness
    never pulls hundreds of KB of image. Servers honoring the range answer 206;
    ones ignoring it answer 200 and the body is dropped unread.
    """
    import httpx

    bad: list[tuple[int | str, str]] = []
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for url in urls[:limit]:
            try:
                with client.stream(
                    "GET", url, headers={"Range": "bytes=0-0"}
                ) as resp:
                    code: int | str = resp.status_code
            except Exception as exc:  # noqa: BLE001 - a network blip is a finding
                code = type(exc).__name__
            if code not in (200, 206):
                bad.append((code, url))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="check every inventory row, not just crm_offerable ones")
    ap.add_argument("--http", action="store_true",
                    help="also verify the URLs return HTTP 200")
    ap.add_argument("--http-limit", type=int, default=3,
                    help="URLs to HEAD per lot when --http is set (default 3)")
    args = ap.parse_args()

    where = "" if args.all else "WHERE crm_offerable = true"
    rows = db.fetch_all(QUERY.format(where=where))
    if not rows:
        print("no rows matched — nothing to check")
        return 0

    failures: list[str] = []
    for row in rows:
        lot_id = row.get("lot_id")
        resolved = lot_images.resolve(row)
        backend = lot_images.storage_backend(resolved.hero)
        label = f"{lot_id:<46} {str(row.get('status') or ''):<12}"

        if not resolved.urls:
            local = len(resolved.local_paths)
            hint = f"{local} on local disk only" if local else "none anywhere"
            print(f"FAIL {label} no durable image URLs ({hint})")
            failures.append(str(lot_id))
            continue

        bad = check_http(resolved.urls, args.http_limit) if args.http else []
        if bad:
            print(f"FAIL {label} {len(resolved.urls)} urls [{backend}] — "
                  f"{bad[0][0]} on {bad[0][1]}")
            failures.append(str(lot_id))
            continue

        suffix = " (verified)" if args.http else ""
        print(f"ok   {label} {len(resolved.urls)} urls [{backend}]{suffix}")

    scope = "all lots" if args.all else "crm_offerable lots"
    if failures:
        print(f"\n{len(failures)}/{len(rows)} {scope} have no usable photos: "
              f"{', '.join(failures)}", file=sys.stderr)
        print("Fix: ./.venv/bin/python scripts/backfill_listing_images.py "
              "--lot <lot_id>", file=sys.stderr)
        return 1

    print(f"\nAll {len(rows)} {scope} have usable photos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

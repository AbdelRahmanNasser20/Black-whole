#!/usr/bin/env python
"""Guard: every FB Marketplace listing in a relist plan must match the ledger.

Modeled on ``check_offerable_images.py``. The failure this catches is a listing
that quietly disagrees with reality — a wrong price, a wrong city, a quantity
we no longer have, a dead photo URL, or (the one that has burned real buyers)
the Idaho pre-order lot 31225 worded as if it were available for pickup today.

Input is a plan JSON (default: ``scripts/fb_relist_plan_2026-08-24.json``) —
one entry per listing, ``kind: specific`` (one lot) or ``kind: general``
(metro evergreen backed by ``backing_skus``). Checks per listing:

  1. SKU is a real ``inventory.lot_id`` (general: every backing SKU is).
  2. Lot is sellable: not sold out, not ``fake_sold_out``, quantity > 0.
  3. Price equals ``price_per_chair`` (general: matches one backing lot).
  4. City/state match the ledger row (state names normalized to 2-letter).
  5. No number in the title/quantity wording exceeds ``quantity_remaining``.
  6. Photos: cover is first in the photo set; every photo URL belongs to the
     gallery of a `backing_skus` **or** `photo_skus` lot (the latter supply
     illustrative photos only and back no stock claim); original text:
     lot's resolved gallery (``lot_images.resolve``); none is a dead Supabase
     Storage URL (``storage_backend`` — Supabase 402s on every object).
  7. HTTP (default on, ``--skip-http`` to disable): site link and every photo
     URL answer 200/206 to a ranged GET (never HEAD — r2.dev 403s HEAD).
  8. Lot 31225 anywhere in the listing => copy must say mid-September /
     pre-order and must NOT contain immediate-availability language.
  9. Without ``--dry-run``: ``fb_listing_url`` must be a real Marketplace item
     URL and the <PROFILE_ID> placeholder must be resolved — i.e. the plan
     reflects what was actually posted.

Exit code 1 on any failure, with one readable line per finding.

    ./.venv/bin/python scripts/verify_fb_listings.py --dry-run
    ./.venv/bin/python scripts/verify_fb_listings.py --dry-run --skip-http
    ./.venv/bin/python scripts/verify_fb_listings.py            # post-flight
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `config` is imported for its side effect: it loads the repo-root .env, which
# is where BLACKWHOLE_DB_URL lives.
from automation import config, db, lot_images  # noqa: E402,F401

DEFAULT_PLAN = Path(__file__).resolve().parent / "fb_relist_plan_2026-08-24.json"

QUERY = f"""
    SELECT {', '.join(lot_images.ROW_COLUMNS)},
           title, status, city, state, zip_code,
           quantity_remaining, price_per_chair, fake_sold_out
    FROM inventory
    WHERE lot_id = %s
"""

UNSELLABLE_STATUSES = {"sold_out", "lost_sold_out", "lost", "hidden"}

# Lots that exist only as a future arrival. Copy must promise the future, never
# the present. 31225 = Idaho 3,700, pickup mid-September at the earliest.
PREORDER_LOTS = {"31225"}
PREORDER_REQUIRED = re.compile(r"mid[- ]?september|pre[- ]?order", re.I)
PREORDER_BANNED = [
    re.compile(p, re.I)
    for p in (
        r"available (now|today|immediately)",
        r"in (hand|stock) now",
        r"ready (for|to) (pick\s*up|pickup|go)",
        r"pick\s*up today",
        r"same[- ]day",
        r"immediate (pickup|availability|delivery)",
        r"on the ground",
        r"come (get|see) (them|it) (now|today)",
    )
]

STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def norm_state(value: str | None) -> str:
    v = (value or "").strip().lower()
    return STATE_CODES.get(v, v.upper())


def city_matches(ledger_city: str | None, plan_city: str | None) -> bool:
    """Token overlap, not equality — the ledger says 'Selfridge Angh', the
    listing says 'Selfridge ANGB (Harrison Twp)'. Any ledger-city token longer
    than 3 chars appearing in the plan city counts as the same place."""
    a = re.findall(r"[a-z]{4,}", (ledger_city or "").lower())
    b = (plan_city or "").lower()
    return bool(a) and any(tok in b for tok in a)


def numbers_in(text: str) -> list[int]:
    """Bare integers in listing copy, ignoring $-prefixed prices and measurements
    (60" / 29 in / 32"H — the Augusta round tables are 60 inches, not 60 units)."""
    return [
        int(m.group(1).replace(",", ""))
        for m in re.finditer(
            r"(?<!\$)\b(\d{1,3}(?:,\d{3})+|\d+)\b(?!\s*(?:\"|″|”|-?inch|\s?in\b|'|ft\b|\s?x\s?\d))",
            text or "")
    ]


def check_http(urls: list[str]) -> list[tuple[int | str, str]]:
    """Ranged GET, never HEAD — the r2.dev public bucket answers HEAD with 403
    while serving the identical GET with 200 (see check_offerable_images.py)."""
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


def verify_listing(listing: dict, *, dry_run: bool, http: bool) -> list[str]:
    """All problems with one plan entry, as human-readable strings."""
    problems: list[str] = []
    sku = listing.get("sku") or "<missing sku>"
    kind = listing.get("kind") or "specific"
    if listing.get("fb_status") == "sold":
        # Retired on purpose via lot_channels remove (fake sold-out + Mark as
        # sold). Its ledger row is meant to fail every sellable check.
        return []
    copy = " ".join(
        str(listing.get(k) or "")
        for k in ("title", "quantity_wording", "description")
    )

    # -- 1. resolve the lot(s) this listing stands on ------------------------
    lot_skus = listing.get("backing_skus") if kind == "general" else [sku]
    if not lot_skus:
        return [f"{kind} listing has no backing_skus"]
    rows: dict[str, dict] = {}
    for lot in lot_skus:
        row = db.fetch_one(QUERY, (lot,))
        if not row:
            problems.append(f"SKU {lot!r} is not a lot_id in inventory")
        else:
            rows[lot] = row
    if not rows:
        return problems

    # -- 2. sellable ---------------------------------------------------------
    for lot, row in rows.items():
        if row["status"] in UNSELLABLE_STATUSES:
            problems.append(f"lot {lot} has status {row['status']!r} — not sellable")
        if row.get("fake_sold_out"):
            problems.append(f"lot {lot} is fake_sold_out — never offer it")
        if not (row.get("quantity_remaining") or 0) > 0:
            problems.append(f"lot {lot} has quantity_remaining "
                            f"{row.get('quantity_remaining')!r}")

    # -- 3. price ------------------------------------------------------------
    price = listing.get("price")
    ledger_prices = {lot: row.get("price_per_chair") for lot, row in rows.items()}
    if price is None:
        problems.append("no price in plan")
    elif kind == "specific":
        expect = ledger_prices.get(sku)
        if expect is None or Decimal(str(price)) != Decimal(str(expect)):
            problems.append(f"price ${price} != ledger price_per_chair ${expect}")
    else:
        if Decimal(str(price)) not in {
            Decimal(str(p)) for p in ledger_prices.values() if p is not None
        }:
            problems.append(f"price ${price} matches no backing lot "
                            f"({ledger_prices})")

    # -- 4. location (specific listings pin the lot's real city) -------------
    if kind == "specific":
        row = rows.get(sku) or {}
        if not city_matches(row.get("city"), listing.get("city")):
            problems.append(f"city {listing.get('city')!r} != ledger "
                            f"{row.get('city')!r}")
        if norm_state(listing.get("state")) != norm_state(row.get("state")):
            problems.append(f"state {listing.get('state')!r} != ledger "
                            f"{row.get('state')!r}")
        if row.get("zip_code") and listing.get("zip") \
                and str(listing["zip"]) != str(row["zip_code"]):
            problems.append(f"zip {listing.get('zip')!r} != ledger "
                            f"{row.get('zip_code')!r}")

    # -- 5. quantity wording can't promise more than we have -----------------
    limit = sum(r.get("quantity_remaining") or 0 for r in rows.values())
    visible = (listing.get("title") or "") + " " + (listing.get("quantity_wording") or "")
    too_big = [n for n in numbers_in(visible) if n > limit]
    if too_big:
        problems.append(f"quantity wording claims {max(too_big)} but only "
                        f"{limit} remain across {sorted(rows)}")

    # -- 6. photos: real, ours, and not on the dead backend ------------------
    # `photo_skus` are lots a listing may take PHOTOS from but which back no
    # stock claim — e.g. GEN-MD leads with the blue/silver Baltimore chairs
    # (3,000 of them already sold there) to show the kind of chair we move,
    # while its quantities come from `backing_skus` alone. Keeping the two
    # lists apart is the point: checks 2-5 stay pinned to real, sellable stock.
    allowed: set[str] = set()
    for row in rows.values():
        allowed.update(lot_images.resolve(row).urls)
    for lot in listing.get("photo_skus") or []:
        row = db.fetch_one(QUERY, (lot,))
        if not row:
            problems.append(f"photo_skus entry {lot!r} is not a lot_id in inventory")
            continue
        allowed.update(lot_images.resolve(row).urls)
    photos = listing.get("photo_urls") or []
    cover = listing.get("cover_url")
    if not photos:
        problems.append("no photo_urls")
    if not cover:
        problems.append("no cover_url")
    elif photos and photos[0] != cover:
        problems.append("cover_url is not photo_urls[0] — FB uses the first "
                        "photo as the cover")
    for url in {*photos, *( [cover] if cover else [] )}:
        backend = lot_images.storage_backend(url)
        if backend == "supabase":
            problems.append(f"DEAD Supabase Storage URL (402s): {url}")
        if url not in allowed:
            problems.append(f"photo not in the lot's resolved gallery: {url}")

    # -- 7. links live -------------------------------------------------------
    site_link = listing.get("site_link")
    if not site_link:
        problems.append("no site_link")
    if http:
        to_check = ([site_link] if site_link else []) + photos
        for code, url in check_http(to_check):
            problems.append(f"HTTP {code} on {url}")

    # -- 8. (removed 2026-08-25) --------------------------------------------
    # There used to be a pre-order guard forcing lot 31225's copy to say
    # "mid-September". The operator wants every lot worded the same way, so
    # availability caveats now live in `inventory.description` alone.

    # -- 9. posted-state checks (skipped in --dry-run) -----------------------
    if not dry_run:
        fb = listing.get("fb_listing_url") or ""
        if "facebook.com/marketplace/item/" not in fb:
            problems.append(f"fb_listing_url missing/invalid ({fb!r}) — was "
                            "this actually posted?")
        if "<PROFILE_ID>" in copy:
            problems.append("<PROFILE_ID> placeholder still in the copy — "
                            "fill the real marketplace/profile id")
    elif "<PROFILE_ID>" in copy:
        print(f"     note {listing.get('sku')}: <PROFILE_ID> placeholder still "
              "unfilled (ok in dry-run, must be resolved before posting)")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plan", nargs="?", default=str(DEFAULT_PLAN),
                    help=f"plan JSON (default {DEFAULT_PLAN.name})")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the plan BEFORE posting (fb_listing_url and "
                         "<PROFILE_ID> may still be unfilled)")
    ap.add_argument("--skip-http", action="store_true",
                    help="skip the live HTTP 200 checks (offline)")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text())
    listings = plan.get("listings") or []
    if not listings:
        print("plan has no listings — nothing to check")
        return 1

    failures = 0
    for listing in sorted(listings, key=lambda l: l.get("order", 999)):
        sku = listing.get("sku") or "<missing sku>"
        label = f"{sku:<46} {listing.get('kind', 'specific'):<9}"
        problems = verify_listing(listing, dry_run=args.dry_run,
                                  http=not args.skip_http)
        if problems:
            failures += 1
            print(f"FAIL {label}")
            for p in problems:
                print(f"     - {p}")
        else:
            suffix = "" if args.skip_http else " (links verified live)"
            print(f"ok   {label} {len(listing.get('photo_urls') or [])} "
                  f"photos{suffix}")

    mode = "dry-run (pre-posting)" if args.dry_run else "post-flight"
    if failures:
        print(f"\n{failures}/{len(listings)} listings failed {mode} "
              "verification — fix before posting.", file=sys.stderr)
        return 1
    print(f"\nAll {len(listings)} listings pass {mode} verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

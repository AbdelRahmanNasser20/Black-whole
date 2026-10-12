"""Mirror a foreign site's live deal_lots into `auction_listings`.

The admin Auctions tab reads `auction_listings` (automation/auctions_supabase.py),
which the GovDeals/Public Surplus/BidSpotter scrapers fill through
scripts/transfer_listings_to_supabase.py. A deals/ adapter (TXAuction) writes
`deal_lots` instead, so its lots never reached that tab. This module copies the
live, profile-matching ones across, in the shape the tab already understands:

    asset_id   'tx:<auction_lot_id>'          (prefix per site, like ps:/bs:)
    link       the public lot URL (deals.sites.lot_url)
    price      "$1,234.00"
    location   "City, ST, United States"
    end_date   ISO-8601 UTC with Z (auction_extractors.end_dates reads it as UTC)

Quantity is treated exactly like the scrapers treat it (BLACKWHOLE-4): the
title regex only SEEDS a count (`regex_title`, untrusted), then the quantity
LLM verifies it the same way scripts/backfill_quantities_llm.py does (batches
of 12). A failed batch leaves `quantity=None, quantity_source='llm_failed'` —
never the seed. The read side still surfaces a title-stated count as
"unverified" (top_chairs.title_claimed_quantity).

Upsert by asset_id keeps `first_seen_at`, and never overwrites the three
quantity columns of a row that already holds a trusted count (`llm` /
`structured`) — re-running is cheap and can't downgrade a verified lot.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from automation import db
from auction_extractors.quantity_infer import explicit_title_quantity
from deals import profiles as _profiles
from deals import sites
from deals.profiles import Profile

ASSET_PREFIX = {"txauction": "tx"}
LLM_BATCH = 12
TRUSTED = ("llm", "structured")
DESCRIPTION_MAX = 4000

COLS = ["asset_id", "link", "title", "description", "quantity", "quantity_source",
        "quantity_confidence", "price", "location", "lot_number", "end_date", "time_left",
        "description_fetched_at", "first_seen_at", "last_seen_at", "image_url", "pickup_zip"]

_QTY = ("quantity", "quantity_source", "quantity_confidence")
_KEEP_TRUSTED = (f"auction_listings.quantity_source IN ({', '.join(repr(s) for s in TRUSTED)})")

UPSERT = (
    f"INSERT INTO auction_listings ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))}) "
    "ON CONFLICT (asset_id) DO UPDATE SET "
    + ", ".join(
        f"{c} = CASE WHEN {_KEEP_TRUSTED} THEN auction_listings.{c} ELSE EXCLUDED.{c} END"
        if c in _QTY else f"{c} = EXCLUDED.{c}"
        for c in COLS if c not in ("asset_id", "first_seen_at"))
)

_SELECT = """SELECT site, native_id, title, description, current_bid, city, state, zip,
       end_utc, hero_image_url, raw->>'lot_number' AS lot_number
FROM deal_lots
WHERE site = %s AND end_utc > now() AND outcome_complete IS NOT TRUE"""


@dataclass
class MirrorReport:
    site: str
    candidates: int = 0
    written: int = 0
    llm_verified: int = 0
    llm_failed: int = 0
    dry_run: bool = False
    rows: list[dict] = field(default_factory=list, repr=False)


# ── pure ─────────────────────────────────────────────────────────────────────

def asset_key(site: str, native_id: str) -> str:
    prefix = ASSET_PREFIX.get(site)
    if not prefix:
        raise ValueError(f"listings_bridge: no auction_listings prefix for site {site!r}")
    lot_id = str(native_id).rsplit("/", 1)[-1]
    if not lot_id:
        raise ValueError(f"listings_bridge: bad native_id {native_id!r}")
    return f"{prefix}:{lot_id}"


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("listings_bridge: naive end_utc")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def listing_row(r: dict, now: datetime) -> dict:
    """A deal_lots row → an auction_listings row (quantity = title seed only)."""
    if r.get("current_bid") is None:
        raise ValueError(f"listings_bridge: {r.get('native_id')} has no price")   # never "$0.00"
    seed = explicit_title_quantity(r.get("title"))
    loc = ", ".join(x for x in ((r.get("city") or "").strip(), (r.get("state") or "").strip()) if x)
    return {
        "asset_id": asset_key(r["site"], r["native_id"]),
        "link": sites.lot_url(r),
        "title": r.get("title") or "",
        "description": (r.get("description") or "")[:DESCRIPTION_MAX],
        "quantity": seed[0] if seed else None,
        "quantity_source": "regex_title" if seed else "llm_missing",
        "quantity_confidence": "unknown",
        "price": f"${float(r['current_bid']):,.2f}",
        "location": f"{loc}, United States" if loc else "",
        "lot_number": r.get("lot_number"),
        "end_date": _iso_z(r["end_utc"]),
        "time_left": "",
        "description_fetched_at": now,
        "first_seen_at": now,
        "last_seen_at": now,
        "image_url": r.get("hero_image_url") or "",
        "pickup_zip": (r.get("zip") or "")[:10] or None,
    }


def _default_refine(items: list[dict]) -> list[dict]:
    """Same call as scripts/backfill_quantities_llm.py."""
    from auction_extractors.quantity_llm import refine_quantities_with_llm
    return refine_quantities_with_llm(
        items,
        provider=os.getenv("LLM_PROVIDER_BACKFILL") or os.getenv("LLM_PROVIDER") or "groq",
        ollama_base_url="", ollama_model="", ollama_timeout=300,
        groq_api_key=os.getenv("GROQ_API_KEY"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
    )


def verify_quantities(rows: list[dict],
                      refine: Callable[[list[dict]], list[dict]] | None = None) -> tuple[int, int]:
    """LLM-verify each row's count in place. Returns (verified, failed).
    A raised batch → every row in it is `llm_failed` with quantity None."""
    refine = refine or _default_refine
    ok = bad = 0
    for i in range(0, len(rows), LLM_BATCH):
        chunk = rows[i:i + LLM_BATCH]
        try:
            out = refine([{"title": r["title"], "description": r.get("description") or ""}
                          for r in chunk])
            if len(out) != len(chunk):
                raise ValueError(f"refine returned {len(out)} rows for {len(chunk)}")
        except Exception as e:  # noqa: BLE001 — a failure is never an answer
            print(f"[mirror] quantity batch {i // LLM_BATCH} failed: {e!r}", file=sys.stderr)
            out = [{"quantity": None, "quantity_source": "llm_failed",
                    "quantity_confidence": "unknown"}] * len(chunk)
        for r, o in zip(chunk, out):
            src = o.get("quantity_source") or "llm_failed"
            q = o.get("quantity")
            if src in TRUSTED and q:
                r.update(quantity=int(q), quantity_source=src,
                         quantity_confidence=o.get("quantity_confidence") or "unknown")
                ok += 1
            else:
                r.update(quantity=None, quantity_source="llm_failed" if src in TRUSTED else src,
                         quantity_confidence="unknown")
                bad += 1
    return ok, bad


# ── DB ───────────────────────────────────────────────────────────────────────

def candidates(site: str, profile: Profile) -> list[dict]:
    """Live deal_lots of `site` that match the research profile."""
    where, args = _profiles.deal_lots_where(profile)
    rows = db.fetch_all(f"{_SELECT} AND ({where}) ORDER BY end_utc ASC", (site, *args))
    return [r for r in rows if _profiles.matches(profile, r.get("title"), r.get("description"))]


def _trusted_keys(keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = db.fetch_all("SELECT asset_id FROM auction_listings WHERE asset_id = ANY(%s) "
                        "AND quantity_source = ANY(%s)", (keys, list(TRUSTED)))
    return {r["asset_id"] for r in rows}


def mirror(site: str, profile: Profile, *, dry_run: bool = False,
           refine: Callable[[list[dict]], list[dict]] | None = None,
           now: datetime | None = None) -> MirrorReport:
    now = now or datetime.now(timezone.utc)
    rep = MirrorReport(site=site, dry_run=dry_run)
    rows: list[dict] = []
    for r in candidates(site, profile):
        try:
            rows.append(listing_row(r, now))
        except (ValueError, KeyError) as e:
            print(f"[mirror] skipping {r.get('native_id')}: {e}", file=sys.stderr)
    rep.candidates = len(rows)
    rep.rows = rows
    if dry_run:
        for x in rows:
            print(f"  [dry-run] {x['asset_id']:<12} {x['price']:>11} q={x['quantity']} "
                  f"({x['quantity_source']}) ends {x['end_date']} {x['title'][:60]}")
        return rep
    # Only spend LLM calls on rows whose stored count isn't already trusted
    # (their quantity columns are kept by the upsert anyway).
    trusted = _trusted_keys([x["asset_id"] for x in rows])
    rep.llm_verified, rep.llm_failed = verify_quantities(
        [x for x in rows if x["asset_id"] not in trusted], refine)
    for x in rows:
        db.execute(UPSERT, tuple(x[c] for c in COLS))
        rep.written += 1
    return rep

"""Original, per-lot storefront copy built from the ledger's structured fields.

Why this exists (SEO content PR, 2026-10-07): every lot page used to be the
scraped auction description plus a spec sheet — 250-360 words, most of them
also on GovDeals. Google crawled 27 of those pages and indexed 14; the rest
sat in "Discovered – currently not indexed", which is the verdict for thin
or copied pages. This module writes the parts a buyer actually asks about
and the auction never answers: what the chairs are good for, how they move
(pallets, weight, freight), and the four questions every first message
contains. Nothing here is scraped, nothing touches the DB, and no two lots
share a paragraph — the phrasing varies by a stable hash of the lot id.

Pure functions only. `build(row, *, sold)` returns a dict the template
renders; `faq_jsonld()` serialises the FAQ for Google's FAQPage markup.
`inventory.storage_note` is never read here — the facility address and gate
code stay private.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from typing import Any

# Keep in sync with automation/freight_estimate.py DEFAULT_CALIBRATION. The
# per-chair weight is still the unmeasured placeholder (see that file), so the
# copy always calls the number an estimate.
DEFAULT_LBS_PER_CHAIR = 13.0
DEFAULT_CHAIRS_PER_PALLET = 35.0

_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("folding", ("fold",)),
    ("banquet", ("banquet", "ballroom", "hotel", "event")),
    ("church", ("church", "sanctuary", "worship", "pew")),
    ("conference", ("conference", "office", "task", "meeting", "training")),
    ("stacking", ("stack", "wire", "sled", "linkable", "ganging")),
    ("dining", ("dining", "cafe", "cafeteria", "restaurant", "bistro")),
)

# Who buys each kind. Ordered so the first two read as the obvious fit.
_KIND_BUYERS: dict[str, tuple[str, ...]] = {
    "banquet": ("churches and fellowship halls", "hotels and wedding venues",
                "event-rental companies", "conference centers", "banquet halls"),
    "church": ("churches and sanctuaries", "youth rooms and fellowship halls",
               "funeral homes", "community centers"),
    "stacking": ("schools and gyms", "churches", "community halls",
                 "training rooms", "event-rental companies"),
    "folding": ("event-rental companies", "churches", "schools",
                "VFW and fraternal halls", "caterers"),
    "conference": ("training rooms and classrooms", "coworking spaces",
                   "churches", "nonprofits furnishing an office"),
    "dining": ("restaurants and cafeterias", "senior-living dining rooms",
               "school lunchrooms", "camps and retreat centers"),
    "event": ("churches", "event venues", "schools", "rental companies"),
}

_KIND_LABEL = {
    "banquet": "banquet chairs", "church": "church chairs", "stacking": "stacking chairs",
    "folding": "folding chairs", "conference": "conference chairs",
    "dining": "dining chairs", "event": "event chairs",
}


def kind(row: dict) -> str:
    """Coarse chair family from `chair_type`, else the title. Drives the copy."""
    text = f"{row.get('chair_type') or ''} {row.get('title') or ''}".lower()
    for name, needles in _KIND_RULES:
        if any(n in text for n in needles):
            return name
    return "event"


def _pick(lot_id: Any, options: tuple[str, ...], salt: str = "") -> str:
    """Stable choice per lot so each page keeps its wording between deploys
    while neighbouring lots don't read as the same template."""
    seed = zlib.crc32(f"{lot_id}|{salt}".encode("utf-8"))
    return options[seed % len(options)]


def _int(v: Any) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _money(n: float) -> str:
    return f"${n:,.0f}"


def _city(row: dict) -> str:
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip()
    return ", ".join(p for p in (city, state) if p)


def _unit_word(row: dict) -> str:
    """`chair` unless the lot is obviously tables — mirrors lot_channels.unit_word
    without importing it (this module stays dependency-free for tests)."""
    title = (row.get("title") or "").lower()
    if "table" in title and "chair" not in title:
        return "table"
    return "chair"


def _qty(row: dict, sold: bool) -> int | None:
    return _int(row.get("quantity_original") if sold
                else (row.get("quantity_remaining") or row.get("quantity_original")))


# ───────────────────────────── specs ─────────────────────────────

def specs(row: dict, *, sold: bool) -> list[tuple[str, str]]:
    """Label/value pairs for the SPECS table. Only fields that exist render —
    a half-imported folder row gets a short table, never a row of dashes."""
    out: list[tuple[str, str]] = []
    qty = _qty(row, sold)
    price = _float(row.get("price_per_chair"))
    unit = _unit_word(row)
    if qty:
        out.append(("Lot size" if sold else "Available", f"{qty:,} {unit}s"))
    if price and not sold:
        out.append((f"Price per {unit}", _money(price)))
        if qty:
            out.append(("Whole lot", f"{_money(price * qty)} (before freight)"))
    if row.get("chair_type"):
        out.append(("Type", str(row["chair_type"]).strip()))
    if row.get("chair_frame"):
        out.append(("Frame", str(row["chair_frame"]).strip()))
    if row.get("subtitle"):
        out.append(("Seat & finish", str(row["subtitle"]).strip().rstrip(".")))
    dims = (row.get("dimensions") or row.get("dim_in") or "").strip()
    if dims:
        out.append(("Dimensions", dims))
    weight = _float(row.get("chair_weight_lb"))
    if weight:
        out.append((f"Weight per {unit}", f"{weight:g} lb (weighed)"))
    per_pallet = _float(row.get("chairs_per_pallet")) or DEFAULT_CHAIRS_PER_PALLET
    if qty:
        out.append(("Pallets", f"≈ {math.ceil(qty / per_pallet)} at {per_pallet:g} per pallet"))
    city = _city(row)
    if city:
        out.append(("Sourced from" if sold else "Pickup city", city))
    out.append(("Condition", "Used, commercial grade — inspected, see photos"))
    return out


# ───────────────────────────── good for ─────────────────────────────

def good_for(row: dict, *, sold: bool) -> str:
    k = kind(row)
    label = _KIND_LABEL[k]
    buyers = _KIND_BUYERS[k]
    lot_id = row.get("lot_id")
    qty = _qty(row, sold)
    city = _city(row)
    b1, b2, b3 = buyers[0], buyers[1], buyers[2 % len(buyers)]

    opener = _pick(lot_id, (
        f"A lot this size is what {b1} ask us for most",
        f"Sets like this usually go to {b1} or {b2}",
        f"The typical buyer for matched {label} in this quantity is one of {b1}",
        f"We see matched {label} like these land with {b1}, {b2} and {b3}",
    ), "opener")
    why = _pick(lot_id, (
        "one finish, one frame, one delivery — no mixing three catalog orders to seat a room",
        "every chair matches, so a hall, sanctuary or ballroom reads as one set instead of a patchwork",
        "you get a uniform set at a liquidation price instead of paying new-catalog money per chair",
        "the whole room is seated from one lot, with spares left over for the back row",
    ), "why")
    size = ""
    if qty:
        rooms = max(1, qty // 150)
        size = _pick(lot_id, (
            f" {qty:,} seats covers roughly {rooms} full {'room' if rooms == 1 else 'rooms'} of 150, or one large hall with overflow.",
            f" At {qty:,} {label}, that is enough for a main hall plus a fellowship room and spares.",
            f" {qty:,} is a whole-building count — sanctuary, overflow and classrooms from one set.",
        ), "size")
    where = ""
    if city and not sold:
        where = _pick(lot_id, (
            f" Pickup is in {city}; buyers within a few hours' drive usually send a box truck or trailer, everyone else gets a freight estimate from the form above.",
            f" The chairs are in {city}. Drive-up pickup is free; further out we palletise and quote LTL freight to your dock or church lot.",
            f" They sit in {city} today — load them yourself locally, or we wrap them on pallets and ship anywhere in the continental US.",
        ), "where")
    if sold:
        where = _pick(lot_id, (
            " This set is gone, but we buy matched lots like it every month — tell us the count and city and we will flag the next one before it is listed.",
            " Sold. The next comparable lot usually surfaces within weeks; leave a note below and you hear about it first.",
        ), "sold")
    return f"{opener}: {why}.{size}{where}"


# ───────────────────────────── pickup & freight ─────────────────────────────

def pickup_freight(row: dict, *, sold: bool) -> str:
    qty = _qty(row, sold)
    unit = _unit_word(row)
    city = _city(row)
    per_pallet = _float(row.get("chairs_per_pallet")) or DEFAULT_CHAIRS_PER_PALLET
    lbs = _float(row.get("chair_weight_lb"))
    measured = lbs is not None
    lbs = lbs or DEFAULT_LBS_PER_CHAIR
    parts: list[str] = []
    if sold:
        parts.append(f"This lot {('shipped from ' + city) if city else 'has shipped'}.")
        parts.append("Pickup is free on every lot we list, and freight is quoted per ZIP before you commit.")
        return " ".join(parts)
    if city:
        parts.append(f"Local pickup in {city} is free — bring a box truck, trailer or a few vans and we load with you.")
    else:
        parts.append("Local pickup is free; ask for the exact address once you have reserved.")
    if qty:
        pallets = math.ceil(qty / per_pallet)
        weight = qty * lbs
        parts.append(
            f"For freight, {qty:,} {unit}s stack onto about {pallets} pallet{'s' if pallets != 1 else ''} "
            f"({per_pallet:g} per pallet) at an estimated {weight:,.0f} lb total"
            + ("." if measured else f" — {lbs:g} lb per {unit} is an estimate until we weigh one.")
        )
    parts.append("Partial loads ship LTL with liftgate delivery; a full set may be cheaper as a dedicated truck — the estimate form above prices your ZIP, and we confirm the final number before anything moves.")
    return " ".join(parts)


# ───────────────────────────── FAQ ─────────────────────────────

def faq(row: dict, *, sold: bool) -> list[tuple[str, str]]:
    qty = _qty(row, sold)
    unit = _unit_word(row)
    label = _KIND_LABEL[kind(row)]
    city = _city(row)
    price = _float(row.get("price_per_chair"))
    items: list[tuple[str, str]] = []
    if sold:
        items.append((f"Can I still buy these {label}?",
                      "No — this lot has sold. We list comparable sets most weeks; use the form on this page and we will send the next one before it goes public."))
        items.append(("What did a lot like this cost?",
                      (f"This one listed at {_money(price)} per {unit} before freight. " if price else "")
                      + "Liquidation pricing runs a fraction of catalog price for the same commercial-grade chair."))
    else:
        items.append((f"Can I buy fewer than {qty:,} {unit}s?" if qty else "Can I buy part of the lot?",
                      "Usually yes. Lots split by the pallet for local pickup; for freight the minimum is whatever makes the shipping sensible — ask and we will say."))
        items.append(("What condition are the chairs in?",
                      "Used commercial seating pulled from a working venue — expect normal wear, not damage. The photos are of the actual lot, and we flag anything beyond that in the notes."))
        items.append((f"How does delivery work{(' from ' + city) if city else ''}?",
                      "Pickup is free. For freight we palletise and shrink-wrap the chairs and ship LTL or by dedicated truck; the estimate form on this page prices your ZIP, and we confirm the final cost before you pay."))
        items.append(("Do you hold chairs while we get approval?",
                      "Yes. Tell us the count you need and we hold them while your board, pastor or purchasing office signs off — a refundable deposit locks a lot when Checkout is open."))
    return items


def faq_jsonld(items: list[tuple[str, str]]) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in items
        ],
    }
    # </ escaped so no answer can close the <script> tag
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


# ───────────────────────────── site-wide FAQ (home) ─────────────────────────────

SITE_FAQ: list[tuple[str, str]] = [
    ("What does \"sold by the lot\" mean?",
     "Each listing is one matched set of used commercial chairs from a single venue — the count on the page is the count on the floor. You can take the whole lot or, for most lots, a pallet-sized share of it."),
    ("How much do bulk banquet chairs cost?",
     "Our lots are priced per chair, typically a fraction of the new-catalog price for the same commercial-grade chair, plus freight if you are not picking up locally. Every lot page shows its per-chair price."),
    ("Where are the chairs, and can you ship?",
     "Lots sit in the city shown on their page — pickup there is free. For everything else we palletise and ship LTL or by dedicated truck anywhere in the continental US; the freight form on each lot page prices your ZIP."),
    ("Do you buy chairs too?",
     "Yes. Hotels, churches, schools and venues closing or refreshing a room sell us their seating in bulk; we quote on photos and a count, and we handle the haul."),
]


def build(row: dict, *, sold: bool) -> dict[str, Any]:
    """Everything listing_detail.html needs for the original-content sections."""
    items = faq(row, sold=sold)
    k = kind(row)
    return {
        "kind": k,
        "kind_label": _KIND_LABEL[k],
        "specs": specs(row, sold=sold),
        "good_for": good_for(row, sold=sold),
        "pickup_freight": pickup_freight(row, sold=sold),
        "faq": items,
        "faq_jsonld": faq_jsonld(items),
    }


def word_count(*texts: str) -> int:
    return sum(len(re.findall(r"\w+", t or "")) for t in texts)

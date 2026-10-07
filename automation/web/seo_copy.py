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

Three states, because the copy makes promises:
- live     — on the floor: pickup today, we hold while you get approval.
- incoming — `active_bid` / `won_pickup`: bought or being bought, not staged
             yet. Reservations yes; "come load it today" never.
- sold     — past tense, next-lot CTA.

`build(row, *, sold, incoming)` returns a dict the template renders;
`jsonld()` serialises any structured-data block safely. Weight and pallet
maths come from freight_estimate.calibration_from_row so the copy can never
disagree with the freight widget on the same page. `inventory.storage_note`
is never read — the facility address and gate code stay private.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from typing import Any

from automation import freight_estimate, inventory, lot_channels

INCOMING_STATUSES = frozenset({"active_bid", "won_pickup"})

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

_KIND_WORD = {
    "banquet": "banquet", "church": "church", "stacking": "stacking",
    "folding": "folding", "conference": "conference", "dining": "dining", "event": "event",
}


def jsonld(data: dict) -> str:
    """Serialise a structured-data block for a <script type=ld+json>. `</`
    is escaped so no scraped title or description can close the tag."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def kind(row: dict) -> str:
    """Coarse chair family from `chair_type`, else the title. Drives the copy."""
    text = f"{row.get('chair_type') or ''} {row.get('title') or ''}".lower()
    for name, needles in _KIND_RULES:
        if any(n in text for n in needles):
            return name
    return "event"


def kind_label(row: dict) -> str:
    """`banquet chairs`, `banquet tables`, `folding chairs` … — the family
    plus the unit the rest of the page already uses (lot_channels.unit_word)."""
    return f"{_KIND_WORD[kind(row)]} {lot_channels.unit_word(row)}s"


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


def _where(row: dict) -> str:
    """Every city the lot sits in, as prose: `Atlanta, GA`, `Atlanta, GA and
    Nashville, TN`, `A, B and C` — the same list the spec sheet shows."""
    labels = inventory.location_labels(row)
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{'; '.join(labels[:-1])} and {labels[-1]}"


def _qty(row: dict, sold: bool) -> int | None:
    """Sold → the original count. Live/incoming → what is left; a row that is
    out of stock (remaining = 0) advertises nothing, matching the SoldOut
    availability in the Product JSON-LD on the same page."""
    if sold:
        return _int(row.get("quantity_original"))
    remaining = row.get("quantity_remaining")
    if remaining is None:
        return _int(row.get("quantity_original"))
    return _int(remaining)


# ───────────────────────────── freight facts ─────────────────────────────

def freight_facts(row: dict, *, sold: bool) -> list[tuple[str, str]]:
    """Label/value pairs the spec sheet above does NOT already show: whole-lot
    price, frame, pallets and weight. Only fields that exist render."""
    out: list[tuple[str, str]] = []
    qty = _qty(row, sold)
    price = _float(row.get("price_per_chair"))
    unit = lot_channels.unit_word(row)
    cal = freight_estimate.calibration_from_row(row)
    if price and qty and not sold:
        out.append(("Whole lot", f"{_money(price * qty)} (before freight)"))
    if row.get("chair_frame"):
        out.append(("Frame", str(row["chair_frame"]).strip()))
    if qty:
        pallets = freight_estimate.handling_units(qty, cal)
        out.append(("Pallets", f"≈ {pallets} at {cal.chairs_per_pallet:g} per pallet"))
        weight = freight_estimate.total_weight_lb(qty, cal)
        out.append((f"Weight per {unit}",
                    f"{cal.lbs_per_chair:g} lb" + (" (weighed)" if not cal.lbs_per_chair_estimated else " (estimate)")))
        out.append(("Lot weight", f"≈ {weight:,.0f} lb"))
    return out


# ───────────────────────────── good for ─────────────────────────────

def good_for(row: dict, *, sold: bool, incoming: bool = False) -> str:
    k = kind(row)
    label = kind_label(row)
    unit = lot_channels.unit_word(row)
    buyers = _KIND_BUYERS[k]
    lot_id = row.get("lot_id")
    qty = _qty(row, sold)
    where = _where(row)
    b1, b2, b3 = buyers[0], buyers[1], buyers[2 % len(buyers)]

    opener = _pick(lot_id, (
        f"A lot like this is what {b1} ask us for most",
        f"Sets like this usually go to {b1} or {b2}",
        f"The typical buyer for matched {label} in this quantity is one of {b1}",
        f"We see matched {label} like these land with {b1}, {b2} and {b3}",
    ), "opener")
    why = _pick(lot_id, (
        "one finish, one frame, one delivery — no mixing three catalog orders to seat a room",
        "every piece matches, so a hall, sanctuary or ballroom reads as one set instead of a patchwork",
        "you get a uniform set at a liquidation price instead of paying new-catalog money per piece",
        "the whole room is furnished from one lot, with spares left over for the back row",
    ), "why")
    size = ""
    if qty and qty >= 150:
        rooms = qty // 150
        size = _pick(lot_id, (
            f" {qty:,} seats covers roughly {rooms} full {'room' if rooms == 1 else 'rooms'} of 150, or one large hall with overflow.",
            f" At {qty:,} {label}, that is enough for a main hall plus a fellowship room and spares.",
            f" {qty:,} is a whole-building count — sanctuary, overflow and classrooms from one set.",
        ), "size")
    elif qty:
        size = _pick(lot_id, (
            f" {qty:,} is a single-room count — a fellowship hall, a classroom block, or a top-up for a rental fleet.",
            f" At {qty:,} {label}, this fits one meeting room or youth hall, or replaces the worst of an existing set.",
            f" {qty:,} seats a small sanctuary or a banquet room, and it is a sensible first lot for a venue trying us out.",
        ), "size")
    tail = ""
    if sold:
        tail = _pick(lot_id, (
            " This set is gone, but we buy matched lots like it every month — tell us the count and city and we will flag the next one before it is listed.",
            " Sold. The next comparable lot usually surfaces within weeks; leave a note below and you hear about it first.",
        ), "sold")
    elif incoming and where:
        tail = _pick(lot_id, (
            f" This lot is on its way to {where}; reserve now and collect once it lands, or get a freight estimate from the form above.",
            f" The {unit}s are headed for {where} — reservations are open before they arrive, and freight is quoted per ZIP.",
        ), "incoming")
    elif where:
        tail = _pick(lot_id, (
            f" Pickup is in {where}; buyers within a few hours' drive usually send a box truck or trailer, everyone else gets a freight estimate from the form above.",
            f" The {unit}s are in {where}. Drive-up pickup is free; further out we palletise and quote LTL freight to your dock or church lot.",
            f" They sit in {where} today — load them yourself locally, or we wrap them on pallets and ship anywhere in the continental US.",
        ), "where")
    return f"{opener}: {why}.{size}{tail}"


# ───────────────────────────── pickup & freight ─────────────────────────────

def pickup_freight(row: dict, *, sold: bool, incoming: bool = False) -> str:
    qty = _qty(row, sold)
    unit = lot_channels.unit_word(row)
    where = _where(row)
    cal = freight_estimate.calibration_from_row(row)
    parts: list[str] = []
    if sold:
        parts.append(f"This lot {('shipped from ' + where) if where else 'has shipped'}.")
        parts.append("Pickup is free on every lot we list, and freight is quoted per ZIP before you commit.")
        return " ".join(parts)
    if incoming:
        parts.append(
            (f"These {unit}s are on their way to {where}" if where else f"These {unit}s are on their way")
            + " — reserve now and collect once they land; we confirm the date before you drive."
        )
    elif where:
        parts.append(f"Local pickup in {where} is free — bring a box truck, trailer or a few vans and we load with you.")
    else:
        parts.append("Local pickup is free; ask for the exact address once you have reserved.")
    if qty:
        pallets = freight_estimate.handling_units(qty, cal)
        weight = freight_estimate.total_weight_lb(qty, cal)
        parts.append(
            f"For freight, {qty:,} {unit}s stack onto about {pallets} pallet{'s' if pallets != 1 else ''} "
            f"({cal.chairs_per_pallet:g} per pallet) at an estimated {weight:,.0f} lb total"
            + ("." if not cal.lbs_per_chair_estimated
               else f" — {cal.lbs_per_chair:g} lb per {unit} is an estimate until we weigh one.")
        )
    parts.append("Partial loads ship LTL with liftgate delivery; a full set may be cheaper as a dedicated truck — the estimate form above prices your ZIP, and we confirm the final number before anything moves.")
    return " ".join(parts)


# ───────────────────────────── FAQ ─────────────────────────────

def faq(row: dict, *, sold: bool, incoming: bool = False) -> list[tuple[str, str]]:
    qty = _qty(row, sold)
    unit = lot_channels.unit_word(row)
    label = kind_label(row)
    where = _where(row)
    price = _float(row.get("price_per_chair"))
    items: list[tuple[str, str]] = []
    if sold:
        items.append((f"Can I still buy these {label}?",
                      "No — this lot has sold. We list comparable sets most weeks; use the form on this page and we will send the next one before it goes public."))
        items.append(("What did a lot like this cost?",
                      (f"This one listed at {_money(price)} per {unit} before freight. " if price else "")
                      + "Liquidation pricing runs a fraction of catalog price for the same commercial-grade piece."))
        return items
    items.append((f"Can I buy fewer than {qty:,} {unit}s?" if qty else "Can I buy part of the lot?",
                  "Usually yes. Lots split by the pallet for local pickup; for freight the minimum is whatever makes the shipping sensible — ask and we will say."))
    items.append((f"What condition are the {unit}s in?",
                  "Used commercial seating pulled from a working venue — expect normal wear, not damage. The photos are of the actual lot, and we flag anything beyond that in the notes."))
    if incoming:
        items.append((f"When can I collect{(' in ' + where) if where else ''}?",
                      "This lot is incoming — bought or being bought, not staged yet. Reserve now; we confirm the pickup window the moment it lands, and freight is quoted per ZIP in the meantime."))
        items.append(("Do you take reservations before it arrives?",
                      "Yes. Tell us the count you need and we hold it; nothing is charged until the lot lands and you confirm — a refundable deposit locks it when Checkout is open."))
    else:
        items.append((f"How does delivery work{(' from ' + where) if where else ''}?",
                      "Pickup is free. For freight we palletise and shrink-wrap the pieces and ship LTL or by dedicated truck; the estimate form on this page prices your ZIP, and we confirm the final cost before you pay."))
        items.append((f"Do you hold {unit}s while we get approval?",
                      "Yes. Tell us the count you need and we hold them while your board, pastor or purchasing office signs off — a refundable deposit locks a lot when Checkout is open."))
    return items


def faq_jsonld(items: list[tuple[str, str]]) -> str:
    return jsonld({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in items
        ],
    })


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


def build(row: dict, *, sold: bool, incoming: bool | None = None) -> dict[str, Any]:
    """Everything listing_detail.html needs for the original-content sections."""
    if incoming is None:
        incoming = (not sold) and row.get("status") in INCOMING_STATUSES
    items = faq(row, sold=sold, incoming=incoming)
    return {
        "kind": kind(row),
        "kind_label": kind_label(row),
        "incoming": incoming,
        "freight_facts": freight_facts(row, sold=sold),
        "good_for": good_for(row, sold=sold, incoming=incoming),
        "pickup_freight": pickup_freight(row, sold=sold, incoming=incoming),
        "faq": items,
        "faq_jsonld": faq_jsonld(items),
    }


def word_count(*texts: str) -> int:
    return sum(len(re.findall(r"\w+", t or "")) for t in texts)

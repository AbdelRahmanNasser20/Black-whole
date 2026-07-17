"""eBay bulk listing exporter — Seller Hub / File Exchange CSV (BLACKWHOLE-9).

Pure, DB-free rendering of `inventory` rows into the CSV that eBay Seller Hub
bulk upload (a.k.a. File Exchange) ingests to **create** (``Action=Add``) or
**edit** (``Action=Revise`` + ``ItemID``) fixed-price listings. The SQL that
*selects* sellable lots lives in `inventory.list_catalog_feed()`; this module
turns those rows into eBay columns and decides per-row eligibility.

Content is generated **deterministically from inventory data — no LLM** (the
BLACKWHOLE-9 key decision). Item specifics are derived by reusing the keyword
enrichment in `listing_content.parse_attributes` so the eBay ``C:<Name>``
columns are filled as completely as the free text allows. Photos come from the
durable **Supabase** URLs (`hero_image_url` + `image_urls`), never local disk —
eBay fetches ``PicURL`` server-side, so only absolute http(s) URLs are usable.

Keeping this separate from the browser driver and the web layer means the CSV
format is unit-testable with plain dict fixtures — no database, no Playwright,
no network.

⚠️ OPERATOR INPUT REQUIRED before a produced CSV will import cleanly (see the
BLACKWHOLE-9 ticket "blocked" item — the downloaded Seller Hub template pins
these down exactly):
  * ``EBAY_BANQUET_CATEGORY_ID`` — the numeric leaf category id for the
    "Banquet Chairs" category. Left blank until set; eBay requires it on Add.
  * ``EBAY_SHIPPING_PROFILE`` / ``EBAY_RETURN_PROFILE`` / ``EBAY_PAYMENT_PROFILE``
    — your eBay Business Policy names. Emitted as columns only when set.
  * Confirm the exact **required** Banquet Chairs item-specific labels against
    the downloaded template; adjust ``_SPECIFIC_LABELS`` if eBay names differ.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Iterable

from .listing_content import parse_attributes
from .templates import listing_title

# ── eBay File Exchange constants ──────────────────────────────────────────────
ACTION_ADD = "Add"
ACTION_REVISE = "Revise"
CONDITION_USED = "3000"          # eBay ConditionID for "Used"
FORMAT = "FixedPrice"
DURATION = "GTC"                 # Good 'Til Cancelled
MAX_PICS = 24                    # eBay's per-listing photo cap
DEFAULT_QUANTITY = 1            # lead-gen: qty 1 + ad-fee outreach (ticket)

# Item-specific labels, in a stable column order. Derived from the enriched
# ChairAttributes. Confirm these against the operator's downloaded template.
_SPECIFIC_LABELS = [
    "Brand", "Type", "Color", "Frame Material",
    "Seat Material", "Frame Color", "Features",
]

_BASE_COLUMNS = [
    "Action", "CustomLabel", "Category", "Title", "ConditionID",
    "PicURL", "Description", "Format", "Duration", "StartPrice",
    "Quantity", "Location", "PostalCode",
]
_SPECIFIC_COLUMNS = [f"C:{label}" for label in _SPECIFIC_LABELS]
_TAIL_COLUMNS = ["ItemID"]

# Full header, in order.
CSV_COLUMNS = _BASE_COLUMNS + _SPECIFIC_COLUMNS + _TAIL_COLUMNS

# Optional Business Policy columns, appended only when the env vars are set.
_POLICY_ENV = {
    "ShippingProfileName": "EBAY_SHIPPING_PROFILE",
    "ReturnProfileName": "EBAY_RETURN_PROFILE",
    "PaymentProfileName": "EBAY_PAYMENT_PROFILE",
}

_DESC_MAX = 4000
_ITM_RE = re.compile(r"/itm/(\d{9,})")
_ID_DIGITS_RE = re.compile(r"\b(\d{9,})\b")


def site_base_url() -> str:
    """Base URL for the black-whole.com backlink on every listing."""
    return os.getenv("LISTING_WEBSITE_BASE", "https://black-whole.com").rstrip("/")


def banquet_category_id() -> str:
    """Numeric eBay category id for Banquet Chairs, or '' until the operator
    supplies ``EBAY_BANQUET_CATEGORY_ID``."""
    return (os.getenv("EBAY_BANQUET_CATEGORY_ID") or "").strip()


def policy_columns() -> dict[str, str]:
    """Business Policy columns present in the environment (may be empty)."""
    return {col: v for col, env in _POLICY_ENV.items()
            if (v := (os.getenv(env) or "").strip())}


def columns() -> list[str]:
    """The full CSV header for the current environment (policies appended)."""
    return CSV_COLUMNS + list(policy_columns())


def _clean(text: object) -> str:
    """Collapse whitespace so a value is safe inside one CSV cell."""
    return " ".join(str(text).split()) if text not in (None, "") else ""


def _price(row: dict) -> str | None:
    """`"<amount>"` for a positive per-chair price, else None (row dropped)."""
    val = row.get("price_per_chair")
    if val in (None, ""):
        return None
    try:
        amount = float(val)
    except (TypeError, ValueError):
        return None
    return f"{amount:.2f}" if amount > 0 else None


def _image_urls(row: dict) -> list[str]:
    """Absolute http(s) photo URLs (Supabase), hero first, deduped, capped.

    ``image_urls`` may arrive as a JSON string (psycopg Jsonb round-trips) or a
    Python list; both are handled. Relative/local paths are ignored — eBay
    fetches ``PicURL`` server-side, so only durable public URLs work.
    """
    raw = row.get("image_urls")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
    urls: list[str] = []
    hero = (row.get("hero_image_url") or "").strip()
    if hero:
        urls.append(hero)
    for u in (raw or []):
        u = (u or "").strip() if isinstance(u, str) else ""
        if u:
            urls.append(u)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_PICS]


def _pic_url(row: dict) -> str | None:
    """eBay ``PicURL`` cell: pipe-joined image URLs, or None (row dropped)."""
    urls = _image_urls(row)
    return "|".join(urls) if urls else None


def existing_item_id(row: dict) -> str | None:
    """The eBay item id already recorded for this lot, else None.

    Reads ``ebay_item_id`` if present, otherwise parses ``/itm/<id>`` out of the
    stored ``ebay_url``. Determines Add (new) vs Revise (edit an existing live
    listing). Pure — no DB.
    """
    direct = _clean(row.get("ebay_item_id"))
    if direct:
        m = _ID_DIGITS_RE.search(direct)
        if m:
            return m.group(1)
    url = _clean(row.get("ebay_url"))
    m = _ITM_RE.search(url) or _ID_DIGITS_RE.search(url)
    return m.group(1) if m else None


def _title(row: dict) -> str:
    """The 80-char-capped eBay title (shared with the FB/driver format)."""
    return listing_title(
        row.get("chair_type") or "",
        city=row.get("city") or "", state=row.get("state") or "",
        fallback=row.get("title") or "Bulk Banquet Chairs",
    )[:80]


def _description(row: dict, website: str) -> str:
    """Deterministic listing body + black-whole.com backlink.

    Uses the lot's own copy when present, then a location/quantity sentence and
    the lead-gen call-to-action, and finally the backlink to the lot page.
    """
    parts: list[str] = []
    base = _clean(row.get("description"))
    if base:
        parts.append(base)
    loc = ", ".join(p for p in (_clean(row.get("city")), _clean(row.get("state"))) if p)
    qty = row.get("quantity_remaining")
    if qty and loc:
        parts.append(f"Approximately {int(qty)} available in {loc}. Used, good "
                     "condition with normal wear from prior service.")
    elif loc:
        parts.append(f"Available in {loc}.")
    parts.append("Local pickup; freight delivery quotes available on request. "
                 "Ideal for churches, banquet halls, schools, event venues and "
                 "rental companies. Message us for quantity and freight quotes.")
    parts.append(f"More photos, full details and our other lots: {website}")
    return _clean(" ".join(parts))[:_DESC_MAX]


def csv_row(row: dict, base_url: str | None = None, *,
            quantity: int = DEFAULT_QUANTITY) -> dict | None:
    """Map one inventory row to an eBay File Exchange row, or None if it lacks
    the essentials (positive price + at least one Supabase photo URL).

    ``Action`` is ``Revise`` (+ ``ItemID``) when the lot already has a live eBay
    listing, else ``Add``.
    """
    base = (base_url or site_base_url()).rstrip("/")
    lot_id = _clean(row.get("lot_id"))
    price = _price(row)
    pics = _pic_url(row)
    if not (lot_id and price and pics):
        return None

    attrs = parse_attributes(row).as_ebay_specifics()
    item_id = existing_item_id(row)
    website = f"{base}/listings/{lot_id}"

    out = {c: "" for c in CSV_COLUMNS}
    out.update({
        "Action": ACTION_REVISE if item_id else ACTION_ADD,
        "CustomLabel": lot_id,                  # SKU = GovDeals lot id verbatim
        "Category": banquet_category_id(),      # blank until operator sets env
        "Title": _title(row),
        "ConditionID": CONDITION_USED,
        "PicURL": pics,
        "Description": _description(row, website),
        "Format": FORMAT,
        "Duration": DURATION,
        "StartPrice": price,
        "Quantity": str(quantity),
        "Location": ", ".join(p for p in (_clean(row.get("city")),
                                          _clean(row.get("state"))) if p),
        "PostalCode": _clean(row.get("zip_code")),
        "ItemID": item_id or "",
    })
    for label in _SPECIFIC_LABELS:
        if label in attrs:
            out[f"C:{label}"] = attrs[label]
    out.update(policy_columns())
    return out


def build_rows(rows: Iterable[dict], *, base_url: str | None = None,
               quantity: int = DEFAULT_QUANTITY) -> list[dict]:
    """Eligible eBay rows for the given inventory rows (drops incomplete)."""
    base = (base_url or site_base_url()).rstrip("/")
    return [r for src in rows
            if (r := csv_row(src, base_url=base, quantity=quantity))]


def rows_to_csv(rows: Iterable[dict], *, base_url: str | None = None,
                quantity: int = DEFAULT_QUANTITY) -> str:
    """Render inventory rows to the eBay bulk CSV (header + eligible rows)."""
    fieldnames = columns()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(build_rows(rows, base_url=base_url, quantity=quantity))
    return buf.getvalue()

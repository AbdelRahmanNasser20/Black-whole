"""LLM analysis of an archived lot — run once, cached next to the archive.

Reuses the deals analyzer rather than a new prompt:
- `deals.llm_steps.extract_identity` — brand/model/item type, quantity,
  condition, eBay search queries, a rough used price per unit.
- `deals.classify.classify_category` — our canonical category.
- `deals.quantity.lot_quantity` — the deterministic title/description count.
- `deals.comps` + `deals.llm_steps.judge_comps` + `deals.valuation` — eBay sold
  comps, when the Pi comps service is configured (`COMPS_URL`/`COMPS_KEY`).
- Auction comps from OUR data: `deal_lots` closes (whole site, ~110k priced) and
  `sold_comps` (the recorder's view), matched on the identity's keywords.

"Was the final price a deal?" compares the lot's final per-unit price with the
auction comps' median per-unit price. Fewer than 3 comps is "not enough comps",
never a guess.

**A failure is never an answer** (deals.md). If the identity call fails the
analysis is stored as `status: unavailable` with the error — the page says so
and offers a re-run; nothing is filled with a default. A failed category call
leaves `category.llm` null the same way.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable

from recorder import lot_archive

MIN_COMPS = 3
MAX_COMPS = 60
ANALYSIS_VERSION = 1

_STOP = {"lot", "of", "the", "and", "with", "for", "used", "misc", "miscellaneous", "assorted",
         "various", "set", "sets", "pcs", "pc", "qty", "approx", "each", "new", "old", "unit",
         "units", "item", "items", "surplus", "a", "an", "in", "on", "to", "or", "w", "x",
         "bulk", "pieces", "piece", "university", "school", "county", "city", "state", "good",
         "condition", "working", "excellent", "sold", "ebay", "auction", "government"}


def keywords(item_type: str | None, queries: list[str] | None, title: str | None = None,
             limit: int = 3) -> list[str]:
    """Search words for comps, most specific first. Pure.

    The identity's `item_type` names the thing ("banquet chair"); queries add a
    brand/model. Words are singularised crudely ("chairs" → "chair") so an
    ILIKE '%chair%' also matches the plural."""
    words: list[str] = []
    for text in [item_type or ""] + list(queries or [])[:1] + [title or ""]:
        for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text.lower()):
            w = w.strip("-")
            if w in _STOP or len(w) < 3:
                continue
            if w.endswith("ies") and len(w) > 4:
                w = w[:-3] + "y"
            elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
                w = w[:-1]
            if w not in words:
                words.append(w)
        if len(words) >= limit:
            break
    return words[:limit]


def _percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        raise ValueError("empty")
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def deal_verdict(final_per_unit: float | None, comp_per_units: list[float], outcome: str | None) -> dict:
    """Pure. Final per-unit price vs the auction comps' median per-unit."""
    n = len(comp_per_units)
    out = {"comps": n, "final_per_unit": final_per_unit, "median_per_unit": None,
           "p25_per_unit": None, "p75_per_unit": None, "ratio": None, "verdict": None}
    if n < MIN_COMPS:
        out["verdict_label"] = f"not enough comps ({n} < {MIN_COMPS})"
        return out
    med = statistics.median(comp_per_units)
    out.update(median_per_unit=round(med, 2),
               p25_per_unit=round(_percentile(comp_per_units, 0.25), 2),
               p75_per_unit=round(_percentile(comp_per_units, 0.75), 2))
    if final_per_unit is None or med <= 0:
        out["verdict_label"] = "no final price to compare"
        return out
    ratio = final_per_unit / med
    out["ratio"] = round(ratio, 2)
    if ratio <= 0.6:
        v = "steal"
    elif ratio <= 0.85:
        v = "good deal"
    elif ratio <= 1.15:
        v = "market price"
    else:
        v = "above market"
    out["verdict"] = v
    sold = outcome == "sold"
    out["verdict_label"] = (f"{v} — {'sold' if sold else 'high bid'} at {ratio:.2f}× the median "
                            f"of {n} similar closes"
                            + ("" if sold else " (not a sale: reserve/no-bid/cancelled)"))
    return out


# ─────────────────────────── auction comps (our DB) ───────────────────────────

_DEAL_LOT_COMPS_SQL = """
SELECT asset_id, account_id, auction_id, title, final_bid AS price, final_bid_count AS bids,
       closed_at, state
FROM deal_lots
WHERE site = 'govdeals' AND outcome IN ('sold', 'low_bid') AND final_bid > 0
  AND title ILIKE ALL(%s)
  AND NOT (asset_id = %s AND account_id = %s AND auction_id = %s)
ORDER BY end_utc DESC
LIMIT %s
"""

# sold_comps has no title; the recorder kept the search asset in raw.
_SOLD_COMPS_SQL = """
SELECT c.source, c.source_lot_id, c.final_price AS price, c.bid_count AS bids, c.sold_at AS closed_at,
       t.title
FROM sold_comps c
JOIN LATERAL (
    SELECT COALESCE(raw->>'assetShortDescription', raw->>'title', raw->>'name') AS title
    FROM listing_snapshots s
    WHERE s.source = c.source AND s.source_lot_id = c.source_lot_id
      AND COALESCE(raw->>'assetShortDescription', raw->>'title', raw->>'name') IS NOT NULL
    ORDER BY s.observed_at DESC LIMIT 1
) t ON TRUE
WHERE c.final_price > 0 AND t.title ILIKE ALL(%s) AND c.source_lot_id <> %s
ORDER BY c.sold_at DESC NULLS LAST
LIMIT %s
"""


def auction_comps(words: list[str], lot_key: str) -> tuple[list[dict], list[str]]:
    """Similar closes from our own tables. Narrows from all keywords down to
    the first one until at least MIN_COMPS match. Returns (comps, words used)."""
    from automation import db
    from recorder.store import _read_with_backoff

    a, b, c = (int(p) for p in lot_key.split("/"))
    for n in range(len(words), 0, -1):
        use = words[:n]
        pats = [f"%{w}%" for w in use]
        rows = [dict(r, origin="deal_lots") for r in
                _read_with_backoff(db.fetch_all, _DEAL_LOT_COMPS_SQL, (pats, a, b, c, MAX_COMPS))]
        try:
            rows += [dict(r, origin=f"sold_comps:{r['source']}") for r in
                     _read_with_backoff(db.fetch_all, _SOLD_COMPS_SQL, (pats, lot_key, 20))]
        except Exception as e:  # noqa: BLE001 - optional second source
            print(f"[lot_analysis] sold_comps unreadable: {e}", file=sys.stderr)
        if len(rows) >= MIN_COMPS or n == 1:
            return rows, use
    return [], []


# ─────────────────────────────── the analysis ───────────────────────────────

IDENTITY_MAX_TOKENS = 900
CLASSIFY_MAX_TOKENS = 300


def identity(lot) -> "LotIdentity":
    """`deals.llm_steps.extract_identity`'s prompt and parser with a larger
    reply budget. The provider's default model is now `openai/gpt-oss-120b`, a
    reasoning model: its hidden reasoning spends `max_tokens` first, and the
    stock 300 came back EMPTY on a real lot (2026-09-29). Same failure rule —
    an unparseable reply raises `LlmStepError`."""
    from deals.llm_steps import _IDENTITY_PROMPT, LlmStepError, parse_identity_response
    from deals.llm_provider import LlmUnavailable, chat
    try:
        text = chat(_IDENTITY_PROMPT.format(title=lot.title[:200], desc=(lot.description or "")[:1500]),
                    max_tokens=IDENTITY_MAX_TOKENS)
    except LlmUnavailable as e:
        raise LlmStepError(f"identity call failed: {e}") from e
    return parse_identity_response(text)


def classify(title: str, description: str) -> tuple[str, float]:
    """`deals.classify` prompt + parser, with room for the reply. The stock
    `classify_category` asks for 64 tokens, which the current Groq model cut
    mid-JSON on a real lot (2026-09-29: `{"label":"seating_furniture","confidence`)
    — a truncated reply is still a failure, so it raises, never defaults."""
    from deals.classify import ClassificationUnavailable, build_prompt, parse_response
    from deals.llm_provider import LlmUnavailable, chat
    try:
        text = chat(build_prompt(title, description), max_tokens=CLASSIFY_MAX_TOKENS)
    except LlmUnavailable as e:
        raise ClassificationUnavailable(str(e)) from e
    return parse_response(text)


def usable_for_per_unit(our_qty: int, comp_qty_source: str) -> bool:
    """A comp's per-unit price only means something when its unit count is
    known. A 150-chair lot compared with "Bulk Stacking Chairs" (count
    unstated, read as 1) would call $1,725 a steal against $20 — so unknown-
    count comps count only against a single-unit lot."""
    return comp_qty_source != "default" or our_qty <= 1


def _identity_lot(summary: dict) -> SimpleNamespace:
    return SimpleNamespace(title=summary.get("title") or "", description=summary.get("description") or "")


def analyze(doc: dict, *, identity_fn: Callable | None = None, classify_fn: Callable | None = None,
            comps_fn: Callable = auction_comps, ebay_provider=None, fees=None,
            now: datetime | None = None) -> dict:
    """One archived lot → analysis dict. Never raises for an LLM problem:
    returns `status: unavailable` with the error instead."""
    from deals.llm_steps import LlmStepError, judge_comps
    from deals.llm_provider import LlmUnavailable, active_provider
    from deals.quantity import lot_quantity, unit_price
    from deals.fees import fee_model_from_env, landed_cost
    from deals.valuation import value_from_comps

    identity_fn = identity_fn or identity
    classify_fn = classify_fn or classify
    now = now or datetime.now(timezone.utc)
    s = doc.get("summary") or {}
    out: dict = {"version": ANALYSIS_VERSION, "lot_key": doc["lot_key"],
                 "analyzed_at": now.isoformat(), "status": "ok"}
    try:
        prov = active_provider()
        out["provider"] = prov[0] if prov else None   # never the key (tuple[1])
    except Exception:  # noqa: BLE001
        out["provider"] = None

    lot = _identity_lot(s)
    try:
        ident = identity_fn(lot)
    except (LlmStepError, LlmUnavailable) as e:
        out.update(status="unavailable", stage="identity", error=str(e)[:300])
        return out

    try:
        label, conf = classify_fn(lot.title, lot.description)
        category = {"llm": label, "confidence": conf}
    except LlmUnavailable as e:
        category = {"llm": None, "confidence": None, "error": str(e)[:200]}
    category.update(native=s.get("category_name"), canonical=s.get("canonical_category"))
    out["category"] = category

    det_qty, det_src = lot_quantity(lot.title, lot.description)
    qty = det_qty if det_src != "default" else max(1, ident.quantity or 1)
    out["quantity"] = {"value": qty, "deterministic": det_qty, "deterministic_source": det_src,
                       "llm": ident.quantity,
                       "source": det_src if det_src != "default" else "llm"}
    out["identity"] = {"brand": ident.brand, "model": ident.model, "item_type": ident.item_type,
                       "queries": ident.queries}
    out["condition"] = {"llm": ident.condition, "govdeals_code": s.get("condition_code")}

    final = s.get("final_price")
    final_pu = unit_price(final, qty) if final is not None else None

    # Auction comps (what similar surplus closed for) → "was it a deal?"
    words = keywords(ident.item_type, ident.queries, lot.title)
    try:
        comps, used = comps_fn(words, doc["lot_key"]) if words else ([], [])
    except Exception as e:  # noqa: BLE001 - DB trouble: say so, don't invent
        comps, used = [], []
        out["comps_error"] = str(e)[:200]
    pus = []
    excluded = 0
    for c in comps:
        cq, src = lot_quantity(c.get("title"), None)
        pu = unit_price(float(c["price"]), cq)
        c["quantity"], c["per_unit"] = cq, pu
        c["used"] = bool(pu) and usable_for_per_unit(qty, src)
        if c["used"]:
            pus.append(pu)
        else:
            excluded += 1
    out["deal"] = deal_verdict(final_pu, pus, s.get("outcome"))
    out["deal"]["keywords"] = used
    out["deal"]["excluded_unknown_count"] = excluded
    comps.sort(key=lambda c: not c.get("used"))
    out["comps"] = [{"title": (c.get("title") or "")[:120], "price": float(c["price"]),
                     "per_unit": c.get("per_unit"), "quantity": c.get("quantity"),
                     "used": c.get("used"),
                     "closed_at": c.get("closed_at"), "origin": c.get("origin"),
                     "lot_key": (f"{c['asset_id']}/{c['account_id']}/{c['auction_id']}"
                                 if c.get("asset_id") is not None else c.get("source_lot_id"))}
                    for c in comps[:25]]

    # Fair resale value: eBay sold comps when the Pi service is reachable,
    # else the LLM's own per-unit estimate, labelled low confidence.
    fees = fees or fee_model_from_env()
    resale: dict = {"method": None}
    if ebay_provider is not None and ident.queries:
        from deals.comps import CompsUnavailable
        for q in ident.queries:
            try:
                res = ebay_provider.fetch(q)
            except CompsUnavailable as e:
                resale["ebay_error"] = str(e)[:200]
                continue
            kept = judge_comps(ident, res.items)
            val = value_from_comps(kept, qty, final or 0.0, fees)
            if val is not None:
                prices = sorted(c.price for c in kept)
                resale = {"method": "ebay_comps", "query": q, "comps": len(kept),
                          "per_unit": val.per_unit,
                          "low": round(_percentile(prices, 0.25) * qty * val.recovery_tier, 2),
                          "high": round(_percentile(prices, 0.75) * qty * val.recovery_tier, 2),
                          "estimate": val.est_resale, "confidence": val.confidence}
                break
    if resale.get("method") is None:
        est = ident.est_resale_per_unit
        if est:
            resale.update(method="llm_estimate", per_unit=est, confidence="low",
                          estimate=round(est * qty, 2),
                          low=round(est * qty * 0.75, 2), high=round(est * qty * 1.25, 2),
                          note="the model's own guess (±25%) — no sold comps behind it")
        else:
            resale.update(method="unavailable", note="no comps and no model estimate")
    if final is not None and resale.get("estimate"):
        lc = landed_cost(float(final), qty, fees).total
        resale["landed_cost"] = round(lc, 2)
        resale["margin"] = round(resale["estimate"] - lc, 2)
    out["resale"] = resale
    return out


def load(store, key) -> dict | None:
    blob = store.get(lot_archive.analysis_key(key))
    return json.loads(blob) if blob else None


def analyze_and_store(store, key: str, **kw) -> dict:
    doc = lot_archive.load(store, key)
    if doc is None:
        return {"lot_key": key, "status": "unavailable", "error": "lot is not archived"}
    if "ebay_provider" not in kw:
        from deals.comps import comps_provider_from_env
        kw["ebay_provider"] = comps_provider_from_env()
    a = analyze(doc, **kw)
    store.put(lot_archive.analysis_key(key), json.dumps(a, default=str).encode(), "application/json")
    return a

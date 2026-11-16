"""All-in cost block for the public lot page: bid + buyer's premium.

Two rules:

1. **Never invent a number.** A premium shows only for a source with a row in
   `SOURCE_PREMIUM`, and it is always labelled an estimate: the real rate is
   set per auction by the seller and is on the listing. A source with no row
   says so in plain words and shows no total.
2. **No sales-tax number.** Tax depends on the seller, the buyer's state and
   resale exemption; we have no sourced rate table, so the page states that
   tax is charged at checkout and is not included.

The rates are the ones `deals/fees.py` already uses for landed cost
(`SITE_BUYER_PREMIUM`), keyed here by the web source key.
"""
from __future__ import annotations

from deals.fees import SITE_BUYER_PREMIUM

# deals/fees.py site key -> web source key
_FEE_KEYS = {"govdeals": "govdeals", "allsurplus": "allsurplus", "publicsurplus": "public_surplus"}
SOURCE_PREMIUM: dict[str, float] = {
    web: float(SITE_BUYER_PREMIUM[fee]) for fee, web in _FEE_KEYS.items() if fee in SITE_BUYER_PREMIUM
}
TAX_NOTE = "Sales tax is charged by the seller at checkout and is not included."


def estimate(source: str | None, bid: float | None) -> dict:
    """`{known, pct, bid, premium, total, tax_note}` for one lot.

    `known` False = no premium on file for this site: `pct`, `premium` and
    `total` are None and the template says to check the listing."""
    pct = SOURCE_PREMIUM.get(source or "")
    out = {"known": pct is not None, "pct": None, "bid": bid, "premium": None,
           "total": None, "tax_note": TAX_NOTE}
    if pct is None:
        return out
    out["pct"] = round(pct * 100, 2)
    if bid is not None:
        out["premium"] = round(float(bid) * pct, 2)
        out["total"] = round(float(bid) + out["premium"], 2)
    return out

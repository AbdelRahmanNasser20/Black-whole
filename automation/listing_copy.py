"""One guard for public listing copy: no bulk / tiered / volume prices.

Operator HARD RULE (2026-10-15): "Don't include any bulk prices at all, for any
listing." Public copy carries ONE plain per-unit price and nothing else —
no "$22 for 500+", no "bulk discounts", no "take all 300 for $6,000", no
whole-lot total.

Every channel writer runs its text through :func:`strip_bulk_pricing` right
before it leaves the building (storefront, FB/Google catalog feeds, FB
Marketplace, eBay, Craigslist). It drops the whole sentence (or line) that
carries the tier wording, so a stray line in ``inventory.description`` or an
LLM rewrite can never put a bulk price on a channel. :func:`find_bulk_pricing`
reports the offending sentences for audits and tests.

Buyer chat / CRM negotiation is out of scope — this is listing copy only.
"""
from __future__ import annotations

import re

_MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?"
_QTY = r"\d[\d,]*"

# Each pattern marks a sentence as carrying a bulk/tier price.
_PATTERNS: tuple[str, ...] = (
    # "bulk discount(s)", "volume pricing", "quantity discount", "wholesale rate"
    r"\b(?:bulk|volume|quantity|wholesale|tiered|tier)\s+(?:discounts?|pric(?:e|es|ing)|rates?|deals?)\b",
    r"\bprice\s+breaks?\b",
    r"\bbulk\s+tiers?\b",
    r"\bdiscounts?\s+(?:on|for)\s+(?:larger|bigger|bulk|volume|the\s+(?:full|whole|entire)|all)\b",
    r"\b(?:larger|bigger|bulk|volume)\s+orders?\s+(?:get|save|receive|qualify)\b",
    r"\b(?:better|lower|best|special)\s+(?:price|pricing|rate|deal)s?\s+(?:on|for)\s+"
    r"(?:larger|bigger|bulk|volume|more|the\s+(?:full|whole|entire)|all)\b",
    r"\bthe\s+more\s+you\s+buy\b",
    r"\bbuy\s+more,?\s+save\b",
    # "$22 for 500+", "$22 each for 100 or more", "$20/chair on 300+"
    _MONEY + r"[^.!?\n]{0,30}?\b(?:for|on|at|with)\s+(?:orders?\s+of\s+)?" + _QTY + r"\s*(?:\+|or\s+more|and\s+up)",
    # "500+ chairs at $22", "100 or more for $20"
    _QTY + r"\s*(?:\+|or\s+more|and\s+up)[^.!?\n]{0,30}?(?:@|→|->|=|:|\bat\b|\bfor\b|\bjust\b|\bonly\b)\s*" + _MONEY,
    # "Take ALL 300 for $6,000", "all 300 chairs for $6000", "buy all for $..."
    r"\b(?:take|buy|get)?\s*all\s+(?:" + _QTY + r"\s+)?(?:\w+\s+){0,2}for\s+" + _MONEY,
    # "whole lot for $X", "Whole lot: $6,000", "full lot $X", "entire lot price"
    r"\b(?:whole|full|entire)\s+lot\b[^.!?\n]{0,40}?" + _MONEY,
    r"\b(?:whole|full|entire)[- ]lot\s+(?:price|pricing|total|discount|deal|rate)\b",
    # "make an offer on the whole lot/all of them" — an offer invite on the lot
    r"\boffers?\s+(?:on|for)\s+(?:all\b|(?:the\s+)?(?:whole|full|entire)\s+lot\b)",
    r"\b(?:whole|full|entire)(?:\s+remaining)?[- ]lot\s*(?:→|->|=|:|for|at)\s*(?:best\s+)?offer",
)
BULK_PRICE_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(line: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(line) if s]


def find_bulk_pricing(text: str | None) -> list[str]:
    """Every sentence in ``text`` that carries bulk/tier/volume pricing."""
    if not text:
        return []
    hits: list[str] = []
    for line in str(text).splitlines():
        for sent in _sentences(line):
            if BULK_PRICE_RE.search(sent):
                hits.append(sent.strip())
    return hits


def has_bulk_pricing(text: str | None) -> bool:
    return bool(text) and BULK_PRICE_RE.search(str(text)) is not None


def strip_bulk_pricing(text: str | None) -> str:
    """``text`` minus every sentence that carries bulk/tier pricing.

    Line breaks survive (FB/Craigslist bodies are line-shaped); a line that
    held only an offending sentence is dropped, and runs of blank lines left
    behind collapse to one. Clean text comes back unchanged.
    """
    if not text:
        return text or ""
    text = str(text)
    if not has_bulk_pricing(text):
        return text
    out: list[str] = []
    for line in text.splitlines():
        if not BULK_PRICE_RE.search(line):
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        kept = [s for s in _sentences(line.strip()) if not BULK_PRICE_RE.search(s)]
        if kept:
            out.append(indent + " ".join(kept))
    # Collapse blank runs and trim the ends.
    collapsed: list[str] = []
    for line in out:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip("\n")

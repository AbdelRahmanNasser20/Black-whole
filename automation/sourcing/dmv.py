"""DMV (DC / MD / VA) sourcing-region definition — BLACKWHOLE-19.

There is currently **zero** DC inventory; the closest stock is Atlanta (~640 mi
south). This module is the single source of truth for *where* and *what* the DC
sourcing alerts watch, plus the follow-through listing plan once a lot is won.

Nothing here touches the DB or the network. It is plain data + a couple of pure
predicates so the matcher (`automation.sourcing.alerts`) and the CLI stay
trivially unit-testable.

The three ticket requirements map to the three sections below:
  1. Saved GovDeals + PublicSurplus searches scoped to DC/MD/VA  → ``SAVED_SEARCHES``
  2. Alert on any chair lot within 100 mi of DC                  → ``DC_ANCHOR`` + ``RADIUS_MILES``
  3. Follow-through once sourced (FB/eBay/Craigslist per metro)  → ``FOLLOW_THROUGH``
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── 1. Geo anchor: "within 100 mi of DC" ─────────────────────────────────────
# Downtown Washington, DC (National Mall). A 100-mi radius reaches Baltimore MD,
# Richmond VA, and the whole NoVA corridor — the DMV government-surplus belt.
DC_ANCHOR: tuple[float, float] = (38.8951, -77.0364)
RADIUS_MILES: int = 100

# States that make up the DMV. Used for the honest state-precision degrade when a
# lot only resolves to a state centroid (centroids carry ±200 mi of noise, so a
# same-region state match is the truthful call rather than a hard distance test).
DMV_STATES: frozenset[str] = frozenset({"DC", "MD", "VA"})

# ── What counts as a chair lot ───────────────────────────────────────────────
# Discovery already tags a canonical category, but sourcing must not miss lots
# the classifier hasn't scored yet, so the title is also scanned. Kept broad on
# purpose — a false positive is a glance; a false negative is a missed lot.
CHAIR_TERMS: tuple[str, ...] = (
    "chair", "chairs", "seating", "banquet", "folding chair", "stack chair",
    "stacking chair", "chiavari", "ballroom chair", "conference chair",
    "auditorium", "task chair", "office chair",
)
# Category strings emitted by deals.categories that mean "chairs".
CHAIR_CATEGORIES: frozenset[str] = frozenset({"chairs", "seating"})


# ── 2. Saved searches scoped to DC / MD / VA ─────────────────────────────────
@dataclass(frozen=True)
class SavedSearchDef:
    """A declarative saved-search definition.

    ``params`` uses only keys the deals saved-search runner already whitelists
    (see ``deals.saved_search_alerts._ALLOWED``) so a GovDeals row can be
    registered verbatim into the ``saved_searches`` table. ``source`` records
    intent (GovDeals rows flow through ``deal_lots``; PublicSurplus is a separate
    scraper) and is metadata only — the DMV radius filter is applied on top by
    ``automation.sourcing.alerts``, which the single-``state`` saved-search
    runner cannot express on its own.
    """
    name: str
    source: str  # "govdeals" | "publicsurplus"
    params: dict
    alert: bool = True


# One saved search per DMV state per source. ``q="chairs"`` narrows to seating;
# the per-state split matches the saved-search runner's single-``state`` filter.
SAVED_SEARCHES: tuple[SavedSearchDef, ...] = tuple(
    SavedSearchDef(
        name=f"DMV sourcing — {source_label} {state} chairs",
        source=source_key,
        params={"q": "chairs", "state": state, "category": "chairs"},
    )
    for source_key, source_label in (("govdeals", "GovDeals"),
                                     ("publicsurplus", "PublicSurplus"))
    for state in ("DC", "MD", "VA")
)


# ── 3. Follow-through: list once sourced ─────────────────────────────────────
@dataclass(frozen=True)
class MetroPlan:
    """Where a won DMV lot gets cross-posted, per the epic's DC channel plan."""
    metro: str
    platforms: tuple[str, ...]
    craigslist_cities: tuple[str, ...]


FOLLOW_THROUGH: tuple[MetroPlan, ...] = (
    MetroPlan("Washington DC", ("fb", "ebay", "craigslist"),
              ("washingtondc",)),
    MetroPlan("Baltimore MD", ("fb", "ebay", "craigslist"),
              ("baltimore",)),
    MetroPlan("Northern Virginia", ("fb", "ebay", "craigslist"),
              ("nova",)),
    MetroPlan("Richmond VA", ("fb", "ebay", "craigslist"),
              ("richmond",)),
)


def is_chair_text(title: str | None, category: str | None) -> bool:
    """True if a lot's title or canonical category reads as a chair lot."""
    cat = (category or "").strip().lower()
    if cat in CHAIR_CATEGORIES:
        return True
    t = (title or "").lower()
    return any(term in t for term in CHAIR_TERMS)

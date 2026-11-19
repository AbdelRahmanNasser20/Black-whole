# deals/auction_comps.py
"""Flip Score from our own closed auctions. Pure: no LLM, no network, no DB.

The question the score answers: "is this lot's bid low against what lots like
it closed for?" Same idea as GovAuctions' public method (score = 100 /
(1 + (bid / comp median)^2), up to 25 closest completed sales, per-unit for
bulk lots, weak evidence clamped, damage capped). Ours differs in three ways:

- cost = bid + the site's buyer premium, not the bare bid;
- comps are weighted by title match (rare words count more) and by age;
- a vehicle comp must be within YEAR_SPAN model years.

`deals/flip.py` is the other half (resale margin from a verdict). This module
needs no verdict, so it can score every live lot.

Use: `Matcher(lots)` -> `add_sale(...)` for each closed sale -> `results()`.
A sale is held as a small tuple, never as a row, so 100k+ sales stay light.
"""
from __future__ import annotations

import heapq
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from deals.quantity import lot_quantity

WINDOW_DAYS = 180        # how far back a comp may reach
HALF_LIFE_DAYS = 60.0    # a comp this old counts half
MAX_COMPS = 25
MIN_COMPS = 5            # under this: no score at all
STRONG_COMPS = 8         # under this: score clamped to WEAK_BAND
MIN_SIM = 0.45           # weighted title match a comp needs
SCATTER_MAX = 8.0        # p75 / p25 above this = comps disagree, discard
WEAK_BAND = (25, 75)
DAMAGE_CAP = 50
YEAR_SPAN = 4
BADGE_SCORE = 80
_PREFILTER = 80          # comps kept per lot before the final weighting

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and the of for with in on at to by from or lot lots qty quantity "
    "approx approximately each ea misc miscellaneous assorted various item "
    "items unit units pc pcs piece pieces one two three four five six set "
    "sets used new see description details photos pictures total box boxes "
    "pallet pallets surplus county city state school district department "
    "no number model serial vin id tag asset equipment supplies supply parts "
    "stuff other mixed".split())
_DAMAGE = re.compile(
    r"\b(?:salvage|non[\s-]?running|not running|does not run|doesn'?t run|"
    r"inoperable|inop|for parts|parts only|parts unit|scrap|as[\s-]?is parts|"
    r"missing parts|wrecked|totaled|totalled|burned|burnt|flood(?:ed)?|"
    r"broken|damaged|not working|non[\s-]?working|untested|unknown condition|"
    r"needs repair|blown (?:engine|motor|transmission)|bad (?:engine|motor|"
    r"transmission))\b", re.I)


def is_damaged(title: str | None) -> bool:
    return bool(_DAMAGE.search(title or ""))


def _is_year(tok: str) -> bool:
    return len(tok) == 4 and tok.isdigit() and 1950 <= int(tok) <= 2035


def tokens(title: str | None) -> tuple[frozenset[str], int | None]:
    """(words that identify the item, model year or None). Drops filler words,
    damage words (`is_damaged` reads those), one-letter tokens and short bare
    numbers (counts, not model names)."""
    words, year = set(), None
    for tok in _WORD.findall(_DAMAGE.sub(" ", (title or "").lower())):
        if _is_year(tok):
            year = year or int(tok)
            continue
        if len(tok) < 2 or tok in _STOP:
            continue
        if tok.isdigit() and len(tok) < 3:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        words.add(tok)
    return frozenset(words), year


def weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Median of (value, weight) pairs: the value where half the weight sits."""
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2.0
    run = 0.0
    for value, weight in pairs:
        run += weight
        if run >= half:
            return value
    return pairs[-1][0]


def _quantile(sorted_vals: list[float], q: float) -> float:
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def flip_score(cost: float, comp_value: float, comp_count: int,
               damaged: bool = False) -> int:
    """0-100. Cost equal to the comp value gives 50. Half the comp value gives
    80. Weak evidence is clamped; a damaged lot never scores above 50."""
    if comp_value <= 0:
        return 0
    score = 100.0 / (1.0 + (max(cost, 0.0) / comp_value) ** 2)
    if comp_count < STRONG_COMPS:
        score = min(max(score, WEAK_BAND[0]), WEAK_BAND[1])
    if damaged:
        score = min(score, DAMAGE_CAP)
    return int(round(score))


def deal_rank(cost: float, comp_value: float, hours_left: float | None,
              bid_count: int) -> float:
    """Order for a "best deals" list: dollar saving and percent discount, both
    square-root compressed, with more weight on a lot that closes soon and has
    real bids (an early $1 bid is not a deal yet)."""
    saving = comp_value - cost
    if saving <= 0 or comp_value <= 0:
        return 0.0
    rank = math.sqrt(saving) * math.sqrt(saving / comp_value)
    if hours_left is not None:
        rank *= 1.0 if hours_left <= 6 else 0.85 if hours_left <= 24 else 0.65
    rank *= 1.0 if bid_count >= 5 else 0.85 if bid_count >= 2 else 0.6
    return round(rank, 3)


@dataclass
class CompResult:
    comp_count: int
    comp_value: float        # whole-lot value (unit median x lot quantity)
    unit_value: float
    p25: float               # whole-lot spread
    p75: float
    quantity: int
    damaged: bool
    score: int
    cost: float

    def as_dict(self) -> dict:
        return {"comp_count": self.comp_count, "comp_value": self.comp_value,
                "p25": self.p25, "p75": self.p75, "quantity": self.quantity,
                "damaged": self.damaged, "score": self.score, "cost": self.cost}


@dataclass
class _Lot:
    key: object
    category: str
    words: frozenset
    year: int | None
    quantity: int
    cost: float
    damaged: bool
    heap: list = field(default_factory=list)


class Matcher:
    """Scores many live lots against a stream of closed sales in one pass."""

    def __init__(self) -> None:
        self._lots: list[_Lot] = []
        self._index: dict[tuple[str, str], list[int]] = defaultdict(list)
        self._df: Counter = Counter()
        self._docs: Counter = Counter()
        self._seq = 0

    def add_lot(self, key, *, title: str | None, category: str | None,
                bid: float, premium_pct: float = 0.0) -> None:
        words, year = tokens(title)
        if not words:
            return
        cat = category or ""
        i = len(self._lots)
        self._lots.append(_Lot(key, cat, words, year, lot_quantity(title)[0],
                               float(bid) * (1.0 + premium_pct), is_damaged(title)))
        for w in words:
            self._index[(cat, w)].append(i)

    def add_sale(self, *, title: str | None, category: str | None,
                 price: float, age_days: float, key=None) -> None:
        """One closed sale. `key` (optional) stops a lot matching itself."""
        if not price or price <= 0 or age_days < 0 or age_days > WINDOW_DAYS:
            return
        words, year = tokens(title)
        if not words:
            return
        cat = category or ""
        self._docs[cat] += 1
        for w in words:
            self._df[(cat, w)] += 1
        if is_damaged(title):
            return                      # a damaged sale is not a price for a working unit
        hits: Counter = Counter()
        for w in words:
            for i in self._index.get((cat, w), ()):
                hits[i] += 1
        if not hits:
            return
        unit = float(price) / lot_quantity(title)[0]
        for i, shared in hits.items():
            lot = self._lots[i]
            if lot.key == key and key is not None:
                continue
            if lot.year and year and abs(lot.year - year) > YEAR_SPAN:
                continue
            if bool(lot.year) != bool(year) and (lot.year or year):
                pre_penalty = 0.9       # one side names a year, the other does not
            else:
                pre_penalty = 1.0
            pre = pre_penalty * shared / (len(lot.words) + len(words) - shared)
            self._seq += 1
            item = (pre, self._seq, words, unit, age_days, pre_penalty)
            if len(lot.heap) < _PREFILTER:
                heapq.heappush(lot.heap, item)
            elif item > lot.heap[0]:
                heapq.heapreplace(lot.heap, item)

    def _idf(self, cat: str, word: str) -> float:
        return math.log(1.0 + (self._docs[cat] + 1.0) / (self._df[(cat, word)] + 1.0))

    def results(self) -> dict[object, CompResult]:
        out: dict[object, CompResult] = {}
        for lot in self._lots:
            scored = []
            lot_w = sum(self._idf(lot.category, w) for w in lot.words)
            for _pre, _seq, words, unit, age, penalty in lot.heap:
                shared = sum(self._idf(lot.category, w) for w in lot.words & words)
                union = lot_w + sum(self._idf(lot.category, w) for w in words - lot.words)
                sim = penalty * shared / union if union else 0.0
                if sim >= MIN_SIM:
                    scored.append((sim, unit, age))
            if len(scored) < MIN_COMPS:
                continue
            scored.sort(reverse=True)
            scored = scored[:MAX_COMPS]
            units = sorted(u for _, u, _ in scored)
            p25, p75 = _quantile(units, 0.25), _quantile(units, 0.75)
            if p25 <= 0 or p75 / p25 > SCATTER_MAX:
                continue
            unit_value = weighted_median(
                [(u, s * 0.5 ** (age / HALF_LIFE_DAYS)) for s, u, age in scored])
            q = lot.quantity
            value = unit_value * q
            out[lot.key] = CompResult(
                comp_count=len(scored), comp_value=round(value, 2),
                unit_value=round(unit_value, 2), p25=round(p25 * q, 2),
                p75=round(p75 * q, 2), quantity=q, damaged=lot.damaged,
                score=flip_score(lot.cost, value, len(scored), lot.damaged),
                cost=round(lot.cost, 2))
        return out

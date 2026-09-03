# deals/profiles.py
"""Research profiles: what the operator is hunting for on the auction sites.

One `Profile` = one item family (chairs, desks, dental chairs, …). Every
surface that used to hardcode "chair" asks this module instead:
  * Auctions tab   -> auction_listings_where()   (Supabase scrape mirror)
  * Deals tab/CLI  -> deal_lots_where()           (deal tracker)
  * discover gate  -> matches()                   (pure, in-process)
`chairs` is the default profile, so an un-parameterised call is today's
behaviour. Rows live in Supabase `research_profiles` (migration 006).

Until migration 006 is applied the table is absent; every reader here
degrades to `SEED_PROFILES` (same values as the migration) instead of
raising, so the default chairs path never breaks. Writers (upsert/delete)
raise `ProfilesUnavailable` in that state.
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field

from automation import db

_SLUG_RE = re.compile(r"^[a-z0-9-]{2,40}$")


class ProfilesUnavailable(RuntimeError):
    """research_profiles table unreachable (migration 006 not applied / DB down)."""


@dataclass
class Profile:
    slug: str
    name: str
    keywords: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    native_category_ids: list[str] = field(default_factory=list)
    canonical_categories: list[str] = field(default_factory=list)
    min_quantity: int = 1
    item_noun: str = "units"
    states: list[str] = field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    enabled: bool = True
    is_default: bool = False

    def to_row(self) -> dict:
        d = asdict(self)
        d["min_price"] = None if self.min_price is None else float(self.min_price)
        d["max_price"] = None if self.max_price is None else float(self.max_price)
        return d


_NON_CHAIR = ["scale", "stool", "ottoman", "pouf", "footrest", "lumbar support", "recliner",
              "filing cabinet", "file cabinet", "pillow", "drafting chair",
              "chair cover", "seat cover", "chair cushion", "seat cushion", "chair mat"]
_MEDICAL_KW = ["dental", "dentist", "exam chair", "examination chair", "treatment chair",
               "procedure chair", "phlebotomy", "dialysis", "geriatric", "optometry",
               "ophthalmic", "podiatry", "tattoo", "salon chair", "barber chair",
               "exam table", "examination table", "treatment couch", "stretcher", "gurney",
               "dental cabinet", "dental cart", "midmark", "ritter", "pelton & crane",
               "pelton and crane", "takara belmont", "umf medical", "clinton industries",
               "dexta", "smr apex", "lumex", "dntlworks"]

# Same values as scripts/sql/006_research_profiles.sql. Used by tests and as
# the fallback when the table is unreachable (resolve() never returns None).
SEED_PROFILES: dict[str, Profile] = {
    "chairs": Profile(
        slug="chairs", name="Banquet chairs",
        keywords=["chair", "banquet", "stackable", "seating"],
        exclude_terms=_NON_CHAIR + ["dental", "exam chair", "treatment chair",
                                    "procedure chair", "phlebotomy", "wheelchair", "wheel chair"],
        search_terms=["chairs", "banquet chairs", "stackable chairs", "church chairs",
                      "event chairs", "conference chairs", "folding chairs"],
        native_category_ids=["372", "47B", "47C", "47A", "46", "47D", "28E", "266"],
        min_quantity=50, item_noun="chairs", is_default=True),
    "medical": Profile(
        slug="medical", name="Medical chairs & tables",
        keywords=list(_MEDICAL_KW),
        search_terms=["dental chair", "exam chair", "treatment chair", "phlebotomy chair",
                      "procedure chair", "exam table"],
        native_category_ids=["67", "301"], min_quantity=1, item_noun="chairs"),
}


def validate_slug(s: str) -> str:
    slug = (s or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("slug must be 2-40 chars of a-z, 0-9, '-'")
    return slug


def _clean_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip() for x in v if str(x).strip()]


def _to_bool(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def from_row(row: dict) -> Profile:
    return Profile(
        slug=row["slug"], name=row.get("name") or row["slug"],
        keywords=_clean_list(row.get("keywords")),
        exclude_terms=_clean_list(row.get("exclude_terms")),
        search_terms=_clean_list(row.get("search_terms")),
        native_category_ids=_clean_list(row.get("native_category_ids")),
        canonical_categories=_clean_list(row.get("canonical_categories")),
        min_quantity=int(row.get("min_quantity") or 1),
        item_noun=(row.get("item_noun") or "units").strip(),
        states=[s.upper() for s in _clean_list(row.get("states"))],
        min_price=None if row.get("min_price") in (None, "") else float(row["min_price"]),
        max_price=None if row.get("max_price") in (None, "") else float(row["max_price"]),
        enabled=_to_bool(row.get("enabled"), True),
        is_default=_to_bool(row.get("is_default"), False),
    )


# ── pure matching ────────────────────────────────────────────────────────────

def matched_keyword(p: Profile, title: str | None, description: str | None) -> str:
    t = (title or "").lower()
    if any(x.lower() in t for x in p.exclude_terms):
        return ""
    blob = f"{t} {(description or '').lower()}"
    for kw in p.keywords:
        if kw.lower() in blob:
            return kw
    return ""


def matches(p: Profile, title: str | None, description: str | None) -> bool:
    t = (title or "").lower()
    if any(x.lower() in t for x in p.exclude_terms):
        return False
    if not p.keywords:
        return True
    return matched_keyword(p, title, description) != ""


# ── SQL fragments ────────────────────────────────────────────────────────────

def _likes(terms: list[str]) -> list[str]:
    return [f"%{t}%" for t in terms]


def deal_lots_where(p: Profile) -> tuple[str, list]:
    """Fragment over unqualified deal_lots columns (title, description,
    canonical_category, state, current_bid). Joins must not alias a table
    that shares those names."""
    where: list[str] = []
    args: list = []
    if p.keywords:
        where.append("(title ILIKE ANY(%s) OR description ILIKE ANY(%s))")
        args += [_likes(p.keywords), _likes(p.keywords)]
    if p.exclude_terms:
        where.append("NOT (title ILIKE ANY(%s))")
        args.append(_likes(p.exclude_terms))
    if p.canonical_categories:
        where.append("canonical_category = ANY(%s)")
        args.append(list(p.canonical_categories))
    if p.states:
        where.append("state = ANY(%s)")
        args.append([s.upper() for s in p.states])
    if p.min_price is not None:
        where.append("current_bid >= %s")
        args.append(float(p.min_price))
    if p.max_price is not None:
        where.append("current_bid <= %s")
        args.append(float(p.max_price))
    return (" AND ".join(where) or "TRUE", args)


def auction_listings_where(p: Profile, min_quantity: int | None = None) -> tuple[str, list]:
    """Fragment over auction_listings (title, description, quantity)."""
    where: list[str] = []
    args: list = []
    if p.keywords:
        where.append("(title ILIKE ANY(%s) OR description ILIKE ANY(%s))")
        args += [_likes(p.keywords), _likes(p.keywords)]
    if p.exclude_terms:
        where.append("NOT (title ILIKE ANY(%s))")
        args.append(_likes(p.exclude_terms))
    q = p.min_quantity if min_quantity is None else int(min_quantity)
    where.append("quantity >= %s")
    args.append(max(1, q))
    return (" AND ".join(where), args)


# ── storage ──────────────────────────────────────────────────────────────────

_COLS = ("slug, name, keywords, exclude_terms, search_terms, native_category_ids, "
         "canonical_categories, min_quantity, item_noun, states, min_price, max_price, "
         "enabled, is_default")

_warned = False


def _unavailable(e: Exception) -> None:
    """Log once, then stay quiet: the seeds cover the default path."""
    global _warned
    if not _warned:
        _warned = True
        print(f"[profiles] research_profiles unavailable ({e!r}); using built-in seeds",
              file=sys.stderr)


def _seed_list(include_disabled: bool) -> list[Profile]:
    rows = [p for p in SEED_PROFILES.values() if include_disabled or p.enabled]
    return sorted(rows, key=lambda p: (not p.is_default, p.name))


def list_all(include_disabled: bool = False) -> list[Profile]:
    sql = f"SELECT {_COLS} FROM research_profiles"
    if not include_disabled:
        sql += " WHERE enabled"
    sql += " ORDER BY is_default DESC, name"
    try:
        rows = db.fetch_all(sql)
    except Exception as e:  # table absent (migration 006 not applied) / DB down
        _unavailable(e)
        return _seed_list(include_disabled)
    if not rows:
        return _seed_list(include_disabled)
    return [from_row(r) for r in rows]


def load(slug: str) -> Profile | None:
    try:
        r = db.fetch_one(f"SELECT {_COLS} FROM research_profiles WHERE slug=%s", (slug,))
    except Exception as e:
        _unavailable(e)
        return SEED_PROFILES.get(slug)
    return from_row(r) if r else None


def default_slug() -> str:
    for p in list_all():
        if p.is_default:
            return p.slug
    return "chairs"


def resolve(slug: str | None) -> Profile:
    """None -> the default profile (falls back to the chairs seed if the table
    is empty); unknown slug -> KeyError."""
    if slug in (None, "", "default"):
        for p in list_all():
            if p.is_default:
                return p
        return SEED_PROFILES["chairs"]
    p = load(slug)
    if p is None:
        raise KeyError(slug)
    return p


def upsert(p: Profile) -> Profile:
    p.slug = validate_slug(p.slug)
    if not p.name.strip():
        raise ValueError("name required")
    try:
        if p.is_default:
            db.execute("UPDATE research_profiles SET is_default=false WHERE is_default AND slug<>%s",
                       (p.slug,))
        db.execute(
            f"""INSERT INTO research_profiles ({_COLS})
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slug) DO UPDATE SET
                  name=EXCLUDED.name, keywords=EXCLUDED.keywords, exclude_terms=EXCLUDED.exclude_terms,
                  search_terms=EXCLUDED.search_terms, native_category_ids=EXCLUDED.native_category_ids,
                  canonical_categories=EXCLUDED.canonical_categories, min_quantity=EXCLUDED.min_quantity,
                  item_noun=EXCLUDED.item_noun, states=EXCLUDED.states, min_price=EXCLUDED.min_price,
                  max_price=EXCLUDED.max_price, enabled=EXCLUDED.enabled, is_default=EXCLUDED.is_default,
                  updated_at=now()""",
            (p.slug, p.name.strip(), p.keywords, p.exclude_terms, p.search_terms,
             p.native_category_ids, p.canonical_categories, int(p.min_quantity), p.item_noun,
             [s.upper() for s in p.states], p.min_price, p.max_price, bool(p.enabled),
             bool(p.is_default)))
    except Exception as e:
        raise ProfilesUnavailable(
            f"cannot save profile — apply scripts/sql/006_research_profiles.sql first ({e!r})")
    return load(p.slug) or p


def delete(slug: str) -> bool:
    p = load(slug)
    if p is None:
        return False
    if p.is_default:
        raise ValueError("cannot delete the default profile; make another one default first")
    try:
        return db.execute("DELETE FROM research_profiles WHERE slug=%s", (slug,)) > 0
    except Exception as e:
        raise ProfilesUnavailable(
            f"cannot delete profile — apply scripts/sql/006_research_profiles.sql first ({e!r})")

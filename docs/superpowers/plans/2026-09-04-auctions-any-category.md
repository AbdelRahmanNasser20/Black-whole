# Auctions For Any Category — Research Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the chairs-only Auctions/Deals research flow into a "research profile" the operator can create for any item (or set of items), and have every surface — Auctions tab, Deals tab, deals CLI, scraper launch — read the profile instead of hardcoded chair values, for live lots *and* past outcomes.

**Architecture:** One new Supabase table `research_profiles` (keywords, exclude terms, search terms, sweep categories, min quantity, states, price band). One new module `deals/profiles.py` owns the dataclass, the seed rows (today's chairs + medical values, migrated verbatim), the pure matcher, and the two SQL-fragment builders (`deal_lots`, `auction_listings`). Every consumer takes a `Profile` (or a `--profile` slug) and asks the module for its filter; nothing else learns what a chair is. `chairs` is the default profile, so with no arguments every path behaves exactly as today.

**Tech Stack:** Python 3.11, FastAPI + vanilla JS admin, Supabase Postgres via `automation/db.py`, pytest.

**Spec:** Operator ask (verbatim): *"pretty much what I have done with auctions 04 for chairs I should do it for any other category or even multiple categories. set up the auctions tabs and abstract it for any other item I would like to search for including past and present so I can continue to do my own research."* Design notes are in this plan's header + the "Decisions" block below.

## Global Constraints

- Repo root for every path below: `listing_automation/`. Tests: `.venv/bin/python -m pytest tests/deals/ -q` (no `pytest` console script). Run it after every task; also run `tests/web/` when a task touches `automation/web/app.py`.
- All DB code through `automation/db.py` (`fetch_one` / `fetch_all` / `execute`); `%s` placeholders only; never f-string user input into SQL.
- Schema changes ship as a migration file under `scripts/sql/` **and** the DDL is added to `../docs/claude-reference/data-model.md` in the same commit. Apply to Supabase (project `nihgzltpjriekyqqucbd`) via the Supabase MCP `apply_migration` or the SQL Editor — never at runtime, never psql one-liners.
- `auction_extractors/state/listings.db` is read-only to this repo. This plan never adds a column there. The Auctions tab reads Supabase `auction_listings` (mirrored by `scripts/transfer_listings_to_supabase.py`), which is where the profile filter is applied.
- `OLLAMA_MODEL=gpt-oss:120b-cloud` stays. Condition-scoring / quantity LLMs are not re-benchmarked here.
- Supabase free tier goes read-only at 500 MB: `research_profiles` holds a handful of short rows; no blobs.
- Outcome columns are never overwritten. No-bid = `bid_count==0` only. Nothing here writes to `deal_lots`.
- Changes to `automation/web/app.py`, `templates/`, `static/` need `python -m automation.web` restarted to see them.
- Default behavior with no `--profile` / no `profile=` must be byte-for-byte today's chairs behavior.

## Decisions (read before Task 1)

| Question | Decision |
|---|---|
| What is a profile? | `slug`, `name`, `keywords[]` (must match title OR description, case-insensitive substring), `exclude_terms[]` (title match → drop), `search_terms[]` (what the scrapers type into the site search box), `native_category_ids[]` (GovDeals codes for `deals discover`), `canonical_categories[]` (optional narrowing on `deal_lots`), `min_quantity`, `item_noun` (word the quantity LLM counts), `states[]`, `min_price`/`max_price`, `enabled`, `is_default`. |
| Multiple categories? | One profile can hold many keywords and many native ids. "Multiple categories" = one profile with a wide keyword set, or several profiles switched from the tab. No profile groups in v1. |
| Where does the filter run? | In SQL on `deal_lots` (`deals/profiles.deal_lots_where`) and on `auction_listings` (`deals/profiles.auction_listings_where`). Pure `matches()` exists for in-process checks (discover archive gate, tests). |
| Past + present? | Present = Auctions tab (scrape cache) + Deals tab `status=active`. Past = Deals tab `status=closed` with the same profile filter + a new `/api/profiles/{slug}/outcomes` roll-up (closed count, no-bid %, median final bid, latest sold comps from `deal_verdicts`). |
| Seeds | `chairs` (default) and `medical` — the two sub-tabs that exist today, values lifted verbatim from `auction_extractors/top_chairs.py`, `govdeals_chairs_extraction.py`, `deals/cli.py`. |
| Scraper | `auction_extractors/*` keep working unchanged when the env is empty. `SCRAPE_SEARCH_TERMS` / `SCRAPE_ITEM_NOUN` env override them when the dashboard launches a scrape with a profile. The scraper's own Telegram alert stays chairs-shaped (it is not the Auctions tab's data path — the cache keeps every row). |

## Cut from this session (say so in the handoff)

- Profile-aware **condition-scoring prompt** (`top_chairs._enrich_via_llm` still says "chair") — optional checkbox, unchanged.
- **States / price band on the Auctions tab** (`auction_listings.price` is free text, `location` is a string). Both are honored on the Deals path only.
- **Quantity on `deal_lots`** — there is no quantity column; `min_quantity` applies to `auction_listings` only.
- **Render cron edits** — `DEALS_PROFILE` env is read by the CLI so crons can be switched from the Render dashboard without a `render.yaml` change; no blueprint re-apply here.
- **Relist / analyze gating per profile** (`DEALS_RELIST_CATEGORIES`, `lots_for_analysis`) — unchanged.
- **Profile groups, per-profile Telegram topics, Public Surplus deals adapter, CRM/bot awareness** — out of scope.

## File structure

| File | Responsibility |
|---|---|
| `scripts/sql/006_research_profiles.sql` (create) | DDL + the two seed rows. |
| `../docs/claude-reference/data-model.md` (modify) | Mirror the DDL. |
| `deals/profiles.py` (create) | `Profile` dataclass, `SEED_PROFILES`, `matches`, `matched_keyword`, `deal_lots_where`, `auction_listings_where`, DB CRUD (`load`, `resolve`, `list_all`, `upsert`, `delete`). |
| `automation/web/deals_query.py` (modify) | `build_where(..., profile_where=None)`. |
| `deals/saved_search_alerts.py` (modify) | Accept `profile` in saved params. |
| `deals/store.py` (modify) | `bidder_targets(extra_where=)`, `due_for_poll(now, extra_where=)`. |
| `deals/discover.py` (modify) | `run_discovery(..., archive_predicate=)`. |
| `deals/digest.py` (modify) | `candidate_rows(profile)`, `send_daily_digest(fees, profile=None)`. |
| `deals/cli.py` (modify) | `--profile` on discover / watch-once / digest / track-bidders; `DEALS_PROFILE` env default. |
| `automation/auctions_supabase.py` (modify) | `get_top_lots(profile, ...)`; `get_top_chairs` becomes a wrapper. |
| `automation/web/app.py` (modify) | `/api/profiles` CRUD, `/api/profiles/{slug}/outcomes`, `profile=` on `/api/auctions`, `/api/deals`, `/api/deals/geo`, `/api/deals/tree`, `/api/scrape/start`. |
| `automation/web/templates/index.html` + `static/app.js` (modify) | Auctions tab profile switcher + inline "new profile" form; Deals tab profile select + outcomes strip; de-chair labels. |
| `auction_extractors/govdeals_chairs_extraction.py`, `public_surplus_automation.py`, `quantity_llm.py` (modify) | Env overrides `SCRAPE_SEARCH_TERMS`, `SCRAPE_ITEM_NOUN`. |
| `tests/deals/test_profiles.py`, `tests/deals/test_profiles_api.py`, `tests/deals/test_profiles_cli.py` (create) | Unit + TestClient coverage. |
| `docs/claude-reference/deals.md`, `repo-layout.md`, `todos-and-history.md`, `CLAUDE.md` (modify) | Doc the profile concept + commands. |

---

### Task 1: Migration + seed rows + DDL mirror

**Files:**
- Create: `scripts/sql/006_research_profiles.sql`
- Modify: `../docs/claude-reference/data-model.md` (append a new block after the `llm_compare_logs` DDL, before `### 8a. Setup`)

**Interfaces:**
- Produces: table `research_profiles` with the columns below; rows `chairs` (default) and `medical`. Tasks 2+ read it.

- [ ] **Step 1: Write the migration file**

```sql
-- scripts/sql/006_research_profiles.sql
-- Research profiles: what the operator is hunting for on the auction sites.
-- One row per item family. Replaces the chair-only constants that lived in
-- auction_extractors/top_chairs.py, govdeals_chairs_extraction.py and
-- deals/cli.py. `chairs` is the default so an un-parameterised call keeps
-- today's behaviour. Apply by hand in Supabase; never at runtime.
BEGIN;

CREATE TABLE IF NOT EXISTS research_profiles (
  slug                 TEXT PRIMARY KEY,                     -- url/cli handle: [a-z0-9-]
  name                 TEXT NOT NULL,
  keywords             TEXT[] NOT NULL DEFAULT '{}',         -- title OR description ILIKE any
  exclude_terms        TEXT[] NOT NULL DEFAULT '{}',         -- title ILIKE any -> drop
  search_terms         TEXT[] NOT NULL DEFAULT '{}',         -- typed into the site search box by the scrapers
  native_category_ids  TEXT[] NOT NULL DEFAULT '{}',         -- GovDeals category codes for `deals discover`
  canonical_categories TEXT[] NOT NULL DEFAULT '{}',         -- optional narrowing on deal_lots.canonical_category
  min_quantity         INT NOT NULL DEFAULT 1,               -- auction_listings.quantity floor
  item_noun            TEXT NOT NULL DEFAULT 'units',        -- what the quantity LLM counts
  states               TEXT[] NOT NULL DEFAULT '{}',         -- USPS codes; empty = any (deal_lots only)
  min_price            NUMERIC(12,2),                        -- deal_lots.current_bid band (deal_lots only)
  max_price            NUMERIC(12,2),
  enabled              BOOLEAN NOT NULL DEFAULT true,
  is_default           BOOLEAN NOT NULL DEFAULT false,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- exactly one default
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_profiles_default
  ON research_profiles ((is_default)) WHERE is_default;

INSERT INTO research_profiles
  (slug, name, keywords, exclude_terms, search_terms, native_category_ids,
   canonical_categories, min_quantity, item_noun, is_default)
VALUES
  ('chairs', 'Banquet chairs',
   ARRAY['chair','banquet','stackable','seating'],
   ARRAY['scale','stool','ottoman','pouf','footrest','lumbar support','recliner',
         'filing cabinet','file cabinet','pillow','drafting chair',
         'chair cover','seat cover','chair cushion','seat cushion','chair mat',
         'dental','exam chair','treatment chair','procedure chair','phlebotomy',
         'wheelchair','wheel chair'],
   ARRAY['chairs','banquet chairs','stackable chairs','church chairs',
         'event chairs','conference chairs','folding chairs'],
   ARRAY['372','47B','47C','47A','46','47D','28E','266'],
   ARRAY[]::text[], 50, 'chairs', true),
  ('medical', 'Medical chairs & tables',
   ARRAY['dental','dentist','exam chair','examination chair','treatment chair',
         'procedure chair','phlebotomy','dialysis','geriatric','optometry',
         'ophthalmic','podiatry','tattoo','salon chair','barber chair',
         'exam table','examination table','treatment couch','stretcher','gurney',
         'dental cabinet','dental cart','midmark','ritter','pelton & crane',
         'pelton and crane','takara belmont','umf medical','clinton industries',
         'dexta','smr apex','lumex','dntlworks'],
   ARRAY[]::text[],
   ARRAY['dental chair','exam chair','treatment chair','phlebotomy chair',
         'procedure chair','exam table'],
   ARRAY['67','301'],
   ARRAY[]::text[], 1, 'chairs', false)
ON CONFLICT (slug) DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Mirror the DDL into the data-model doc**

Append to `../docs/claude-reference/data-model.md` right before `### 8a. Setup`:

````markdown
#### research_profiles (2026-09-04, `listing_automation/scripts/sql/006_research_profiles.sql`)

What the operator is hunting for. One row per item family; `chairs` is the default. Read by the Auctions tab, the Deals tab, and `deals.cli --profile`. Code owner: `listing_automation/deals/profiles.py`.

```sql
CREATE TABLE research_profiles (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL,
  keywords TEXT[] NOT NULL DEFAULT '{}', exclude_terms TEXT[] NOT NULL DEFAULT '{}',
  search_terms TEXT[] NOT NULL DEFAULT '{}', native_category_ids TEXT[] NOT NULL DEFAULT '{}',
  canonical_categories TEXT[] NOT NULL DEFAULT '{}',
  min_quantity INT NOT NULL DEFAULT 1, item_noun TEXT NOT NULL DEFAULT 'units',
  states TEXT[] NOT NULL DEFAULT '{}', min_price NUMERIC(12,2), max_price NUMERIC(12,2),
  enabled BOOLEAN NOT NULL DEFAULT true, is_default BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_research_profiles_default ON research_profiles ((is_default)) WHERE is_default;
```
````

- [ ] **Step 3: Apply to Supabase and verify**

Apply `scripts/sql/006_research_profiles.sql` with the Supabase MCP `apply_migration` (name `research_profiles`, project `nihgzltpjriekyqqucbd`) or paste into the SQL Editor. Then:

Run: `.venv/bin/python -c "from automation import db; print(db.fetch_all('SELECT slug, is_default, min_quantity FROM research_profiles ORDER BY slug'))"`
Expected: `[{'slug': 'chairs', 'is_default': True, 'min_quantity': 50}, {'slug': 'medical', 'is_default': False, 'min_quantity': 1}]`

- [ ] **Step 4: Run the gate, commit**

Run: `.venv/bin/python -m pytest tests/deals/ -q` — Expected: all pass (nothing changed in code yet).

```bash
git add scripts/sql/006_research_profiles.sql ../docs/claude-reference/data-model.md
git commit -m "deals: research_profiles table + chairs/medical seeds (migration 006)"
```

**Done when:** the two rows exist in Supabase and the DDL is in `data-model.md`.

---

### Task 2: `deals/profiles.py` — dataclass, seeds, matcher, SQL fragments, CRUD

**Files:**
- Create: `deals/profiles.py`
- Test: `tests/deals/test_profiles.py`

**Interfaces:**
- Produces:
  - `@dataclass Profile(slug, name, keywords: list[str], exclude_terms: list[str], search_terms: list[str], native_category_ids: list[str], canonical_categories: list[str], min_quantity: int, item_noun: str, states: list[str], min_price: float | None, max_price: float | None, enabled: bool, is_default: bool)` with `.to_row() -> dict` (JSON-safe).
  - `SEED_PROFILES: dict[str, Profile]` (`chairs`, `medical`) — same values as the migration.
  - `from_row(row: dict) -> Profile`
  - `matches(p: Profile, title: str | None, description: str | None) -> bool`
  - `matched_keyword(p, title, description) -> str` (first hit or `""`)
  - `deal_lots_where(p) -> tuple[str, list]` — SQL fragment on unqualified `deal_lots` columns.
  - `auction_listings_where(p, min_quantity: int | None = None) -> tuple[str, list]`
  - `load(slug) -> Profile | None`, `resolve(slug: str | None) -> Profile` (None → default; unknown → `KeyError`), `default_slug() -> str`, `list_all(include_disabled=False) -> list[Profile]`, `upsert(p) -> Profile`, `delete(slug) -> bool` (refuses the default → `ValueError`).
  - `validate_slug(s) -> str` (lowercase `[a-z0-9-]{2,40}` else `ValueError`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/deals/test_profiles.py
import pytest
from deals import profiles
from deals.profiles import Profile, SEED_PROFILES


def _p(**over):
    base = dict(slug="x", name="X", keywords=["chair"], exclude_terms=["stool"],
                search_terms=["chairs"], native_category_ids=["372"],
                canonical_categories=[], min_quantity=50, item_noun="chairs",
                states=[], min_price=None, max_price=None, enabled=True, is_default=False)
    base.update(over)
    return Profile(**base)


def test_seeds_match_today_defaults():
    c = SEED_PROFILES["chairs"]
    assert c.is_default and c.min_quantity == 50 and "chair" in c.keywords
    assert "372" in c.native_category_ids and "266" in c.native_category_ids
    assert "chair cover" in c.exclude_terms
    m = SEED_PROFILES["medical"]
    assert m.min_quantity == 1 and "dental" in m.keywords and m.native_category_ids == ["67", "301"]


def test_matches_keyword_in_title_or_description():
    p = _p()
    assert profiles.matches(p, "Lot of 200 CHAIRS", "")
    assert profiles.matches(p, "Furniture lot", "banquet chair x 40")
    assert not profiles.matches(p, "Desks", "tables")


def test_exclude_term_on_title_wins():
    p = _p()
    assert not profiles.matches(p, "Chair scale stool", "")
    assert profiles.matched_keyword(p, "50 chairs", "") == "chair"
    assert profiles.matched_keyword(p, "desk", "") == ""


def test_empty_keywords_match_everything():
    assert profiles.matches(_p(keywords=[], exclude_terms=[]), "anything", None)


def test_deal_lots_where_binds_arrays_and_band():
    p = _p(states=["AZ", "nv"], min_price=10, max_price=500, canonical_categories=["seating_furniture"])
    where, args = profiles.deal_lots_where(p)
    assert "title ILIKE ANY(%s) OR description ILIKE ANY(%s)" in where
    assert "NOT (title ILIKE ANY(%s))" in where
    assert "canonical_category = ANY(%s)" in where
    assert "state = ANY(%s)" in where and "current_bid >= %s" in where and "current_bid <= %s" in where
    assert ["%chair%"] in args and ["%stool%"] in args
    assert ["AZ", "NV"] in args and 10.0 in args and 500.0 in args


def test_deal_lots_where_empty_profile_is_true():
    where, args = profiles.deal_lots_where(_p(keywords=[], exclude_terms=[]))
    assert where == "TRUE" and args == []


def test_auction_listings_where_uses_min_quantity_default_and_override():
    p = _p()
    where, args = profiles.auction_listings_where(p)
    assert "quantity >= %s" in where and 50 in args
    _, args2 = profiles.auction_listings_where(p, min_quantity=5)
    assert 5 in args2 and 50 not in args2


def test_validate_slug():
    assert profiles.validate_slug(" Office-Desks ") == "office-desks"
    with pytest.raises(ValueError):
        profiles.validate_slug("bad slug!")


def test_from_row_and_to_row_roundtrip():
    row = _p().to_row()
    assert row["keywords"] == ["chair"] and row["min_price"] is None
    assert profiles.from_row(row) == _p()


def test_resolve_none_returns_default(monkeypatch):
    monkeypatch.setattr(profiles, "list_all", lambda include_disabled=False: [_p(slug="a"), _p(slug="b", is_default=True)])
    assert profiles.resolve(None).slug == "b"


def test_resolve_unknown_raises(monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda slug: None)
    with pytest.raises(KeyError):
        profiles.resolve("nope")


def test_delete_refuses_default(monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda slug: _p(slug="chairs", is_default=True))
    with pytest.raises(ValueError):
        profiles.delete("chairs")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_profiles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deals.profiles'`

- [ ] **Step 3: Implement the module**

```python
# deals/profiles.py
"""Research profiles: what the operator is hunting for on the auction sites.

One `Profile` = one item family (chairs, desks, dental chairs, …). Every
surface that used to hardcode "chair" asks this module instead:
  * Auctions tab   -> auction_listings_where()   (Supabase scrape mirror)
  * Deals tab/CLI  -> deal_lots_where()           (deal tracker)
  * discover gate  -> matches()                   (pure, in-process)
`chairs` is the default profile, so an un-parameterised call is today's
behaviour. Rows live in Supabase `research_profiles` (migration 006).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from automation import db

_SLUG_RE = re.compile(r"^[a-z0-9-]{2,40}$")


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
        enabled=bool(row.get("enabled", True)),
        is_default=bool(row.get("is_default", False)),
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


def list_all(include_disabled: bool = False) -> list[Profile]:
    sql = f"SELECT {_COLS} FROM research_profiles"
    if not include_disabled:
        sql += " WHERE enabled"
    sql += " ORDER BY is_default DESC, name"
    return [from_row(r) for r in db.fetch_all(sql)]


def load(slug: str) -> Profile | None:
    r = db.fetch_one(f"SELECT {_COLS} FROM research_profiles WHERE slug=%s", (slug,))
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
    if p.is_default:
        db.execute("UPDATE research_profiles SET is_default=false WHERE is_default AND slug<>%s", (p.slug,))
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
         [s.upper() for s in p.states], p.min_price, p.max_price, bool(p.enabled), bool(p.is_default)))
    return load(p.slug) or p


def delete(slug: str) -> bool:
    p = load(slug)
    if p is None:
        return False
    if p.is_default:
        raise ValueError("cannot delete the default profile; make another one default first")
    return db.execute("DELETE FROM research_profiles WHERE slug=%s", (slug,)) > 0
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_profiles.py -q` — Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add deals/profiles.py tests/deals/test_profiles.py
git commit -m "deals: profiles module — Profile dataclass, seeds, matcher, SQL fragments, CRUD"
```

**Done when:** `test_profiles.py` green; `.venv/bin/python -c "from deals import profiles; print(profiles.resolve(None).slug)"` prints `chairs` against the live DB.

---

### Task 3: Deals query path takes a profile (`build_where`, `/api/deals*`, saved-search alerts)

**Files:**
- Modify: `automation/web/deals_query.py:24-88` (`build_where`)
- Modify: `automation/web/app.py` — `list_deals` (~600), `deals_geo` (~693), `deals_tree` (~761)
- Modify: `deals/saved_search_alerts.py:22-45`
- Test: `tests/deals/test_deals_query_v2.py` (append), `tests/deals/test_profiles_api.py` (create)

**Interfaces:**
- Consumes: `profiles.resolve(slug)`, `profiles.deal_lots_where(p)`.
- Produces: `build_where(..., profile_where: tuple[str, list] | None = None)`; query param `profile=<slug>` on `/api/deals`, `/api/deals/geo`, `/api/deals/tree`; app helper `_profile_where(slug: str | None) -> tuple[str, list] | None` (None/"" → None; unknown → 404).

- [ ] **Step 1: Failing tests**

Append to `tests/deals/test_deals_query_v2.py`:

```python
def test_profile_where_is_spliced_with_binding():
    where, args = build_where(status="active", profile_where=("(title ILIKE ANY(%s))", [["%desk%"]]))
    assert "(title ILIKE ANY(%s))" in where and [["%desk%"]][0] in args


def test_profile_where_none_adds_nothing():
    where, _ = build_where(status="active", profile_where=None)
    assert "ILIKE ANY" not in where
```

Create `tests/deals/test_profiles_api.py`:

```python
# tests/deals/test_profiles_api.py
import importlib
from fastapi.testclient import TestClient
from deals.profiles import Profile


def _profile(slug="desks", **over):
    kw = dict(slug=slug, name="Desks", keywords=["desk"], exclude_terms=[], search_terms=["desks"],
              native_category_ids=["372"], canonical_categories=[], min_quantity=1, item_noun="desks",
              states=[], min_price=None, max_price=None, enabled=True, is_default=False)
    kw.update(over)
    return Profile(**kw)


def _client(monkeypatch, rows=()):
    webapp = importlib.import_module("automation.web.app")
    cap = {}

    def fake_fetch_all(sql, params=()):
        cap.setdefault("sqls", []).append((sql, params))
        if "row_to_json(v.*) AS verdict" in sql:
            cap["rows_sql"], cap["rows_params"] = sql, params
            return [dict(r) for r in rows]
        return []

    def fake_fetch_one(sql, params=()):
        if "count(*) AS c" in sql:
            return {"c": len(rows)}
        return {"total_lots": 1, "candidates": 0, "ending_24h": 0}

    monkeypatch.setattr(webapp.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(webapp.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webapp.profiles, "load", lambda slug: _profile() if slug == "desks" else None)
    monkeypatch.setattr(webapp.profiles, "list_all",
                        lambda include_disabled=False: [_profile("chairs", is_default=True), _profile()])
    return TestClient(webapp.app), cap


def test_deals_profile_param_filters_sql(monkeypatch):
    client, cap = _client(monkeypatch)
    r = client.get("/api/deals?profile=desks&status=closed")
    assert r.status_code == 200
    assert "title ILIKE ANY(%s)" in cap["rows_sql"]
    assert ["%desk%"] in list(cap["rows_params"])


def test_deals_unknown_profile_404(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.get("/api/deals?profile=nope").status_code == 404


def test_deals_no_profile_no_filter(monkeypatch):
    client, cap = _client(monkeypatch)
    client.get("/api/deals")
    assert "ILIKE ANY" not in cap["rows_sql"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_deals_query_v2.py tests/deals/test_profiles_api.py -q`
Expected: FAIL — `TypeError: build_where() got an unexpected keyword argument 'profile_where'`; `AttributeError: module 'automation.web.app' has no attribute 'profiles'`.

- [ ] **Step 3: Implement**

`automation/web/deals_query.py` — add the parameter and splice it last:

```python
def build_where(*, q=None, category=None, native=None, state=None, max_bids=None,
                ending_within=None, status="active", min_margin=None, min_price=None,
                max_price=None, list_id=None, tag=None, bbox=None,
                profile_where: tuple[str, list] | None = None) -> tuple[str, list]:
    ...  # existing body unchanged up to the tag block, then:
    if profile_where is not None and profile_where[0] and profile_where[0] != "TRUE":
        where.append(f"({profile_where[0]})")
        args += list(profile_where[1])
    return (" AND ".join(where) or "TRUE", args)
```

`automation/web/app.py` — near the other deals imports add `from deals import profiles` (module import, so tests can monkeypatch `webapp.profiles.load`). Add a helper next to `_parse_bbox`:

```python
def _profile_where(slug: str | None) -> tuple[str, list] | None:
    """Resolve ?profile=<slug> into a deal_lots SQL fragment. Empty → no filter."""
    if not slug:
        return None
    try:
        return profiles.deal_lots_where(profiles.resolve(slug))
    except KeyError:
        raise HTTPException(404, f"unknown profile {slug!r}")
```

Add `profile: str | None = None` to the signatures of `list_deals`, `deals_geo`, `deals_tree`, and pass `profile_where=_profile_where(profile)` into every `deals_query.build_where(...)` call in those three handlers. In `deals_tree`, the counts query builds its own WHERE from `_DEALS_ACTIVE`; append `AND ({pw[0]})` with `pw[1]` bound when a profile is given.

`deals/saved_search_alerts.py` — add `"profile"` to `_ALLOWED`; in `run_saved_search_alerts`, before `build_where(**params)`:

```python
            slug = params.pop("profile", None)
            if slug:
                from deals import profiles
                params["profile_where"] = profiles.deal_lots_where(profiles.resolve(slug))
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/ tests/web/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/web/deals_query.py automation/web/app.py deals/saved_search_alerts.py tests/deals/test_deals_query_v2.py tests/deals/test_profiles_api.py
git commit -m "deals: profile= on /api/deals, /geo, /tree; saved searches carry a profile"
```

**Done when:** `curl 'http://127.0.0.1:8765/api/deals?profile=chairs&status=closed&limit=3'` returns chair rows with `outcome` set; `?profile=nope` → 404; no `profile` → identical to before.

---

### Task 4: `/api/profiles` CRUD + `/api/profiles/{slug}/outcomes` (the "past" view)

**Files:**
- Modify: `automation/web/app.py` (new block after the saved-search handlers, ~line 930)
- Test: `tests/deals/test_profiles_api.py` (append)

**Interfaces:**
- Produces:
  - `GET /api/profiles` → `{"profiles": [Profile.to_row()...], "default": slug}`
  - `POST /api/profiles` body = Profile fields (lists may be comma strings) → row, 400 on bad slug/name
  - `DELETE /api/profiles/{slug}` → `{"ok": true}`, 404 unknown, 409 default
  - `GET /api/profiles/{slug}/outcomes?days=365` → `{"closed", "no_bid", "sold", "no_bid_pct", "median_final_bid", "last_closed_at", "comps": [...]}`

- [ ] **Step 1: Failing tests** (append to `tests/deals/test_profiles_api.py`)

```python
def test_list_profiles(monkeypatch):
    client, _ = _client(monkeypatch)
    body = client.get("/api/profiles").json()
    assert body["default"] == "chairs" and [p["slug"] for p in body["profiles"]] == ["chairs", "desks"]


def test_create_profile_coerces_comma_lists(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    saved = {}
    monkeypatch.setattr(webapp.profiles, "upsert", lambda p: saved.setdefault("p", p) or p)
    r = client.post("/api/profiles", json={"slug": "Office-Desks", "name": "Office desks",
                                           "keywords": "desk, credenza", "min_quantity": "5"})
    assert r.status_code == 200
    assert saved["p"].slug == "office-desks" and saved["p"].keywords == ["desk", "credenza"]
    assert saved["p"].min_quantity == 5


def test_create_profile_bad_slug_400(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.post("/api/profiles", json={"slug": "no way", "name": "x"}).status_code == 400


def test_delete_default_409(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    def boom(slug):
        raise ValueError("default")
    monkeypatch.setattr(webapp.profiles, "delete", boom)
    assert client.delete("/api/profiles/chairs").status_code == 409


def test_outcomes_rollup_uses_profile_filter(monkeypatch):
    client, cap = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    monkeypatch.setattr(webapp.db, "fetch_one", lambda sql, params=(): {
        "closed": 10, "no_bid": 4, "sold": 6, "median_final_bid": 120.0, "last_closed_at": None})
    r = client.get("/api/profiles/desks/outcomes")
    assert r.status_code == 200
    body = r.json()
    assert body["closed"] == 10 and body["no_bid_pct"] == 40.0 and body["comps"] == []
    comps_sql = [s for s, _ in cap["sqls"] if "deal_verdicts" in s]
    assert comps_sql and "title ILIKE ANY(%s)" in comps_sql[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_profiles_api.py -q` — Expected: 404s / assertion failures on the five new tests.

- [ ] **Step 3: Implement** (add to `automation/web/app.py` after `deals_search_delete`)

```python
# ── Research profiles: what we're hunting for (deals/profiles.py) ────────────

@app.get("/api/profiles")
async def profiles_list():
    rows = await asyncio.to_thread(profiles.list_all, True)
    default = next((p.slug for p in rows if p.is_default), "chairs")
    return {"profiles": [p.to_row() for p in rows], "default": default}


@app.post("/api/profiles")
async def profiles_create(payload: dict):
    payload = payload or {}
    try:
        p = profiles.from_row({**payload, "slug": profiles.validate_slug(payload.get("slug", ""))})
        if not p.name.strip():
            raise ValueError("name required")
        saved = await asyncio.to_thread(profiles.upsert, p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _AUCTIONS_CACHE.clear()
    return saved.to_row()


@app.delete("/api/profiles/{slug}")
async def profiles_delete(slug: str):
    try:
        ok = await asyncio.to_thread(profiles.delete, slug)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, "profile not found")
    _AUCTIONS_CACHE.clear()
    return {"ok": True}


@app.get("/api/profiles/{slug}/outcomes")
async def profiles_outcomes(slug: str, days: int = 365):
    """Past results for a profile: closed lots + their sold comps. This is the
    'research the past' half — the Deals tab with status=closed is the table,
    this is the roll-up above it."""
    pw = _profile_where(slug)
    where, args = (pw or ("TRUE", []))
    days = max(1, min(int(days), 3650))

    def _fetch():
        roll = db.fetch_one(
            f"""SELECT count(*) AS closed,
                       count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid,
                       count(*) FILTER (WHERE outcome = 'sold') AS sold,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY final_bid)
                         FILTER (WHERE final_bid > 0) AS median_final_bid,
                       max(closed_at) AS last_closed_at
                FROM deal_lots
                WHERE outcome_complete IS TRUE
                  AND closed_at >= now() - make_interval(days => %s)
                  AND ({where})""",
            (days, *args))
        comps = db.fetch_all(
            f"""SELECT v.asset_id, v.account_id, v.auction_id, v.analyzed_at, v.method,
                       v.comp_count, v.per_unit, v.margin_pct, v.comps, deal_lots.title
                FROM deal_verdicts v
                JOIN deal_lots ON deal_lots.asset_id = v.asset_id
                              AND deal_lots.account_id = v.account_id
                              AND deal_lots.auction_id = v.auction_id
                WHERE v.comp_count > 0 AND ({where})
                ORDER BY v.analyzed_at DESC LIMIT 20""",
            tuple(args))
        return roll or {}, comps

    try:
        roll, comps = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise HTTPException(503, f"outcomes query failed: {e!r}")
    closed = int(roll.get("closed") or 0)
    no_bid = int(roll.get("no_bid") or 0)
    return {
        "profile": slug, "days": days, "closed": closed, "no_bid": no_bid,
        "sold": int(roll.get("sold") or 0),
        "no_bid_pct": round(100.0 * no_bid / closed, 1) if closed else 0.0,
        "median_final_bid": (None if roll.get("median_final_bid") is None
                             else round(float(roll["median_final_bid"]), 2)),
        "last_closed_at": (roll["last_closed_at"].isoformat()
                           if roll.get("last_closed_at") else None),
        "comps": [dict(c, analyzed_at=c["analyzed_at"].isoformat() if c.get("analyzed_at") else None)
                  for c in comps],
    }
```

Note: the `deal_lots_where` fragment uses unqualified `title`/`description`/`state`/`current_bid`/`canonical_category`; `deal_verdicts` has none of those columns, so the join is unambiguous.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/ tests/web/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/web/app.py tests/deals/test_profiles_api.py
git commit -m "web: /api/profiles CRUD + /api/profiles/{slug}/outcomes roll-up"
```

**Done when:** `curl http://127.0.0.1:8765/api/profiles` lists `chairs` + `medical`; `curl http://127.0.0.1:8765/api/profiles/chairs/outcomes` returns a non-zero `closed` count against the live DB.

---

### Task 5: Auctions API reads the profile (`auctions_supabase.get_top_lots`, `/api/auctions?profile=`)

**Files:**
- Modify: `automation/auctions_supabase.py` (whole loader/filter path)
- Modify: `automation/web/app.py:1971-2028` (`list_auctions`), import line ~67
- Test: `tests/deals/test_profiles_api.py` (append)

**Interfaces:**
- Consumes: `profiles.Profile`, `profiles.auction_listings_where`, `profiles.matched_keyword`.
- Produces: `get_top_lots(profile: Profile, source="gd", n=15, min_quantity=None, include_condition=True, active_only=True, max_stale_days=2) -> list[dict]` — same row shape as today, with `category = profile.slug`, `category_keyword = matched keyword`. `get_top_chairs(...)` kept as a wrapper (`category` `None`/`"banquet"` → chairs seed, `"medical"` → medical seed). `/api/auctions` accepts `profile=<slug>`; legacy `category=` still works and maps to it.

- [ ] **Step 1: Failing tests** (append to `tests/deals/test_profiles_api.py`)

```python
def test_get_top_lots_filters_by_profile_sql(monkeypatch):
    from automation import auctions_supabase as aus
    cap = {}
    def fake_fetch_all(sql, params=()):
        cap["sql"], cap["params"] = sql, params
        return [{"asset_id": "1/2", "link": "https://www.govdeals.com/en/asset/1/2", "title": "40 desks",
                 "description": "", "quantity": 40, "quantity_source": "llm", "price": "$10",
                 "location": "Phoenix, Arizona", "pickup_zip": "85054", "end_date": "", "time_left": "",
                 "image_url": "", "last_seen_at": None, "contact_email": "", "contact_phone": ""}]
    monkeypatch.setattr(aus.db, "fetch_all", fake_fetch_all)
    rows = aus.get_top_lots(_profile(), source="gd", n=5, include_condition=False, active_only=False)
    assert "title ILIKE ANY(%s)" in cap["sql"] and ["%desk%"] in list(cap["params"])
    assert rows[0]["category"] == "desks" and rows[0]["category_keyword"] == "desk"


def test_auctions_endpoint_profile_and_legacy_category(monkeypatch):
    client, _ = _client(monkeypatch)
    webapp = importlib.import_module("automation.web.app")
    seen = []
    def fake_top(profile, **kw):
        seen.append((profile.slug, kw["min_quantity"]))
        return []
    monkeypatch.setattr(webapp, "get_top_lots", fake_top)
    webapp._AUCTIONS_CACHE.clear()
    assert client.get("/api/auctions?profile=desks&n=3").status_code == 200
    assert client.get("/api/auctions?category=medical&n=3").status_code == 200
    assert client.get("/api/auctions?profile=nope").status_code == 404
    assert seen[0][0] == "desks" and seen[0][1] == 1      # profile.min_quantity when min_qty absent
    assert seen[1][0] == "medical"
```

For the legacy-category test to resolve `medical`, extend the `_client` fixture's `load` fake: `lambda slug: {"desks": _profile(), "medical": _profile("medical", keywords=["dental"]), "chairs": _profile("chairs", is_default=True, min_quantity=50)}.get(slug)`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_profiles_api.py -q` — Expected: FAIL — `AttributeError: module 'automation.auctions_supabase' has no attribute 'get_top_lots'`.

- [ ] **Step 3: Implement**

`automation/auctions_supabase.py` — replace `_load_from_supabase` + `get_top_chairs` bodies:

```python
from deals import profiles as _profiles
from deals.profiles import Profile


def _load_from_supabase(profile: Profile, source: Source, min_quantity: int | None) -> list[dict]:
    frag = _SOURCE_FRAG[source]
    pwhere, pargs = _profiles.auction_listings_where(profile, min_quantity)
    rows = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE {pwhere}
          AND quantity <= %s
          AND quantity_source = ANY(%s)
          AND link ILIKE %s
        ORDER BY quantity DESC
        """,
        (*pargs, _SANE_MAX_QUANTITY, list(TRUSTED_QUANTITY_SOURCES), f"%{frag}%"),
    )
    for r in rows:
        ls = r.get("last_seen_at")
        if isinstance(ls, datetime):
            r["last_seen_at"] = ls.isoformat()
    rows.sort(key=lambda x: (-int(x.get("quantity") or 0), _price_to_float(x.get("price"))))
    return rows


def get_top_lots(profile: Profile, source: Source = "gd", n: int = 15,
                 min_quantity: int | None = None, include_condition: bool = True,
                 active_only: bool = True, max_stale_days: int = 2) -> list[dict]:
    """Top-n cached lots matching `profile`. Same row shape as get_top_chairs;
    `category` is the profile slug, `category_keyword` the keyword that hit."""
    if source not in _SOURCE_FRAG:
        raise ValueError(f"source must be one of {sorted(_SOURCE_FRAG)}, got {source!r}")
    items = _load_from_supabase(profile, source, min_quantity)
    for it in items:
        it["category"] = profile.slug
        it["category_keyword"] = _profiles.matched_keyword(profile, it.get("title"), it.get("description"))
    if active_only:
        now = datetime.now(timezone.utc)
        items = [it for it in items if _is_active(it, now, max_stale_days)]
    top = items[: max(0, int(n))]
    if not top:
        return []
    enrich = None
    if include_condition:
        try:
            enrich = _enrich_via_llm(top)
        except Exception as e:
            import sys
            print(f"[auctions_supabase] condition LLM unavailable ({e!r}); raw titles", file=sys.stderr)
    if enrich is None:
        enrich = [{"title": it.get("title") or "", "condition": None, "condition_note": None} for it in top]
    out = []
    for i, (it, en) in enumerate(zip(top, enrich), start=1):
        out.append({
            "rank": i, "quantity": int(it.get("quantity") or 0),
            "title": en["title"], "raw_title": it.get("title") or "",
            "price": it.get("price") or "", "end_date": it.get("end_date") or "",
            "time_left": it.get("time_left") or "", "link": it.get("link") or "",
            "image_url": it.get("image_url") or "", "location": it.get("location") or "",
            "pickup_zip": it.get("pickup_zip") or "", "contact_email": it.get("contact_email") or "",
            "contact_phone": it.get("contact_phone") or "",
            "category": it["category"], "category_keyword": it["category_keyword"],
            "condition": en["condition"] if include_condition else None,
            "condition_note": en["condition_note"] if include_condition else None,
        })
    return out


def get_top_chairs(source: Source = "gd", n: int = 15, min_quantity: int = 50,
                   include_condition: bool = True, active_only: bool = True,
                   max_stale_days: int = 2, category: str | None = None) -> list[dict]:
    """Back-compat wrapper: category None/'banquet' -> chairs, 'medical' -> medical."""
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES} or None, got {category!r}")
    slug = "medical" if category == "medical" else "chairs"
    profile = _profiles.load(slug) or _profiles.SEED_PROFILES[slug]
    return get_top_lots(profile, source=source, n=n, min_quantity=min_quantity,
                        include_condition=include_condition, active_only=active_only,
                        max_stale_days=max_stale_days)
```

Remove the now-unused `_classify`, `_is_non_chair_lot`, `os` imports from this file (keep `CATEGORIES`, `_SANE_MAX_QUANTITY`, `TRUSTED_QUANTITY_SOURCES`, `_enrich_via_llm`, `_is_active`, `_price_to_float`).

`automation/web/app.py` — import line ~67 becomes `from ..auctions_supabase import get_top_chairs, get_top_lots, cache_stats as _auctions_cache_stats` (set `get_top_lots = None` in the `except`). Replace `list_auctions`:

```python
@app.get("/api/auctions")
async def list_auctions(
    source: str = "gd", n: int = 15, min_qty: int | None = None, condition: int = 0,
    active_only: int = 1, max_stale_days: int = 2,
    category: str | None = None, profile: str | None = None,
):
    if get_top_lots is None:
        raise HTTPException(503, "auction_extractors package not available")
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
    # legacy sub-tab param → profile slug (banquet == default chairs profile)
    if not profile and category not in (None, "", "all"):
        if category not in ("banquet", "medical"):
            raise HTTPException(400, "category must be banquet|medical (or use profile=)")
        profile = "medical" if category == "medical" else None
    try:
        prof = await asyncio.to_thread(profiles.resolve, profile)
    except KeyError:
        raise HTTPException(404, f"unknown profile {profile!r}")
    n = max(1, min(int(n), 100))
    min_qty = prof.min_quantity if min_qty is None else max(1, int(min_qty))
    include_condition = bool(int(condition))
    active_flag = bool(int(active_only))
    stale = max(1, int(max_stale_days))
    key = f"{prof.slug}|{source}|{n}|{min_qty}|{int(include_condition)}|{int(active_flag)}|{stale}"
    now = time.time()
    cached = _AUCTIONS_CACHE.get(key)
    if cached and (now - cached[0]) < _AUCTIONS_TTL:
        return {"items": cached[1], "cached": True, "age": int(now - cached[0]), "profile": prof.slug}
    try:
        items = await asyncio.to_thread(
            get_top_lots, prof, source=source, n=n, min_quantity=min_qty,
            include_condition=include_condition, active_only=active_flag, max_stale_days=stale)
    except Exception as e:
        raise HTTPException(500, f"get_top_lots failed: {e!r}")
    try:
        _annotate_auction_geo(items)
    except Exception as e:
        print(f"[auctions] geo annotation failed: {e!r}")
    _AUCTIONS_CACHE[key] = (now, items)
    return {"items": items, "cached": False, "age": 0, "profile": prof.slug}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/ tests/web/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/auctions_supabase.py automation/web/app.py tests/deals/test_profiles_api.py
git commit -m "auctions: profile-driven loader (get_top_lots) + profile= on /api/auctions"
```

**Done when:** `curl 'http://127.0.0.1:8765/api/auctions?n=5'` returns the same 5 chair lots as before the change; `curl 'http://127.0.0.1:8765/api/auctions?profile=medical&n=5'` returns dental/exam rows with `category: "medical"`.

---

### Task 6: Scraper launch honors the profile (`SCRAPE_SEARCH_TERMS`, `SCRAPE_ITEM_NOUN`)

**Files:**
- Modify: `auction_extractors/govdeals_chairs_extraction.py:92-98` (SEARCH_TERMS)
- Modify: `auction_extractors/public_surplus_automation.py:68-83` (SEARCH_TERMS)
- Modify: `auction_extractors/quantity_llm.py:435` (prompt first line)
- Modify: `automation/web/app.py:1724-1748` (`_run_scraper`), `1855-1870` (`scrape_start`)
- Test: `tests/deals/test_profiles_cli.py` (create; the env helper test lives here)

**Interfaces:**
- Produces: `auction_extractors.govdeals_chairs_extraction.search_terms_from_env(default: list[str], env: dict) -> list[str]` (same in `public_surplus_automation`); `POST /api/scrape/start` accepts `profile: slug`; `_run_scraper(source, test_mode, profile_slug: str | None = None)`.

- [ ] **Step 1: Failing test**

```python
# tests/deals/test_profiles_cli.py
import sys
from pathlib import Path

AE = str(Path(__file__).resolve().parents[2] / "auction_extractors")
if AE not in sys.path:
    sys.path.insert(0, AE)


def test_search_terms_env_override():
    import govdeals_chairs_extraction as gd
    assert gd.search_terms_from_env(["chairs"], {}) == ["chairs"]
    assert gd.search_terms_from_env(["chairs"], {"SCRAPE_SEARCH_TERMS": "desks, office desks ,"}) == ["desks", "office desks"]


def test_ps_search_terms_env_override():
    import public_surplus_automation as ps
    assert ps.search_terms_from_env(["chairs"], {"SCRAPE_SEARCH_TERMS": "lockers"}) == ["lockers"]


def test_quantity_prompt_uses_item_noun(monkeypatch):
    import quantity_llm
    monkeypatch.setenv("SCRAPE_ITEM_NOUN", "desks")
    assert "DESKS" in quantity_llm._quantity_prompt_header()
    monkeypatch.delenv("SCRAPE_ITEM_NOUN")
    assert "CHAIRS" in quantity_llm._quantity_prompt_header()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_profiles_cli.py -q` — Expected: FAIL — `AttributeError: ... has no attribute 'search_terms_from_env'`.

- [ ] **Step 3: Implement**

In both scraper files, replace the `SEARCH_TERMS = [...]` literal with:

```python
def search_terms_from_env(default: list[str], env: dict | None = None) -> list[str]:
    """Dashboard-launched scrapes pass the research profile's terms as
    SCRAPE_SEARCH_TERMS (comma list). Empty/unset → the chair defaults."""
    raw = (env if env is not None else os.environ).get("SCRAPE_SEARCH_TERMS", "")
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    return terms or list(default)


_DEFAULT_SEARCH_TERMS = [ ...existing literal list, unchanged... ]
SEARCH_TERMS = search_terms_from_env(_DEFAULT_SEARCH_TERMS)
```

In `quantity_llm.py` add near the top (after imports; add `import os` if absent):

```python
def _quantity_prompt_header() -> str:
    noun = (os.getenv("SCRAPE_ITEM_NOUN") or "chairs").strip()
    return f"You estimate how many {noun.upper()} (individual {noun.lower()} units) are in each auction lot."
```

and change the prompt's first line at ~435 from the literal to `{_quantity_prompt_header()}` inside the f-string. Leave the rule examples as-is (they are examples of what *not* to count).

`automation/web/app.py`:
- `_run_scraper(source, test_mode, profile_slug: str | None = None)`. After `env["PYTHONUNBUFFERED"] = "1"`:

```python
        if profile_slug:
            try:
                prof = profiles.resolve(profile_slug)
                if prof.search_terms:
                    env["SCRAPE_SEARCH_TERMS"] = ",".join(prof.search_terms)
                env["SCRAPE_ITEM_NOUN"] = prof.item_noun
                await scrape_state.broadcast({"t": time.time(), "stream": "system",
                    "data": f"[profile {prof.slug}] terms={prof.search_terms} noun={prof.item_noun}"})
            except KeyError:
                await scrape_state.broadcast({"t": time.time(), "stream": "system",
                    "data": f"[profile {profile_slug!r} unknown — using scraper defaults]"})
```

- `scrape_start`: read `profile_slug = (payload.get("profile") or "").strip() or None` and pass it: `asyncio.create_task(_run_scraper(source, test_mode, profile_slug))`; include `"profile": profile_slug` in the response.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/ -q` — Expected: PASS. Also: `cd auction_extractors && ../.venv/bin/python -c "import govdeals_chairs_extraction as g; print(g.SEARCH_TERMS[:3])"` prints `['chairs', 'banquet chairs', 'stackable chairs']` (defaults intact).

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/govdeals_chairs_extraction.py auction_extractors/public_surplus_automation.py auction_extractors/quantity_llm.py automation/web/app.py tests/deals/test_profiles_cli.py
git commit -m "scrape: SCRAPE_SEARCH_TERMS / SCRAPE_ITEM_NOUN from the research profile"
```

**Done when:** `curl -X POST localhost:8765/api/scrape/start -H 'content-type: application/json' -d '{"source":"gd","profile":"medical"}'` shows `[profile medical] terms=[...]` in the SCRAPE strip; a plain `{"source":"gd"}` launch prints nothing new and scrapes the chair terms.

---

### Task 7: deals CLI `--profile` (discover / watch-once / digest / track-bidders)

**Files:**
- Modify: `deals/discover.py` (`run_discovery` signature + archive gate)
- Modify: `deals/digest.py` (`candidate_rows`, `send_daily_digest`)
- Modify: `deals/store.py:158-176` (`due_for_poll`), `:218-240` (`bidder_targets`)
- Modify: `deals/watch.py:13-15` (`poll_once` signature + the `due_for_poll(now)` call)
- Modify: `deals/cli.py`
- Test: `tests/deals/test_discover.py` (append), `tests/deals/test_digest.py` (append), `tests/deals/test_profiles_cli.py` (append)

**Interfaces:**
- Consumes: `profiles.resolve`, `profiles.matches`, `profiles.deal_lots_where`.
- Produces:
  - `run_discovery(adapter, *, categories, classify=True, archive_candidates=True, now=None, max_pages=60, archive_predicate: Callable[[Lot], bool] | None = None)` — default predicate = `lot.canonical_category == "seating_furniture"` (today).
  - `digest.candidate_rows(profile_where: tuple[str, list] | None) -> list[dict]`; `send_daily_digest(fees, profile: Profile | None = None)`; `format_digest(rows, fees, label: str = "")`.
  - `store.due_for_poll(now, extra_where: tuple[str, list] | None = None)`; `store.bidder_targets(..., extra_where=None)`.
  - `watch.poll_once(adapter, now, extra_where=None)`.
  - `cli.profile_arg(sub)` adds `--profile` (default `os.environ.get("DEALS_PROFILE")`) to a subparser; `cli.resolve_profile(slug) -> Profile | None` (None when unset).

- [ ] **Step 1: Failing tests**

Append to `tests/deals/test_discover.py`:

```python
def test_archive_predicate_overrides_seating_gate(monkeypatch):
    arch = []
    monkeypatch.setattr("deals.discover.upsert_lot", lambda l: None)
    monkeypatch.setattr("deals.discover.set_poll_schedule", lambda k, t, ln: None)
    monkeypatch.setattr("deals.discover.apply_classification", lambda l, **k: l)
    monkeypatch.setattr("deals.discover.archive_lot_images", lambda l, g: arch.append(l) or [])
    lots = [_lot(cat="266", canon="general_merchandise")]      # not seating, but title says chairs
    rep = run_discovery(FakeAdapter(lots), categories=["x"],
                        now=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
                        archive_predicate=lambda lot: "chairs" in lot.title)
    assert rep.archived == 1
```

Append to `tests/deals/test_digest.py`:

```python
def test_digest_label_and_profile_rows(monkeypatch):
    from deals import digest
    cap = {}
    monkeypatch.setattr(digest.db, "fetch_all", lambda sql, params=(): cap.update(sql=sql, params=params) or [])
    assert digest.candidate_rows(("title ILIKE ANY(%s)", [["%desk%"]])) == []
    assert "title ILIKE ANY(%s)" in cap["sql"] and ["%desk%"] in list(cap["params"])
    assert "bid_count = 0" in cap["sql"] and "interval '24 hours'" in cap["sql"]
    out = format_digest([], fees=FeeModel(), label="Desks")
    assert "Desks" in out
```

Append to `tests/deals/test_profiles_cli.py`:

```python
def test_cli_profile_arg_defaults_from_env(monkeypatch):
    import argparse
    import deals.cli as cli
    monkeypatch.setenv("DEALS_PROFILE", "desks")
    ap = argparse.ArgumentParser(); cli.profile_arg(ap)
    assert ap.parse_args([]).profile == "desks"
    assert ap.parse_args(["--profile", "chairs"]).profile == "chairs"
    monkeypatch.delenv("DEALS_PROFILE")
    ap2 = argparse.ArgumentParser(); cli.profile_arg(ap2)
    assert ap2.parse_args([]).profile is None


def test_discover_with_profile_uses_native_ids(monkeypatch, capsys, make_lot):
    import deals.cli as cli
    import deals.profiles as profiles
    from deals.profiles import Profile
    prof = Profile(slug="desks", name="Desks", keywords=["desk"], native_category_ids=["372", "47B"])
    monkeypatch.setattr(profiles, "resolve", lambda slug: prof)
    seen = {}
    monkeypatch.setattr(cli, "run_discovery",
                        lambda adapter, categories, max_pages, archive_predicate=None:
                            seen.update(categories=categories, pred=archive_predicate) or "ok")
    monkeypatch.setattr(cli.sites, "get_adapter", lambda key: object())
    monkeypatch.setattr(sys, "argv", ["deals.cli", "discover", "--profile", "desks"])
    cli.main()
    assert seen["categories"] == ["372", "47B"]
    assert seen["pred"](make_lot(title="10 desks")) and not seen["pred"](make_lot(title="10 chairs"))


def test_store_extra_where_is_bound(monkeypatch):
    from deals import store
    cap = {}
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): cap.update(sql=sql, params=params) or [])
    store.bidder_targets(limit=5, category=None, extra_where=("title ILIKE ANY(%s)", [["%desk%"]]))
    assert "title ILIKE ANY(%s)" in cap["sql"] and ["%desk%"] in list(cap["params"])
    store.due_for_poll(datetime.now(timezone.utc), extra_where=("state = ANY(%s)", [["AZ"]]))
    assert "state = ANY(%s)" in cap["sql"] and ["AZ"] in list(cap["params"])
```

(add `from datetime import datetime, timezone` at the top of `test_profiles_cli.py`).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_discover.py tests/deals/test_digest.py tests/deals/test_profiles_cli.py -q` — Expected: FAIL on the four new tests (unexpected kwargs / missing attributes).

- [ ] **Step 3: Implement**

`deals/discover.py`:

```python
from typing import Callable
from deals.models import Lot

def _default_archive_gate(lot: Lot) -> bool:
    return lot.canonical_category == "seating_furniture"

def run_discovery(adapter: SiteAdapter, *, categories: list[str], classify: bool = True,
                  archive_candidates: bool = True, now: datetime | None = None,
                  max_pages: int = 60,
                  archive_predicate: Callable[[Lot], bool] | None = None) -> DiscoveryReport:
    gate = archive_predicate or _default_archive_gate
    ...
                if archive_candidates and lot.bid_count == 0 and not lot.is_free and gate(lot):
```

`deals/digest.py`:

```python
_CANDIDATE_SQL = """SELECT asset_id, account_id, auction_id, title, current_bid, bid_count,
       city, state, end_utc, canonical_category
FROM deal_lots
WHERE outcome_complete IS NOT TRUE AND bid_count = 0 AND is_free = false
  AND currency_code = 'USD' AND end_utc <= now() + interval '24 hours'"""

def candidate_rows(profile_where: tuple[str, list] | None = None) -> list[dict]:
    """Same predicate as the deal_candidates view, plus the profile filter.
    (The view has no description column, so the filter runs on deal_lots.)"""
    if profile_where is None or profile_where[0] == "TRUE":
        return db.fetch_all("SELECT * FROM deal_candidates")
    return db.fetch_all(f"{_CANDIDATE_SQL} AND ({profile_where[0]}) ORDER BY end_utc ASC",
                        tuple(profile_where[1]))

def format_digest(rows: list[dict], fees: FeeModel, label: str = "") -> str:
    tag = f" [{label}]" if label else ""
    if not rows:
        return f"🪑 No 0-bid lots closing in the next 24h{tag}."
    lines = [f"🪑 {len(rows)} lots closing <24h with 0 bids{tag}:\n"]
    ...  # rest unchanged

def send_daily_digest(fees: FeeModel, profile=None) -> tuple[bool, str | None]:
    from deals import profiles
    pw = profiles.deal_lots_where(profile) if profile else None
    rows = candidate_rows(pw)
    return send_message_sync(format_digest(rows, fees, label=profile.name if profile else ""), topic="deals")
```

`deals/store.py`:

```python
def due_for_poll(now: datetime, extra_where: tuple[str, list] | None = None) -> list[Lot]:
    from deals.mapping import asset_to_lot
    where = "outcome_complete IS NOT TRUE AND raw IS NOT NULL AND (next_poll_at IS NULL OR next_poll_at<=%s)"
    params: list = [now]
    if extra_where and extra_where[0] != "TRUE":
        where += f" AND ({extra_where[0]})"; params += list(extra_where[1])
    rows = db.fetch_all(f"SELECT raw FROM deal_lots WHERE {where}", tuple(params))
    ...  # loop unchanged

def bidder_targets(limit=200, *, category="seating_furniture", title_like=None,
                   ending_within_hours=None, min_bids=0,
                   extra_where: tuple[str, list] | None = None):
    ...  # existing where/params, then before `params.append(limit)`:
    if extra_where and extra_where[0] != "TRUE":
        where.append(f"({extra_where[0]})"); params += list(extra_where[1])
```

`deals/watch.py:13-15`: `def poll_once(adapter, now: datetime, extra_where: tuple[str, list] | None = None) -> PollReport:` and line 15 becomes `due = due_for_poll(now, extra_where)`. Nothing else in the file changes.

`deals/cli.py`:

```python
import os
from deals import profiles as _profiles

def profile_arg(p):
    p.add_argument("--profile", default=os.environ.get("DEALS_PROFILE") or None,
                   help="research profile slug (research_profiles); env DEALS_PROFILE")

def resolve_profile(slug):
    return _profiles.resolve(slug) if slug else None
```

Call `profile_arg(d)`, `profile_arg(tb)`, and `profile_arg(...)` on the `watch-once` and `digest` subparsers (assign them: `wo = sub.add_parser("watch-once"); profile_arg(wo)`; `dg = sub.add_parser("digest"); profile_arg(dg)`). Then in `main()`:

```python
    prof = resolve_profile(getattr(a, "profile", None))
    pw = _profiles.deal_lots_where(prof) if prof else None
    ...
    elif a.cmd == "discover":
        cats = (list(prof.native_category_ids) if prof and prof.native_category_ids and a.categories is None
                else sweep_categories(a.categories, os.environ))
        pred = (lambda lot: _profiles.matches(prof, lot.title, lot.description)) if prof else None
        ...
            rep = run_discovery(sites.get_adapter(key), categories=cats, max_pages=a.max_pages,
                                archive_predicate=pred)
    elif a.cmd == "track-bidders":
        ...
            keys = store.bidder_targets(limit=a.limit,
                category=(None if (a.category == "all" or prof) else a.category),
                title_like=a.title_like, ending_within_hours=a.ending_within,
                min_bids=a.min_bids, extra_where=pw)
    elif a.cmd == "watch-once":
        print(poll_once(adapter, datetime.now().astimezone(), extra_where=pw))
    elif a.cmd == "digest":
        ok, err = send_daily_digest(fee_model_from_env(), profile=prof)
```

Explicit `--categories` still wins over the profile's native ids (operator override). `--profile` with `track-bidders` drops the `seating_furniture` category default (the profile is the filter now).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/ -q` — Expected: PASS (all, including the existing `test_dry_run_*` and `test_cli_sweep` tests).

- [ ] **Step 5: Commit**

```bash
git add deals/discover.py deals/digest.py deals/store.py deals/watch.py deals/cli.py tests/deals/test_discover.py tests/deals/test_digest.py tests/deals/test_profiles_cli.py
git commit -m "deals: --profile on discover/watch-once/digest/track-bidders (DEALS_PROFILE env default)"
```

**Done when:** `.venv/bin/python -m deals.cli discover --profile medical --dry-run --limit 3` prints 3 lots and "nothing written"; `.venv/bin/python -m deals.cli digest --profile chairs` prints `digest sent` (or `telegram_not_configured`) with the `[Banquet chairs]` label in the formatted text; a bare `discover --dry-run --limit 1` is unchanged.

---

### Task 8: Admin UI — profile switcher on Auctions, profile select + outcomes strip on Deals

**Files:**
- Modify: `automation/web/templates/index.html:179-262` (Auctions controls), `:511-580` (Deals filters)
- Modify: `automation/web/static/app.js:562-660` (auc state + seg handlers), `:876-960` (`loadAuctions`, `renderFilterSummary`), `:2279-2300` (deal state), `:2402-2425` (`loadDeals`), `:2540-2570` (chips), `:2828-2880` (`currentDealParams` / `applyDealSearch`), `:2787-2830` (`loadDealMeta`)
- No unit tests (vanilla JS); verification is `node --check` + browser.

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/profiles`, `GET /api/profiles/{slug}/outcomes`, `?profile=` on `/api/auctions`, `/api/deals*`, `POST /api/scrape/start {profile}`.

- [ ] **Step 1: Auctions tab markup** — replace the `CATEGORY` `ac-group` block (`#auc-category`) with:

```html
    <div class="ac-group">
      <span class="ac-label" title="Research profile: keyword set + quantity floor + sweep categories (research_profiles)">PROFILE</span>
      <div class="seg" id="auc-profile"></div>
      <button type="button" class="btn btn-small" id="auc-profile-new" title="Create a research profile for another item">＋ profile</button>
      <button type="button" class="btn btn-small" id="auc-profile-del" title="Delete the selected profile (not the default)">×</button>
    </div>
    <div class="ac-group" id="auc-profile-form" hidden>
      <input type="text" id="pf-slug" placeholder="slug (office-desks)" style="width:130px">
      <input type="text" id="pf-name" placeholder="name" style="width:140px">
      <input type="text" id="pf-keywords" placeholder="keywords: desk, credenza" style="width:220px">
      <input type="text" id="pf-exclude" placeholder="exclude: lamp, mat" style="width:160px">
      <input type="text" id="pf-terms" placeholder="site search terms: desks, office desks" style="width:220px">
      <input type="text" id="pf-native" placeholder="GovDeals cat ids: 372,47B" style="width:160px">
      <input type="number" id="pf-minqty" placeholder="min qty" value="1" min="1" style="width:80px">
      <input type="text" id="pf-noun" placeholder="noun: desks" style="width:100px">
      <button type="button" class="btn btn-small btn-primary" id="pf-save">save</button>
      <button type="button" class="btn btn-small" id="pf-cancel">cancel</button>
    </div>
```

De-chair the copy in the same section: hero-sub `Live (and archived) bulk-chair lots` → `Live (and archived) bulk lots for the selected research profile`; `Min chairs per lot` → `Min units per lot`; `ranking by chair count` → `ranking by quantity`; map-toggle title `visible chair lots` → `visible lots`.

- [ ] **Step 2: Auctions tab JS** — in `app.js`:

Replace `category: ''` in `auc` with `profile: '', profiles: [], defaultProfile: 'chairs',`. Delete the `#auc-category` click-handler block. Add:

```js
async function loadProfiles() {
  const body = await apiFetch('/api/profiles');
  auc.profiles = body.profiles || [];
  auc.defaultProfile = body.default || 'chairs';
  if (!auc.profile || !auc.profiles.some(p => p.slug === auc.profile)) auc.profile = auc.defaultProfile;
  renderProfileSeg();
  const sel = $('#deal-profile');
  if (sel) {
    sel.innerHTML = '<option value="">any profile</option>' +
      auc.profiles.map(p => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('');
    sel.value = deal.profile || '';
  }
  return auc.profiles;
}

function renderProfileSeg() {
  const seg = $('#auc-profile');
  seg.innerHTML = auc.profiles.map(p =>
    `<button type="button" class="seg-btn ${p.slug === auc.profile ? 'active' : ''}" data-value="${esc(p.slug)}"
       title="${esc((p.keywords || []).join(', '))} · min ${p.min_quantity}">${esc(p.name)}</button>`).join('');
  $('#auc-profile-del').disabled = !!(auc.profiles.find(p => p.slug === auc.profile) || {}).is_default;
}

$('#auc-profile').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg-btn'); if (!btn) return;
  auc.profile = btn.dataset.value;
  const p = auc.profiles.find(x => x.slug === auc.profile);
  if (p) { $('#auc-min-qty').value = p.min_quantity; $('#auc-min-qty-out').textContent = p.min_quantity; }
  renderProfileSeg();
  loadAuctions();
});

$('#auc-profile-new').addEventListener('click', () => { $('#auc-profile-form').hidden = false; });
$('#pf-cancel').addEventListener('click', () => { $('#auc-profile-form').hidden = true; });
$('#pf-save').addEventListener('click', (e) => withButtonLoading(e.currentTarget, 'saving…', async () => {
  const body = {
    slug: $('#pf-slug').value, name: $('#pf-name').value, keywords: $('#pf-keywords').value,
    exclude_terms: $('#pf-exclude').value, search_terms: $('#pf-terms').value,
    native_category_ids: $('#pf-native').value, min_quantity: $('#pf-minqty').value,
    item_noun: $('#pf-noun').value || 'units',
  };
  try {
    const saved = await apiFetch('/api/profiles', {method: 'POST', body: JSON.stringify(body),
                                                   headers: {'content-type': 'application/json'}});
    auc.profile = saved.slug;
    $('#auc-profile-form').hidden = true;
    await loadProfiles();
    toast(`profile ${saved.slug} saved`, 'ok');
    loadAuctions();
  } catch (err) { toast(`save failed: ${err.message || err}`, 'err'); }
}));

$('#auc-profile-del').addEventListener('click', async () => {
  if (!auc.profile || !confirm(`Delete profile "${auc.profile}"?`)) return;
  try {
    await apiFetch(`/api/profiles/${encodeURIComponent(auc.profile)}`, {method: 'DELETE'});
    auc.profile = '';
    await loadProfiles();
    loadAuctions();
  } catch (err) { toast(`delete failed: ${err.message || err}`, 'err'); }
});
```

In `loadAuctions()` replace `if (auc.category) qs.set('category', auc.category);` with:

```js
  if (!auc.profiles.length) await loadProfiles();
  qs.set('profile', auc.profile || auc.defaultProfile);
```

In `renderFilterSummary`, `min-chairs` → `min-units`, `ranked by chair count` → `ranked by quantity`. In `startScrape(source, test)` (find it near the scrape dropdown handlers) add `profile: auc.profile || auc.defaultProfile` to the POST body so a dashboard scrape uses the profile's search terms. The `🪑` image fallback stays (it is the app's icon).

`apiFetch(url, opts)` (app.js:49) is a thin `fetch` wrapper — it does not JSON-encode, so the `JSON.stringify` + `content-type` header above is correct. `startScrape(source, test)` is at app.js:679; its POST body gains `profile`.

- [ ] **Step 3: Deals tab** — in `index.html` after the `#deal-category` select add:

```html
      <select id="deal-profile" title="Restrict to a research profile's keywords/states/price band"><option value="">any profile</option></select>
```

and directly under the filters row (before the table) add:

```html
  <div class="cache-header" id="deal-outcomes" hidden></div>
```

In `app.js`: add `profile: ''` to the `deal` state object. In `loadDeals()` add `if (deal.profile) p.set('profile', deal.profile);` (and the same line in the geo/tree param builders at ~2695 and ~2369 — tree uses a string URL: append `&profile=${encodeURIComponent(deal.profile)}` when set). In `currentDealParams()` add `if (deal.profile) p.profile = deal.profile;`; in `applyDealSearch()` add `deal.profile = params.profile || '';` and `syncSel('#deal-profile', deal.profile);`. Chips: `if (deal.profile) chips.push({k: 'profile', label: 'profile: ' + deal.profile});` and a `case 'profile': deal.profile = ''; $('#deal-profile').value = ''; break;`. Handler + outcomes strip:

```js
$('#deal-profile').addEventListener('change', (e) => {
  deal.profile = e.target.value; deal.offset = 0; loadDeals(); loadDealOutcomes();
});

async function loadDealOutcomes() {
  const host = $('#deal-outcomes');
  if (!deal.profile) { host.hidden = true; return; }
  try {
    const o = await apiFetch(`/api/profiles/${encodeURIComponent(deal.profile)}/outcomes?days=365`);
    const med = o.median_final_bid == null ? '—' : '$' + o.median_final_bid.toLocaleString();
    host.hidden = false;
    host.innerHTML = `PAST 365d · ${o.closed} closed · ${o.no_bid_pct}% no-bid · median final ${med} · ${o.comps.length} lots with sold comps` +
      (o.comps.length ? ` · <a href="#" id="deal-outcomes-comps">show comps</a>` : '');
    const a = $('#deal-outcomes-comps');
    if (a) a.addEventListener('click', (ev) => {
      ev.preventDefault();
      host.innerHTML += '<div class="mono tiny">' + o.comps.map(c =>
        `${esc(c.title || '')} — ${c.comp_count} comps, $${c.per_unit ?? '—'}/unit, margin ${c.margin_pct ?? '—'}%`).join('<br>') + '</div>';
    });
  } catch (err) { host.hidden = false; host.textContent = `outcomes unavailable: ${err.message || err}`; }
}
```

Call `loadProfiles()` once from `loadDealMeta()` (so the select is filled when the Deals tab opens first).

- [ ] **Step 4: Verify**

Run: `node --check automation/web/static/app.js` — Expected: no output.
Restart: kill the server, `python -m automation.web`, open `http://127.0.0.1:8765/admin` → `04 Auctions`: seg shows `Banquet chairs` (active) + `Medical chairs & tables`; clicking Medical sets min-qty to 1 and loads dental rows; `＋ profile` → save `office-desks` / keywords `desk` / terms `desks` → new seg button appears, cards load (likely empty until a scrape with that profile runs). `10 Deals`: pick `Banquet chairs` in the profile select, set status `closed` → rows filter, `PAST 365d …` strip renders.

- [ ] **Step 5: Commit**

```bash
git add automation/web/templates/index.html automation/web/static/app.js
git commit -m "web: profile switcher on Auctions (+ create/delete), profile filter + past-outcomes strip on Deals"
```

**Done when:** the four browser checks above pass and `.venv/bin/python -m pytest tests/deals/ tests/web/ -q` is still green.

---

### Task 9: Docs + CLAUDE.md pointers

**Files:**
- Modify: `docs/claude-reference/deals.md` (new subsection after `discover.py / digest.py`), `docs/claude-reference/repo-layout.md` (Auctions tab + `/api/auctions` + new APIs), `docs/claude-reference/auction-extractors.md` (env overrides), `docs/claude-reference/todos-and-history.md` (Done log), `CLAUDE.md` (one command line)

- [ ] **Step 1: `deals.md`** — add:

```markdown
- `profiles.py` — **research profiles** (2026-09-04). One row in `research_profiles` per item family: `keywords` (title/description match), `exclude_terms` (title veto), `search_terms` (what the scrapers type), `native_category_ids` (discover sweep), `canonical_categories`/`states`/`min_price`/`max_price` (deal_lots narrowing), `min_quantity` + `item_noun` (scrape cache). `chairs` is the default; `medical` is the second seed. `--profile <slug>` (or env `DEALS_PROFILE`) on `discover` (sweeps the profile's native ids, archives 0-bid lots that `matches()`), `watch-once`, `digest`, `track-bidders`. Without it every command is today's chairs behavior. Past results per profile: `GET /api/profiles/{slug}/outcomes` (closed / no-bid % / median final / sold comps from `deal_verdicts`) and the Deals tab with `status=closed`.
```

- [ ] **Step 2: `repo-layout.md`** — in the `04 Auctions` sentence add: "Profile switcher (`/api/profiles`) replaces the banquet/medical sub-tabs; `/api/auctions?profile=<slug>` (legacy `category=` still maps). `＋ profile` creates a new `research_profiles` row inline." Add to the Admin JSON APIs list: `/api/profiles[...]` (list/create/delete), `/api/profiles/{slug}/outcomes`, and `profile=` on `/api/deals`, `/api/deals/geo`, `/api/deals/tree`, `/api/scrape/start`.

- [ ] **Step 3: `auction-extractors.md`** — under **Env** add: "`SCRAPE_SEARCH_TERMS` (comma list) and `SCRAPE_ITEM_NOUN` override the chair defaults; the dashboard sets both from the selected research profile when it launches a scrape. The scraper's own Telegram alert (`partition_for_alert`) is still chairs-only; the cache (and therefore the Auctions tab) keeps every scraped row."

- [ ] **Step 4: `todos-and-history.md`** Done log entry (date, one line each: profiles table, `--profile`, UI, scraper env) and the cut list from this plan under "Known TODOs".

- [ ] **Step 5: `CLAUDE.md`** — in *Key paths / commands / env*, append to the Deals line: `--profile <slug>` on `discover|watch-once|digest|track-bidders`; profiles CRUD at `/api/profiles`.

- [ ] **Step 6: Gate + commit**

Run: `.venv/bin/python -m pytest tests/deals/ tests/web/ -q` — Expected: PASS.

```bash
git add docs/claude-reference/deals.md docs/claude-reference/repo-layout.md docs/claude-reference/auction-extractors.md docs/claude-reference/todos-and-history.md CLAUDE.md
git commit -m "docs: research profiles — deals.md, repo-layout, auction-extractors env, todos"
```

**Done when:** `grep -n "research_profiles" CLAUDE.md docs/claude-reference/*.md ../docs/claude-reference/data-model.md` hits every file above.

---

## Self-review

- **Spec coverage:** "any other category / multiple categories" → Task 1-2 (profile = keyword set + native ids), Task 8 (create inline). "set up the auctions tab" → Task 5 + 8. "past and present" → present = Task 5 (cache) + Task 3 (`status=active`); past = Task 3 (`status=closed` + profile) + Task 4 (outcomes + comps). "continue to do my own research" → CLI `--profile` (Task 7), scrape by profile (Task 6), saved searches carry a profile (Task 3).
- **Placeholders:** none; every code step is literal (`deals/watch.py` edit is line-exact: 13-15).
- **Type consistency:** `Profile` fields identical in Task 1 DDL, Task 2 dataclass, Task 8 form; `deal_lots_where` → `tuple[str, list]` consumed as `profile_where` / `extra_where` / `pw` everywhere; `get_top_lots(profile, source=, n=, min_quantity=, include_condition=, active_only=, max_stale_days=)` matches Task 5's test fake and the `/api/auctions` call; `run_discovery(..., archive_predicate=)` matches Task 7's CLI patch and test.
- **Test gate per task:** every task ends with `.venv/bin/python -m pytest tests/deals/ -q` (+ `tests/web/` when `app.py` changes).

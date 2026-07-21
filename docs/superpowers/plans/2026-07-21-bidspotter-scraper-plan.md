# BidSpotter Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add BidSpotter.com as the third auction-site scraper source (`bs`) in `auction_extractors/`, surfaced in the dashboard's 04 Auctions tab exactly like Public Surplus.

**Architecture:** New plain-HTTP scraper `auction_extractors/bidspotter_automation.py` (requests only, no browser; AWS-WAF-202 retry) mirroring `public_surplus_automation.py`'s pipeline shape, plus additive `bs` plumbing through the shared read surface (`listings_db` → `top_chairs` → `auctions_supabase` → `automation/web`). Quantity comes from BidSpotter's structured field (`quantity_source="structured"`, a NEW trusted value) with the regex-seed + LLM pass as fallback; the trust guard widens from `== "llm"` to `{"llm", "structured"}`.

**Tech Stack:** Python 3.11, `requests`, stdlib `re`/`json` parsing (no bs4 — matches the PS parser), SQLite (`listings_db`), Supabase Postgres (`automation/db`), FastAPI dashboard, vanilla-JS front-end.

**Spec:** `docs/superpowers/specs/2026-07-21-bidspotter-scraper-design.md` — read it first; it carries the verified selectors, the forItem `"1"` ambiguity rule, and the `Model`-wrapped bid-info shape.

## Global Constraints

- Source key is exactly `"bs"`; cache key prefix is exactly `bs:<lotGuid>` (GUID = 8-4-4-4-12 lowercase hex). No `source` column is added to any schema.
- Trusted quantity sources are exactly `frozenset({"llm", "structured"})`. `regex_title`, `regex_fulltext`, `llm_failed`, `llm_missing` stay untrusted.
- `structured` quantity confidence is `"high"`. Structured rows are NEVER passed through `refine_quantities_with_llm` (it returns copies and overwrites unconditionally).
- HTTP: `User-Agent: Mozilla/5.0` (boring on purpose — a full Chrome UA trips AWS WAF), cookie `user_preference_pagesize=120`, retry on status 202 + header `x-amzn-waf-action`.
- Stage prefixes on stdout: `[1]` scrape, `[1b]` hydration, `[1d]` LLM refine, `[1e]` cache store, `[2]` ranking, `[3a]` telegram (dashboard `_SCRAPE_STAGES` parses these).
- Archive before filter: `listings_db.store_listings()` runs on ALL rows before the keep-filter.
- `MIN_CHAIR_QUANTITY = 50`; price format GovDeals-style `"USD 7.00"`.
- Test runner: this venv has NO `pytest` console script — always `.venv/bin/python -m pytest …`.
- Fixtures already committed (do not re-fetch): `auction_extractors/tests/fixtures/bidspotter_search_chairs.html` (3 real cards: `00a1c5e1…` no-structured-qty/featured; `aaf53c9e…` "Ghost chairs" qty 43, Belmont, North Carolina; `ea890a07…` "Tuscan Chairs" qty 127) and `auction_extractors/tests/fixtures/bidspotter_bid_info.json` (live-captured, `{"Model": {…}}`-wrapped, LeadingBid 50/7/6, TotalBids 1/3/2).
- ⚠ SHARED-FILE tasks (touch code gd/ps depend on — extra care, run both test suites after): Tasks 2, 3, 4, 9, 10, 11.

**File → task map (each file is edited in one task or adjacent tasks only):**
`listings_db.py` T1 · `top_chairs.py`+`__main__.py`+`tests/test_quantity_trust.py` T2-T3 · `automation/auctions_supabase.py` T4 · `bidspotter_automation.py`+`tests/test_bidspotter_parse.py` T5-T8 · `automation/web/app.py` T9 · `templates/index.html` T10 · `static/app.js`+`app.css` T11 · `.env.example`+`scripts/*.sh` T12.

---

### Task 1: `bs:<guid>` cache key in `listings_db.extract_asset_id`

**Files:**
- Modify: `auction_extractors/listings_db.py:87-103` (`extract_asset_id`)
- Test: `auction_extractors/tests/test_bidspotter_cache_key.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `extract_asset_id(url: str) -> str` now returns `f"bs:{guid}"` for BidSpotter lot URLs. Task 8's `store_listings` and Task 9's favorites depend on this mapping.

- [ ] **Step 1: Write the failing test**

```python
# auction_extractors/tests/test_bidspotter_cache_key.py
"""extract_asset_id must key BidSpotter lot URLs as ``bs:<lotGuid>`` —
otherwise every BidSpotter row is silently skipped by store_listings.
Run standalone:  python tests/test_bidspotter_cache_key.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from listings_db import extract_asset_id

BS_URL = ("https://www.bidspotter.com/en-us/auction-catalogues/stone/"
          "catalogue-id-stone-10117/lot-aaf53c9e-a9d1-4de6-a794-b477015c7b1f")


def test_bidspotter_url_maps_to_bs_prefixed_guid():
    assert extract_asset_id(BS_URL) == "bs:aaf53c9e-a9d1-4de6-a794-b477015c7b1f"


def test_existing_patterns_unchanged():
    assert extract_asset_id("https://www.govdeals.com/en/asset/305/10340") == "305/10340"
    assert extract_asset_id(
        "https://www.publicsurplus.com/sms/auction/view?auc=4020144") == "ps:4020144"


def test_garbage_still_uncacheable():
    assert extract_asset_id("") == ""
    assert extract_asset_id("https://example.com/lot-123") == ""
    # GUID shape on a non-bidspotter host must NOT match.
    assert extract_asset_id(
        "https://evil.example/lot-aaf53c9e-a9d1-4de6-a794-b477015c7b1f") == ""


if __name__ == "__main__":
    test_bidspotter_url_maps_to_bs_prefixed_guid()
    test_existing_patterns_unchanged()
    test_garbage_still_uncacheable()
    print("ok — bidspotter cache key")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_cache_key.py -v`
Expected: FAIL — `assert '' == 'bs:aaf53c9e-…'` (pattern not recognized yet).

- [ ] **Step 3: Implement**

In `auction_extractors/listings_db.py`, replace the body of `extract_asset_id` (keep gd/ps patterns first, add bs before the final `return ""`), and extend the docstring:

```python
def extract_asset_id(url: str) -> str:
    """Return a stable cache key for a listing URL across supported sites.

    GovDeals: ``/en/asset/<assetId>/<sellerId>`` → ``<assetId>/<sellerId>``
              (legacy format — kept unprefixed for back-compat with existing rows).
    PublicSurplus: ``/sms/auction/view?auc=<aucId>`` → ``ps:<aucId>``.
    BidSpotter: ``bidspotter.com/…/lot-<lotGuid>`` → ``bs:<lotGuid>``
                (relists mint a new GUID, so one key = one auction round).

    Returns empty string if the URL matches none (uncacheable).
    """
    u = url or ""
    m = re.search(r"/asset/(\d+)/(\d+)", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"[?&]auc=(\d+)", u)
    if m:
        return f"ps:{m.group(1)}"
    m = re.search(
        r"bidspotter\.com/.*/lot-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        u,
    )
    if m:
        return f"bs:{m.group(1)}"
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_cache_key.py -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/listings_db.py auction_extractors/tests/test_bidspotter_cache_key.py
git commit -m "feat(bs): extract_asset_id keys BidSpotter lot URLs as bs:<guid>"
```

---

### Task 2: Widen the trust guard to `{"llm", "structured"}` ⚠ SHARED

**Files:**
- Modify: `auction_extractors/top_chairs.py:87` (constant), `:90-106` (`trusted_quantity`), `:164-192` (`_load_from_cache` SQL)
- Test: `auction_extractors/tests/test_quantity_trust.py` (update in place)

**Interfaces:**
- Consumes: nothing new.
- Produces: `TRUSTED_QUANTITY_SOURCES: frozenset[str]` (the singular `TRUSTED_QUANTITY_SOURCE` constant is REMOVED — its only importer is this test file). `trusted_quantity(row: dict) -> int | None` now returns the int for `quantity_source` in `{"llm","structured"}`. Tasks 4 and 8 import both names.

- [ ] **Step 1: Update the trust tests (failing first)**

In `auction_extractors/tests/test_quantity_trust.py`:
replace the import line and two tests; add a serving-path test.

```python
from top_chairs import TRUSTED_QUANTITY_SOURCES, trusted_quantity
```

Replace `test_trusted_quantity_only_trusts_llm_source` with:

```python
def test_trusted_quantity_trusts_llm_and_structured_only() -> None:
    # LLM-verified and structured → the count is returned as an int.
    assert trusted_quantity({"quantity": 200, "quantity_source": "llm"}) == 200
    assert trusted_quantity({"quantity": "150", "quantity_source": "llm"}) == 150
    assert trusted_quantity({"quantity": 43, "quantity_source": "structured"}) == 43

    # Every other source is untrusted → None (never coerced to 0 / kept).
    for src in ("regex_title", "regex_fulltext", "llm_failed", "llm_missing", "", None):
        assert trusted_quantity({"quantity": 110, "quantity_source": src}) is None, src

    # Missing source key, or a trusted source with an unusable quantity → None.
    assert trusted_quantity({"quantity": 110}) is None
    assert trusted_quantity({"quantity": None, "quantity_source": "llm"}) is None
    assert trusted_quantity({"quantity": "n/a", "quantity_source": "structured"}) is None
```

Replace `test_trusted_source_constant` with:

```python
def test_trusted_source_constant() -> None:
    assert TRUSTED_QUANTITY_SOURCES == frozenset({"llm", "structured"})
```

Append a `_load_from_cache` serving test (temp SQLite via `LISTINGS_DB_PATH`):

```python
def test_load_from_cache_serves_structured_and_excludes_regex(tmp_path=None) -> None:
    """A structured BidSpotter row IS served; a regex-seeded row is NOT."""
    import tempfile
    import listings_db
    import top_chairs

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LISTINGS_DB_PATH")
        os.environ["LISTINGS_DB_PATH"] = os.path.join(td, "t.db")
        old_db_path = listings_db.DB_PATH
        listings_db.DB_PATH = type(old_db_path)(os.environ["LISTINGS_DB_PATH"])
        try:
            conn = listings_db.connect()
            listings_db.upsert_listing(conn, {
                "link": ("https://www.bidspotter.com/en-us/auction-catalogues/stone/"
                         "catalogue-id-stone-10117/lot-aaf53c9e-a9d1-4de6-a794-b477015c7b1f"),
                "title": "Ghost chairs", "description": "43 ghost chairs",
                "quantity": 43 + 60,  # comfortably over the min_quantity=50 below
                "quantity_source": "structured", "quantity_confidence": "high",
            })
            listings_db.upsert_listing(conn, {
                "link": "https://www.govdeals.com/en/asset/999/111",
                "title": "Chairs maybe", "description": "d",
                "quantity": 500, "quantity_source": "regex_title",
                "quantity_confidence": "medium",
            })
            conn.close()
            rows = top_chairs._load_from_cache("bs", min_quantity=50)
            assert [r["asset_id"] for r in rows] == ["bs:aaf53c9e-a9d1-4de6-a794-b477015c7b1f"]
            gd_rows = top_chairs._load_from_cache("gd", min_quantity=50)
            assert gd_rows == []  # regex_title row is untrusted
        finally:
            listings_db.DB_PATH = old_db_path
            if old is None:
                os.environ.pop("LISTINGS_DB_PATH", None)
            else:
                os.environ["LISTINGS_DB_PATH"] = old
```

Update the `__main__` block at the bottom to call the renamed/new tests.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_quantity_trust.py -v`
Expected: FAIL — `ImportError: cannot import name 'TRUSTED_QUANTITY_SOURCES'`.

- [ ] **Step 3: Implement in `top_chairs.py`**

Replace the constant + `trusted_quantity` (lines ~78-106; keep the BLACKWHOLE-4 comment, amend it):

```python
# Only quantities from a trusted provenance are used for ranking, the
# MIN_CHAIR_QUANTITY filter, and Telegram alerts:
#   "llm"        — verified by the LLM pass over title+description.
#   "structured" — read from a site's structured quantity field (BidSpotter
#                  exposes one; GovDeals/PublicSurplus do not).
# Regex-seeded counts (`regex_title` / `regex_fulltext`) and LLM failures
# (`llm_failed` / `llm_missing`) are NOT trusted — the regex path routinely
# misreads a lot number, spec, or model number as a chair count. Untrusted
# rows stay in the cache but are excluded from the read surface.
# (Read-side guard for BLACKWHOLE-4; widened for BidSpotter 2026-07-21.)
TRUSTED_QUANTITY_SOURCES = frozenset({"llm", "structured"})


def trusted_quantity(row: dict) -> int | None:
    """Return the row's chair quantity ONLY when its provenance is trusted.

    Returns an int when ``quantity_source`` ∈ ``TRUSTED_QUANTITY_SOURCES``
    and ``quantity`` parses, else ``None``. Callers must treat ``None`` as
    "no trustworthy count" — exclude from ranking, the qty filter, and
    alerts (never coerce to 0, never fall back to the regex number).
    """
    if (row.get("quantity_source") or "") not in TRUSTED_QUANTITY_SOURCES:
        return None
    q = row.get("quantity")
    if q is None:
        return None
    try:
        return int(q)
    except (TypeError, ValueError):
        return None
```

In `_load_from_cache` change the SQL (`quantity_source = ?` → `IN`) and params:

```python
    trusted = sorted(TRUSTED_QUANTITY_SOURCES)
    rows = conn.execute(
        """
        SELECT asset_id, link, title, description, quantity,
               quantity_source, quantity_confidence, price, location,
               pickup_zip, contact_email, contact_phone,
               end_date, time_left, image_url, last_seen_at
        FROM listings
        WHERE quantity >= ?
          AND quantity <= ?
          AND quantity_source IN (?, ?)
          AND link LIKE ?
        ORDER BY quantity DESC
        """,
        (min_quantity, _SANE_MAX_QUANTITY, trusted[0], trusted[1], f"%{frag}%"),
    ).fetchall()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_quantity_trust.py auction_extractors/tests/ -q`
Expected: trust tests PASS; the rest of the package suite still green (`test_govdeals_llm_gates.py` / `test_publicsurplus_llm_gates.py` exercise `_keep` via `trusted_quantity` — they must not regress).

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/top_chairs.py auction_extractors/tests/test_quantity_trust.py
git commit -m "feat(bs): trust guard accepts quantity_source=structured alongside llm"
```

---

### Task 3: `bs` as a valid source on the package read surface ⚠ SHARED

**Files:**
- Modify: `auction_extractors/top_chairs.py:53-57` (frags + Literal), `:164-169` (frag map), `:381-382` (validation), `:478` (CLI choices)
- Modify: `auction_extractors/__main__.py:45` (CLI choices)

**Interfaces:**
- Consumes: Task 2's guard.
- Produces: `get_top_chairs(source="bs", …)` works; `Source = Literal["gd", "ps", "bs"]`; `_BIDSPOTTER_URL_FRAG = "bidspotter.com"`. Tasks 4 and 9 rely on `"bs"` validating.

- [ ] **Step 1: Make the changes** (no new test file — the serving test from Task 2 already exercises `_load_from_cache("bs", …)`; validation is asserted below)

In `top_chairs.py`:

```python
_GOVDEALS_URL_FRAG = "govdeals.com"
_PUBLIC_SURPLUS_URL_FRAG = "publicsurplus.com"
_BIDSPOTTER_URL_FRAG = "bidspotter.com"

_SOURCE_FRAGS = {
    "gd": _GOVDEALS_URL_FRAG,
    "ps": _PUBLIC_SURPLUS_URL_FRAG,
    "bs": _BIDSPOTTER_URL_FRAG,
}

Source = Literal["gd", "ps", "bs"]
```

In `_load_from_cache`, replace the frag ternary:

```python
    frag = _SOURCE_FRAGS[source]
```

In `get_top_chairs` replace the validation + docstring source line:

```python
    if source not in _SOURCE_FRAGS:
        raise ValueError(f"source must be one of {sorted(_SOURCE_FRAGS)}, got {source!r}")
```

(docstring: `source: "gd" (GovDeals), "ps" (Public Surplus) or "bs" (BidSpotter).`)

In `top_chairs.py::main` and `__main__.py`:

```python
    ap.add_argument("--source", choices=("gd", "ps", "bs"), default="gd")
```

(`__init__.py` needs NO change — it only re-exports `get_top_chairs`.)

- [ ] **Step 2: Verify**

Run:
```bash
cd auction_extractors && ../.venv/bin/python -c "
from top_chairs import get_top_chairs
try:
    get_top_chairs(source='xx', include_condition=False)
    raise SystemExit('FAIL: xx accepted')
except ValueError as e:
    print('ok invalid rejected:', e)
print('bs rows:', len(get_top_chairs(source='bs', include_condition=False)))
" && cd ..
.venv/bin/python -m auction_extractors top --source bs --no-condition
```
Expected: `ok invalid rejected: …`, `bs rows: 0` (empty cache), and the module CLI prints `[]`.

- [ ] **Step 3: Run the package suite + commit**

Run: `.venv/bin/python -m pytest auction_extractors/tests/ -q` → all green.

```bash
git add auction_extractors/top_chairs.py auction_extractors/__main__.py
git commit -m "feat(bs): bs is a valid source in top_chairs + module CLI"
```

---

### Task 4: `automation/auctions_supabase.py` — bs source + trusted-source filter ⚠ SHARED

**Files:**
- Modify: `automation/auctions_supabase.py:32-34` (Literal + `_SOURCE_FRAG`), `:43-64` (`_load_from_supabase`), `:83-84` (validation), `:161-192` (`cache_stats`)

**Interfaces:**
- Consumes: `TRUSTED_QUANTITY_SOURCES` from `auction_extractors.top_chairs` (Task 2).
- Produces: `/api/auctions?source=bs` serves rows; `cache_stats()["by_source"]["bs"]` exists. **Behavior change (documented in spec §3):** untrusted-source rows stop being served for gd/ps too — this restores parity with the SQLite path's BLACKWHOLE-4 guard (the mirror's docstring already promises byte-for-byte parity).

- [ ] **Step 1: Implement**

```python
from auction_extractors.top_chairs import (  # noqa: E402
    CATEGORIES,
    _SANE_MAX_QUANTITY,
    TRUSTED_QUANTITY_SOURCES,
    _classify,
    _enrich_via_llm,
    _is_active,
    _is_non_chair_lot,
    _price_to_float,
)

Source = Literal["gd", "ps", "bs"]

_SOURCE_FRAG = {"gd": "govdeals.com", "ps": "publicsurplus.com", "bs": "bidspotter.com"}
```

`_load_from_supabase` query (add the trusted filter):

```python
    rows = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE quantity >= %s AND quantity <= %s
          AND quantity_source = ANY(%s)
          AND link ILIKE %s
        ORDER BY quantity DESC
        """,
        (min_quantity, _SANE_MAX_QUANTITY, list(TRUSTED_QUANTITY_SOURCES), f"%{frag}%"),
    )
```

Validation in `get_top_chairs`:

```python
    if source not in _SOURCE_FRAG:
        raise ValueError(f"source must be one of {sorted(_SOURCE_FRAG)}, got {source!r}")
```

`cache_stats` CASE — add the bs arm and its param:

```python
    by_rows = db.fetch_all(
        """
        SELECT CASE
                 WHEN link ILIKE %s THEN 'gd'
                 WHEN link ILIKE %s THEN 'ps'
                 WHEN link ILIKE %s THEN 'bs'
                 ELSE 'other'
               END AS src,
               count(*) AS n,
               max(last_seen_at) AS newest
        FROM auction_listings GROUP BY 1
        """,
        (f"%{_SOURCE_FRAG['gd']}%", f"%{_SOURCE_FRAG['ps']}%", f"%{_SOURCE_FRAG['bs']}%"),
    )
```

- [ ] **Step 2: Verify** (validation is checked before any DB access, so this needs no network)

Run:
```bash
.venv/bin/python -c "
from automation.auctions_supabase import get_top_chairs, _SOURCE_FRAG
assert _SOURCE_FRAG['bs'] == 'bidspotter.com'
try:
    get_top_chairs(source='xx')
    raise SystemExit('FAIL')
except ValueError as e:
    print('ok:', e)
"
```
Expected: `ok: source must be one of ['bs', 'gd', 'ps'] …`. Then, with `.env` present (live DB): `.venv/bin/python -c "from automation.auctions_supabase import cache_stats; print(cache_stats()['by_source'].keys())"` → keys include `gd`/`ps` (and `bs` once rows exist).

- [ ] **Step 3: Repo suite + commit**

Run: `.venv/bin/python -m pytest tests/ -q` → green (deals/web tests untouched).

```bash
git add automation/auctions_supabase.py
git commit -m "feat(bs): Supabase read layer knows bs + enforces trusted quantity sources"
```

---

### Task 5: BidSpotter card + forItem parsers (fixture-tested)

**Files:**
- Create: `auction_extractors/bidspotter_automation.py`
- Test: `auction_extractors/tests/test_bidspotter_parse.py` (new)

**Interfaces:**
- Consumes: `infer_chair_quantity_from_title(title: str) -> int` (`quantity_infer.py`).
- Produces: `_parse_search_cards(html: str) -> list[dict]` (card-dict contract below), `_parse_for_item(html: str) -> dict[str, dict]`, `_parse_total_pages(html: str) -> int`, `_text(fragment: str) -> str`, module constants `BASE_URL`, `_GUID`. Tasks 6-9 build on this module.

- [ ] **Step 1: Write the failing parse test**

```python
# auction_extractors/tests/test_bidspotter_parse.py
"""Parser guard for the BidSpotter plain-HTTP scrape path.

Fixture is a real search page saved 2026-07-21, trimmed to 3 representative
cards (see the fixture's header comment). Pure / no network.
Run standalone:  python tests/test_bidspotter_parse.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bidspotter_automation import (
    _parse_for_item,
    _parse_search_cards,
    _parse_total_pages,
)

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

with open(os.path.join(_FIXTURES, "bidspotter_search_chairs.html")) as f:
    SEARCH_HTML = f.read()

# Contract with listings_db / top_chairs — same keys the PS parser guarantees.
CARD_KEYS = {
    "title", "link", "quantity", "quantity_source", "quantity_confidence",
    "location", "price", "lot_number", "end_date", "time_left", "image_url",
}

GHOST = "aaf53c9e-a9d1-4de6-a794-b477015c7b1f"


def test_card_contract_and_structured_quantity():
    cards = _parse_search_cards(SEARCH_HTML)
    assert len(cards) == 3
    by_guid = {c["lot_guid"]: c for c in cards}

    ghost = by_guid[GHOST]
    assert CARD_KEYS - set(ghost) == set()
    assert ghost["title"] == "Ghost chairs"
    assert ghost["link"] == (
        "https://www.bidspotter.com/en-us/auction-catalogues/stone/"
        "catalogue-id-stone-10117/lot-" + GHOST)
    assert ghost["quantity"] == 43
    assert ghost["quantity_source"] == "structured"
    assert ghost["quantity_confidence"] == "high"
    assert ghost["location"] == "Belmont, North Carolina"
    assert ghost["lot_number"] == "stone-10117#93"
    assert ghost["end_date"] == "2026-07-22T15:32:00Z"   # forItem "Lot End Time UTC"
    assert ghost["image_url"].startswith("https://cdn.globalauctionplatform.com/")
    assert "?" not in ghost["image_url"]                  # resize query stripped
    assert ghost["auction_type"] == "timed"

    tuscan = by_guid["ea890a07-d99f-4a24-94e9-b477015c7b1f"]
    assert tuscan["quantity"] == 127
    assert tuscan["quantity_source"] == "structured"


def test_missing_structured_quantity_seeds_regex_title():
    cards = _parse_search_cards(SEARCH_HTML)
    eps = next(c for c in cards if c["lot_guid"].startswith("00a1c5e1"))
    # No li.quantity on this card (forItem says "1" — the ambiguous default),
    # so it must take the untrusted regex-title seed and flow to the LLM pass.
    assert eps["quantity_source"] == "regex_title"
    assert eps["location"] == "Grand Rapids, Michigan"
    assert eps["description"]        # snippet present for the LLM
    assert eps["end_date"] == "2026-08-25T16:40:00Z"


def test_for_item_json():
    fi = _parse_for_item(SEARCH_HTML)
    assert set(fi) == {
        "00a1c5e1-2b6b-4029-8bb3-b45200f057c5",
        GHOST,
        "ea890a07-d99f-4a24-94e9-b477015c7b1f",
    }
    assert fi[GHOST]["Lot Quantity"] == "43"
    assert fi[GHOST]["Lot End Time UTC"].endswith("Z")
    assert fi[GHOST]["Auction House Name"]


def test_pagination_and_garbage():
    assert _parse_total_pages(SEARCH_HTML) == 13
    assert _parse_total_pages("<html></html>") == 1
    assert _parse_search_cards("<html></html>") == []
    assert _parse_search_cards("") == []
    assert _parse_for_item("<html></html>") == {}


if __name__ == "__main__":
    test_card_contract_and_structured_quantity()
    test_missing_structured_quantity_seeds_regex_title()
    test_for_item_json()
    test_pagination_and_garbage()
    print("ok — bidspotter parse")
```

**Note:** the end-time literals were read from the committed fixture's forItem
JSON (Ghost `2026-07-22T15:32:00Z`, EPS `2026-08-25T16:40:00Z`) — verifiable via
`python3 -c "import re,json;h=open('auction_extractors/tests/fixtures/bidspotter_search_chairs.html').read();print(json.loads(re.search(r'forItem\s*=\s*(\{.*?\});',h,re.S).group(1))['aaf53c9e-a9d1-4de6-a794-b477015c7b1f'])"`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bidspotter_automation'`.

- [ ] **Step 3: Create `bidspotter_automation.py` with the parsers**

```python
#!/usr/bin/env python3
"""
bidspotter_automation.py — BidSpotter.com bulk chair scraper (source key "bs").

1. Plain HTTP (requests) against the server-rendered search pages — no browser.
   BidSpotter fronts with AWS WAF: a boring ``User-Agent: Mozilla/5.0`` passes,
   a full spoofed Chrome UA gets challenged, and any request may draw an
   HTTP 202 + ``x-amzn-waf-action`` challenge that a simple retry clears.
2. Quantity comes from BidSpotter's STRUCTURED per-lot quantity field when
   present (~80-85% of cards) → quantity_source="structured" (trusted).
   Cards without it get the regex-title seed + LLM pass, same as GD/PS.
3. Prices are not in the static HTML — one batched unauthenticated POST per
   page (reload-timed-bid-info) fills them, best-effort.
4. Same downstream pipeline as the other scrapers: cache-hydrate → LLM →
   archive-all → keep-filter → rank → Telegram.

Setup: same .env as govdeals_chairs_extraction.py (Telegram + LLM provider
vars); BidSpotter knobs are BIDSPOTTER_* (see .env.example).
"""

import html as html_lib
import json
import os
import re
import time
import urllib.parse

import requests
from dotenv import load_dotenv

from quantity_infer import infer_chair_quantity_from_title
from quantity_llm import refine_quantities_with_llm
import listings_db

load_dotenv()
_SCRIPT_DIR = os.path.dirname(__file__)
_LOCAL_ENV = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_LOCAL_ENV):
    load_dotenv(_LOCAL_ENV, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
QUANTITY_LLM_PROVIDER = (os.getenv("QUANTITY_LLM_PROVIDER") or LLM_PROVIDER).strip().lower()
try:
    OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
except ValueError:
    OLLAMA_TIMEOUT = 600

BASE_URL = "https://www.bidspotter.com"
# Confirmed from the page's own SearchBiddingInfoSettings.bidReloadInfoUrl.
BID_INFO_PATH = "/en-us/lot/reload-timed-bid-info?v=1.3.0.1&c=lotsearch"

SEARCH_TERMS = [
    t.strip()
    for t in (os.getenv("BIDSPOTTER_SEARCH_TERMS") or "chairs").split(",")
    if t.strip()
]

MIN_CHAIR_QUANTITY = 50
PAGE_SIZE = int(os.getenv("BIDSPOTTER_PAGE_SIZE", "120"))
MAX_PAGES = int(os.getenv("BIDSPOTTER_MAX_PAGES", "20"))
WAF_RETRIES = int(os.getenv("BIDSPOTTER_WAF_RETRIES", "4"))
WAF_BACKOFF_SEC = float(os.getenv("BIDSPOTTER_WAF_BACKOFF_SEC", "2"))
HTTP_DELAY_SEC = float(os.getenv("BIDSPOTTER_HTTP_DELAY_SEC", "0.5"))

# Boring UA on purpose — a full spoofed Chrome UA trips the WAF.
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}

_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Class-prefix match: real cards are `panel item ` (trailing space) or
# `panel item featured` — an exact-class match finds zero cards.
_CARD_SPLIT_RE = re.compile(r'<article class="panel item[^"]*">')
# Match the ASSIGNMENT, not the earlier `forItem: null` object member.
_FOR_ITEM_RE = re.compile(
    r"window\.gapAmplitudeConfig\.forItem\s*=\s*(\{.*?\});", re.S)
_PAGES_RE = re.compile(r'class="pagination-content"[^>]*data-pages="(\d+)"')


def _text(fragment: str) -> str:
    """Tag-strip + entity-unescape an HTML fragment to flat text."""
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _parse_total_pages(html: str) -> int:
    m = _PAGES_RE.search(html)
    return int(m.group(1)) if m else 1


def _parse_for_item(html: str) -> dict:
    """Per-lot metadata map keyed by lot GUID from the page's embedded
    amplitude config. Values are all-string dicts carrying "Lot Quantity",
    "Lot End Time UTC" (ISO UTC), "Auction House Name", etc.

    ⚠ "Lot Quantity" is "1" BOTH for genuine single-item lots AND when the
    seller left the structured field empty — never use it as the trusted
    quantity signal. The DOM ``bulk-quantity-value`` element is the
    discriminator (see _parse_search_cards).
    """
    m = _FOR_ITEM_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_search_cards(html: str) -> list:
    """Parse one search-results page into the standard card dicts.

    Card shape matches GD/PS (title, link, quantity, quantity_source,
    quantity_confidence, location, price, lot_number, end_date, time_left,
    image_url, description) plus BidSpotter extras (lot_guid, auction_type,
    currency, auction_house) consumed in-run only — listings_db ignores them.
    """
    for_item = _parse_for_item(html)
    cards = []
    for seg in _CARD_SPLIT_RE.split(html)[1:]:
        gm = re.search(r'id="lot-(%s)"' % _GUID, seg)
        if not gm:
            continue
        guid = gm.group(1)
        tm = re.search(
            r'<h3>\s*<a href="(/en-us/auction-catalogues/[^"]+)"[^>]*>([^<]+)</a>',
            seg,
        )
        if not tm:
            continue
        link = BASE_URL + tm.group(1)
        title = html_lib.unescape(tm.group(2)).strip()

        meta = for_item.get(guid) or {}

        qty = None
        qm = re.search(r"bulk-quantity-value[^>]*>\s*([\d,]+)", seg)
        if qm:
            try:
                qty = int(qm.group(1).replace(",", ""))
            except ValueError:
                qty = None

        location = ""
        lm = re.search(
            r'class="lotlocation">\s*Location:\s*<strong>([^<]+)</strong>', seg)
        if lm:
            location = html_lib.unescape(lm.group(1)).strip()

        image_url = ""
        im = re.search(r'<img id="i%s"[^>]*data-src="([^"]+)"' % guid, seg)
        if im:
            # ?h=175 is a CDN downsize — strip the query for full-res.
            image_url = im.group(1).split("?")[0]

        lot_no = ""
        nm = re.search(r'<div class="number"><span>Lot</span>\s*([^<]+)</div>', seg)
        if not nm:
            nm = re.search(r'<span class="lot-number">([^<]+)</span>', seg)
        if nm:
            lot_no = nm.group(1).strip()
        am = re.search(r'data-auction-ref="([^"]*)"', seg)
        auction_ref = am.group(1) if am else ""
        lot_number = f"{auction_ref}#{lot_no}" if (auction_ref or lot_no) else ""

        description = ""
        dm = re.search(r'<div class="description">\s*<p>(.*?)</p>', seg, re.S)
        if dm:
            description = _text(dm.group(1))[:4000]

        auction_type = (meta.get("Auction Type") or "").strip().lower()
        if not auction_type:
            atm = re.search(r'data-auction-type="([^"]*)"', seg)
            auction_type = ((atm.group(1) if atm else "") or "").lower()
        currency = (meta.get("Auction Currency") or "").strip()
        if not currency:
            cm = re.search(r'data-currency="([^"]*)"', seg)
            currency = ((cm.group(1) if cm else "") or "").strip()

        end_date = (
            meta.get("Lot End Time UTC") or meta.get("Auction End Time UTC") or ""
        ).strip()

        if qty is not None:
            quantity, q_src, q_conf = qty, "structured", "high"
        else:
            seed = infer_chair_quantity_from_title(title)
            quantity, q_src = seed, "regex_title"
            q_conf = "low" if seed == 1 else "medium"

        cards.append({
            "title": title,
            "link": link,
            "quantity": quantity,
            "quantity_source": q_src,
            "quantity_confidence": q_conf,
            "location": location,
            "price": "",
            "lot_number": lot_number,
            "end_date": end_date,
            "time_left": "",
            "image_url": image_url,
            "description": description,
            # In-run extras (not persisted by listings_db):
            "lot_guid": guid,
            "auction_type": auction_type,
            "currency": currency or "USD",
            "auction_house": (meta.get("Auction House Name") or "").strip(),
        })
    return cards
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/bidspotter_automation.py auction_extractors/tests/test_bidspotter_parse.py
git commit -m "feat(bs): BidSpotter card + forItem parsers, fixture-tested"
```

---

### Task 6: Bid-info (price) fetch, parse, apply

**Files:**
- Modify: `auction_extractors/bidspotter_automation.py` (append)
- Test: `auction_extractors/tests/test_bidspotter_parse.py` (append)

**Interfaces:**
- Consumes: `_fetch` (Task 7 — for now `_fetch_bid_info` calls `session.post` directly; Task 7 swaps it, see note).
- Produces: `_parse_bid_info(data: list) -> dict[str, dict]` (guid → unwrapped Model), `_apply_bid_info(cards: list[dict], info: dict) -> None` (mutates price/time_left/bid_count), `_format_time_left(seconds) -> str`, `_fetch_bid_info(session, guids: list[str]) -> dict`.

- [ ] **Step 1: Append the failing tests**

```python
# append to auction_extractors/tests/test_bidspotter_parse.py

from bidspotter_automation import (  # noqa: E402  (add to the existing import)
    _apply_bid_info,
    _format_time_left,
    _parse_bid_info,
)

with open(os.path.join(_FIXTURES, "bidspotter_bid_info.json")) as f:
    BID_INFO = json.load(f)


def test_bid_info_parse_and_price():
    info = _parse_bid_info(BID_INFO)          # live-captured, Model-wrapped
    assert GHOST in info
    m = info[GHOST]
    assert m["LeadingBid"] == 7.0 and m["TotalBids"] == 3

    cards = _parse_search_cards(SEARCH_HTML)
    _apply_bid_info(cards, info)
    ghost = next(c for c in cards if c["lot_guid"] == GHOST)
    assert ghost["price"] == "USD 7.00"       # LeadingBid, GovDeals-style format
    assert ghost["bid_count"] == 3
    assert ghost["time_left"]                 # derived from SecondsRemaining


def test_bid_info_zero_bids_uses_start_price_and_unwrapped_rows():
    # Synthetic zero-bid row + a bare (non-Model-wrapped) row — the site's own
    # parse handler accepts both shapes, so must we.
    data = [
        {"Model": {"LotId": "00000000-0000-4000-8000-00000000000a",
                   "TotalBids": 0, "LeadingBid": 0.0, "StartPrice": 25.0,
                   "Currency": "USD", "SecondsRemaining": 90}},
        {"LotId": "00000000-0000-4000-8000-00000000000b",
         "TotalBids": 2, "LeadingBid": 40.0, "StartPrice": 10.0,
         "Currency": "CAD", "SecondsRemaining": 4000},
    ]
    info = _parse_bid_info(data)
    card_a = {"lot_guid": "00000000-0000-4000-8000-00000000000a",
              "currency": "USD", "price": "", "time_left": ""}
    card_b = {"lot_guid": "00000000-0000-4000-8000-00000000000b",
              "currency": "CAD", "price": "", "time_left": ""}
    card_c = {"lot_guid": "ffffffff-ffff-4fff-8fff-ffffffffffff",
              "currency": "USD", "price": "", "time_left": ""}   # not in response
    _apply_bid_info([card_a, card_b, card_c], info)
    assert card_a["price"] == "USD 25.00"     # zero bids → opening price
    assert card_b["price"] == "CAD 40.00"
    assert card_c["price"] == ""              # missing lot → untouched


def test_format_time_left():
    assert _format_time_left(2998788) == "34d 16h"
    assert _format_time_left(4000) == "1h 6m"
    assert _format_time_left(0) == ""
    assert _format_time_left(None) == ""
    assert _format_time_left("junk") == ""
```

Also extend the standalone `__main__` block with the three new test calls.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: new tests FAIL with ImportError (`_parse_bid_info` undefined); old 4 still pass.

- [ ] **Step 3: Implement (append to `bidspotter_automation.py`)**

```python
def _parse_bid_info(data) -> dict:
    """Response rows → {lot_guid: model_dict}. Rows arrive wrapped as
    ``{"Model": {…}}`` (verified live + in the site's own updateSuccess
    handler); bare rows are accepted too, mirroring the site's parse()."""
    out = {}
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        model = row.get("Model") if isinstance(row.get("Model"), dict) else row
        lot_id = str(model.get("LotId") or "").lower()
        if lot_id:
            out[lot_id] = model
    return out


def _format_time_left(seconds) -> str:
    """SecondsRemaining → compact countdown ("34d 16h" / "1h 6m"), "" if unusable."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {rem // 60}m"


def _apply_bid_info(cards: list, info: dict) -> None:
    """Fill price / time_left / bid_count in place. Missing lots untouched.

    Price rule: LeadingBid when TotalBids > 0, else StartPrice (the opening
    price — a 0-bid lot's LeadingBid is not meaningful). GovDeals-style
    format: "USD 7.00"."""
    for card in cards:
        model = info.get((card.get("lot_guid") or "").lower())
        if not model:
            continue
        total_bids = model.get("TotalBids") or 0
        amount = model.get("LeadingBid") if total_bids else model.get("StartPrice")
        if amount is None:
            amount = model.get("StartPrice")
        currency = (model.get("Currency") or card.get("currency") or "USD").strip()
        if amount is not None:
            try:
                card["price"] = f"{currency} {float(amount):.2f}"
            except (TypeError, ValueError):
                pass
        try:
            card["bid_count"] = int(total_bids)
        except (TypeError, ValueError):
            card["bid_count"] = 0
        tl = _format_time_left(model.get("SecondsRemaining"))
        if tl:
            card["time_left"] = tl


def _fetch_bid_info(session, guids: list) -> dict:
    """One batched POST for a page's worth of lot GUIDs. Raises on HTTP
    failure — the caller treats prices as best-effort."""
    if not guids:
        return {}
    body = [{"LotId": g, "BidderHasBids": False} for g in guids]
    resp = _fetch(session, "post", BASE_URL + BID_INFO_PATH, json=body)
    return _parse_bid_info(resp.json())
```

(`_fetch` doesn't exist until Task 7 — that's fine: `_fetch_bid_info` isn't
called by any test in this task, only `_parse_bid_info`/`_apply_bid_info` are.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/bidspotter_automation.py auction_extractors/tests/test_bidspotter_parse.py
git commit -m "feat(bs): batched reload-timed-bid-info price fill (Model unwrap, 0-bid→StartPrice)"
```

---

### Task 7: WAF-retry fetch, session, search URL, dedup

**Files:**
- Modify: `auction_extractors/bidspotter_automation.py` (append)
- Test: `auction_extractors/tests/test_bidspotter_parse.py` (append)

**Interfaces:**
- Consumes: module constants from Task 5.
- Produces: `_session() -> requests.Session`, `_fetch(session, method, url, *, retries=None, **kwargs) -> requests.Response` (raises `RuntimeError` when the WAF challenge persists), `_search_url(term, page) -> str`, `_dedup(listings) -> list`. `/api/test-scrape` (Task 9) imports `_session`, `_fetch`, `_search_url`, `_dedup`, `_parse_search_cards`, `_parse_total_pages`.

- [ ] **Step 1: Append the failing tests**

```python
# append to auction_extractors/tests/test_bidspotter_parse.py
import bidspotter_automation as bs_mod


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeSession:
    """Yields queued responses; records how many requests were made."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


def test_waf_202_retries_then_succeeds(monkeypatch=None):
    old = bs_mod.WAF_BACKOFF_SEC
    bs_mod.WAF_BACKOFF_SEC = 0            # no sleeping in tests
    try:
        ok = _FakeResp(200)
        challenged = _FakeResp(202, {"x-amzn-waf-action": "challenge"})
        sess = _FakeSession([challenged, challenged, ok])
        resp = bs_mod._fetch(sess, "get", "https://x/", retries=3)
        assert resp is ok
        assert sess.calls == 3
    finally:
        bs_mod.WAF_BACKOFF_SEC = old


def test_waf_202_exhausted_raises():
    old = bs_mod.WAF_BACKOFF_SEC
    bs_mod.WAF_BACKOFF_SEC = 0
    try:
        challenged = _FakeResp(202, {"x-amzn-waf-action": "challenge"})
        sess = _FakeSession([challenged] * 3)
        try:
            bs_mod._fetch(sess, "get", "https://x/", retries=2)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "WAF" in str(e)
        assert sess.calls == 3                    # retries+1 attempts
    finally:
        bs_mod.WAF_BACKOFF_SEC = old


def test_plain_202_without_waf_header_is_not_retried():
    # A 202 without x-amzn-waf-action is NOT a challenge — pass it through.
    sess = _FakeSession([_FakeResp(202)])
    resp = bs_mod._fetch(sess, "get", "https://x/", retries=5)
    assert resp.status_code == 202
    assert sess.calls == 1


def test_search_url_and_dedup():
    assert bs_mod._search_url("chairs", 2) == (
        "https://www.bidspotter.com/en-us/search-results?searchTerm=chairs&page=2")
    a = {"link": "https://x/1", "title": "a"}
    b = {"link": "https://x/1", "title": "dupe of a"}
    c = {"link": "https://x/2", "title": "c"}
    assert bs_mod._dedup([a, b, c]) == [a, c]
```

Extend the standalone `__main__` block accordingly.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: new tests FAIL — `AttributeError: … has no attribute '_fetch'`.

- [ ] **Step 3: Implement (append to `bidspotter_automation.py`)**

```python
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HTTP_HEADERS)
    # Page size is cookie-driven; the pageSize URL param is ignored.
    s.cookies.set("user_preference_pagesize", str(PAGE_SIZE),
                  domain="www.bidspotter.com")
    return s


def _is_waf_challenge(resp) -> bool:
    return resp.status_code == 202 and "x-amzn-waf-action" in resp.headers


def _fetch(session, method: str, url: str, *, retries: int | None = None, **kwargs):
    """HTTP request with AWS-WAF-challenge retry.

    The WAF challenge is probabilistic: the same request can 202 once and 200
    on the next try. Retry with linear backoff (WAF_BACKOFF_SEC * attempt);
    when the challenge persists past ``retries`` attempts, raise RuntimeError
    so the per-term error isolation catches it. Any other non-2xx raises via
    raise_for_status."""
    attempts = (WAF_RETRIES if retries is None else retries) + 1
    for attempt in range(attempts):
        resp = session.request(method, url, timeout=30, **kwargs)
        if _is_waf_challenge(resp):
            if attempt < attempts - 1 and WAF_BACKOFF_SEC > 0:
                time.sleep(WAF_BACKOFF_SEC * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"AWS WAF challenge persisted after {attempts} attempts: {url}")


def _search_url(term: str, page: int) -> str:
    q = urllib.parse.urlencode({"searchTerm": term, "page": page})
    return f"{BASE_URL}/en-us/search-results?{q}"


def _dedup(listings: list) -> list:
    """One card per lot, keyed by link (then lot_number / title) — same as
    the GD/PS dedup helpers."""
    dedup = {}
    for item in listings:
        key = item.get("link") or item.get("lot_number") or item.get("title")
        if key not in dedup:
            dedup[key] = item
    return list(dedup.values())
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest auction_extractors/tests/test_bidspotter_parse.py -v`
Expected: 11 PASSED.

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/bidspotter_automation.py auction_extractors/tests/test_bidspotter_parse.py
git commit -m "feat(bs): WAF-202 retry fetch, cookie session, search URL, dedup"
```

---

### Task 8: `scrape_listings` + `main()` pipeline (mock mode, rank, Telegram)

**Files:**
- Modify: `auction_extractors/bidspotter_automation.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 5-7; `refine_quantities_with_llm(listings, *, provider, ollama_base_url, ollama_model, ollama_timeout, groq_api_key, openai_api_key, gemini_api_key=None) -> list[dict]` (returns COPIES — stitch back); `listings_db.hydrate_from_cache(listings) -> (hits, misses, relists)`; `listings_db.store_listings(listings) -> dict`; `top_chairs._classify` / `_is_non_chair_lot` / `trusted_quantity` (Task 2).
- Produces: runnable script; `main()`; `MOCK_LISTINGS`. Task 9's `_SCRAPE_SCRIPTS["bs"]` and Task 12's cron lines invoke it.

- [ ] **Step 1: Append the pipeline code**

```python
def scrape_listings() -> list:
    print("[1] scrape via plain HTTP (BidSpotter search pages, no browser)")
    listings = []
    session = _session()
    for term in SEARCH_TERMS:
        term_listings = []
        print(f"\n → Filter: '{term}'")
        # Per-term isolation: a persistent WAF block on one term must not
        # kill the remaining terms (mirrors the PS scraper).
        try:
            page = 1
            total_pages = 1
            while page <= min(total_pages, MAX_PAGES):
                resp = _fetch(session, "get", _search_url(term, page))
                if page == 1:
                    total_pages = _parse_total_pages(resp.text)
                page_cards = _parse_search_cards(resp.text)
                if not page_cards:
                    if page == 1:
                        print(f"   No results for '{term}'.")
                    break
                # Prices are skeleton-loaded client-side — one batched POST
                # per page fills them. Best-effort: a failure keeps the cards
                # (they archive fine with price="").
                try:
                    info = _fetch_bid_info(
                        session, [c["lot_guid"] for c in page_cards])
                    _apply_bid_info(page_cards, info)
                except Exception as e:  # noqa: BLE001
                    print(f"   • bid-info failed on page {page}: {e} "
                          "(prices left empty)")
                term_listings.extend(page_cards)
                print(f"   Page {page}: {len(page_cards)} listings "
                      f"(of {total_pages} page(s))")
                page += 1
                if HTTP_DELAY_SEC > 0:
                    time.sleep(HTTP_DELAY_SEC)
        except Exception as e:  # noqa: BLE001
            print(f"   • '{term}' failed ({e}); skipping to next term.")
        listings.extend(term_listings)
        print(f"   Filter '{term}' total: {len(term_listings)} listings")
    unique = _dedup(listings)
    print(f"\n[1] Done scrape_listings(); {len(unique)} unique lots "
          f"(from {len(listings)} across {len(SEARCH_TERMS)} terms)")
    return unique


def rank_with_llm(listings):
    """Deterministic sort by quantity desc, price asc — name kept for parity
    with the GD/PS scrapers (the LLM never ranked; sorting is free)."""
    print("[2] Ranking listings (deterministic sort by quantity desc, price asc)…")
    return _fallback_rank(listings)


def _fallback_rank(listings):
    def _price_for_sort(p):
        s = str(p or "")
        digits = "".join(c for c in s if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else float("inf")
        except ValueError:
            return float("inf")

    safe = [x for x in listings if isinstance(x, dict)]
    safe.sort(key=lambda x: (-int(x.get("quantity") or 0),
                             _price_for_sort(x.get("price"))))
    for i, item in enumerate(safe, 1):
        item["rank"] = i
    return safe


def _format_output(listings):
    lines = ["🪑 BidSpotter chairs (bulk lots), ranked by quantity\n"]
    for item in listings:
        qty = item.get("quantity", "?")
        src = item.get("quantity_source") or ""
        tag = {"structured": "📋", "llm": "🤖"}.get(src, "")
        end_date = item.get("end_date") or "N/A"
        time_left = item.get("time_left") or "N/A"
        lines.append(
            f"#{item['rank']}: {item['title']}\n"
            f"Qty: {qty} {tag} · {item.get('price') or 'N/A'}\n"
            f"Ends: {end_date} · Time left: {time_left}\n"
            f"↗ {item['link']}\n"
        )
    return "\n".join(lines)


def _alert_on_quantity_degradation(listings: list, *, provider: str) -> None:
    """Telegram-alert when the LLM quantity pass failed for some rows —
    silent regex fallback is how big lots get the wrong count. Structured
    rows never route through the LLM, so they can't appear here."""
    degraded = [
        it for it in listings
        if it.get("quantity_source") in ("llm_failed", "llm_missing")
    ]
    if not degraded or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    reason = next(
        (it.get("quantity_error") for it in degraded if it.get("quantity_error")),
        "unknown")
    text = (
        f"⚠️ BidSpotter scrape: quantity LLM ({provider}) FAILED on "
        f"{len(degraded)}/{len(listings)} lots — falling back to regex, "
        f"counts may be wrong. First error: {str(reason)[:160]}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f" → Telegram degradation-alert error: {e}")


def send_telegram(listings):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" → Telegram not configured (set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID); skipping.")
        return
    print("[3a] Sending to Telegram…")
    body = _format_output(listings)
    if not body or not body.strip():
        body = "BidSpotter automation: no listings, test message."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 4000
    if len(body) > max_len:
        body = body[: max_len - 20] + "\n…(truncated)"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": body,
                  "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        print(" → Telegram message sent.")
    except Exception as e:  # noqa: BLE001
        print(f" → Telegram error: {e}")


def _llm_quantity_enabled() -> bool:
    return os.getenv("USE_LLM_QUANTITY", "1") == "1"


def _include_live() -> bool:
    """Whether `live`-auction lots may reach ranking/alerts. They are always
    ARCHIVED either way; their prices can't be polled, so default-include is
    harmless but tunable."""
    return os.getenv("BIDSPOTTER_INCLUDE_LIVE", "1") == "1"


MOCK_LISTINGS = [
    {
        "title": "Lot of stackable banquet chairs",
        "link": (f"{BASE_URL}/en-us/auction-catalogues/mockhouse/"
                 "catalogue-id-mock-10001/lot-00000000-0000-4000-8000-000000000001"),
        "quantity": 120, "quantity_source": "structured",
        "quantity_confidence": "high",
        "location": "Grand Rapids, Michigan", "price": "USD 50.00",
        "lot_number": "mock-10001#12",
        "end_date": "2027-01-01T16:40:00Z", "time_left": "34d 16h",
        "image_url": "", "description": "120 stackable banquet chairs",
        "lot_guid": "00000000-0000-4000-8000-000000000001",
        "auction_type": "timed", "currency": "USD", "auction_house": "Mock House",
    },
    {
        "title": "Banquet chairs, various",
        "link": (f"{BASE_URL}/en-us/auction-catalogues/mockhouse/"
                 "catalogue-id-mock-10001/lot-00000000-0000-4000-8000-000000000002"),
        "quantity": 1, "quantity_source": "regex_title",
        "quantity_confidence": "low",
        "location": "", "price": "", "lot_number": "mock-10001#13",
        "end_date": "2027-01-01T16:45:00Z", "time_left": "",
        "image_url": "", "description": "Approximately 75 padded banquet chairs.",
        "lot_guid": "00000000-0000-4000-8000-000000000002",
        "auction_type": "timed", "currency": "USD", "auction_house": "Mock House",
    },
]


def main():
    import sys

    use_test_data = os.getenv("RUN_TEST") == "1" or "--test" in sys.argv
    print("=== BidSpotter chairs extraction ===")
    if use_test_data:
        print("(Test mode: using mock listings, no scrape)")
        listings = [dict(x) for x in MOCK_LISTINGS]
    else:
        listings = scrape_listings()
    if not listings:
        print("No listings found. Exiting.")
        return

    # [1b] Cache hydration — ONLY rows without a structured quantity. A fresh
    # structured count must never be clobbered by a stale cached one (sellers
    # edit quantities); hydration exists to spare the LLM pass on lots a
    # previous run already resolved.
    unstructured = [x for x in listings
                    if x.get("quantity_source") != "structured"]
    if unstructured:
        hits, _, relists = listings_db.hydrate_from_cache(unstructured)
        print(f"[1b] Cache hydration: {hits} hit(s) on {len(unstructured)} "
              f"unstructured lot(s), {relists} relist(s).")
    else:
        print("[1b] Every lot carries a structured quantity; no hydration needed.")

    # [1d] LLM pass — only rows still lacking a trusted quantity.
    # refine_quantities_with_llm returns COPIES, so stitch back by index.
    if _llm_quantity_enabled():
        idx = [i for i, x in enumerate(listings)
               if x.get("quantity_source") not in ("structured", "llm")]
        if idx:
            print(f"[1d] Inferring quantities with LLM for {len(idx)} lot(s) "
                  "missing a structured count…")
            refined = refine_quantities_with_llm(
                [listings[i] for i in idx],
                provider=QUANTITY_LLM_PROVIDER,
                ollama_base_url=OLLAMA_BASE_URL,
                ollama_model=OLLAMA_MODEL,
                ollama_timeout=OLLAMA_TIMEOUT,
                groq_api_key=GROQ_API_KEY,
                openai_api_key=OPENAI_API_KEY,
                gemini_api_key=GEMINI_API_KEY,
            )
            for i, row in zip(idx, refined):
                listings[i] = row
            _alert_on_quantity_degradation(listings, provider=QUANTITY_LLM_PROVIDER)
        else:
            print("[1d] Nothing to refine — quantities all structured or cached-LLM.")
    else:
        print("[1d] USE_LLM_QUANTITY=0 — unstructured lots keep untrusted regex seeds.")

    # [1e] Archive EVERY processed listing before the quantity filter, so even
    # small lots are remembered (mirrors GD/PS).
    cache_counts = listings_db.store_listings(listings)
    if cache_counts.get("disabled"):
        print(f"[1e] Cache disabled; {cache_counts['disabled']} listings not stored.")
    else:
        print(
            f"[1e] Cache: +{cache_counts['insert']} new, "
            f"~{cache_counts['update']} updated, "
            f"{cache_counts['skip']} skipped (uncacheable URL)."
        )

    # Keep-filter: banquet/event chairs over MIN_CHAIR_QUANTITY with a TRUSTED
    # count; medical gated by INCLUDE_MEDICAL; live auctions gated by
    # BIDSPOTTER_INCLUDE_LIVE. Cache already has every row.
    from top_chairs import _classify, _is_non_chair_lot, trusted_quantity
    include_medical = os.getenv("INCLUDE_MEDICAL") == "1"
    include_live = _include_live()

    def _keep(item: dict) -> bool:
        if not include_live and (item.get("auction_type") or "") == "live":
            return False
        title = item.get("title") or ""
        cat, _ = _classify(title, item.get("description"))
        if cat == "medical":
            return include_medical
        if _is_non_chair_lot(title):
            return False
        q = trusted_quantity(item)
        return q is not None and q > MIN_CHAIR_QUANTITY

    listings = [item for item in listings if _keep(item)]
    if not listings:
        print(f"No banquet chairs with quantity > {MIN_CHAIR_QUANTITY}. Exiting.")
        return
    print(f" → {len(listings)} banquet-chair listings kept (qty > {MIN_CHAIR_QUANTITY})")
    ranked = rank_with_llm(listings)
    if not ranked:
        print("Ranking failed or returned empty. Exiting.")
        return
    send_telegram(ranked)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("(Telegram not configured; set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID to receive output.)")
    print("=== Script complete ===")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Mock-mode smoke (no network, no LLM)**

Run:
```bash
cd auction_extractors && LISTINGS_DB_PATH=/tmp/bs_smoke.db USE_LLM_QUANTITY=0 \
  ../.venv/bin/python bidspotter_automation.py --test; cd ..
```
Expected output includes: `[1b] Cache hydration: 0 hit(s) on 1 unstructured lot(s)`, `[1d] USE_LLM_QUANTITY=0 …`, `[1e] Cache: +2 new`, ` → 1 banquet-chair listings kept` (the structured qty-120 mock passes; the regex-seed mock is untrusted and dropped), `[2] Ranking…`, Telegram skip line.
Then verify the archive keyed correctly:
```bash
sqlite3 /tmp/bs_smoke.db "SELECT asset_id, quantity, quantity_source FROM listings ORDER BY asset_id"
```
Expected: two rows keyed `bs:00000000-…-000000000001|120|structured` and `…0002|1|regex_title`.

- [ ] **Step 3: Full package suite**

Run: `.venv/bin/python -m pytest auction_extractors/tests/ -q` → all green.

- [ ] **Step 4: Commit**

```bash
git add auction_extractors/bidspotter_automation.py
git commit -m "feat(bs): scrape_listings + main() pipeline (hydrate/LLM-subset/archive/filter/rank/telegram)"
```

---

### Task 9: `automation/web/app.py` — scrape runner, APIs, favorites ⚠ SHARED

**Files:**
- Modify: `automation/web/app.py:1300-1304` (`_SCRAPE_SCRIPTS`/`_SCRAPE_LABELS`), `:1439-1460` (`_run_scraper` steps + `--test` flag), `:1573-1575` (`/api/scrape/start` validation), `:1649-1650` (`/api/auctions` validation), `:1711-1748` (`_test_scrape_sync`), `:1756-1757` (`/api/test-scrape` validation), `:1769-1783` (`_asset_id_from_link`), `:1992-2081` (`/api/listings` source filter + `_source_of`)

**Interfaces:**
- Consumes: `bidspotter_automation` module functions `_session`, `_fetch`, `_search_url`, `_parse_search_cards`, `_parse_total_pages`, `_dedup` (Tasks 5-7); `auctions_supabase.get_top_chairs(source="bs")` (Task 4).
- Produces: `POST /api/scrape/start {"source":"bs"}` runs the new script; `"both"` now runs gd → ps → bs; `GET /api/auctions?source=bs`, `GET /api/test-scrape?source=bs`, `GET /api/listings?source=bs` work; BidSpotter links can be starred.

- [ ] **Step 1: Make the edits**

```python
_SCRAPE_SCRIPTS = {
    "gd": "govdeals_chairs_extraction.py",
    "ps": "public_surplus_automation.py",
    "bs": "bidspotter_automation.py",
}
_SCRAPE_LABELS = {"gd": "GovDeals", "ps": "Public Surplus", "bs": "BidSpotter"}
```

`_run_scraper` (line ~1439-1441) — `"both"` = all configured scrapers (decision documented in the spec: key kept for back-compat, semantics extended):

```python
async def _run_scraper(source: str, test_mode: bool) -> None:
    """Run one or all scrapers sequentially (gd → ps → bs when source='both')."""
    steps: list[str] = ["gd", "ps", "bs"] if source == "both" else [source]
```

Test-flag pass-through (line ~1459): the BS script supports `--test` like PS:

```python
        if step in ("ps", "bs") and test_mode:
            cmd.append("--test")
```

`/api/scrape/start` (line ~1574):

```python
    if source not in ("gd", "ps", "bs", "both"):
        raise HTTPException(400, "source must be 'gd', 'ps', 'bs', or 'both'")
```

`/api/auctions` (line ~1649):

```python
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
```

`_test_scrape_sync` — insert a `bs` branch between the `gd` branch and the PS `else` (turn the `else` into `elif source == "ps":` … keep its body; add a final `else: raise ValueError(source)`):

```python
    elif source == "bs":
        import bidspotter_automation as bs
        session = bs._session()
        cards = []
        for page in range(1, pages + 1):
            resp = bs._fetch(session, "get", bs._search_url(q, page))
            page_cards = bs._parse_search_cards(resp.text)
            if not page_cards:
                break
            cards.extend(page_cards)
            if page >= bs._parse_total_pages(resp.text):
                break
        cards = bs._dedup(cards)
```

`/api/test-scrape` (line ~1756):

```python
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
```

`_asset_id_from_link` — add the bs pattern (without it, starring a BidSpotter
card 400s / renders no star button):

```python
def _asset_id_from_link(link: str) -> str:
    """Lift the auction_extractors helper inline to avoid an import cycle.

    GovDeals: ``/asset/<a>/<b>`` → ``"<a>/<b>"``.
    PublicSurplus: ``?auc=<n>`` → ``"ps:<n>"``.
    BidSpotter: ``bidspotter.com/…/lot-<guid>`` → ``"bs:<guid>"``.
    """
    if not link:
        return ""
    m = re.search(r"/asset/(\d+)/(\d+)", link)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"[?&]auc=(\d+)", link)
    if m:
        return f"ps:{m.group(1)}"
    m = re.search(
        r"bidspotter\.com/.*/lot-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        link,
    )
    if m:
        return f"bs:{m.group(1)}"
    return ""
```

`/api/listings` (line ~1994 docstring `'all' | 'gd' | 'ps' | 'bs'`; ~2021-2024 filter; ~2075-2078 `_source_of`):

```python
    if source == "gd":
        where.append("link LIKE '%govdeals.com%'")
    elif source == "ps":
        where.append("link LIKE '%publicsurplus.com%'")
    elif source == "bs":
        where.append("link LIKE '%bidspotter.com%'")
```

```python
    def _source_of(link: str) -> str:
        if "govdeals.com" in link: return "gd"
        if "publicsurplus.com" in link: return "ps"
        if "bidspotter.com" in link: return "bs"
        return "other"
```

- [ ] **Step 2: Verify (server smoke)**

Restart the dashboard (`python -m automation.web` — the FastAPI process caches modules), then:

```bash
curl -s "http://127.0.0.1:8765/api/auctions?source=bs&n=5&condition=0" | head -c 300
curl -s "http://127.0.0.1:8765/api/auctions?source=zz&n=5" | head -c 200   # → 400
curl -s "http://127.0.0.1:8765/api/listings?source=bs&limit=5" | head -c 300
curl -s -X POST http://127.0.0.1:8765/api/scrape/start -H 'Content-Type: application/json' -d '{"source":"bs","test":true}'
```
Expected: first returns `{"items": []…}` (empty until a real scrape), second `{"detail":"source must be 'gd', 'ps', or 'bs'"}`, third `{"items": [], "total": 0…}`, fourth `{"ok":true,"source":"bs","test_mode":true}` and the SCRAPE strip shows the BS mock run finishing with `[BidSpotter exit 0]` then the Supabase sync.

- [ ] **Step 3: Repo suite + commit**

Run: `.venv/bin/python -m pytest tests/ -q` → green.

```bash
git add automation/web/app.py
git commit -m "feat(bs): dashboard APIs + scrape runner know BidSpotter (both = gd→ps→bs)"
```

---

### Task 10: `index.html` — source selectors + scrape menu ⚠ SHARED

**Files:**
- Modify: `automation/web/templates/index.html:176-178` (Auctions `#auc-source`), `:215-220` (scrape dropdown), `:549-552` (Listings-DB `#ldb-source`), `:654-658` (Test-Scrape `#ts-source`)

**Interfaces:**
- Consumes: Task 9's endpoints.
- Produces: the buttons Task 11's JS handlers read (`data-value="bs"`, `data-scrape="bs"`).

- [ ] **Step 1: Edits**

Auctions source seg (line ~176):

```html
      <div class="seg" id="auc-source">
        <button type="button" class="seg-btn active" data-value="gd">GovDeals</button>
        <button type="button" class="seg-btn" data-value="ps">Public Surplus</button>
        <button type="button" class="seg-btn" data-value="bs">BidSpotter</button>
      </div>
```

Scrape dropdown (line ~215-220) — add BidSpotter, relabel Both:

```html
        <div class="dropdown-menu" hidden>
          <button type="button" data-scrape="gd">GovDeals</button>
          <button type="button" data-scrape="ps">Public Surplus</button>
          <button type="button" data-scrape="bs">BidSpotter</button>
          <button type="button" data-scrape="both">All (gd → ps → bs)</button>
          <button type="button" data-scrape="ps-test" class="dropdown-sep">Public Surplus (test)</button>
        </div>
```

Listings-DB source seg (line ~549):

```html
      <div class="seg" id="ldb-source">
        <button type="button" class="seg-btn active" data-value="all">All</button>
        <button type="button" class="seg-btn" data-value="gd">GovDeals</button>
        <button type="button" class="seg-btn" data-value="ps">Public Surplus</button>
        <button type="button" class="seg-btn" data-value="bs">BidSpotter</button>
      </div>
```

Test-Scrape source seg (line ~654):

```html
      <div class="seg" id="ts-source">
        <button type="button" class="seg-btn active" data-value="both">All</button>
        <button type="button" class="seg-btn" data-value="gd">GovDeals</button>
        <button type="button" class="seg-btn" data-value="ps">Public Surplus</button>
        <button type="button" class="seg-btn" data-value="bs">BidSpotter</button>
      </div>
```

- [ ] **Step 2: Verify + commit** (full visual check happens in Task 11's step 2 — templates are inert without the JS changes)

Restart the server; open `http://127.0.0.1:8765/admin` → the three segments show the new buttons.

```bash
git add automation/web/templates/index.html
git commit -m "feat(bs): BidSpotter in Auctions/Listings-DB/Test-Scrape source selectors + scrape menu"
```

---

### Task 11: `app.js` + `app.css` — names, star support, pills ⚠ SHARED

**Files:**
- Modify: `automation/web/static/app.js:549-556` (`_assetIdFromLink`), `:925-929` (empty-cache message), `:1910` (Listings-DB pill class), `:2044` (test-scrape `both` fan-out), `:2062` + `:2111` (source-name ternaries)
- Modify: `automation/web/static/app.css:1133-1134` (`.src-pill`), `:1250` (`.ts-source-pill`)

**Interfaces:**
- Consumes: Task 10's buttons (the existing generic `.seg-btn` click handlers pick them up — no new listeners needed).
- Produces: a shared `SOURCE_NAMES` const. **No Launch-button change:** gating is already an allow-list (`link.includes('govdeals.com')` at line ~953; queue-all filter at ~759; Listings-DB `r.source === 'gd'` at ~1930) — BidSpotter links come out disabled with the "Pipeline only supports GovDeals URLs" tooltip automatically.

- [ ] **Step 1: Edits**

Add once, directly above `_assetIdFromLink` (line ~549):

```js
const SOURCE_NAMES = { gd: 'GovDeals', ps: 'Public Surplus', bs: 'BidSpotter' };
```

`_assetIdFromLink` — add the bs pattern (enables the ★ star on BidSpotter cards):

```js
function _assetIdFromLink(link) {
  if (!link) return '';
  let m = link.match(/\/asset\/(\d+)\/(\d+)/);
  if (m) return `${m[1]}/${m[2]}`;
  m = link.match(/[?&]auc=(\d+)/);
  if (m) return `ps:${m[1]}`;
  m = link.match(/bidspotter\.com\/.*\/lot-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/);
  if (m) return `bs:${m[1]}`;
  return '';
}
```

Empty-cache message (line ~928):

```js
      ? `Cache is empty for ${SOURCE_NAMES[auc.source] || auc.source}. Hit ⟳ scrape now to populate.`
```

Listings-DB pill class (line ~1910):

```js
    const srcClass = ['gd', 'ps', 'bs'].includes(r.source) ? `src-${r.source}` : 'src-other';
```

Test-scrape fan-out (line ~2044):

```js
  const sources = _ts.source === 'both' ? ['gd', 'ps', 'bs'] : [_ts.source];
```

Test-scrape name ternaries (lines ~2062 and ~2111) — both become:

```js
      const name = SOURCE_NAMES[sources[i]] || sources[i];
```
```js
  const srcName = SOURCE_NAMES[it._source] || it._source;
```

`app.css` — after line 1134 and after line 1250:

```css
.src-pill.src-bs    { color: var(--accent-2); }
```
```css
.ts-source-pill[data-source="bs"] { border-color: var(--accent-2); color: var(--accent-2); }
```

- [ ] **Step 2: Verify**

Restart the server, hard-reload `/admin`:
1. 04 Auctions → BidSpotter segment selected → grid shows "Cache is empty for BidSpotter…" (until a scrape).
2. `⟳ scrape now ▾` lists BidSpotter + "All (gd → ps → bs)".
3. 08 Test Scrape → source BidSpotter, keyword `chairs`, 1 page → live cards render with the `bs` pill; "All" fans out to three requests.
4. 07 Listings DB → BidSpotter filter returns 0 rows (until a scrape) without errors.

- [ ] **Step 3: Commit**

```bash
git add automation/web/static/app.js automation/web/static/app.css
git commit -m "feat(bs): front-end source names, bs star support, pills"
```

---

### Task 12: Env docs + cron scripts

**Files:**
- Modify: `auction_extractors/.env.example` (append)
- Modify: `scripts/run_discovery.sh` (add BS step before the transfer)
- Modify: `scripts/daily_scrape.sh` (add BS step)

**Interfaces:**
- Consumes: the finished scraper (Task 8).
- Produces: BidSpotter rows keep flowing on the daily crons. Without this, `/api/auctions` `max_stale_days=2` staleness hides every BS row two days after a manual scrape (this is exactly how PS rows went invisible before PR #41 — see the comment block inside `run_discovery.sh`).

- [ ] **Step 1: `.env.example`** — append:

```bash
# BidSpotter (bidspotter_automation.py) — plain HTTP, no browser.
# Comma-separated search terms (default: chairs)
# BIDSPOTTER_SEARCH_TERMS=chairs
# Cards per page via the pagesize cookie (site honors 120)
# BIDSPOTTER_PAGE_SIZE=120
# Hard cap on pages per term (data-pages is read from each response)
# BIDSPOTTER_MAX_PAGES=20
# Polite delay between page fetches (seconds)
# BIDSPOTTER_HTTP_DELAY_SEC=0.5
# AWS-WAF 202-challenge retries + linear backoff base (seconds)
# BIDSPOTTER_WAF_RETRIES=4
# BIDSPOTTER_WAF_BACKOFF_SEC=2
# Keep `live`-auction lots in ranking/alerts (they are always archived;
# their prices cannot be polled). 0 = timed-only alerts.
# BIDSPOTTER_INCLUDE_LIVE=1
```

- [ ] **Step 2: `scripts/run_discovery.sh`** — insert between the Public Surplus block and the transfer step (same non-fatal pattern as PS; plain HTTP, so the Chromium-less cloud image is fine):

```sh
# BidSpotter is plain-HTTP (AWS WAF handled by in-script retries) — safe in
# this Chromium-less image. Non-fatal: a BS failure must never cost us the
# GovDeals/PS sync below.
echo "[discovery] BidSpotter scrape -> staging DB"
python auction_extractors/bidspotter_automation.py \
  || echo "[discovery] BidSpotter scrape FAILED — continuing without BS rows"
```

- [ ] **Step 3: `scripts/daily_scrape.sh`** — after the Public Surplus block:

```sh
  echo "--- BidSpotter ---"
  "$PY" bidspotter_automation.py || fail=$((fail + 1))
  echo ""
```

- [ ] **Step 4: Verify + commit**

Run: `sh -n scripts/run_discovery.sh && bash -n scripts/daily_scrape.sh && echo syntax-ok`
Expected: `syntax-ok`.

```bash
git add auction_extractors/.env.example scripts/run_discovery.sh scripts/daily_scrape.sh
git commit -m "feat(bs): env docs + BidSpotter step in discovery/daily cron scripts"
```

---

### Task 13: Integration verification (final gate)

**Files:** none (verification only).

- [ ] **Step 1: Both test suites**

```bash
.venv/bin/python -m pytest auction_extractors/tests/ -q
.venv/bin/python -m pytest tests/ -q
```
Expected: all green.

- [ ] **Step 2: One real 1-page scrape** (network; LLM off so nothing is spent — structured rows are trusted regardless)

```bash
cd auction_extractors && BIDSPOTTER_MAX_PAGES=1 USE_LLM_QUANTITY=0 \
  ../.venv/bin/python bidspotter_automation.py; cd ..
```
Expected: `[1] … Page 1: ~60-120 listings`, `[1e] Cache: +N new`, kept-count > 0 if any structured lot exceeds 50 (fine if 0 kept — archiving is the point), exit 0. If every request dies with `AWS WAF challenge persisted`, raise `BIDSPOTTER_WAF_RETRIES` and retry once before investigating.

- [ ] **Step 3: Read-surface smoke**

```bash
.venv/bin/python -m auction_extractors top --source bs --no-condition --min-qty 10 --include-expired
sqlite3 auction_extractors/state/listings.db \
  "SELECT count(*), sum(quantity_source='structured') FROM listings WHERE asset_id LIKE 'bs:%'"
.venv/bin/python scripts/transfer_listings_to_supabase.py
.venv/bin/python -c "
from automation.auctions_supabase import get_top_chairs, cache_stats
print(cache_stats()['by_source'].get('bs'))
print(len(get_top_chairs(source='bs', min_quantity=10, include_condition=False, active_only=False)))
"
```
Expected: the CLI prints JSON rows (all `quantity_source` trusted); the SQLite count matches the scrape; after transfer, `cache_stats()['by_source']['bs']` is non-null and the Supabase read returns rows.

- [ ] **Step 4: Dashboard end-to-end**

Restart `python -m automation.web`, open `/admin` → 04 Auctions → BidSpotter: cards render (image, qty ×, price, ⏱, disabled ▶ launch with the GovDeals-only tooltip, working ★ star). Set `MIN QTY` to 10 if the default 50 filters everything.

- [ ] **Step 5: Final commit** (only if verification produced fixes)

```bash
git add -A && git commit -m "fix(bs): integration-verification fixes"
```

---

## Self-Review (done)

1. **Spec coverage:** cache key → T1; trust widening (incl. `_load_from_cache` SQL) → T2; source plumbing package-side → T3; Supabase mirror + trusted filter + cache_stats → T4; parsers/forItem/pagination → T5; bid-info prices → T6; WAF/session/URL/dedup → T7; pipeline (hydrate-subset, LLM-stitch, archive-first, keep-filter, live-gate, rank, Telegram, mocks, stage prefixes) → T8; app.py (scripts/labels/validations/both→3/test flag/test-scrape branch/favorites/listings) → T9; templates → T10; JS/CSS (names/star/pills/fan-out; no Launch change needed) → T11; env + crons → T12; integration → T13. Transfer script: verified no-change (spec §3), no task needed.
2. **Placeholder scan:** none — every step carries runnable code/commands; the one deliberate implementer-check (fixture end-time values in T5) includes the exact command to resolve it.
3. **Type consistency:** `TRUSTED_QUANTITY_SOURCES` (T2) consumed in T4/T8; `_parse_search_cards`/`_parse_total_pages`/`_session`/`_fetch`/`_search_url`/`_dedup` names match between T5-T7 definitions and T8/T9 call sites; `bs:<guid>` regex identical in T1 (py), T9 (py), T11 (js).

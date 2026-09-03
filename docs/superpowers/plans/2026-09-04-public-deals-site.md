# Public Deals Site ("Surplus Radar") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the admin-only Deals tab into a public, chair-buyer-safe, paginated GovDeals browser at `black-whole.com/deals` with quantity + `$/unit` (the thing GovAuctions lacks) and a portfolio landing section — while cleaning up the A/B tab, fixing the slow Inventory tab, and reshaping the admin Deals tab (search on top, filters on the side, real pager).

**Architecture:** One FastAPI process, one new server-side read module (`automation/web/public_deals.py`) that wraps the existing `deals_query.build_where` with an exclusion policy (seating categories + seating keywords + every lot the operator has starred/tracked/listed) and serves offset-paged JSON under `/deals/api/*` (outside the `/api/` auth prefix). The public page is its own template + CSS + JS (no storefront chrome, no auction photos, no verdicts, no distance-from-home). Two small partial indexes make active-set queries index-driven. Quantity is parsed from the title via the operator's own `auction_extractors.quantity_infer.explicit_title_quantity` (reused, not re-written) and folded into `deals_query.enrich` so both admin and public get `quantity` / `unit_bid` / `unit_landed`.

**Tech Stack:** Python 3.11, FastAPI + Jinja2, psycopg via `automation/db.py` (Supabase Postgres), vanilla JS/CSS, Leaflet via the existing `static/admin_map.js`, pytest (`.venv/bin/python -m pytest`).

**Spec:** the operator's 2026-09-03 note (quoted in the task that spawned this plan) + `docs/govauctions-feature-map.md` + `docs/claude-reference/deals.md`. No separate design doc — this plan carries the design decisions inline (see "Decisions" below).

## Global Constraints

- DB access ONLY via `from automation import db` / `from .. import db` (`connect`, `fetch_one`, `fetch_all`). `%s` placeholders only. No `psycopg.connect` in repo code.
- Schema changes = a file under `scripts/sql/` applied by hand + DDL mirrored in `docs/claude-reference/data-model.md` (workspace) in the same commit. **No new columns or tables in this plan** — the DB is at 501 MB (free-tier read-only trips at 500 MB; `default_transaction_read_only` was still `off` on 2026-09-04). Only two small partial indexes are added.
- RLS is disabled on `deal_lots`/`deal_verdicts` (verified 2026-09-04; `inventory` has it on). The public surface is **server-side only**: the anon key is never used by `automation/web`, every public read goes through the pooler role in FastAPI. No RLS work is needed for this plan; do not add a client-side Supabase call anywhere.
- **Never render source auction photos publicly** (`hero_image_url`, `archived_hero_url`, `gallery_urls`) — copyright. Public rows never SELECT those columns; the archived-lot viewer hides its gallery unless an operator session cookie is present.
- **Chair-buyer isolation:** the public surface must never show `canonical_category = 'seating_furniture'`, any title matching the seating keyword list, or any lot in `tracked_lots` / `auction_favorites` / `deal_list_items`. Policy lives in ONE place: `automation/web/public_deals.py`.
- Public rows also never carry: `description`, `distance_mi` (derived from `DEALS_HOME_LAT/LNG`), verdict fields (`est_resale`, `margin_pct`, `rank_score`, `comps`), `seller`, `high_bidder`, `raw`.
- Auth middleware (`automation/web/auth.py`) protects `/admin`, `/api/`, `/screenshot/`. Public deals endpoints live under `/deals/api/` on purpose — do not put them under `/api/`.
- Changes to `automation/web/app.py`, `templates/`, `static/` require killing and relaunching `python -m automation.web`.
- Tests: `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` (no `pytest` console script). Web tests are DB-free via monkeypatching (pattern: `tests/web/test_deals_api.py`, `tests/web/test_sold_showcase.py`).
- Commit after every green test cycle. Branch: `feat/public-deals-site` off `main`.
- Answer shape for anything written to docs: summary first, details nested.

## Decisions (with the why)

| Question | Decision | Why |
|---|---|---|
| Hostname vs path | **Path: `black-whole.com/deals`**, `robots.txt` `Disallow: /deals`, no link from the chair storefront nav/footer/sitemap, own branding ("Surplus Radar"). | One-shot: zero DNS/Render/Cloudflare steps, no Host-header routing. Chair buyers reach the storefront via search/FB links; an unlinked, noindexed path is invisible to them, and the exclusion policy makes it harmless if found. A subdomain (`radar.black-whole.com` → same Render service + a Host check) is a 30-min follow-up once the page earns it. Portfolio reviewers get the direct link — they don't need SEO. |
| Pagination | **Offset**, `page`+`per_page` (25/50/100, default 25), `total` + `pages` in every response, page ≤ 400. | "Page 3 of 392 · 9,791 lots" needs a total anyway; sort keys (`end_utc`, `current_bid`) are non-unique so a cursor needs composite keys for no gain; the active set is ~9.8k rows, so OFFSET cost is nil once `end_utc` is indexed. |
| Search scope (public) | Title only (`title ILIKE`), trigram-indexed. | `description ILIKE` over the active set measured 1.5 s (TOAST reads); title trigram is milliseconds. Admin keeps title+description. |
| Quantity source | `auction_extractors.quantity_infer.explicit_title_quantity(title)` → `(qty, label)` or `None` → default 1. No LLM, no new column. | Reuses the operator's own tested extractor; deterministic; costs nothing at 210k rows. Named patterns ("Lot of (30)", "Lot of 6", "qty 12", "approx 16", "lot size 40") are generic; positional "(199) chairs" ones are chair-only — fine since chairs are excluded publicly. `deal_verdicts.identity.quantity` (LLM) covers 0 active lots today, so it is not a source. |
| `$/unit` sort / qty filter | **Cut.** Display only. | Needs the regex in SQL or a persisted column; DB has no headroom for a backfilled column. Follow-up: add `deal_lots.quantity` populated by `mapping.asset_to_lot` after `scripts/reclaim_db_space.py --all`. |
| Map on public page | Yes, reusing `static/admin_map.js`; pins feed capped at 5,000 rows, 5-min cache, exclusion-filtered. | Docs said ~21k pins; live count is 8,681 active mapped. Cap keeps a bad filter from shipping 20 MB. |
| A/B tab | **Remove** tab, panel, JS, `/api/compare*` routes. Keep `llm_compare_logs` table, its writer (`automation/llm/__init__.py`) and the inventory-backfill reader. | Gemini was promoted long ago; the tab is a dev artifact. Data stays, so the decision is reversible with `git revert`. |
| Inventory slowness | Merge the two admin calls into one endpoint hitting one connection; lazy-load thumbnails. | See diagnosis in Task 2. Connection pooling in `automation/db.py` is a workspace-level decision (shared `core/db.py` contract) — noted as follow-up, not done here. |

## What was cut (say it, don't hide it)

- Separate hostname / custom domain; client-side Supabase reads; RLS policies (not needed for a server-side surface).
- `$/unit` sort, quantity ≥ filter, quantity from description on list pages.
- Cursor pagination; public map pins beyond 5,000; public description search.
- Public alerts/accounts/saved searches; email digests; "+N at this location" grouping.
- Connection pool in `automation/db.py`; a Render `deals-analyze` cron fix (verdicts cover 0 active lots — separate ticket).
- Public Surplus / BidSpotter on the public page (GovDeals-only, same as `deals/` v1).
- Light/dark theme toggle on the public page (single committed dark look).

## Baseline numbers (measured 2026-09-04 from the laptop)

- `deal_lots`: 210,932 rows, 9,792 active, 8,681 active with coords, 186,951 closed (87,229 no-bid), 68 states/territories active, 15 canonical categories (`seating_furniture` = 629 active).
- Fresh pooler connection ≈ 1.3 s. `inventory.list_all()` 1.88 s, `inventory.stats()` 2.16 s (4 sequential queries on one connection). Cold first query of the process 13 s.
- `/api/deals` = 5 separate connections per page flip (rows, count, categories facet, states facet, stats).
- `EXPLAIN`: active-set count 27 ms warm via `ix_deal_lots_poll`; `title ILIKE OR description ILIKE` over active set 1,522 ms.
- Inventory hero thumbnails are full-res R2 originals (54 KB PNG … 303 KB JPEG) in a 72×54 box, not lazy-loaded; 38 rows ≈ 5–8 MB.

---

### Task 1: Remove the A/B tab

**Files:**
- Modify: `automation/web/templates/index.html:26-29` (tab button), `:278-292` (panel), renumber `tab-num` spans
- Modify: `automation/web/static/app.js:67` (panels map), `:89` (activateTab hook), `:470-560` (`loadCompare`, `renderCompareEntry`, `FIELDS`)
- Modify: `automation/web/static/app.css` (every `.compare-*` / `.diff-*` / `.ce-*` rule)
- Modify: `automation/web/app.py:1531-1577` (`/api/compare`, `/api/compare/{cid}/rate`), module docstring lines 9-10
- Modify: `automation/web/__init__.py` docstring
- Test: `tests/web/test_admin_tabs.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: admin has 10 tabs numbered 01–10 (`launcher, drafts, auctions, inventory, inquiries, listings-db, test-scrape, subscribers, deals, tracking`); `/api/compare` no longer routes.

- [ ] **Step 1: Write the failing test**

```python
# tests/web/test_admin_tabs.py
"""The A/B (compare) tab was removed 2026-09-04. Guard against it creeping back."""
import re

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_admin_has_no_compare_tab():
    html = TestClient(app).get("/admin").text
    assert 'data-tab="compare"' not in html
    assert 'data-pane="compare"' not in html
    # 10 tab buttons after removal (regex so `class="tabs"` / `tab-num` spans don't count)
    assert len(re.findall(r'<button class="tab(?: active)?" data-tab="', html)) == 10


def test_compare_api_is_gone():
    assert TestClient(app).get("/api/compare").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_admin_tabs.py -q`
Expected: FAIL — `data-tab="compare"` present; `/api/compare` returns 200/503, not 404.

- [ ] **Step 3: Remove markup, JS, CSS, routes**

index.html — delete the button block:
```html
    <button class="tab" data-tab="compare">
      <span class="tab-num">03</span><span class="tab-label">A/B</span>
    </button>
```
and the whole `<section class="panel" data-pane="compare" hidden> … </section>` (the block that starts with `<div class="hero-eyebrow">A/B / EXTRACTORS</div>`). Renumber the remaining `tab-num` spans in order: auctions `03`, inventory `04`, inquiries `05`, listings-db `06`, test-scrape `07`, subscribers `08`, deals `09`, tracking `10`.

app.js — delete `compare:  $('[data-pane="compare"]'),` from `panels`, delete `if (name === 'compare') loadCompare();`, delete the block from the line `// ───────── compare ─────────` through the closing `}` of `renderCompareEntry` (includes `const FIELDS = [...]`). Before deleting, run `grep -n "FIELDS\|loadCompare\|renderCompareEntry" automation/web/static/app.js` and confirm the only hits are inside that block. Keep `function esc(s)` (line ~464) — other tabs use it.

app.css — delete every rule whose selector starts with `.compare-`, `.diff-`, or `.ce-` (`grep -n "^\.compare-\|^\.diff-\|^\.ce-" automation/web/static/app.css` lists them).

app.py — delete the two handlers `list_compare` and `rate_compare` (from `@app.get("/api/compare")` through the `return {"ok": True, "ratings": ratings}` line) and the two docstring lines mentioning `/api/compare`. Leave `llm_compare_logs` reads in `inv_backfill` untouched.

`automation/web/__init__.py` — replace the docstring's three-tab list with:
```python
"""Local web dashboard for listing_automation.

Launch with::

    python -m automation.web

Serves on http://127.0.0.1:8765 — public storefront (`/`, `/listings`,
`/deals`) + admin console (`/admin`, ten tabs). The A/B compare tab was
removed 2026-09-04; `llm_compare_logs` is still written by the pipeline and
read by the Inventory backfill.
"""
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/web/ -q`
Expected: all PASS (existing suites untouched).

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/public-deals-site
git add automation/web/templates/index.html automation/web/static/app.js automation/web/static/app.css automation/web/app.py automation/web/__init__.py tests/web/test_admin_tabs.py
git commit -m "admin: remove the A/B compare tab (data + writer kept)"
```

**Done when:** `/admin` shows 10 tabs numbered 01–10, `/api/compare` → 404, `tests/web` green.

---

### Task 2: Inventory tab — diagnose and fix the slow load

**Diagnosis (record this in the commit message):** `loadInventory()` fires `GET /api/inventory` and `GET /api/inventory-stats` in parallel. Each opens its own Supabase session-pooler connection (~1.3 s TLS handshake from the laptop); `stats()` then runs 4 sequential queries, so the slower call is ≈2.2 s before any HTML renders, for 38 rows. Then the table injects 38 `<img>` tags pointing at full-resolution R2 originals (54–303 KB each) into 72×54 boxes with no `loading="lazy"`, ≈5–8 MB on every tab open. Fix: one endpoint, one connection, lazy thumbnails.

**Files:**
- Modify: `automation/inventory.py:201-212` (`list_all`), `:279-311` (`stats`) → add `list_with_stats`
- Modify: `automation/web/app.py:2523-2526` (`inv_list`)
- Modify: `automation/web/static/app.js:1358-1372` (`loadInventory`), `:1391` (`<img>` in `renderInvTable`)
- Test: `tests/test_inventory_list_with_stats.py` (create), `tests/web/test_inventory_api.py` (create)

**Interfaces:**
- Produces: `inventory.list_with_stats(status: str | None = None) -> dict` → `{"items": list[dict], "stats": {"lots","chairs","cities","moved"}}` on ONE connection.
- Produces: `GET /api/inventory?with_stats=1` → `{"items": [...], "stats": {...}}` (without the flag: unchanged `{"items": [...]}`).

- [ ] **Step 1: Write the failing unit test (one connection)**

```python
# tests/test_inventory_list_with_stats.py
"""list_with_stats must do all its reads on ONE connection (the pooler
handshake is ~1.3 s; the old two-endpoint path opened two)."""
from automation import inventory


class _Cur:
    def __init__(self, sql):
        self.sql = sql
    def fetchall(self):
        return [{"lot_id": "1", "status": "listed"}] if "FROM inventory ORDER BY" in self.sql else []
    def fetchone(self):
        return {"n": 3}


class _Conn:
    def __init__(self, log):
        self.log = log
    def __enter__(self):
        self.log.append("open")
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, params=None):
        return _Cur(sql)


def test_list_with_stats_opens_one_connection(monkeypatch):
    log = []
    monkeypatch.setattr(inventory, "connect", lambda: _Conn(log))
    out = inventory.list_with_stats()
    assert log == ["open"]
    assert out["items"] == [{"lot_id": "1", "status": "listed"}]
    assert out["stats"] == {"lots": 3, "chairs": 3, "cities": 3, "moved": 3}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_inventory_list_with_stats.py -q`
Expected: FAIL — `AttributeError: module 'automation.inventory' has no attribute 'list_with_stats'`.

- [ ] **Step 3: Refactor `list_all` / `stats` around a shared connection**

In `automation/inventory.py` replace `list_all` and `stats` with:

```python
def _list_on(conn, status: str | None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE status = %s ORDER BY updated_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM inventory ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_all(status: str | None = None) -> list[dict]:
    with connect() as conn:
        return _list_on(conn, status)


def _stats_on(conn) -> dict:
    """Headline counts for the landing page (same visible set as list_public)."""
    statuses = list(PUBLIC_STATUSES)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory WHERE status = ANY(%s) "
        "AND (quantity_remaining IS NULL OR quantity_remaining > 0)",
        (statuses,),
    ).fetchone()["n"]
    chairs = conn.execute(
        "SELECT COALESCE(SUM(quantity_remaining), 0) AS n FROM inventory "
        "WHERE status = ANY(%s)",
        (statuses,),
    ).fetchone()["n"]
    cities = conn.execute(
        "SELECT COUNT(DISTINCT city) AS n FROM inventory "
        "WHERE city IS NOT NULL AND city != '' AND status = ANY(%s)",
        (statuses,),
    ).fetchone()["n"]
    moved = conn.execute(
        f"SELECT COALESCE(SUM(quantity_original), 0) AS n FROM inventory "
        f"WHERE {_SOLD_SHOWCASE_WHERE}",
        (list(SOLD_STATUSES),),
    ).fetchone()["n"]
    return {
        "lots": int(total),
        "chairs": int(chairs or 0),
        "cities": int(cities),
        "moved": int(moved or 0),
    }


def stats() -> dict:
    with connect() as conn:
        return _stats_on(conn)


def list_with_stats(status: str | None = None) -> dict:
    """Admin Inventory tab: rows + headline counts on ONE pooler connection.

    Two separate endpoints cost two ~1.3 s handshakes per tab open (2026-09-04
    diagnosis). Keep them together.
    """
    with connect() as conn:
        return {"items": _list_on(conn, status), "stats": _stats_on(conn)}
```

Note `_SOLD_SHOWCASE_WHERE` is defined *after* the old `stats` in the file — `_stats_on` only reads it at call time, so order is fine; keep `_stats_on` where `stats` was.

- [ ] **Step 4: Run unit test**

Run: `.venv/bin/python -m pytest tests/test_inventory_list_with_stats.py tests/test_inventory_locations.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing endpoint test**

```python
# tests/web/test_inventory_api.py
import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


ROW = {"lot_id": "9006", "title": "Mauve Banquet Chairs", "status": "owned",
       "hero_image_url": "https://cdn.example.com/9006.jpg", "image_urls": [],
       "locations": None, "govdeals_password": "secret", "buyer_cert_path": None}


def test_inventory_with_stats_is_one_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(inventory, "list_with_stats",
                        lambda status=None: (calls.append(status) or
                                             {"items": [dict(ROW)], "stats": {"lots": 1, "chairs": 442, "cities": 1, "moved": 0}}))
    monkeypatch.setattr(inventory, "list_all", lambda status=None: pytest.fail("must not use list_all"))
    body = TestClient(app).get("/api/inventory?with_stats=1&status=owned").json()
    assert calls == ["owned"]
    assert body["stats"]["chairs"] == 442
    assert body["items"][0]["govdeals_password_set"] is True
    assert "govdeals_password" not in body["items"][0]


def test_inventory_without_flag_is_unchanged(monkeypatch):
    monkeypatch.setattr(inventory, "list_all", lambda status=None: [dict(ROW)])
    body = TestClient(app).get("/api/inventory").json()
    assert set(body) == {"items"}
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_inventory_api.py -q`
Expected: FAIL — first test gets `{"items": [...]}` without `stats`.

- [ ] **Step 7: Endpoint + JS + lazy thumbnails**

app.py — replace `inv_list`:
```python
@app.get("/api/inventory")
async def inv_list(status: str | None = None, with_stats: int = 0):
    """`with_stats=1` returns rows + headline counts from ONE connection —
    the admin tab uses it so a tab open costs one pooler handshake, not two."""
    if with_stats:
        data = await asyncio.to_thread(inventory.list_with_stats, status)
        return {"items": [_inventory_to_public(r) for r in data["items"]],
                "stats": data["stats"]}
    rows = inventory.list_all(status=status)
    return {"items": [_inventory_to_public(r) for r in rows]}
```

app.js — replace the body of `loadInventory`:
```js
async function loadInventory() {
  const tbody = $('#inv-tbody');
  tbody.innerHTML = '<tr><td colspan="11" class="drafts-empty">Loading…</td></tr>';
  try {
    const qs = new URLSearchParams({with_stats: '1'});
    if (_invStatusFilter) qs.set('status', _invStatusFilter);
    const res = await fetch('/api/inventory?' + qs.toString());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _invItems = data.items || [];
    renderInvStats(data.stats || {lots: 0, chairs: 0, cities: 0});
    renderInvTable(_invItems);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="11" class="drafts-empty">Load failed: ${e}</td></tr>`;
  }
}
```
and in `renderInvTable` change the thumbnail line to:
```js
      <td class="inv-hero">${item.hero_image_url
        ? `<img src="${item.hero_image_url}" alt="" loading="lazy" decoding="async" width="72" height="54">`
        : '<div class="inv-hero-fallback">◉</div>'}</td>
```

- [ ] **Step 8: Run tests + measure**

Run: `.venv/bin/python -m pytest tests/web/ tests/test_inventory_list_with_stats.py -q` → PASS.
Then relaunch the server and measure (auth disabled locally or pass the cookie):
```bash
pkill -f "automation.web"; (.venv/bin/python -m automation.web >/tmp/web.log 2>&1 &); sleep 4
for i in 1 2 3; do curl -s -o /dev/null -w '%{time_total}\n' 'http://127.0.0.1:8765/api/inventory?with_stats=1'; done
```
Expected: ≤ 1.6 s each (one handshake + 5 queries), versus ≈2.2 s for the old slower-of-two.

- [ ] **Step 9: Commit**

```bash
git add automation/inventory.py automation/web/app.py automation/web/static/app.js tests/test_inventory_list_with_stats.py tests/web/test_inventory_api.py
git commit -m "admin: Inventory tab loads rows+stats on one connection, lazy thumbnails

Diagnosis: two parallel endpoints = two ~1.3 s pooler handshakes (stats ran 4
queries serially on its own), then 38 full-res R2 originals (54-303 KB each)
eager-loaded into 72x54 boxes. Follow-up: connection pool in automation/db.py
(workspace decision, core/db.py contract)."
```

**Done when:** one request per tab open, `curl -w %{time_total}` ≤ 1.6 s, images carry `loading="lazy"`, tests green.

---

### Task 3: `deals/quantity.py` — quantity per lot + `$/unit` in `enrich`

**Files:**
- Create: `deals/quantity.py`
- Modify: `automation/web/deals_query.py:88-95` (`enrich`)
- Test: `tests/deals/test_quantity.py` (create); `tests/web/test_deals_query.py` (extend)

**Interfaces:**
- Produces: `deals.quantity.lot_quantity(title: str | None, description: str | None = None) -> tuple[int, str]` — `(qty ≥ 1, source ∈ {"title","description","default"})`.
- Produces: `deals.quantity.unit_price(amount: float | None, quantity: int) -> float | None`.
- Produces: `deals_query.enrich` adds `quantity`, `quantity_source`, `unit_bid`, `unit_landed` to every row (admin + public).

- [ ] **Step 1: Write the failing tests**

```python
# tests/deals/test_quantity.py
from deals.quantity import lot_quantity, unit_price


def test_named_patterns_from_real_titles():
    assert lot_quantity("Lot of (30) Lenovo Thinkpads T460") == (30, "title")
    assert lot_quantity("Lot of 6 - Three Dell Monitors And Three Keyboards") == (6, "title")
    assert lot_quantity("One lot of 4 recliners") == (4, "title")
    assert lot_quantity("Pallet Lot of Approx 16 Flat Screen Televisions") == (16, "title")


def test_no_blind_integer_fallback():
    assert lot_quantity("2009 Ford F550 Regular Cab") == (1, "default")
    assert lot_quantity("UMF Medical 8678 Power Phlebotomy Chair") == (1, "default")
    assert lot_quantity(None) == (1, "default")


def test_description_window_is_second_source():
    assert lot_quantity("HP Servers", "Rack pull. Qty: 12 units, tested.") == (12, "description")


def test_thousands_separator():
    assert lot_quantity("Lot of 2,100 folding tables") == (2100, "title")


def test_unit_price():
    assert unit_price(300.0, 30) == 10.0
    assert unit_price(300.0, 0) == 300.0
    assert unit_price(None, 5) is None
```

Append to `tests/web/test_deals_query.py`:
```python
def test_enrich_adds_quantity_and_unit_columns():
    row = {"asset_id": 1, "account_id": 2, "auction_id": 3,
           "title": "Lot of (30) Lenovo Thinkpads", "current_bid": 300.0}
    out = deals_query.enrich(row, FeeModel(buyer_premium_pct=0.125, tax_pct=0, freight=0))
    assert out["quantity"] == 30 and out["quantity_source"] == "title"
    assert out["unit_bid"] == 10.0
    assert out["landed_cost"] == 337.5 and out["unit_landed"] == 11.25
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/deals/test_quantity.py tests/web/test_deals_query.py -q`
Expected: FAIL — `ModuleNotFoundError: deals.quantity`; `KeyError: 'quantity'`.

- [ ] **Step 3: Implement**

```python
# deals/quantity.py
"""Quantity per lot from the listing text — the value knob GovAuctions lacks.

Reuses the operator's own extractor (`auction_extractors.quantity_infer.
explicit_title_quantity`): named patterns only ("Lot of (30)", "Lot of 6",
"qty 12", "approx 16", "lot size 40"), NO blind-integer fallback (a "2009 Ford"
is not 2009 trucks). Deterministic, no LLM, no DB column — the DB is at its
free-tier ceiling (2026-09-04), so quantity is derived at read time.
"""
from __future__ import annotations

from auction_extractors.quantity_infer import explicit_title_quantity

DESCRIPTION_WINDOW = 600  # chars; descriptions are TOAST blobs, don't scan them all


def lot_quantity(title: str | None, description: str | None = None) -> tuple[int, str]:
    """(quantity, source). quantity >= 1; source in {'title','description','default'}."""
    for text, source in ((title, "title"), ((description or "")[:DESCRIPTION_WINDOW], "description")):
        if not text:
            continue
        hit = explicit_title_quantity(text)
        if hit and hit[0] > 0:
            return int(hit[0]), source
    return 1, "default"


def unit_price(amount: float | None, quantity: int) -> float | None:
    if amount is None:
        return None
    q = quantity if quantity and quantity > 0 else 1
    return round(float(amount) / q, 2)
```

`deals_query.enrich` becomes:
```python
from deals.quantity import lot_quantity, unit_price


def enrich(row: dict, fees: FeeModel) -> dict:
    bid = float(row.get("current_bid") or 0)
    qty, src = lot_quantity(row.get("title"))
    lc = landed_cost(bid, qty=qty, fees=fees)
    row["landed_cost"] = round(lc.total, 2)
    row["quantity"] = qty
    row["quantity_source"] = src
    row["unit_bid"] = unit_price(row.get("current_bid"), qty)
    row["unit_landed"] = round(lc.per_unit, 2)
    row["govdeals_url"] = (
        f"https://www.govdeals.com/en/asset/{row['asset_id']}/{row['account_id']}"
    )
    row["viewer_url"] = f"/deals/{row['asset_id']}/{row['account_id']}/{row['auction_id']}"
    return row
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_quantity.py tests/web/ -q`
Expected: PASS (the existing `landed_cost == 112.5` assertion still holds: "Desk" → qty 1).

- [ ] **Step 5: Commit**

```bash
git add deals/quantity.py automation/web/deals_query.py tests/deals/test_quantity.py tests/web/test_deals_query.py
git commit -m "deals: quantity per lot (reuses quantity_infer) + unit_bid/unit_landed in enrich"
```

**Done when:** both test files green; `enrich` output carries the four new keys.

---

### Task 4: Partial indexes for the active set

**Files:**
- Create: `scripts/sql/006_deal_lots_active_indexes.sql`
- Modify: `../docs/claude-reference/data-model.md` (workspace; append the DDL under the `deal_lots` section)

**Interfaces:** none (DB-only).

- [ ] **Step 1: Write the DDL**

```sql
-- scripts/sql/006_deal_lots_active_indexes.sql
-- Public /deals + admin Deals tab: make the active set index-driven.
-- Both are PARTIAL on `outcome_complete IS NOT TRUE` (~33k rows incl. not-yet-
-- marked closes), so they stay a few MB on a DB sitting at its 500 MB ceiling.
-- Apply with autocommit (CONCURRENTLY refuses to run inside a transaction):
--   .venv/bin/python scripts/apply_sql.py scripts/sql/006_deal_lots_active_indexes.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deal_lots_active_end
  ON deal_lots (end_utc)
  WHERE outcome_complete IS NOT TRUE;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deal_lots_active_title_trgm
  ON deal_lots USING gin (title gin_trgm_ops)
  WHERE outcome_complete IS NOT TRUE;
```

- [ ] **Step 2: Add the tiny applier (autocommit, statement by statement)**

```python
# scripts/apply_sql.py
"""Apply a scripts/sql/*.sql file statement-by-statement in autocommit mode.

Needed for CREATE INDEX CONCURRENTLY, which Postgres refuses inside a
transaction block. Splits on ';' — keep migration files free of ';' inside
string literals.
"""
import pathlib
import sys

from automation import config  # noqa: F401  (loads .env)
from automation import db


def main(path: str) -> None:
    sql = pathlib.Path(path).read_text()
    stmts = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]
    conn = db.connect(autocommit=True)
    try:
        for s in stmts:
            body = "\n".join(l for l in s.splitlines() if not l.strip().startswith("--")).strip()
            if not body:
                continue
            print(f"→ {body[:70]}…")
            conn.execute(body)
    finally:
        conn.close()
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1])
```

- [ ] **Step 3: Apply + verify with EXPLAIN**

Run:
```bash
.venv/bin/python scripts/apply_sql.py scripts/sql/006_deal_lots_active_indexes.sql
.venv/bin/python - <<'EOF'
from automation import config, db
for sql in (
  "EXPLAIN SELECT asset_id FROM deal_lots WHERE outcome_complete IS NOT TRUE AND end_utc > now() ORDER BY end_utc ASC LIMIT 25",
  "EXPLAIN SELECT count(*) FROM deal_lots WHERE outcome_complete IS NOT TRUE AND end_utc > now() AND title ILIKE '%laptop%'",
):
    print("\n".join(r["QUERY PLAN"] for r in db.fetch_all(sql))); print("---")
print(db.fetch_one("SELECT pg_size_pretty(pg_relation_size('ix_deal_lots_active_end')) AS a, pg_size_pretty(pg_relation_size('ix_deal_lots_active_title_trgm')) AS b"))
EOF
```
Expected: first plan contains `Index Scan using ix_deal_lots_active_end`; second contains `Bitmap Index Scan on ix_deal_lots_active_title_trgm`; sizes each < 10 MB. If `DiskFull` appears, run `.venv/bin/python scripts/reclaim_db_space.py --all` first (suspend Render crons per `docs/claude-reference/database-size.md`).

- [ ] **Step 4: Mirror DDL in the workspace data-model doc**

Append under the `deal_lots` section of `../docs/claude-reference/data-model.md`:
```markdown
- Indexes added 2026-09-04 (`listing_automation/scripts/sql/006_deal_lots_active_indexes.sql`), both partial on `outcome_complete IS NOT TRUE`:
  - `ix_deal_lots_active_end (end_utc)` — page/sort by ending time.
  - `ix_deal_lots_active_title_trgm USING gin (title gin_trgm_ops)` — public title search (`pg_trgm`).
```

- [ ] **Step 5: Commit**

```bash
git add scripts/sql/006_deal_lots_active_indexes.sql scripts/apply_sql.py
git commit -m "deals: partial end_utc + title trigram indexes on the active set"
cd .. && git add docs/claude-reference/data-model.md && git commit -m "docs: mirror deal_lots active-set indexes" && cd listing_automation
```

**Done when:** both EXPLAIN plans use the new indexes; combined size < 20 MB.

---

### Task 5: `automation/web/public_deals.py` — exclusion policy + paged public reads

**Files:**
- Create: `automation/web/public_deals.py`
- Modify: `automation/web/deals_query.py:24-45` (`build_where` gains `search_fields`)
- Test: `tests/web/test_public_deals.py` (create)

**Interfaces:**
- Consumes: `deals_query.build_where(...)`, `deals_query.enrich(row, fees)`, `deals.fees.fee_model_from_env`, `automation.db.connect`.
- Produces (all in `public_deals`):
  - `EXCLUDED_CATEGORIES: frozenset[str]` (env `PUBLIC_DEALS_EXCLUDE_CATEGORIES`, comma list, default `seating_furniture`)
  - `EXCLUDED_TITLE_RE: str` (env `PUBLIC_DEALS_EXCLUDE_KEYWORDS`, default `chair,chairs,seating,stool,stools,bench,benches,pew,pews,barstool,barstools,sofa,sofas,couch,couches,banquet`)
  - `PUBLIC_COLS: str`, `PUBLIC_SORTS: dict`, `PER_PAGE_CHOICES = (25, 50, 100)`, `MAX_PAGE = 400`, `PINS_CAP = 5000`, `CACHE_TTL = 300`
  - `exclusion_where() -> tuple[str, list]`
  - `is_excluded(row: dict) -> bool` (category + title regex only — pure)
  - `is_operator_lot(asset_id: int, account_id: int, auction_id: int) -> bool` (DB)
  - `build_public_where(**filters) -> tuple[str, list]`
  - `public_order(sort: str, direction: str | None) -> str`
  - `fetch_page(**filters, sort, dir, page, per_page) -> dict` → `{"rows","total","page","per_page","pages"}`
  - `fetch_pins(**filters) -> dict` → `{"points": [...], "capped": bool}`
  - `fetch_facets() -> dict` → `{"categories","states","stats","cached_at"}`
  - `enrich_public(row: dict, fees) -> dict`
  - `clear_cache() -> None`

- [ ] **Step 1: Write the failing tests (pure parts)**

```python
# tests/web/test_public_deals.py
"""Chair-buyer isolation + paging contract for the public /deals surface.
Pure SQL-building tests; fetch_* are covered via the endpoint tests in
tests/web/test_public_deals_api.py with the DB monkeypatched."""
import re

import pytest

from automation.web import public_deals as pd


def test_exclusion_where_names_every_operator_table():
    where, args = pd.exclusion_where()
    assert "canonical_category <> ALL(%s)" in where
    assert "COALESCE(title, '') !~* %s" in where
    for tbl in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert f"FROM {tbl}" in where
    assert args[0] == sorted(pd.EXCLUDED_CATEGORIES)
    assert args[1] == pd.EXCLUDED_TITLE_RE


def test_title_regex_blocks_seating_only():
    rx = re.compile(pd.EXCLUDED_TITLE_RE, re.I)
    assert rx.search("Lot of (199) Banquet Chairs")
    assert rx.search("Church pews - 40 sections")
    assert rx.search("Bar Stools, set of 12")
    assert not rx.search("Lot of (30) Lenovo Thinkpads T460")
    assert not rx.search("Wheelchair accessible van")  # 'chair' must be a whole word


def test_is_excluded_row():
    assert pd.is_excluded({"canonical_category": "seating_furniture", "title": "Desk"})
    assert pd.is_excluded({"canonical_category": "other", "title": "Stacking chairs x 200"})
    assert not pd.is_excluded({"canonical_category": "other", "title": "Vulcan fryer"})
    assert not pd.is_excluded({"canonical_category": None, "title": "Water plant pumps"})


def test_build_public_where_is_title_only_search():
    where, args = pd.build_public_where(q="laptop", status="active")
    assert "title ILIKE %s" in where and "description" not in where
    assert args.count("%laptop%") == 1


def test_public_order_rejects_private_sorts():
    assert pd.public_order("margin", None) == "ORDER BY end_utc ASC NULLS LAST"
    assert pd.public_order("bid", "asc") == "ORDER BY current_bid ASC NULLS LAST"
    assert pd.public_order("newest", None) == "ORDER BY first_seen_at DESC NULLS LAST"


def test_public_cols_never_leak_private_fields():
    for col in ("hero_image_url", "archived_hero_url", "gallery_urls", "description",
                "seller", "high_bidder", "raw"):
        assert col not in pd.PUBLIC_COLS


@pytest.mark.parametrize("page,per_page,expect", [(0, 25, (1, 25)), (5, 33, (5, 25)), (9999, 100, (400, 100))])
def test_page_clamping(page, per_page, expect):
    assert pd.clamp_page(page, per_page) == expect
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/web/test_public_deals.py -q`
Expected: FAIL — `ImportError: cannot import name 'public_deals'`.

- [ ] **Step 3: `build_where` gains `search_fields`**

In `automation/web/deals_query.py` add the keyword parameter and use it:
```python
def build_where(*, q: str | None = None, category: str | None = None,
                native: str | None = None,
                state: str | None = None, max_bids: int | None = None,
                ending_within: int | None = None,
                status: str = "active",
                min_margin: float | None = None,
                min_price: float | None = None,
                max_price: float | None = None,
                list_id: int | None = None,
                tag: str | None = None,
                bbox: tuple[float, float, float, float] | None = None,
                search_fields: tuple[str, ...] = ("title", "description"),
                ) -> tuple[str, list]:
    where: list[str] = []
    args: list = []
    if status == "active":
        where.append("outcome_complete IS NOT TRUE AND end_utc > now()")
    elif status == "closed":
        where.append("outcome_complete IS TRUE")
    if q:
        where.append("(" + " OR ".join(f"{f} ILIKE %s" for f in search_fields) + ")")
        args += [f"%{q}%"] * len(search_fields)
```
(rest of the function unchanged).

- [ ] **Step 4: Implement `public_deals.py`**

```python
# automation/web/public_deals.py
"""Server-side read model for the PUBLIC /deals page ("Surplus Radar").

Everything the public may see about `deal_lots` passes through here. Three
rules, enforced in SQL so no caller can forget them:

1. **Chair-buyer isolation.** The operator resells seating. A chair buyer who
   finds this page must never see the lots he is bidding on, so we exclude:
   the seating category, any seating word in the title, and every lot in
   `tracked_lots` / `auction_favorites` / `deal_list_items` (the three places
   the operator marks interest). Override lists via env, never by editing SQL.
2. **No source photos, no verdicts, no home distance.** `PUBLIC_COLS` is the
   allow-list; the copyright-bearing image columns, the LLM verdict join, and
   `DEALS_HOME_*` distance never reach a public response.
3. **One connection per request; facets/pins cached 5 min.** The pooler
   handshake is ~1.3 s, so rows+count share a connection and the expensive
   whole-table stats are memoized in-process.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .. import db
from . import deals_query
from deals.fees import fee_model_from_env

# ── policy (env-overridable, comma lists) ────────────────────────────────────
_DEFAULT_CATS = "seating_furniture"
_DEFAULT_WORDS = ("chair,chairs,seating,stool,stools,bench,benches,pew,pews,"
                  "barstool,barstools,sofa,sofas,couch,couches,banquet")


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name) or default
    return [x.strip() for x in raw.split(",") if x.strip()]


EXCLUDED_CATEGORIES: frozenset[str] = frozenset(_csv("PUBLIC_DEALS_EXCLUDE_CATEGORIES", _DEFAULT_CATS))
EXCLUDED_TITLE_RE: str = r"\b(?:" + "|".join(re.escape(w) for w in _csv("PUBLIC_DEALS_EXCLUDE_KEYWORDS", _DEFAULT_WORDS)) + r")\b"
_TITLE_RX = re.compile(EXCLUDED_TITLE_RE, re.I)

PUBLIC_COLS = (
    "asset_id, account_id, auction_id, title, canonical_category, "
    "native_category_name, city, state, bid_count, current_bid, currency_code, "
    "end_utc, outcome, final_bid, final_bid_count, outcome_complete, "
    "first_seen_at, lat, lng"
)
PUBLIC_SORTS = {"ends": "end_utc", "newest": "first_seen_at",
                "bid": "current_bid", "bids": "bid_count"}
PER_PAGE_CHOICES = (25, 50, 100)
MAX_PAGE = 400
PINS_CAP = 5000
CACHE_TTL = 300

_CACHE: dict[str, tuple[float, Any]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cached(key: str, loader):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


# ── exclusion policy ─────────────────────────────────────────────────────────

def exclusion_where() -> tuple[str, list]:
    """SQL fragment (and args) that hides every lot a chair buyer must not see."""
    where = (
        "(canonical_category IS NULL OR canonical_category <> ALL(%s)) "
        "AND COALESCE(title, '') !~* %s "
        "AND NOT EXISTS (SELECT 1 FROM tracked_lots t "
        "  WHERE t.asset_id = deal_lots.asset_id AND t.account_id = deal_lots.account_id) "
        "AND NOT EXISTS (SELECT 1 FROM auction_favorites f "
        "  WHERE f.asset_id = deal_lots.asset_id::text || '/' || deal_lots.account_id::text) "
        "AND NOT EXISTS (SELECT 1 FROM deal_list_items li "
        "  WHERE li.asset_id = deal_lots.asset_id AND li.account_id = deal_lots.account_id "
        "    AND li.auction_id = deal_lots.auction_id)"
    )
    return where, [sorted(EXCLUDED_CATEGORIES), EXCLUDED_TITLE_RE]


def is_excluded(row: dict) -> bool:
    """Pure half of the policy (category + title). Membership needs the DB —
    see `is_operator_lot`."""
    if (row.get("canonical_category") or "") in EXCLUDED_CATEGORIES:
        return True
    return bool(_TITLE_RX.search(row.get("title") or ""))


def is_operator_lot(asset_id: int, account_id: int, auction_id: int) -> bool:
    row = db.fetch_one(
        "SELECT EXISTS (SELECT 1 FROM tracked_lots WHERE asset_id=%s AND account_id=%s) "
        "OR EXISTS (SELECT 1 FROM auction_favorites WHERE asset_id=%s) "
        "OR EXISTS (SELECT 1 FROM deal_list_items WHERE asset_id=%s AND account_id=%s AND auction_id=%s) "
        "AS hit",
        (asset_id, account_id, f"{asset_id}/{account_id}", asset_id, account_id, auction_id),
    )
    return bool(row and row["hit"])


# ── query building ───────────────────────────────────────────────────────────

def build_public_where(*, q=None, category=None, state=None, max_bids=None,
                       ending_within=None, status="active", min_price=None,
                       max_price=None, bbox=None) -> tuple[str, list]:
    where, args = deals_query.build_where(
        q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status, min_price=min_price,
        max_price=max_price, bbox=bbox, search_fields=("title",),
    )
    ex_where, ex_args = exclusion_where()
    return f"{where} AND {ex_where}", [*args, *ex_args]


def public_order(sort: str, direction: str | None) -> str:
    col = PUBLIC_SORTS.get(sort) or PUBLIC_SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def clamp_page(page: int, per_page: int) -> tuple[int, int]:
    per_page = per_page if per_page in PER_PAGE_CHOICES else PER_PAGE_CHOICES[0]
    return max(1, min(int(page or 1), MAX_PAGE)), per_page


def enrich_public(row: dict, fees) -> dict:
    return deals_query.enrich(row, fees)  # PUBLIC_COLS already excludes private fields


# ── reads ────────────────────────────────────────────────────────────────────

def fetch_page(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None, bbox=None, sort="ends", dir=None,
               page=1, per_page=25) -> dict:
    page, per_page = clamp_page(page, per_page)
    where, args = build_public_where(
        q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status, min_price=min_price,
        max_price=max_price, bbox=bbox)
    order = public_order(sort, dir)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {PUBLIC_COLS} FROM deal_lots WHERE {where} {order} LIMIT %s OFFSET %s",
            (*args, per_page, (page - 1) * per_page),
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) AS c FROM deal_lots WHERE {where}", tuple(args)
        ).fetchone()["c"]
    fees = fee_model_from_env()
    return {
        "rows": [enrich_public(dict(r), fees) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def fetch_pins(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None) -> dict:
    params = dict(q=q, category=category, state=state, max_bids=max_bids,
                  ending_within=ending_within, status=status,
                  min_price=min_price, max_price=max_price)
    key = "pins:" + json.dumps(params, sort_keys=True, default=str)

    def load():
        where, args = build_public_where(**params)
        points = db.fetch_all(
            "SELECT asset_id, account_id, auction_id, title, current_bid, bid_count, "
            "end_utc, city, state, lat, lng FROM deal_lots "
            f"WHERE {where} AND lat IS NOT NULL AND lng IS NOT NULL "
            "ORDER BY end_utc ASC LIMIT %s",
            (*args, PINS_CAP + 1),
        )
        capped = len(points) > PINS_CAP
        points = points[:PINS_CAP]
        for p in points:
            p["govdeals_url"] = f"https://www.govdeals.com/en/asset/{p['asset_id']}/{p['account_id']}"
        return {"points": points, "capped": capped}

    return _cached(key, load)


def fetch_facets() -> dict:
    def load():
        where, args = build_public_where(status="active")
        with db.connect() as conn:
            cats = conn.execute(
                "SELECT canonical_category AS value, count(*) AS count FROM deal_lots "
                f"WHERE {where} AND canonical_category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                tuple(args)).fetchall()
            states = conn.execute(
                "SELECT state AS value, count(*) AS count FROM deal_lots "
                f"WHERE {where} AND state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                tuple(args)).fetchall()
            stats = conn.execute(
                "SELECT count(*) AS tracked, "
                "count(*) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS active, "
                "count(*) FILTER (WHERE outcome_complete IS TRUE) AS closed, "
                "count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid, "
                "count(DISTINCT state) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS states, "
                "min(first_seen_at) AS since FROM deal_lots").fetchone()
        return {"categories": cats, "states": states, "stats": stats, "cached_at": time.time()}

    return _cached("facets", load)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/web/test_public_deals.py tests/web/test_deals_query.py -q`
Expected: PASS.

- [ ] **Step 6: Live smoke (read-only, proves the policy on real data)**

```bash
.venv/bin/python - <<'EOF'
from automation import config
from automation.web import public_deals as pd
page = pd.fetch_page(q="chair")
assert page["total"] == 0, page["total"]          # every chair title is gone
page = pd.fetch_page(per_page=100)
assert all(not pd.is_excluded(r) for r in page["rows"])
assert not any(k in page["rows"][0] for k in ("hero_image_url", "description", "distance_mi"))
print("ok", page["total"], "public lots · sample:", page["rows"][0]["title"], page["rows"][0]["quantity"], page["rows"][0]["unit_bid"])
print(pd.fetch_facets()["stats"])
EOF
```
Expected: `ok` with total ≈ 9,000 (9,792 active minus seating/keywords/operator lots), no assertion.

- [ ] **Step 7: Commit**

```bash
git add automation/web/public_deals.py automation/web/deals_query.py tests/web/test_public_deals.py
git commit -m "web: public_deals read model — chair-buyer exclusion policy, offset paging, cached facets/pins"
```

**Done when:** tests green; live smoke prints `ok`; `fetch_page(q="chair")["total"] == 0`.

---

### Task 6: Public endpoints, viewer photo gate, robots

**Files:**
- Modify: `automation/web/app.py:552-563` (`deal_listing`), `:1048-1057` (`robots_txt`), add routes after `deal_listing`
- Modify: `automation/web/templates/deal_listing.html:61-69` (gallery block), `:93-102` (script)
- Modify: `automation/web/auth.py:32-35` docstring (public list) — comment only
- Test: `tests/web/test_public_deals_api.py` (create)

**Interfaces:**
- Produces: `GET /deals` (HTML, Task 7 template), `GET /deals/api/lots`, `GET /deals/api/pins`, `GET /deals/api/facets` — all outside the auth prefix.
- Produces: `deal_listing` passes `show_images: bool` to the template; public requests 404 on excluded/operator lots.

- [ ] **Step 1: Write the failing tests**

```python
# tests/web/test_public_deals_api.py
"""DB-free tests for the public /deals surface: routes are open without a
session, private fields never appear, excluded lots 404 for the public but
render (with photos) for the operator."""
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals as pd
from automation.web import app as app_mod
from automation.web.app import app

PAGE = {"rows": [{"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Lot of (30) Laptops",
                  "canonical_category": "computers_electronics", "city": "Houston", "state": "TX",
                  "bid_count": 0, "current_bid": 300.0, "end_utc": None, "outcome_complete": False,
                  "quantity": 30, "unit_bid": 10.0, "unit_landed": 11.25, "landed_cost": 337.5,
                  "govdeals_url": "https://www.govdeals.com/en/asset/305/10340",
                  "viewer_url": "/deals/305/10340/1"}],
        "total": 1, "page": 1, "per_page": 25, "pages": 1}
LOT = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Lot of (30) Laptops",
       "canonical_category": "computers_electronics", "city": "Houston", "state": "TX",
       "bid_count": 0, "current_bid": 300.0, "currency_code": "USD", "end_utc": None,
       "outcome": None, "final_bid": None, "images_archived": True,
       "archived_hero_url": "https://cdn.example.com/hero.jpg", "hero_image_url": None,
       "gallery_urls": ["https://cdn.example.com/1.jpg"], "description": "d",
       "native_category_id": "29", "native_category_name": "Computers", "seller": "City",
       "opening_bid": 10.0, "has_reserve": False, "first_seen_at": None, "final_bid_count": None}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: dict(PAGE, echo=kw))
    monkeypatch.setattr(pd, "fetch_pins", lambda **kw: {"points": [], "capped": False})
    monkeypatch.setattr(pd, "fetch_facets", lambda: {"categories": [], "states": [], "stats": {"tracked": 210932}, "cached_at": 0})
    monkeypatch.setattr(pd, "is_operator_lot", lambda a, b, c: False)
    monkeypatch.setattr(app_mod.db, "fetch_one", lambda sql, params=(): dict(LOT))
    monkeypatch.setattr("deals.tracking_store.history", lambda a, b: [])
    return TestClient(app, base_url="https://testserver")


def test_public_endpoints_need_no_session(client):
    assert client.get("/deals/api/lots?page=2&per_page=50&sort=bid").status_code == 200
    assert client.get("/deals/api/pins").status_code == 200
    assert client.get("/deals/api/facets").json()["stats"]["tracked"] == 210932
    assert client.get("/deals").status_code == 200
    assert client.get("/api/deals").status_code == 401  # admin feed still gated


def test_lots_passes_paging_and_rejects_bad_status(client):
    body = client.get("/deals/api/lots?page=2&per_page=50&sort=bid&dir=asc").json()
    assert body["echo"]["page"] == 2 and body["echo"]["per_page"] == 50 and body["echo"]["sort"] == "bid"
    assert body["rows"][0]["unit_bid"] == 10.0
    assert client.get("/deals/api/lots?status=bogus").status_code == 400


def test_viewer_hides_photos_for_public_and_shows_them_for_operator(client):
    html = client.get("/deals/305/10340/1").text
    assert "cdn.example.com/hero.jpg" not in html and "deal_card.js" not in html
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    html = client.get("/deals/305/10340/1").text
    assert "cdn.example.com/hero.jpg" in html


def test_viewer_404s_excluded_lot_for_public_only(client, monkeypatch):
    monkeypatch.setattr(app_mod.db, "fetch_one",
                        lambda sql, params=(): dict(LOT, canonical_category="seating_furniture"))
    assert client.get("/deals/305/10340/1").status_code == 404
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    assert client.get("/deals/305/10340/1").status_code == 200


def test_robots_disallows_deals(client):
    assert "Disallow: /deals" in client.get("/robots.txt").text
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/web/test_public_deals_api.py -q`
Expected: FAIL — 404 on `/deals/api/lots`, photos present for public, robots missing the line.

- [ ] **Step 3: Routes + viewer gate + robots**

app.py imports — add `from . import public_deals` next to `from . import deals_query`.

Replace `deal_listing`:
```python
@app.get("/deals/{asset_id}/{account_id}/{auction_id}", response_class=HTMLResponse)
async def deal_listing(request: Request, asset_id: int, account_id: int, auction_id: int):
    """Archived-lot viewer. Public visitors get text only (no source photos —
    copyright) and never see a seating/operator lot (chair-buyer isolation);
    an operator session sees everything."""
    row = db.fetch_one("""SELECT * FROM deal_lots
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (asset_id, account_id, auction_id))
    if not row:
        raise HTTPException(status_code=404, detail="lot not archived")
    operator = (not auth_svc.auth_enabled()) or auth_svc.request_has_session(request)
    if not operator and (
        public_deals.is_excluded(row)
        or await asyncio.to_thread(public_deals.is_operator_lot, asset_id, account_id, auction_id)
    ):
        raise HTTPException(status_code=404, detail="lot not archived")
    from deals import tracking, tracking_store
    history = tracking_store.history(asset_id, account_id)
    return templates.TemplateResponse(request, "deal_listing.html", {
        "lot": row, "history": history, "bidders": tracking.bidder_summary(history),
        "show_images": operator})


# ── Public deals surface ("Surplus Radar") — outside the /api/ auth prefix ──
# Policy + queries: automation/web/public_deals.py. Nothing here touches
# photos, verdicts, or the operator's home distance.

_PUBLIC_STATUSES = ("active", "closed", "all")


@app.get("/deals", response_class=HTMLResponse)
async def public_deals_page(request: Request):
    return templates.TemplateResponse(request, "deals_public.html", {
        "base_url": PUBLIC_BASE_URL, "now": int(time.time()),
        "per_page_choices": public_deals.PER_PAGE_CHOICES})


@app.get("/deals/api/lots")
async def public_deals_lots(
    q: str | None = None, category: str | None = None, state: str | None = None,
    max_bids: int | None = None, ending_within: int | None = None,
    status: str = "active", min_price: float | None = None,
    max_price: float | None = None, bbox: str | None = None,
    sort: str = "ends", dir: str | None = None, page: int = 1, per_page: int = 25,
):
    if status not in _PUBLIC_STATUSES:
        raise HTTPException(400, "status must be active|closed|all")
    try:
        return await asyncio.to_thread(
            public_deals.fetch_page, q=q, category=category, state=state,
            max_bids=max_bids, ending_within=ending_within, status=status,
            min_price=min_price, max_price=max_price, bbox=_parse_bbox(bbox),
            sort=sort, dir=dir, page=page, per_page=per_page)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"deals query failed: {e!r}")


@app.get("/deals/api/pins")
async def public_deals_pins(
    q: str | None = None, category: str | None = None, state: str | None = None,
    max_bids: int | None = None, ending_within: int | None = None,
    status: str = "active", min_price: float | None = None, max_price: float | None = None,
):
    if status not in _PUBLIC_STATUSES:
        raise HTTPException(400, "status must be active|closed|all")
    try:
        return await asyncio.to_thread(
            public_deals.fetch_pins, q=q, category=category, state=state,
            max_bids=max_bids, ending_within=ending_within, status=status,
            min_price=min_price, max_price=max_price)
    except Exception as e:
        raise HTTPException(503, f"pins query failed: {e!r}")


@app.get("/deals/api/facets")
async def public_deals_facets():
    try:
        return await asyncio.to_thread(public_deals.fetch_facets)
    except Exception as e:
        raise HTTPException(503, f"facets query failed: {e!r}")
```
`_parse_bbox` is defined at line ~577, *after* `deal_listing` — move the three new routes below `_parse_bbox` (or below `deals_tree`) so the name resolves; FastAPI route order does not matter for these paths (`/deals/api/lots` has 2 segments, the viewer needs 3 ints).

robots — add `"Disallow: /deals\n"` after `"Disallow: /api/\n"`.

deal_listing.html — wrap the gallery:
```html
  {% set hero = lot.archived_hero_url or lot.hero_image_url %}
  {% set gallery = lot.gallery_urls or [] %}
  {% if show_images and (hero or gallery) %}
  <div class="gallery" id="gallery">
    {% if hero %}<a href="{{ hero }}" target="_blank"><img src="{{ hero }}" alt="{{ lot.title }}"></a>{% endif %}
    {% for g in gallery %}<a href="{{ g }}" target="_blank"><img src="{{ g }}" alt="" loading="lazy"></a>{% endfor %}
  </div>
  {% elif not show_images %}
  <p class="meta">Photos are on the GovDeals listing — we don't republish auction images.</p>
  {% endif %}
```
and wrap the trailing `<script src="/static/deal_card.js"></script><script>…</script>` pair in `{% if show_images %} … {% endif %}` (DealCard reads the auth-gated `/api/deals/...`). Also change the "images from GovDeals CDN — not archived" chip line to `{% if show_images %}{% if lot.images_archived %}…{% else %}…{% endif %}{% endif %}` so the public never sees archive status.

auth.py — in the docstring line "The public storefront (``/``, ``/listings``, ``/sell``, …" add ``/deals`` and ``/deals/api/*`` to the list. Comment only.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/web/ -q`
Expected: PASS (the `/deals` HTML test will fail until Task 7's template exists — if running Tasks out of order, create an empty `templates/deals_public.html` containing `<title>Surplus Radar</title>` as a stub and replace it in Task 7).

- [ ] **Step 5: Commit**

```bash
git add automation/web/app.py automation/web/auth.py automation/web/templates/deal_listing.html tests/web/test_public_deals_api.py
git commit -m "web: public /deals/api/* routes, viewer photo gate for non-operators, robots Disallow /deals"
```

**Done when:** all five tests pass; `curl -s http://127.0.0.1:8765/deals/api/lots?per_page=25 | jq '.total,.pages,.rows[0]|keys'` shows no `hero_image_url`/`description`/`distance_mi`.

---

### Task 7: Public page — template, CSS, JS (top-bar search, side filters, pager, portfolio landing)

**Files:**
- Create: `automation/web/templates/deals_public.html`
- Create: `automation/web/static/deals_public.css`
- Create: `automation/web/static/deals_public.js`
- Test: `tests/web/test_public_deals_page.py` (create)

**Interfaces:**
- Consumes: `/deals/api/lots`, `/deals/api/pins`, `/deals/api/facets` (Task 6), `window.AdminMap` from `/static/admin_map.js`.
- Produces: the page at `/deals`. URL state: `?q=&category=&state=&max_bids=&ending_within=&status=&min_price=&max_price=&sort=&dir=&page=&per_page=`.

**Layout spec (what "search on top, the rest on the side" means here):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ◉ SURPLUS RADAR        every US gov-surplus auction, tracked to close │  header (own brand, no chair nav)
├──────────────────────────────────────────────────────────────────────┤
│ [portfolio landing — collapsible <details>, open on first visit]      │
│  What it is · 5 live stat tiles · How it works · What's different     │
├──────────────────────────────────────────────────────────────────────┤
│ TOP BAR (sticky):  [🔍 search titles……]  bids[any|0|≤3]  ending[any|24h|48h|7d]  status[active|closed]  sort[▾]  [🗺 map] │
│ category pills:  all · other 3918 · vehicles 2245 · computers 676 · …  (never seating)                │
├──────────────┬───────────────────────────────────────────────────────┤
│ SIDE (240px) │ "Page 3 of 392 · 9,791 lots"   [« ‹ 3 › »] [25▾/page]  │  pager top
│ Category     │ ─────────────────────────────────────────────────────  │
│  (facets w/  │ TITLE            CAT   LOCATION  BIDS  BID   QTY  $/UNIT  ENDS  ↗ │  table ≥900px
│  counts)     │ …25 rows…                                              │  cards <900px
│ State [▾]    │ ─────────────────────────────────────────────────────  │
│ Price $[ ]–[ ]│ [« ‹ 3 › »]  jump to page [  ]                          │  pager bottom
│ Landed fees ⓘ│                                                        │
│ Clear all    │                                                        │
└──────────────┴───────────────────────────────────────────────────────┘
```
Map (when toggled) renders full-width between the pills row and the two-column area; panning sets `bbox` and re-queries page 1 (same contract as the admin). On < 900 px the side panel becomes a `<details>` above the list.

- [ ] **Step 1: Write the failing template test**

```python
# tests/web/test_public_deals_page.py
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_deals_page_shell():
    html = TestClient(app).get("/deals").text
    assert "<title>Surplus Radar" in html
    assert 'id="sr-q"' in html and 'id="sr-pager-top"' in html and 'id="sr-side"' in html
    assert 'name="robots" content="noindex' in html
    assert "/static/deals_public.js" in html and "/static/admin_map.js" in html
    # portfolio landing block + no chair-storefront chrome
    assert 'id="sr-about"' in html
    assert "Sell Your Chairs" not in html and "<img" not in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_public_deals_page.py -q`
Expected: FAIL — `TemplateNotFound: deals_public.html` (or the Task 6 stub lacks the ids).

- [ ] **Step 3: Template**

```html
{# automation/web/templates/deals_public.html — public "Surplus Radar" page.
   Standalone: NOT _public_base.html (no chair-storefront nav, no Apollo tracker).
   Never renders auction photos. Data comes from /deals/api/* (public_deals.py). #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Surplus Radar — every US government surplus auction, tracked to close</title>
  <meta name="description" content="Live GovDeals lots with quantity-aware $/unit, landed cost, and recorded outcomes. A side project by Black Whole.">
  <meta name="robots" content="noindex, nofollow">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=JetBrains+Mono:wght@400;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/deals_public.css?v={{ now }}">
</head>
<body>
<header class="sr-head">
  <a class="sr-brand" href="/deals"><span class="sr-mark">◉</span> SURPLUS RADAR</a>
  <span class="sr-tag mono">every US government surplus auction, tracked to close</span>
  <a class="sr-head-link mono" href="#sr-about">ABOUT</a>
</header>

<details class="sr-about" id="sr-about">
  <summary class="mono">WHAT THIS IS <span id="sr-about-hint">— tap to collapse</span></summary>
  <div class="sr-about-grid">
    <div class="sr-about-copy">
      <h1>Government surplus, priced by the unit.</h1>
      <p>Surplus Radar sweeps every live GovDeals auction every six hours, follows each lot to its close, and records how it ended — the no-bids, the $1 wins, the final prices GovDeals itself never publishes.</p>
      <p>It reads the quantity out of the listing text so a "Lot of (286) HP EliteOne" is priced per machine, not per lot, and adds buyer premium so the number you see is what you'd actually pay.</p>
    </div>
    <div class="sr-stats" id="sr-stats">
      <div class="sr-stat"><div class="sr-stat-num" data-stat="tracked">—</div><div class="sr-stat-lab mono">LOTS TRACKED</div></div>
      <div class="sr-stat"><div class="sr-stat-num" data-stat="active">—</div><div class="sr-stat-lab mono">LIVE NOW</div></div>
      <div class="sr-stat"><div class="sr-stat-num" data-stat="closed">—</div><div class="sr-stat-lab mono">OUTCOMES RECORDED</div></div>
      <div class="sr-stat"><div class="sr-stat-num" data-stat="no_bid">—</div><div class="sr-stat-lab mono">CLOSED WITH NO BIDS</div></div>
      <div class="sr-stat"><div class="sr-stat-num" data-stat="states">—</div><div class="sr-stat-lab mono">STATES LIVE</div></div>
    </div>
  </div>
  <div class="sr-how">
    <div><b class="mono">01 SWEEP</b> maestro search API, whole site, every 6 h</div>
    <div><b class="mono">02 WATCH</b> re-read each lot's clock to its close; anti-snipe extensions included</div>
    <div><b class="mono">03 RECORD</b> outcome + final bid stored per auction — relists never overwrite history</div>
    <div><b class="mono">04 PRICE</b> quantity from the title → $/unit, plus 12.5 % buyer premium → landed</div>
  </div>
  <p class="sr-diff mono">vs GovAuctions: + quantity &amp; $/unit · + landed cost · + closed-auction history · − their multi-source aggregation (GovDeals only, for now)</p>
</details>

<form class="sr-topbar" id="sr-topbar" onsubmit="return false;">
  <input type="search" id="sr-q" placeholder="search titles — laptops, forklift, fryer…" autocomplete="off">
  <div class="sr-seg" data-key="max_bids">
    <button type="button" data-value="" class="on">any bids</button>
    <button type="button" data-value="0">no bids</button>
    <button type="button" data-value="3">≤ 3</button>
  </div>
  <div class="sr-seg" data-key="ending_within">
    <button type="button" data-value="" class="on">any time</button>
    <button type="button" data-value="24">&lt; 24 h</button>
    <button type="button" data-value="48">&lt; 48 h</button>
    <button type="button" data-value="168">&lt; 7 d</button>
  </div>
  <div class="sr-seg" data-key="status">
    <button type="button" data-value="active" class="on">live</button>
    <button type="button" data-value="closed">closed</button>
  </div>
  <select id="sr-sort" aria-label="sort">
    <option value="ends">ending soonest</option>
    <option value="newest">newest</option>
    <option value="bid:asc">lowest bid</option>
    <option value="bid:desc">highest bid</option>
    <option value="bids:desc">most bids</option>
  </select>
  <button type="button" class="sr-btn" id="sr-map-toggle">🗺 map</button>
</form>
<div class="sr-pills" id="sr-pills"></div>

<div class="sr-map-wrap" id="sr-map-wrap" hidden>
  <div class="sr-map" id="sr-map"></div>
  <div class="sr-map-note mono" id="sr-map-note"></div>
</div>

<div class="sr-layout">
  <aside class="sr-side" id="sr-side">
    <details open class="sr-side-box">
      <summary class="mono">CATEGORY</summary>
      <div id="sr-cats"></div>
    </details>
    <div class="sr-side-box">
      <label class="mono" for="sr-state">STATE</label>
      <select id="sr-state"><option value="">all states</option></select>
    </div>
    <div class="sr-side-box">
      <label class="mono">CURRENT BID $</label>
      <div class="sr-range">
        <input type="number" id="sr-min-price" placeholder="min" min="0">
        <input type="number" id="sr-max-price" placeholder="max" min="0">
      </div>
    </div>
    <div class="sr-side-box mono sr-fees">LANDED = bid × 1.125 (buyer premium). Tax and freight not included.</div>
    <button type="button" class="sr-btn sr-btn-ghost" id="sr-clear">clear all filters</button>
  </aside>

  <main class="sr-main">
    <div class="sr-pager" id="sr-pager-top"></div>
    <div class="sr-table-wrap">
      <table class="sr-table" id="sr-table">
        <thead><tr>
          <th>Title</th><th>Category</th><th>Location</th><th class="num">Bids</th>
          <th class="num">Bid</th><th class="num" title="parsed from the title; 1 when the title states no count">Qty</th>
          <th class="num" title="current bid ÷ quantity">$/unit</th><th class="num" title="bid + 12.5% buyer premium, per unit">Landed/unit</th>
          <th>Ends</th><th></th>
        </tr></thead>
        <tbody id="sr-rows"><tr><td colspan="10" class="sr-empty">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="sr-cards" id="sr-cards"></div>
    <div class="sr-pager" id="sr-pager-bottom"></div>
  </main>
</div>

<footer class="sr-foot mono">
  <span>SURPLUS RADAR · a Black Whole side project</span>
  <span>data: GovDeals public search API · photos stay on GovDeals</span>
</footer>

<script src="/static/admin_map.js"></script>
<script>window.SR_PER_PAGE = {{ per_page_choices | list | tojson }};</script>
<script src="/static/deals_public.js?v={{ now }}"></script>
</body>
</html>
```

- [ ] **Step 4: CSS**

```css
/* automation/web/static/deals_public.css — Surplus Radar (public /deals).
   Own look, deliberately not the chair storefront: dark slate, mono labels,
   one accent. Single committed theme (no light mode). */
:root {
  --bg: #0f1216; --bg-2: #161a21; --bg-3: #1d222b; --line: #2a303b;
  --ink: #e8e9ec; --ink-2: #a6adba; --ink-3: #6f7784;
  --accent: #ffb020; --ok: #3fb950; --warn: #ff5c5c;
  --f-display: "Archivo Black", "Helvetica Neue", Arial, sans-serif;
  --f-body: "IBM Plex Sans", system-ui, sans-serif;
  --f-mono: "JetBrains Mono", ui-monospace, Menlo, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--f-body); }
a { color: inherit; }
.mono { font-family: var(--f-mono); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; }
.num { text-align: right; font-variant-numeric: tabular-nums; }

/* header */
.sr-head { display: flex; align-items: center; gap: 18px; padding: 14px 24px; border-bottom: 1px solid var(--line); background: var(--bg-2); }
.sr-brand { font-family: var(--f-display); font-size: 18px; letter-spacing: .04em; text-decoration: none; }
.sr-mark { color: var(--accent); }
.sr-tag { color: var(--ink-3); flex: 1; }
.sr-head-link { color: var(--ink-2); text-decoration: none; }

/* portfolio landing */
.sr-about { border-bottom: 1px solid var(--line); background: var(--bg-2); }
.sr-about > summary { cursor: pointer; padding: 10px 24px; color: var(--ink-2); list-style: none; }
.sr-about > summary::-webkit-details-marker { display: none; }
.sr-about-grid { display: grid; grid-template-columns: 1.3fr 1fr; gap: 28px; padding: 8px 24px 18px; }
.sr-about-copy h1 { font-family: var(--f-display); font-size: clamp(24px, 3.6vw, 40px); line-height: 1.05; margin: 0 0 12px; }
.sr-about-copy p { margin: 0 0 10px; color: var(--ink-2); max-width: 60ch; }
.sr-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); }
.sr-stat { background: var(--bg-3); padding: 14px 12px; }
.sr-stat-num { font-family: var(--f-display); font-size: 26px; color: var(--accent); font-variant-numeric: tabular-nums; }
.sr-stat-lab { color: var(--ink-3); margin-top: 4px; }
.sr-how { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 24px; padding: 0 24px 12px; color: var(--ink-2); font-size: 14px; }
.sr-how b { display: block; color: var(--accent); margin-bottom: 2px; }
.sr-diff { padding: 0 24px 16px; color: var(--ink-3); margin: 0; }

/* top bar */
.sr-topbar { position: sticky; top: 0; z-index: 20; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  padding: 12px 24px; background: var(--bg); border-bottom: 1px solid var(--line); }
#sr-q { flex: 1 1 320px; min-width: 200px; padding: 10px 12px; font: inherit; background: var(--bg-3); color: var(--ink);
  border: 1px solid var(--line); border-radius: 6px; }
#sr-q:focus { outline: none; border-color: var(--accent); }
.sr-seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.sr-seg button { font: inherit; font-size: 13px; padding: 7px 11px; background: var(--bg-3); color: var(--ink-2); border: 0; cursor: pointer; }
.sr-seg button + button { border-left: 1px solid var(--line); }
.sr-seg button.on { background: var(--accent); color: #111; font-weight: 600; }
#sr-sort, .sr-side select, .sr-side input { font: inherit; font-size: 13px; padding: 7px 10px; background: var(--bg-3); color: var(--ink);
  border: 1px solid var(--line); border-radius: 6px; }
.sr-btn { font: inherit; font-size: 13px; padding: 7px 12px; background: var(--bg-3); color: var(--ink); border: 1px solid var(--line);
  border-radius: 6px; cursor: pointer; }
.sr-btn.on { border-color: var(--accent); color: var(--accent); }
.sr-btn-ghost { width: 100%; color: var(--ink-2); }
.sr-pills { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 24px 0; }
.sr-pill { font-family: var(--f-mono); font-size: 11px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px;
  color: var(--ink-2); cursor: pointer; background: var(--bg-2); }
.sr-pill.on { border-color: var(--accent); color: var(--accent); }
.sr-pill small { color: var(--ink-3); margin-left: 4px; }

/* map */
.sr-map-wrap { padding: 12px 24px 0; }
.sr-map { height: 380px; border: 1px solid var(--line); border-radius: 8px; }
.sr-map-note { color: var(--ink-3); padding: 6px 2px; }

/* two-column body */
.sr-layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 20px; padding: 16px 24px 40px; }
.sr-side { position: sticky; top: 64px; align-self: start; display: flex; flex-direction: column; gap: 12px; }
.sr-side-box { background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.sr-side-box > summary, .sr-side-box > label { display: block; color: var(--ink-3); margin-bottom: 8px; cursor: pointer; }
.sr-side-box select { width: 100%; }
.sr-range { display: flex; gap: 6px; }
.sr-range input { width: 50%; min-width: 0; }
.sr-cat { display: flex; justify-content: space-between; gap: 8px; padding: 5px 6px; border-radius: 4px; cursor: pointer; font-size: 13px; color: var(--ink-2); }
.sr-cat:hover { background: var(--bg-3); color: var(--ink); }
.sr-cat.on { color: var(--accent); background: var(--bg-3); }
.sr-cat small { color: var(--ink-3); font-variant-numeric: tabular-nums; }
.sr-fees { color: var(--ink-3); text-transform: none; letter-spacing: 0; font-size: 11px; }

/* results */
.sr-pager { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 6px 0 10px; color: var(--ink-2); font-size: 13px; }
.sr-pager .sr-total { flex: 1; font-family: var(--f-mono); font-size: 12px; }
.sr-pager button { font: inherit; font-size: 13px; padding: 5px 9px; background: var(--bg-3); color: var(--ink); border: 1px solid var(--line); border-radius: 5px; cursor: pointer; }
.sr-pager button:disabled { opacity: .35; cursor: default; }
.sr-pager input { width: 64px; font: inherit; font-size: 13px; padding: 5px 8px; background: var(--bg-3); color: var(--ink); border: 1px solid var(--line); border-radius: 5px; }
.sr-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-2); }
.sr-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.sr-table th { text-align: left; font-family: var(--f-mono); font-size: 11px; letter-spacing: .1em; color: var(--ink-3); padding: 10px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
.sr-table th.num { text-align: right; }
.sr-table td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.sr-table tr:last-child td { border-bottom: 0; }
.sr-table tr:hover td { background: var(--bg-3); }
.sr-title a { color: var(--ink); text-decoration: none; font-weight: 500; }
.sr-title a:hover { color: var(--accent); }
.sr-sub { color: var(--ink-3); font-size: 12px; }
.sr-qty-src { color: var(--ink-3); font-size: 11px; }
.sr-ends-red { color: var(--warn); font-weight: 600; }
.sr-ends-yellow { color: var(--accent); }
.sr-empty { text-align: center; color: var(--ink-3); padding: 28px !important; }
.sr-cards { display: none; }
.sr-card { background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
.sr-card-row { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; color: var(--ink-2); margin-top: 6px; }
.sr-foot { display: flex; justify-content: space-between; gap: 12px; padding: 16px 24px 28px; border-top: 1px solid var(--line); color: var(--ink-3); }

@media (max-width: 900px) {
  .sr-about-grid { grid-template-columns: 1fr; }
  .sr-layout { grid-template-columns: 1fr; padding: 12px 14px 32px; }
  .sr-side { position: static; }
  .sr-table-wrap { display: none; }
  .sr-cards { display: block; }
  .sr-head { flex-wrap: wrap; } .sr-tag { display: none; }
  .sr-topbar, .sr-pills, .sr-map-wrap { padding-left: 14px; padding-right: 14px; }
}
```

- [ ] **Step 5: JS**

```js
/* automation/web/static/deals_public.js — Surplus Radar page logic.
 * All state lives in the URL query string (shareable links, back button
 * works). One fetch per state change; facets once (5-min server cache). */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (t) => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const money = (v) => v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 2});
  const PER_PAGE = window.SR_PER_PAGE || [25, 50, 100];
  const KEYS = ['q', 'category', 'state', 'max_bids', 'ending_within', 'status', 'min_price', 'max_price', 'sort', 'dir', 'page', 'per_page', 'bbox'];
  const DEFAULTS = {status: 'active', sort: 'ends', page: '1', per_page: String(PER_PAGE[0])};

  const st = Object.assign({}, DEFAULTS, Object.fromEntries(
    KEYS.map(k => [k, new URLSearchParams(location.search).get(k)]).filter(([, v]) => v)));
  let map = null, mapOn = false, facets = null, lastBody = null;

  function qs(extra = {}) {
    const p = new URLSearchParams();
    for (const k of KEYS) { const v = (extra[k] !== undefined ? extra[k] : st[k]); if (v !== undefined && v !== null && v !== '') p.set(k, v); }
    return p;
  }
  function pushUrl() {
    const p = qs({bbox: ''});  // bbox is transient — never in the shareable URL
    for (const k of Object.keys(DEFAULTS)) if (p.get(k) === DEFAULTS[k]) p.delete(k);
    history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
  }
  function set(patch, {resetPage = true} = {}) {
    Object.assign(st, patch);
    if (resetPage) st.page = '1';
    pushUrl(); syncControls(); load();
    if (mapOn) loadPins();
  }

  // ── controls ──────────────────────────────────────────────────────────
  function syncControls() {
    $('#sr-q').value = st.q || '';
    $$('.sr-seg').forEach(seg => {
      const cur = st[seg.dataset.key] ?? '';
      $$('button', seg).forEach(b => b.classList.toggle('on', (b.dataset.value || '') === String(cur)));
    });
    $('#sr-sort').value = st.dir ? `${st.sort}:${st.dir}` : st.sort;
    $('#sr-state').value = st.state || '';
    $('#sr-min-price').value = st.min_price || '';
    $('#sr-max-price').value = st.max_price || '';
    $$('.sr-pill').forEach(p => p.classList.toggle('on', (p.dataset.cat || '') === (st.category || '')));
    $$('.sr-cat').forEach(p => p.classList.toggle('on', (p.dataset.cat || '') === (st.category || '')));
  }
  let qTimer = null;
  $('#sr-q').addEventListener('input', (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => set({q: e.target.value.trim()}), 300); });
  $$('.sr-seg').forEach(seg => seg.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    set({[seg.dataset.key]: b.dataset.value});
  }));
  $('#sr-sort').addEventListener('change', (e) => { const [sort, dir] = e.target.value.split(':'); set({sort, dir: dir || ''}); });
  $('#sr-state').addEventListener('change', (e) => set({state: e.target.value}));
  let priceTimer = null;
  ['#sr-min-price', '#sr-max-price'].forEach(sel => $(sel).addEventListener('input', () => {
    clearTimeout(priceTimer);
    priceTimer = setTimeout(() => set({min_price: $('#sr-min-price').value, max_price: $('#sr-max-price').value}), 400);
  }));
  $('#sr-clear').addEventListener('click', () => {
    for (const k of KEYS) delete st[k];
    Object.assign(st, DEFAULTS); set({});
  });
  $('#sr-pills').addEventListener('click', (e) => { const p = e.target.closest('.sr-pill'); if (p) set({category: p.dataset.cat}); });
  $('#sr-cats').addEventListener('click', (e) => { const p = e.target.closest('.sr-cat'); if (p) set({category: p.dataset.cat}); });

  // ── facets + portfolio stats ──────────────────────────────────────────
  async function loadFacets() {
    try {
      const r = await fetch('/deals/api/facets'); if (!r.ok) throw new Error(r.status);
      facets = await r.json();
    } catch (e) { facets = {categories: [], states: [], stats: {}}; }
    const s = facets.stats || {};
    $$('#sr-stats [data-stat]').forEach(el => { const v = s[el.dataset.stat]; el.textContent = v == null ? '—' : Number(v).toLocaleString(); });
    const cats = facets.categories || [];
    const total = cats.reduce((a, c) => a + Number(c.count || 0), 0);
    $('#sr-pills').innerHTML = `<span class="sr-pill" data-cat="">all<small>${total.toLocaleString()}</small></span>` +
      cats.slice(0, 10).map(c => `<span class="sr-pill" data-cat="${esc(c.value)}">${esc(c.value.replace(/_/g, ' '))}<small>${Number(c.count).toLocaleString()}</small></span>`).join('');
    $('#sr-cats').innerHTML = `<div class="sr-cat" data-cat="">all categories<small>${total.toLocaleString()}</small></div>` +
      cats.map(c => `<div class="sr-cat" data-cat="${esc(c.value)}">${esc(c.value.replace(/_/g, ' '))}<small>${Number(c.count).toLocaleString()}</small></div>`).join('');
    const sel = $('#sr-state');
    (facets.states || []).forEach(f => { const o = document.createElement('option'); o.value = f.value; o.textContent = `${f.value} (${f.count})`; sel.appendChild(o); });
    syncControls();
  }

  // ── list ──────────────────────────────────────────────────────────────
  function endsCell(iso, closed) {
    if (!iso) return '—';
    const ms = new Date(iso) - Date.now();
    if (closed || ms < 0) return `<span class="sr-sub">${new Date(iso).toLocaleDateString()}</span>`;
    const h = ms / 36e5;
    const txt = h < 1 ? `${Math.max(1, Math.round(ms / 6e4))} min` : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`;
    return `<span class="${h < 6 ? 'sr-ends-red' : h < 24 ? 'sr-ends-yellow' : ''}">${txt}</span>`;
  }
  function rowHtml(r) {
    const closed = !!r.outcome_complete;
    const bid = closed && r.final_bid != null ? r.final_bid : r.current_bid;
    const qty = `${r.quantity}${r.quantity_source === 'default' ? '<div class="sr-qty-src">n/a</div>' : ''}`;
    return `<tr>
      <td class="sr-title"><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
        <div class="sr-sub">${esc(r.native_category_name || '')}${closed ? ` · closed${r.outcome ? ' · ' + esc(r.outcome).replace(/_/g, ' ') : ''}` : ''}</div></td>
      <td>${esc((r.canonical_category || '—').replace(/_/g, ' '))}</td>
      <td>${esc(r.city || '')}${r.state ? ', ' + esc(r.state) : ''}</td>
      <td class="num">${r.bid_count ?? '—'}</td>
      <td class="num">${money(bid)}</td>
      <td class="num">${qty}</td>
      <td class="num">${money(r.unit_bid)}</td>
      <td class="num">${money(r.unit_landed)}</td>
      <td>${endsCell(r.end_utc, closed)}</td>
      <td><a class="sr-sub" href="${esc(r.viewer_url)}" title="our archived copy (text only)">⧉</a></td>
    </tr>`;
  }
  function cardHtml(r) {
    return `<div class="sr-card">
      <div class="sr-title"><a href="${esc(r.govdeals_url)}" target="_blank" rel="noopener">${esc(r.title)}</a></div>
      <div class="sr-card-row"><span>${esc(r.city || '')}${r.state ? ', ' + esc(r.state) : ''}</span><span>${endsCell(r.end_utc, !!r.outcome_complete)}</span></div>
      <div class="sr-card-row"><span>${r.bid_count ?? 0} bids · ${money(r.current_bid)}</span><span>qty ${r.quantity} · ${money(r.unit_bid)}/unit</span></div>
    </div>`;
  }
  function pagerHtml(b, id) {
    const first = (b.page - 1) * b.per_page + 1, last = Math.min(b.total, b.page * b.per_page);
    return `<span class="sr-total">PAGE ${b.page} OF ${b.pages.toLocaleString()} · ${b.total.toLocaleString()} LOTS${b.total ? ` · ${first}–${last}` : ''}${st.bbox ? ' · IN MAP VIEW' : ''}</span>
      <button data-go="1" ${b.page <= 1 ? 'disabled' : ''}>«</button>
      <button data-go="${b.page - 1}" ${b.page <= 1 ? 'disabled' : ''}>‹ prev</button>
      <button data-go="${b.page + 1}" ${b.page >= b.pages ? 'disabled' : ''}>next ›</button>
      <button data-go="${b.pages}" ${b.page >= b.pages ? 'disabled' : ''}>»</button>
      <input type="number" min="1" max="${b.pages}" value="${b.page}" aria-label="jump to page" data-jump>
      <select data-per-page aria-label="rows per page">${PER_PAGE.map(n => `<option value="${n}" ${n === b.per_page ? 'selected' : ''}>${n} / page</option>`).join('')}</select>`;
  }
  async function load() {
    const tbody = $('#sr-rows');
    tbody.innerHTML = '<tr><td colspan="10" class="sr-empty">Loading…</td></tr>';
    let body;
    try {
      const r = await fetch('/deals/api/lots?' + qs().toString());
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      body = await r.json();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="10" class="sr-empty">Could not load lots (${esc(e.message || e)}). Try again in a minute.</td></tr>`;
      return;
    }
    lastBody = body;
    tbody.innerHTML = body.rows.length ? body.rows.map(rowHtml).join('') : '<tr><td colspan="10" class="sr-empty">No lots match. Clear a filter.</td></tr>';
    $('#sr-cards').innerHTML = body.rows.map(cardHtml).join('');
    for (const id of ['#sr-pager-top', '#sr-pager-bottom']) $(id).innerHTML = pagerHtml(body, id);
    if (String(body.page) !== st.page) { st.page = String(body.page); pushUrl(); }
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('.sr-pager button[data-go]'); if (!b || b.disabled) return;
    set({page: b.dataset.go}, {resetPage: false}); window.scrollTo({top: $('#sr-pager-top').offsetTop - 120, behavior: 'smooth'});
  });
  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-jump]')) set({page: String(Math.max(1, Number(e.target.value) || 1))}, {resetPage: false});
    if (e.target.matches('[data-per-page]')) set({per_page: e.target.value});
  });

  // ── map (reuses admin_map.js; pins are exclusion-filtered server-side) ─
  async function loadPins() {
    const p = qs({page: '', per_page: '', sort: '', dir: '', bbox: ''});
    let data;
    try { const r = await fetch('/deals/api/pins?' + p.toString()); data = await r.json(); }
    catch (e) { $('#sr-map-note').textContent = 'map data unavailable'; return; }
    map.setPoints(data.points.map(pt => ({
      lat: pt.lat, lng: pt.lng, title: pt.title,
      popup: `<b>${esc(pt.title)}</b><br>${esc(pt.city || '')}, ${esc(pt.state || '')}<br>${money(pt.current_bid)} · ${pt.bid_count ?? 0} bids<br><a href="${esc(pt.govdeals_url)}" target="_blank" rel="noopener">GovDeals ↗</a>`,
    })));
    if (!st.bbox) map.fit();
    $('#sr-map-note').textContent = `${data.points.length.toLocaleString()} lots pinned${data.capped ? ' (first 5,000 — narrow the filters)' : ''} · pan to filter the list`;
  }
  $('#sr-map-toggle').addEventListener('click', async () => {
    mapOn = !mapOn;
    $('#sr-map-toggle').classList.toggle('on', mapOn);
    $('#sr-map-wrap').hidden = !mapOn;
    if (mapOn && !map) {
      map = await window.AdminMap.mount($('#sr-map'));
      map.onViewport(() => { st.bbox = map.bboxParam(); set({}, {resetPage: true}); });
    }
    if (mapOn) { map.invalidateSize && map.invalidateSize(); loadPins(); }
    else { st.bbox = ''; set({}); }
  });

  // ── about block: open on first visit, remember collapse ───────────────
  try {
    const about = $('#sr-about');
    about.open = localStorage.getItem('sr.about') !== 'closed';
    about.addEventListener('toggle', () => localStorage.setItem('sr.about', about.open ? 'open' : 'closed'));
  } catch (_) { $('#sr-about').open = true; }

  syncControls(); loadFacets(); load();
})();
```

- [ ] **Step 6: Run tests + eyeball**

Run: `.venv/bin/python -m pytest tests/web/ -q` → PASS.
Relaunch: `pkill -f automation.web; (.venv/bin/python -m automation.web >/tmp/web.log 2>&1 &)`; open `http://127.0.0.1:8765/deals` and check: search "laptop" narrows, pills filter, pager shows `PAGE 1 OF N · total`, `?q=laptop&page=2` survives reload, map toggle pins and panning updates "IN MAP VIEW", no `<img>` anywhere (`curl -s http://127.0.0.1:8765/deals | grep -c "<img"` → 0), window < 900 px shows cards.

- [ ] **Step 7: Commit**

```bash
git add automation/web/templates/deals_public.html automation/web/static/deals_public.css automation/web/static/deals_public.js tests/web/test_public_deals_page.py
git commit -m "web: public Surplus Radar page — top-bar search, side filters, real pager, portfolio landing, map"
```

**Done when:** template test green; manual checklist above passes; no image tags on the page.

---

### Task 8: Admin Deals tab — top/side layout, real pager, Qty + $/unit columns, cached facets

**Files:**
- Modify: `automation/web/templates/index.html` (Deals panel: the `<form class="auction-controls deal-controls">` … `</form>`, `.deal-layout` aside, `.deal-pager`)
- Modify: `automation/web/static/app.js` (`deal` state, `loadDeals` pager block ~lines 2491-2497, `#deal-prev/#deal-next` handlers ~2636-2637, row template ~2465-2485)
- Modify: `automation/web/static/app.css` (`.deal-tree` → wider side panel, pager)
- Modify: `automation/web/app.py:600-690` (`list_deals` — facets/stats cache)
- Test: `tests/web/test_deals_api.py` (extend)

**Interfaces:**
- Consumes: `deals_query.enrich` fields `quantity`, `unit_bid`, `unit_landed` (Task 3).
- Produces: `/api/deals` response unchanged in shape; facets+stats served from a 120 s in-process cache (`_DEALS_FACETS_TTL`); `deals_facets_cache_clear()` for tests.

- [ ] **Step 1: Write the failing test (facets cached, rows not)**

Append to `tests/web/test_deals_api.py`:
```python
def test_facets_and_stats_are_cached_between_calls(monkeypatch):
    webapp = importlib.import_module("automation.web.app")
    webapp.deals_facets_cache_clear()
    client, cap = _client(monkeypatch, [ROW])
    client.get("/api/deals")
    client.get("/api/deals?page=2")
    facet_sqls = [s for s in cap["sqls"] if "GROUP BY 1 ORDER BY count DESC" in s]
    row_sqls = [s for s in cap["sqls"] if "row_to_json(v.*) AS verdict" in s]
    assert len(row_sqls) == 2          # every call fetches rows
    assert len(facet_sqls) == 2        # categories + states: ONE pair, not two
    webapp.deals_facets_cache_clear()
```
(`_client` already records every SQL in `cap["sqls"]`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_deals_api.py -q`
Expected: FAIL — `AttributeError: deals_facets_cache_clear` (then 4 facet SQLs).

- [ ] **Step 3: Cache facets/stats in `list_deals`**

In app.py, above `list_deals`:
```python
_DEALS_FACETS_TTL = 120
_DEALS_FACETS: dict[str, tuple[float, tuple]] = {}


def deals_facets_cache_clear() -> None:
    _DEALS_FACETS.clear()


def _deals_facets_and_stats() -> tuple[list, list, dict]:
    """Categories/states facets + headline stats for the admin Deals tab.

    These three queries don't depend on the filter set and each opened its own
    pooler connection (~1.3 s) on every page flip. 120 s cache."""
    hit = _DEALS_FACETS.get("v")
    if hit and time.monotonic() - hit[0] < _DEALS_FACETS_TTL:
        return hit[1]
    cats = db.fetch_all(
        "SELECT canonical_category AS value, count(*) AS count FROM deal_lots "
        f"WHERE {_DEALS_ACTIVE} AND canonical_category IS NOT NULL "
        "GROUP BY 1 ORDER BY count DESC"
    )
    states = db.fetch_all(
        "SELECT state AS value, count(*) AS count FROM deal_lots "
        f"WHERE {_DEALS_ACTIVE} AND state IS NOT NULL "
        "GROUP BY 1 ORDER BY count DESC"
    )
    stats = db.fetch_one(
        "SELECT (SELECT count(*) FROM deal_lots) AS total_lots, "
        "(SELECT count(*) FROM deal_candidates) AS candidates, "
        f"(SELECT count(*) FROM deal_lots WHERE {_DEALS_ACTIVE} "
        "AND end_utc <= now() + interval '24 hours') AS ending_24h"
    )
    value = (cats, states, stats)
    _DEALS_FACETS["v"] = (time.monotonic(), value)
    return value
```
and inside `list_deals._fetch` replace the `cats = …`, `states = …`, `stats = …` blocks with `cats, states, stats = _deals_facets_and_stats()`.

- [ ] **Step 4: Run the API tests**

Run: `.venv/bin/python -m pytest tests/web/test_deals_api.py -q` → PASS.

- [ ] **Step 5: Reshape the Deals panel markup**

In index.html, inside `<section class="panel" data-pane="deals" hidden>`:

Top bar (replace the whole `<form class="auction-controls deal-controls">` with this — only search + the "what am I looking at" toggles stay on top):
```html
  <form class="auction-controls deal-controls deal-topbar" onsubmit="return false;">
    <div class="ac-group ac-grow">
      <span class="ac-label">SEARCH</span>
      <input type="text" id="deal-q" placeholder="title or description…">
    </div>
    <div class="ac-group">
      <span class="ac-label">BIDS</span>
      <div class="seg" id="deal-bids-filter">
        <button type="button" class="seg-btn active" data-value="">any</button>
        <button type="button" class="seg-btn" data-value="0">0 bids</button>
        <button type="button" class="seg-btn" data-value="3">≤3</button>
      </div>
    </div>
    <div class="ac-group">
      <span class="ac-label">ENDING</span>
      <select id="deal-ending">
        <option value="">any time</option>
        <option value="6">&lt; 6h</option>
        <option value="24">&lt; 24h</option>
        <option value="48">&lt; 48h</option>
        <option value="168">&lt; 7d</option>
      </select>
    </div>
    <div class="ac-group">
      <span class="ac-label">STATUS</span>
      <div class="seg" id="deal-status-filter">
        <button type="button" class="seg-btn active" data-value="active">active</button>
        <button type="button" class="seg-btn" data-value="closed">closed</button>
        <button type="button" class="seg-btn" data-value="all">all</button>
      </div>
    </div>
    <div class="ac-actions">
      <button type="button" class="btn btn-small" id="deal-map-toggle" title="Map of the filtered lots — pan/zoom to narrow the table to the viewport">🗺 map</button>
      <button type="button" class="btn btn-small" id="deal-refresh">↻ refresh</button>
    </div>
  </form>
```
Keep `#deal-cat-pills`, `#deal-active-chips`, `#deal-map-wrap` where they are. Replace the `<aside class="deal-tree" …>` with a wider side panel that holds the tree AND the secondary filters (same element ids, so app.js handlers keep working):
```html
    <aside class="deal-side" aria-label="Filters">
      <div class="deal-side-box">
        <div class="deal-tree-head">CATEGORY TREE
          <span class="deal-tree-legend" title="lots · zero-bid lots · ending &lt;24h">n · ⚑ 0-bid · ⏱ &lt;24h</span>
        </div>
        <div class="deal-tree" id="deal-tree-nodes"><div class="drafts-empty">Loading…</div></div>
      </div>
      <div class="deal-side-box deal-side-filters">
        <label class="ac-label" for="deal-category">CATEGORY</label>
        <select id="deal-category"><option value="">all</option></select>
        <label class="ac-label" for="deal-state">STATE</label>
        <select id="deal-state"><option value="">all</option></select>
        <label class="ac-label" title="Filter on current bid ($)">PRICE</label>
        <div class="deal-side-row">
          <input type="number" id="deal-min-price" class="deal-num" placeholder="$ min" min="0">
          <input type="number" id="deal-max-price" class="deal-num" placeholder="$ max" min="0">
        </div>
        <label class="ac-label" for="deal-min-margin" title="Only analyzed lots with margin ≥ this %">MARGIN ≥ %</label>
        <input type="number" id="deal-min-margin" class="deal-num" placeholder="%" min="0" step="10">
        <label class="ac-label" for="deal-max-dist" title="Max distance from home (mi); drops lots with unknown coords">DIST ≤ MI</label>
        <input type="number" id="deal-max-dist" class="deal-num" placeholder="mi" min="0" step="25">
        <label class="ac-label" for="deal-zip" title="Center the map on a ZIP">ZIP</label>
        <input type="text" id="deal-zip" class="deal-num" placeholder="ZIP →" maxlength="5" inputmode="numeric">
        <label class="ac-label" for="deal-list">LIST</label>
        <select id="deal-list"><option value="">all</option></select>
        <label class="ac-label" for="deal-tag">TAG</label>
        <select id="deal-tag"><option value="">all</option></select>
        <div class="deal-side-row">
          <button type="button" class="btn btn-small" id="deal-save-search" title="Save the current filter state as a named search">★ save search</button>
          <button type="button" class="btn btn-small" id="deal-create-alert" title="Save current filters as an alert — checked hourly, new matches → Telegram">🔔 alert</button>
        </div>
      </div>
    </aside>
```
Table: add two headers after `Bid`:
```html
            <th class="num" title="parsed from the title (deals/quantity.py); 1 when the title states no count">Qty</th>
            <th class="num" title="current bid ÷ quantity">$/unit</th>
```
and bump every `colspan="16"` in the Deals tbody messages (markup + app.js) to `colspan="18"`. Pager: replace the `<div class="deal-pager">` block with an id'd container at the top of `.inv-table-wrap` and one at the bottom:
```html
      <div class="deal-pager" id="deal-pager-top"></div>
      …table…
      <div class="deal-pager" id="deal-pager-bottom"></div>
```

- [ ] **Step 6: app.js — pager + cells**

In the row template of `loadDeals`, after `<td>${r.current_bid != null ? '$' + r.current_bid : '—'}</td>` add:
```js
        <td class="num">${r.quantity}${r.quantity_source === 'default' ? '<span class="deal-qty-src" title="no count in title">·</span>' : ''}</td>
        <td class="num">${r.unit_bid != null ? '$' + r.unit_bid : '—'}</td>
```
Replace the pager block at the end of `loadDeals` (from `const page = …` through `$('#deal-next').disabled = …`) with:
```js
  renderDealPager(body.total);
```
Add next to `loadDeals`:
```js
const DEAL_PAGE_SIZES = [25, 50, 100, 200];
function renderDealPager(total) {
  const page = Math.floor(deal.offset / deal.limit) + 1;
  const pages = Math.max(1, Math.ceil(total / deal.limit));
  const scope = deal.mapOn && deal.bbox ? ' · in map view' : '';
  const html = `
    <span class="deal-pager-total">page ${page} / ${pages} · ${total.toLocaleString()} lots${scope}</span>
    <button type="button" class="btn btn-small" data-page="1" ${page <= 1 ? 'disabled' : ''}>«</button>
    <button type="button" class="btn btn-small" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>‹ prev</button>
    <button type="button" class="btn btn-small" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>next ›</button>
    <button type="button" class="btn btn-small" data-page="${pages}" ${page >= pages ? 'disabled' : ''}>»</button>
    <input type="number" class="deal-num" min="1" max="${pages}" value="${page}" data-jump title="jump to page">
    <select data-limit>${DEAL_PAGE_SIZES.map(n => `<option value="${n}" ${n === deal.limit ? 'selected' : ''}>${n}/page</option>`).join('')}</select>`;
  $('#deal-pager-top').innerHTML = html;
  $('#deal-pager-bottom').innerHTML = html;
}
['#deal-pager-top', '#deal-pager-bottom'].forEach(sel => {
  $(sel).addEventListener('click', (e) => {
    const b = e.target.closest('button[data-page]'); if (!b || b.disabled) return;
    deal.offset = (Number(b.dataset.page) - 1) * deal.limit; loadDeals();
  });
  $(sel).addEventListener('change', (e) => {
    if (e.target.matches('[data-jump]')) { deal.offset = (Math.max(1, Number(e.target.value) || 1) - 1) * deal.limit; loadDeals(); }
    if (e.target.matches('[data-limit]')) { deal.limit = Number(e.target.value); deal.offset = 0; loadDeals(); }
  });
});
```
Delete the two old handlers `$('#deal-prev').addEventListener(...)` and `$('#deal-next').addEventListener(...)`. Grep `deal-page-info|deal-prev|deal-next` — zero hits when done.

- [ ] **Step 7: app.css**

Replace the `.deal-tree { … }` rule and add:
```css
.deal-topbar .ac-grow { flex: 1 1 320px; }
.deal-topbar .ac-grow input[type=text] { width: 100%; }
.deal-side { flex: 0 0 280px; position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow-y: auto;
  display: flex; flex-direction: column; gap: 10px; }
.deal-side-box { background: var(--bg-elev); border: 1px solid var(--line); font-family: var(--mono); font-size: 11px; }
.deal-tree { max-height: 42vh; overflow-y: auto; }
.deal-side-filters { display: flex; flex-direction: column; gap: 6px; padding: 10px; }
.deal-side-filters select, .deal-side-filters .deal-num { width: 100%; }
.deal-side-row { display: flex; gap: 6px; }
.deal-side-row .deal-num { width: 50%; }
.deal-pager .deal-pager-total { flex: 1; }
.deal-pager select { padding: 3px 6px; background: var(--bg); border: 1px solid var(--line); color: var(--ink); font: inherit; }
.deal-qty-src { color: var(--ink-dim); margin-left: 2px; }
@media (max-width: 900px) { .deal-side { position: static; flex: none; width: 100%; max-height: none; } }
```
(keep the existing `.deal-tree-head`, `.dt-*` rules; the `@media` block that referenced `.deal-tree` is superseded by this one.)

- [ ] **Step 8: Verify**

Run: `.venv/bin/python -m pytest tests/web/ -q` → PASS. Relaunch the server, open `/admin` → Deals: search on top, tree + secondary filters on the left, Qty and $/unit columns present, pager at top and bottom with «‹›» + jump + page size, page flip is visibly faster (one rows query + one count instead of five queries; watch `/tmp/web.log` timings).

- [ ] **Step 9: Commit**

```bash
git add automation/web/templates/index.html automation/web/static/app.js automation/web/static/app.css automation/web/app.py tests/web/test_deals_api.py
git commit -m "admin: Deals tab — search on top, filters on the side, first/prev/next/last pager with page size, Qty + \$/unit columns, cached facets"
```

**Done when:** test green; `grep -c "deal-page-info" automation/web/static/app.js` → 0; page flips run 2 DB queries, not 5.

---

### Task 9: Docs, feature map, CLAUDE.md hard rule

**Files:**
- Modify: `docs/claude-reference/repo-layout.md` (public site routes list + admin tab list)
- Modify: `docs/claude-reference/deals.md` (new "Public surface" section)
- Modify: `docs/claude-reference/todos-and-history.md` (TODO #4 → moved; Done entry)
- Modify: `docs/govauctions-feature-map.md` (new rows)
- Modify: `CLAUDE.md` (one hard-rule bullet + one key-path line)

- [ ] **Step 1: repo-layout.md**

In the public-site bullet list add:
```markdown
      - `GET  /deals` — **public "Surplus Radar"** (`deals_public.html` + `deals_public.css/js`, standalone chrome, `noindex`, unlinked from the storefront). JSON under `/deals/api/lots|pins|facets` (outside the `/api/` auth prefix). All reads go through `web/public_deals.py` — the ONE place the chair-buyer exclusion policy lives (seating category + seating keywords + every lot in `tracked_lots`/`auction_favorites`/`deal_list_items`). Never selects photo columns, verdicts, or `distance_mi`.
```
In the admin bullet: replace the tab list with `01 Launcher / 02 Drafts / 03 Auctions / 04 Inventory / 05 Inquiries / 06 Listings DB / 07 Test Scrape / 08 Subscribers / 09 Deals / 10 Tracking` and append: "A/B tab removed 2026-09-04 (`llm_compare_logs` still written + read by the Inventory backfill). Deals tab: search/bids/ending/status on top, category tree + the rest on the side, first/prev/next/last pager with 25–200 page size, `Qty` + `$/unit` columns (`deals/quantity.py`), facets/stats cached 120 s." In the Admin JSON APIs bullet, note `/api/inventory?with_stats=1`.

- [ ] **Step 2: deals.md — append**

```markdown
## Public surface — `/deals` "Surplus Radar" (2026-09-04)

- Summary: the Deals tab's data, made public for anyone, minus everything a chair buyer could use against us.
  - Path, not hostname: `black-whole.com/deals`, `robots.txt` `Disallow: /deals`, no storefront links, own branding. Subdomain is a 30-min follow-up.
  - Policy = `automation/web/public_deals.py`. Excluded: `canonical_category IN PUBLIC_DEALS_EXCLUDE_CATEGORIES` (default `seating_furniture`), titles matching `PUBLIC_DEALS_EXCLUDE_KEYWORDS` (chair/seating/stool/bench/pew/barstool/sofa/couch/banquet), and every lot in `tracked_lots` / `auction_favorites` / `deal_list_items`. Tables/desks in the furniture bucket are excluded too — accepted.
  - Never public: photo columns (copyright), `description`, verdicts/margins, `distance_mi`, `seller`, `high_bidder`. The archived-lot viewer hides its gallery + DealCard unless an operator session cookie is present, and 404s excluded lots for the public.
  - Paging: offset, `page`/`per_page` (25/50/100), `total`+`pages`, page ≤ 400. Search is title-only on the public side (trigram index `ix_deal_lots_active_title_trgm`).
  - Quantity: `deals/quantity.py` → `explicit_title_quantity` (no LLM, no column). `quantity_source='default'` means "title states no count" — the UI marks it.
  - Cuts + follow-ups: `$/unit` sort and qty filter need a persisted `deal_lots.quantity` (blocked on DB headroom → `scripts/reclaim_db_space.py --all` first); verdict coverage of active lots is 0 because the Render `deals-analyze` cron isn't applied.
```

- [ ] **Step 3: todos-and-history.md**

Replace TODO #4 with: `4. **Dashboard cost-tracking tile.** \`dewatermark_usage.jsonl\` exists; surface \`today: N calls / cache: M hashes\` on the Launcher tab (the A/B tab is gone).` Add a Done line: `- Public Surplus Radar (/deals) + admin Deals reshape + Inventory speed fix + A/B tab removal — 2026-09-04. Plan: docs/superpowers/plans/2026-09-04-public-deals-site.md.`

- [ ] **Step 4: govauctions-feature-map.md — append rows**

```markdown
| 18 | (they lack) quantity per lot | — | Built: `deals/quantity.py` → Qty + $/unit + landed/unit columns, admin + public |
| 19 | (they lack) closed-auction outcomes | — | Already had (`outcome`, `final_bid`); now public on /deals with status=closed |
| 20 | Public site | govauctions.app/feed | Built: `/deals` (Task 7), exclusion-filtered, paged 25/50/100, noindex |
```

- [ ] **Step 5: CLAUDE.md**

Under **deals/** hard rules add:
```markdown
- **Public `/deals` never shows auction photos, verdicts, home distance, seating lots, or any lot in `tracked_lots`/`auction_favorites`/`deal_list_items`.** The policy is `automation/web/public_deals.py` — add exclusions there (env `PUBLIC_DEALS_EXCLUDE_*`), never in a template. Public JSON lives under `/deals/api/`, not `/api/`.
```
Under **Key paths** add: `Public deals: http://127.0.0.1:8765/deals (JSON `/deals/api/lots?page=&per_page=`).`

- [ ] **Step 6: Commit + push + PR**

```bash
git add docs/claude-reference/repo-layout.md docs/claude-reference/deals.md docs/claude-reference/todos-and-history.md docs/govauctions-feature-map.md CLAUDE.md
git commit -m "docs: public Surplus Radar, admin Deals reshape, Inventory fix, A/B removal"
git push -u origin feat/public-deals-site
gh pr create --title "Public Surplus Radar (/deals) + admin Deals reshape + Inventory speed + A/B removal" --body-file docs/superpowers/plans/2026-09-04-public-deals-site.md
```

**Done when:** `.venv/bin/python -m pytest tests/web/ tests/deals/ tests/test_inventory_list_with_stats.py -q` green; PR open; `curl -s https://black-whole.com/robots.txt | grep -c "Disallow: /deals"` → 1 after deploy.

---

## Self-review (run at plan-write time — results)

1. **Spec coverage.** A/B cleanup → T1. Public site like Auctions → T5–T7. Chair-buyer isolation (category/lot exclusion + path/robots choice, justified) → T5/T6 + Decisions. Inventory slow (endpoint + query found, fix as task) → T2. Follow GovAuctions map + better UI → T7 (map reuse, layout). Better pagination → T7 (public) + T8 (admin). Search on top, rest on side → T7 + T8. Quantity extraction for value → T3 (+ columns in T7/T8). Portfolio landing with live counts → T7 (`#sr-about`, `/deals/api/facets` stats). RLS/photo/geo/auth constraints → Global Constraints + T5/T6 tests. Every task has a done-when and a test command. Cuts listed.
2. **Placeholder scan.** No TBD/TODO-style steps; every code step has the code.
3. **Type consistency.** `fetch_page(**kw)` keyword names match the route params and the test's `echo`; `clamp_page` is used in `fetch_page` and tested; `deals_facets_cache_clear` defined in T8 step 3 and used in its test; `show_images` set in T6 and read in the template; `PER_PAGE_CHOICES` passed as `per_page_choices` and read via `window.SR_PER_PAGE`; `enrich` keys (`quantity`, `quantity_source`, `unit_bid`, `unit_landed`) match both UIs.

**Time estimate:** T1 30 min · T2 45 · T3 30 · T4 20 · T5 75 · T6 60 · T7 120 · T8 90 · T9 20 ≈ 8 h. Stop-anywhere order: T1–T4 are independent wins; T5–T7 ship the public page; T8–T9 polish.

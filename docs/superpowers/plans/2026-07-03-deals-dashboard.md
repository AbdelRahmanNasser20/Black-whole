# Deals Dashboard (BLACKWHOLE-12) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `10 Deals` admin-dashboard tab with search/filter/sort over Supabase `deal_lots`, including a computed landed-cost column, backed by a new `GET /api/deals` endpoint.

**Architecture:** A pure query-builder module (`automation/web/deals_query.py`) turns request params into a parameterized WHERE clause + whitelisted ORDER BY (unit-tested, no DB). A thin FastAPI endpoint in `automation/web/app.py` runs the queries via `automation.db` off-thread and enriches rows with `deals.fees.landed_cost`. The UI follows the existing admin tab pattern (button + `data-pane` panel in `index.html`, loader in `app.js`, `inv-table` styling).

**Tech Stack:** FastAPI, psycopg (Supabase Postgres) via `automation/db.py`, vanilla JS admin dashboard, pytest.

**Spec:** `docs/superpowers/specs/2026-07-03-deals-dashboard-design.md`

## Global Constraints

- All SQL is parameterized (`%s`); sort columns come ONLY from a whitelist dict — never interpolate user input into SQL.
- Fee defaults: `DEALS_BUYER_PREMIUM_PCT=0.125`, `DEALS_TAX_PCT=0.0`, `DEALS_FREIGHT=0.0` (env-overridable).
- Run tests with `.venv/bin/python -m pytest` (this venv has no `pytest` console script). Note: pytest must run from the **main checkout root or worktree root** — either works, tests are path-independent.
- Server restart required after touching `app.py` / templates / static (FastAPI caches).
- `status` lifecycle: `active` = `outcome_complete IS NOT TRUE AND end_utc > now()`; `closed` = `outcome_complete IS TRUE`.
- Dark admin theme; reuse `inv-table`, `seg-btn`, `ac-group`, `btn btn-small` classes. New tab is `<span class="tab-num">10</span><span class="tab-label">Deals</span>`, `data-tab="deals"`.

---

### Task 1: Fee model from env (`deals/fees.py`)

**Files:**
- Modify: `deals/fees.py` (append)
- Modify: `deals/cli.py:30` (use the new helper)
- Test: `tests/deals/test_fees.py` (append)

**Interfaces:**
- Produces: `fee_model_from_env() -> FeeModel` — reads `DEALS_BUYER_PREMIUM_PCT` (default 0.125), `DEALS_TAX_PCT` (0.0), `DEALS_FREIGHT` (0.0).

- [ ] **Step 1: Write the failing tests** — append to `tests/deals/test_fees.py`:

```python
def test_fee_model_from_env_defaults(monkeypatch):
    from deals.fees import fee_model_from_env
    for k in ("DEALS_BUYER_PREMIUM_PCT", "DEALS_TAX_PCT", "DEALS_FREIGHT"):
        monkeypatch.delenv(k, raising=False)
    fm = fee_model_from_env()
    assert fm.buyer_premium_pct == 0.125
    assert fm.tax_pct == 0.0
    assert fm.freight == 0.0


def test_fee_model_from_env_overrides(monkeypatch):
    from deals.fees import fee_model_from_env
    monkeypatch.setenv("DEALS_BUYER_PREMIUM_PCT", "0.18")
    monkeypatch.setenv("DEALS_TAX_PCT", "0.07")
    monkeypatch.setenv("DEALS_FREIGHT", "40")
    fm = fee_model_from_env()
    assert fm.buyer_premium_pct == 0.18
    assert fm.tax_pct == 0.07
    assert fm.freight == 40.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/deals/test_fees.py -v -k from_env`
Expected: FAIL / ImportError `cannot import name 'fee_model_from_env'`

- [ ] **Step 3: Implement** — append to `deals/fees.py`:

```python
import os


def fee_model_from_env() -> FeeModel:
    """FeeModel from DEALS_* env vars; defaults match the digest's 12.5% premium."""
    return FeeModel(
        buyer_premium_pct=float(os.getenv("DEALS_BUYER_PREMIUM_PCT", "0.125")),
        tax_pct=float(os.getenv("DEALS_TAX_PCT", "0")),
        freight=float(os.getenv("DEALS_FREIGHT", "0")),
    )
```

And in `deals/cli.py` line 30, replace `FeeModel(buyer_premium_pct=0.125, tax_pct=0.0, freight=0.0)` with `fee_model_from_env()` (adjust the import at the top of `cli.py` from `from deals.fees import FeeModel` to `from deals.fees import fee_model_from_env` — keep `FeeModel` in the import only if still referenced elsewhere in the file).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/deals/test_fees.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add deals/fees.py deals/cli.py tests/deals/test_fees.py
git commit -m "feat(deals): fee_model_from_env — DEALS_* env-tunable fee model"
```

---

### Task 2: Query builder module (`automation/web/deals_query.py`)

**Files:**
- Create: `automation/web/deals_query.py`
- Create: `tests/web/__init__.py` (empty)
- Test: `tests/web/test_deals_query.py`

**Interfaces:**
- Consumes: `deals.fees.FeeModel`, `deals.fees.landed_cost` (existing).
- Produces:
  - `build_where(*, q=None, category=None, state=None, max_bids=None, ending_within=None, status="active") -> tuple[str, list]` — WHERE clause (no `WHERE` keyword; `"TRUE"` when empty) + positional args.
  - `order_clause(sort: str, direction: str | None) -> str` — full `ORDER BY … NULLS LAST` from whitelist; unknown sort → `ends`; default dir: `asc` for `ends`, else `desc`.
  - `enrich(row: dict, fees: FeeModel) -> dict` — adds `landed_cost` (rounded 2dp), `govdeals_url`, `viewer_url`.

- [ ] **Step 1: Write the failing tests** — `tests/web/test_deals_query.py`:

```python
from automation.web import deals_query
from deals.fees import FeeModel


def test_default_active_filter():
    where, args = deals_query.build_where()
    assert "outcome_complete IS NOT TRUE" in where
    assert "end_utc > now()" in where
    assert args == []


def test_status_closed_and_all():
    where, _ = deals_query.build_where(status="closed")
    assert where == "outcome_complete IS TRUE"
    where, _ = deals_query.build_where(status="all")
    assert where == "TRUE"


def test_search_matches_title_and_description():
    where, args = deals_query.build_where(q="chair", status="all")
    assert "title ILIKE %s" in where and "description ILIKE %s" in where
    assert args == ["%chair%", "%chair%"]


def test_combined_filters_order_and_args():
    where, args = deals_query.build_where(
        q="desk", category="Furniture", state="tx",
        max_bids=0, ending_within=48, status="active")
    assert where.count("%s") == len(args) == 6
    assert "canonical_category = %s" in where
    assert "state = %s" in where
    assert "bid_count <= %s" in where
    assert "make_interval(hours => %s)" in where
    assert "TX" in args  # state upper-cased
    assert 0 in args and 48 in args


def test_order_clause_whitelist():
    assert deals_query.order_clause("ends", None) == "ORDER BY end_utc ASC NULLS LAST"
    assert deals_query.order_clause("bids", None) == "ORDER BY bid_count DESC NULLS LAST"
    assert deals_query.order_clause("landed", "asc") == "ORDER BY current_bid ASC NULLS LAST"
    # unknown sort / dir fall back safely — never raw user input
    assert deals_query.order_clause("evil; DROP TABLE", "x") == "ORDER BY end_utc ASC NULLS LAST"


def test_enrich_landed_cost_and_urls():
    fees = FeeModel(buyer_premium_pct=0.125, tax_pct=0.0, freight=0.0)
    row = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "current_bid": 100.0}
    out = deals_query.enrich(dict(row), fees)
    assert out["landed_cost"] == 112.5
    assert out["govdeals_url"] == "https://www.govdeals.com/en/asset/305/10340"
    assert out["viewer_url"] == "/deals/305/10340/1"


def test_enrich_null_bid():
    fees = FeeModel()
    out = deals_query.enrich({"asset_id": 1, "account_id": 2, "auction_id": 3,
                              "current_bid": None}, fees)
    assert out["landed_cost"] == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/web/test_deals_query.py -v`
Expected: ImportError `No module named 'automation.web.deals_query'`

- [ ] **Step 3: Implement** — `automation/web/deals_query.py`:

```python
"""Pure helpers for /api/deals: request params -> SQL fragments + row enrichment.

No DB access here so everything is unit-testable without a connection.
All values are bound via %s placeholders; sort columns come only from SORTS.
"""
from __future__ import annotations

from deals.fees import FeeModel, landed_cost

# landed cost is monotonic in current_bid for a fixed fee model, so SQL can
# sort by current_bid for both "bid" and "landed".
SORTS = {
    "ends": "end_utc",
    "landed": "current_bid",
    "bid": "current_bid",
    "bids": "bid_count",
    "newest": "first_seen_at",
}


def build_where(*, q: str | None = None, category: str | None = None,
                state: str | None = None, max_bids: int | None = None,
                ending_within: int | None = None,
                status: str = "active") -> tuple[str, list]:
    where: list[str] = []
    args: list = []
    if status == "active":
        where.append("outcome_complete IS NOT TRUE AND end_utc > now()")
    elif status == "closed":
        where.append("outcome_complete IS TRUE")
    if q:
        where.append("(title ILIKE %s OR description ILIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("canonical_category = %s")
        args.append(category)
    if state:
        where.append("state = %s")
        args.append(state.upper())
    if max_bids is not None:
        where.append("bid_count <= %s")
        args.append(max_bids)
    if ending_within is not None:
        where.append("end_utc <= now() + make_interval(hours => %s)")
        args.append(ending_within)
    return (" AND ".join(where) or "TRUE", args)


def order_clause(sort: str, direction: str | None) -> str:
    col = SORTS.get(sort) or SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def enrich(row: dict, fees: FeeModel) -> dict:
    bid = float(row.get("current_bid") or 0)
    row["landed_cost"] = round(landed_cost(bid, qty=1, fees=fees).total, 2)
    row["govdeals_url"] = (
        f"https://www.govdeals.com/en/asset/{row['asset_id']}/{row['account_id']}"
    )
    row["viewer_url"] = f"/deals/{row['asset_id']}/{row['account_id']}/{row['auction_id']}"
    return row
```

Also create empty `tests/web/__init__.py`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/web/test_deals_query.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add automation/web/deals_query.py tests/web/
git commit -m "feat(web): deals query builder — filters/sort whitelist/landed-cost enrichment"
```

---

### Task 3: `GET /api/deals` endpoint

**Files:**
- Modify: `automation/web/app.py` — add endpoint right after the `deal_listing` viewer route (`app.py:~462`, after `@app.get("/deals/{asset_id}/…")` handler ends); add `from . import deals_query` next to the existing `from .. import db` import block (`app.py:50`), and `from deals.fees import fee_model_from_env` below it.
- Test: `tests/web/test_deals_api.py`

**Interfaces:**
- Consumes: `deals_query.build_where/order_clause/enrich` (Task 2), `fee_model_from_env` (Task 1), `automation.db.fetch_all/fetch_one`.
- Produces: `GET /api/deals` → `{"total", "rows", "facets": {"categories", "states"}, "stats": {"total_lots", "candidates", "ending_24h"}}`. Params: `q, category, state, max_bids, ending_within, status, sort, dir, limit, offset`.

- [ ] **Step 1: Write the failing test** — `tests/web/test_deals_api.py` (monkeypatch the db layer; no real connection):

```python
from fastapi.testclient import TestClient


def _client(monkeypatch, rows):
    monkeypatch.setenv("BLACKWHOLE_DB_URL", "postgresql://stub")  # app import guard
    from automation.web import app as webapp

    captured = {}

    def fake_fetch_all(sql, params=()):
        captured.setdefault("sqls", []).append(sql)
        if "FROM deal_lots WHERE" in sql and "SELECT count" not in sql and "GROUP BY" not in sql:
            captured["rows_sql"] = sql
            captured["rows_params"] = params
            return [dict(r) for r in rows]
        return []  # facet queries

    def fake_fetch_one(sql, params=()):
        if "count(*) AS c" in sql:
            return {"c": len(rows)}
        return {"total_lots": 456, "candidates": 25, "ending_24h": 25}

    monkeypatch.setattr(webapp.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(webapp.db, "fetch_one", fake_fetch_one)
    return TestClient(webapp.app), captured


ROW = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Desk",
       "canonical_category": "Furniture", "city": "Houston", "state": "TX",
       "bid_count": 0, "current_bid": 100.0, "currency_code": "USD",
       "end_utc": None, "outcome": None, "final_bid": None,
       "outcome_complete": False, "first_seen_at": None,
       "hero_image_url": None, "archived_hero_url": None}


def test_deals_endpoint_shape_and_enrichment(monkeypatch):
    client, cap = _client(monkeypatch, [ROW])
    r = client.get("/api/deals?max_bids=0&ending_within=48&sort=landed")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["stats"]["candidates"] == 25
    row = body["rows"][0]
    assert row["landed_cost"] == 112.5
    assert row["govdeals_url"].endswith("/asset/305/10340")
    assert "ORDER BY current_bid DESC" in cap["rows_sql"]
    assert 0 in cap["rows_params"] and 48 in cap["rows_params"]


def test_deals_endpoint_rejects_bad_status(monkeypatch):
    client, _ = _client(monkeypatch, [])
    assert client.get("/api/deals?status=bogus").status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/web/test_deals_api.py -v`
Expected: FAIL — 404 (route doesn't exist)

- [ ] **Step 3: Implement** — in `automation/web/app.py`, add imports near line 50 (`from . import deals_query`, `from deals.fees import fee_model_from_env`) and this endpoint after the `deal_listing` route:

```python
# ── Deals dashboard API (BLACKWHOLE-12) ─────────────────────────────────────

_DEALS_COLS = (
    "asset_id, account_id, auction_id, title, canonical_category, city, state, "
    "bid_count, current_bid, currency_code, end_utc, outcome, final_bid, "
    "outcome_complete, first_seen_at, hero_image_url, archived_hero_url"
)

_DEALS_ACTIVE = "outcome_complete IS NOT TRUE AND end_utc > now()"


@app.get("/api/deals")
async def list_deals(
    q: str | None = None,
    category: str | None = None,
    state: str | None = None,
    max_bids: int | None = None,
    ending_within: int | None = None,
    status: str = "active",
    sort: str = "ends",
    dir: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Search/filter/sort deal_lots for the admin Deals tab.

    Facets reflect the full active set (not the filtered subset) — v1 keeps
    the SQL simple; counts guide, not gate.
    """
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, args = deals_query.build_where(
        q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status,
    )
    order = deals_query.order_clause(sort, dir)

    def _fetch():
        rows = db.fetch_all(
            f"SELECT {_DEALS_COLS} FROM deal_lots WHERE {where} {order} "
            "LIMIT %s OFFSET %s",
            (*args, limit, offset),
        )
        total = db.fetch_one(
            f"SELECT count(*) AS c FROM deal_lots WHERE {where}", tuple(args)
        )["c"]
        cats = db.fetch_all(
            f"SELECT canonical_category AS value, count(*) AS count FROM deal_lots "
            f"WHERE {_DEALS_ACTIVE} AND canonical_category IS NOT NULL "
            "GROUP BY 1 ORDER BY count DESC"
        )
        states = db.fetch_all(
            f"SELECT state AS value, count(*) AS count FROM deal_lots "
            f"WHERE {_DEALS_ACTIVE} AND state IS NOT NULL "
            "GROUP BY 1 ORDER BY count DESC"
        )
        stats = db.fetch_one(
            "SELECT (SELECT count(*) FROM deal_lots) AS total_lots, "
            "(SELECT count(*) FROM deal_candidates) AS candidates, "
            f"(SELECT count(*) FROM deal_lots WHERE {_DEALS_ACTIVE} "
            "AND end_utc <= now() + interval '24 hours') AS ending_24h"
        )
        return rows, total, cats, states, stats

    try:
        rows, total, cats, states, stats = await asyncio.to_thread(_fetch)
    except Exception as e:  # DB down / view missing → 503, matches /api/auctions
        raise HTTPException(503, f"deals query failed: {e!r}")

    fees = fee_model_from_env()
    return {
        "total": total,
        "rows": [deals_query.enrich(dict(r), fees) for r in rows],
        "facets": {"categories": cats, "states": states},
        "stats": stats,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/web/ -v`
Expected: all PASS. Also run the full suite: `.venv/bin/python -m pytest tests/ -q` — no regressions.

- [ ] **Step 5: Commit**

```bash
git add automation/web/app.py tests/web/test_deals_api.py
git commit -m "feat(web): GET /api/deals — filtered/sorted deal_lots with landed cost + facets"
```

---

### Task 4: Deals tab markup (`index.html`)

**Files:**
- Modify: `automation/web/templates/index.html` — add tab button after the `subscribers` button (line ~48) and a new panel `<section>` after the subscribers panel (line ~409, before the `<!-- Listings DB -->` comment's section if ordering by nav; placing it after the subscribers `</section>` is fine — panel order in DOM doesn't matter).

**Interfaces:**
- Produces: DOM ids consumed by Task 5's JS: `deal-q`, `deal-category`, `deal-state`, `deal-zero-bids`, `deal-ending`, `deal-status-filter` (seg group), `deal-stats`, `deal-table` (with `<tbody id="deal-rows">`), `deal-prev`, `deal-next`, `deal-page-info`, `deal-refresh`. Sortable headers carry `data-sort="ends|landed|bid|bids|newest"`.

- [ ] **Step 1: Add the tab button** after the subscribers button in the `<nav class="tabs">`:

```html
    <button class="tab" data-tab="deals">
      <span class="tab-num">10</span><span class="tab-label">Deals</span>
    </button>
```

- [ ] **Step 2: Add the panel** after the subscribers `</section>`:

```html
<!-- ───────────── Deals ───────────── -->
<section class="panel" data-pane="deals" hidden>
  <div class="hero hero-compact">
    <div class="hero-eyebrow">DEAL TRACKER / GOVDEALS</div>
    <h1 class="hero-title">What expires cheap.</h1>
    <p class="hero-sub" id="deal-stats">Loading…</p>
  </div>

  <div class="inv-controls">
    <div class="ac-group">
      <span class="ac-label">SEARCH</span>
      <input type="text" id="deal-q" class="ac-input" placeholder="title or description…">
    </div>
    <div class="ac-group">
      <span class="ac-label">CATEGORY</span>
      <select id="deal-category" class="ac-input"><option value="">all</option></select>
    </div>
    <div class="ac-group">
      <span class="ac-label">STATE</span>
      <select id="deal-state" class="ac-input"><option value="">all</option></select>
    </div>
    <div class="ac-group">
      <span class="ac-label">ENDING</span>
      <select id="deal-ending" class="ac-input">
        <option value="">any time</option>
        <option value="6">&lt; 6h</option>
        <option value="24">&lt; 24h</option>
        <option value="48">&lt; 48h</option>
        <option value="168">&lt; 7d</option>
      </select>
    </div>
    <div class="ac-group">
      <span class="ac-label"><input type="checkbox" id="deal-zero-bids"> 0 BIDS</span>
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
      <button type="button" class="btn btn-small" id="deal-refresh">↻ refresh</button>
    </div>
  </div>

  <div class="inv-table-wrap">
    <table class="inv-table" id="deal-table">
      <thead>
        <tr>
          <th></th>
          <th>Title</th>
          <th>Category</th>
          <th>Location</th>
          <th class="sortable" data-sort="bids">Bids</th>
          <th class="sortable" data-sort="bid">Bid</th>
          <th class="sortable" data-sort="landed">Landed</th>
          <th class="sortable" data-sort="ends">Ends</th>
          <th>Outcome</th>
        </tr>
      </thead>
      <tbody id="deal-rows">
        <tr><td colspan="9" class="drafts-empty">Loading deals…</td></tr>
      </tbody>
    </table>
    <div class="deal-pager">
      <button type="button" class="btn btn-small" id="deal-prev">‹ prev</button>
      <span id="deal-page-info"></span>
      <button type="button" class="btn btn-small" id="deal-next">next ›</button>
    </div>
  </div>
</section>
```

(If `ac-input` doesn't exist as a class in `app.css`, check what the Auctions tab controls use — `grep -n 'ac-group\|ac-label' automation/web/templates/index.html` — and copy the input/select classes used there instead. Keep whatever class the existing controls use.)

- [ ] **Step 3: Commit**

```bash
git add automation/web/templates/index.html
git commit -m "feat(admin): Deals tab markup — filters, sortable table, pager"
```

---

### Task 5: Deals tab JS (`app.js`)

**Files:**
- Modify: `automation/web/static/app.js` — 3 edits: (a) add `deals: $('[data-pane="deals"]')` to the `panels` map (~line 74), (b) add `if (name === 'deals') loadDeals();` in `activateTab` (~line 92), (c) append the deals module near the other tab modules (before the `restoreLastTab();` call at the file bottom — module-level consts must be declared before that invocation, so append ABOVE line 2134).

**Interfaces:**
- Consumes: `GET /api/deals` (Task 3 response shape), DOM ids from Task 4, existing helpers `$`, `$$`.
- Produces: `loadDeals()` referenced by `activateTab`.

- [ ] **Step 1: Implement the module** (append above the `restoreLastTab();` line):

```javascript
/* ── Deals tab ─────────────────────────────────────────────── */
const deal = {q: '', category: '', state: '', zero: false, ending: '',
              status: 'active', sort: 'ends', dir: null, offset: 0, limit: 50,
              facetsLoaded: false};

function dealEndsCell(iso) {
  if (!iso) return '<td>—</td>';
  const ms = new Date(iso) - Date.now();
  const h = ms / 3.6e6;
  const cls = h < 2 ? 'deal-ends-red' : (h < 24 ? 'deal-ends-yellow' : '');
  const label = ms <= 0 ? 'ended'
    : h < 1 ? `${Math.round(ms / 6e4)}m`
    : h < 48 ? `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`
    : `${Math.floor(h / 24)}d ${Math.floor(h % 24)}h`;
  return `<td class="${cls}" title="${iso}">${label}</td>`;
}

async function loadDeals() {
  const tbody = $('#deal-rows');
  const p = new URLSearchParams();
  if (deal.q) p.set('q', deal.q);
  if (deal.category) p.set('category', deal.category);
  if (deal.state) p.set('state', deal.state);
  if (deal.zero) p.set('max_bids', '0');
  if (deal.ending) p.set('ending_within', deal.ending);
  p.set('status', deal.status);
  p.set('sort', deal.sort);
  if (deal.dir) p.set('dir', deal.dir);
  p.set('limit', deal.limit);
  p.set('offset', deal.offset);
  let body;
  try {
    const r = await fetch('/api/deals?' + p.toString());
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    body = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="drafts-empty">deals API error: ${e}</td></tr>`;
    return;
  }
  const s = body.stats || {};
  $('#deal-stats').textContent =
    `${s.total_lots ?? '?'} lots tracked · ${s.candidates ?? '?'} candidates (0-bid <24h) · ${s.ending_24h ?? '?'} ending <24h`;
  if (!deal.facetsLoaded && body.facets) {
    const fill = (sel, items) => {
      const el = $(sel);
      items.forEach(f => {
        const o = document.createElement('option');
        o.value = f.value; o.textContent = `${f.value} (${f.count})`;
        el.appendChild(o);
      });
    };
    fill('#deal-category', body.facets.categories || []);
    fill('#deal-state', body.facets.states || []);
    deal.facetsLoaded = true;
  }
  if (!body.rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="drafts-empty">no lots match</td></tr>';
  } else {
    tbody.innerHTML = body.rows.map(r => {
      const img = r.archived_hero_url || r.hero_image_url;
      const thumb = img
        ? `<img class="deal-thumb" src="${img}" loading="lazy" alt="">`
        : '<span class="deal-thumb deal-thumb-empty">🪑</span>';
      const outcome = r.outcome_complete
        ? `<span class="lex ${r.outcome === 'no_bid' ? 'pending' : 'done'}">${r.outcome ?? 'closed'}${r.final_bid != null ? ` $${r.final_bid}` : ''}</span>`
        : '<span class="lex running">open</span>';
      const esc = (t) => (t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
      return `<tr>
        <td>${thumb}</td>
        <td><a href="${r.govdeals_url}" target="_blank" rel="noopener">${esc(r.title)}</a>
            <a class="deal-viewer-link" href="${r.viewer_url}" target="_blank" rel="noopener" title="archived copy">⧉</a></td>
        <td>${esc(r.canonical_category) || '—'}</td>
        <td>${esc(r.city) || ''}${r.state ? ', ' + r.state : ''}</td>
        <td>${r.bid_count ?? '—'}</td>
        <td>${r.current_bid != null ? '$' + r.current_bid : '—'}</td>
        <td>$${r.landed_cost}</td>
        ${dealEndsCell(r.end_utc)}
        <td>${outcome}</td>
      </tr>`;
    }).join('');
  }
  const page = Math.floor(deal.offset / deal.limit) + 1;
  const pages = Math.max(1, Math.ceil(body.total / deal.limit));
  $('#deal-page-info').textContent = `${page} / ${pages} (${body.total} lots)`;
  $('#deal-prev').disabled = deal.offset === 0;
  $('#deal-next').disabled = deal.offset + deal.limit >= body.total;
}

let _dealQTimer;
$('#deal-q').addEventListener('input', (e) => {
  clearTimeout(_dealQTimer);
  _dealQTimer = setTimeout(() => { deal.q = e.target.value.trim(); deal.offset = 0; loadDeals(); }, 300);
});
$('#deal-category').addEventListener('change', (e) => { deal.category = e.target.value; deal.offset = 0; loadDeals(); });
$('#deal-state').addEventListener('change', (e) => { deal.state = e.target.value; deal.offset = 0; loadDeals(); });
$('#deal-ending').addEventListener('change', (e) => { deal.ending = e.target.value; deal.offset = 0; loadDeals(); });
$('#deal-zero-bids').addEventListener('change', (e) => { deal.zero = e.target.checked; deal.offset = 0; loadDeals(); });
$$('#deal-status-filter .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#deal-status-filter .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    deal.status = btn.dataset.value; deal.offset = 0; loadDeals();
  });
});
$$('#deal-table th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (deal.sort === key) {
      deal.dir = (deal.dir ?? (key === 'ends' ? 'asc' : 'desc')) === 'asc' ? 'desc' : 'asc';
    } else { deal.sort = key; deal.dir = null; }
    deal.offset = 0; loadDeals();
  });
});
$('#deal-prev').addEventListener('click', () => { deal.offset = Math.max(0, deal.offset - deal.limit); loadDeals(); });
$('#deal-next').addEventListener('click', () => { deal.offset += deal.limit; loadDeals(); });
$('#deal-refresh').addEventListener('click', () => { deal.facetsLoaded = false; loadDeals(); });
```

Plus the two one-line edits: `deals: $('[data-pane="deals"]'),` in the `panels` map and `if (name === 'deals') loadDeals();` in `activateTab`.

- [ ] **Step 2: Syntax check**

Run: `node --check automation/web/static/app.js`
Expected: no output (exit 0). If `node` is unavailable, load the page and check the browser console instead (Task 7 does this anyway).

- [ ] **Step 3: Commit**

```bash
git add automation/web/static/app.js
git commit -m "feat(admin): Deals tab JS — fetch/render/filters/sort/pager"
```

---

### Task 6: CSS touches (`app.css`)

**Files:**
- Modify: `automation/web/static/app.css` (append)

- [ ] **Step 1: Append styles**:

```css
/* ── Deals tab ─────────────────────────────────────────────── */
.deal-thumb { width: 44px; height: 33px; object-fit: cover; border-radius: 3px;
              display: inline-block; background: var(--bg-elev); }
.deal-thumb-empty { text-align: center; line-height: 33px; font-size: 16px; }
.deal-ends-red { color: #ff5f56; font-weight: 600; }
.deal-ends-yellow { color: #ffbd2e; }
.deal-viewer-link { margin-left: 6px; opacity: .55; text-decoration: none; }
.deal-viewer-link:hover { opacity: 1; }
.deal-pager { display: flex; gap: 12px; align-items: center; padding: 12px 0; }
#deal-table th.sortable { cursor: pointer; user-select: none; }
#deal-table th.sortable:hover { color: var(--fg); }
```

(Verify `--bg-elev` and `--fg` exist in `:root` of `app.css` — `grep -n -- '--bg-elev\|--fg' automation/web/static/app.css | head -3`. If named differently, use the actual variable names.)

- [ ] **Step 2: Commit**

```bash
git add automation/web/static/app.css
git commit -m "feat(admin): Deals tab styles"
```

---

### Task 7: End-to-end verification + docs

**Files:**
- Modify: `CLAUDE.md` (add the Deals tab to the admin-tab list + `/api/deals` to the API list — two one-line edits in the `web/` bullet)

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: Live API smoke** — kill any running dashboard, start the server from this worktree (`.venv` from the main checkout works: `/Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python -m automation.web` with `cwd` = worktree root), then:

```bash
curl -s 'http://127.0.0.1:8765/api/deals?max_bids=0&ending_within=48&sort=ends' | head -c 600
curl -s 'http://127.0.0.1:8765/api/deals?q=chair&status=all&sort=landed&dir=desc&limit=5'
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8765/api/deals?status=bogus'   # expect 400
```

Expected: JSON with `total`, `rows[].landed_cost`, `facets.categories`, `stats.candidates`; 400 on the bad status.

- [ ] **Step 3: Drive the tab in a real browser** — open `http://127.0.0.1:8765/admin`, click `10 Deals`, verify: stats strip populates, category dropdown has counts, search "table" filters, clicking `Landed` header re-sorts, `0 BIDS` checkbox narrows, pager works, no console errors. Screenshot for the PR.

- [ ] **Step 4: Update CLAUDE.md** admin-dashboard bullet: tab list gains `10 Deals`, API list gains `/api/deals` (deals-tracker search/filter/sort with landed cost).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: Deals admin tab + /api/deals in CLAUDE.md"
```

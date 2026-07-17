# Continuous Deal Scanner + Resale-Analysis Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the `deals/` tracker continuously on Render, analyze every lot heading toward a cheap close for resale profit using real eBay sold comps (served by the Pi microservice), surface verdicts in a Zillow-style Deals browser with lists/tags/saved-searches, and detect relists.

**Architecture:** Four Render cron jobs (`discover`/`watch-once`/`analyze`/`digest`) share the existing Supabase tables plus new `deal_verdicts`/`deal_lists`/`deal_lot_tags`/`saved_searches`. The analysis pipeline is Alibaba-LLP-shaped: Gemini Flash extracts identity → Pi comps service returns real sold prices → Flash judges comp relevance → pure-math valuation → verdict row → Telegram above margin threshold. All LLM/network steps are behind pure, unit-tested seams.

**Tech Stack:** Python 3.11, psycopg via `automation/db.py`, httpx, google-genai (`gemini-2.5-flash`), FastAPI (existing `automation/web/app.py`), vanilla JS admin dashboard, Pi service already deployed (`scripts/pi_comps_service.py` is the vendored source).

**Spec:** `docs/superpowers/specs/2026-07-17-continuous-deal-scanner-design.md` — read it first.

## Global Constraints

- DB access ONLY via `from automation import db` (`db.fetch_one/fetch_all/execute`). Rows are dicts; timestamptz come back as `datetime`.
- DDL of record lives in `scripts/sql/*.sql` AND mirrored in the module's `DDL` string + `init_schema()` (existing `deals/store.py` pattern).
- Never overwrite outcome columns on re-upsert; never fabricate data (backfilled outcomes are `outcome_complete = false`).
- LLM never invents a dollar figure presented as a comp: no kept comps → `method='llm_estimate'`, `confidence='low'`, no alert.
- Telegram is best-effort (`send_message_sync` returns `(ok, err)`, never raises).
- Per-item error isolation in every loop (existing `run_discovery` pattern: try/except per lot, count `errors`, print to stderr).
- Tests: `.venv/bin/python -m pytest tests/deals/ -q` (no `pytest` console script in this venv).
- Env config all optional-with-defaults; missing `COMPS_URL` degrades, never crashes.
- Commit after every green test cycle. Branch: current worktree branch.

## Deliberate v1 cuts (spec deviations — follow-up tickets, not silent gaps)

1. **Identity extraction is text-only** (title+description). The spec calls for photo vision; add hero-image bytes to the Gemini call as a follow-up once the text-only accuracy baseline is measured.
2. **No sell-through-rate demand gate.** STR needs active-listing counts (a second scrape per query). v1 confidence uses kept-comp count only; record STR later.
3. **Freight is the existing flat `DEALS_FREIGHT`**, not per-mile (`DEALS_FREIGHT_PER_MILE` deferred). Distance still displays/filters everywhere.
4. **No consolidated "Pi down" ops alert** (spec: one Telegram after N failed passes). v1 logs per-query stderr only; `AnalyzeReport.degraded` makes the condition visible in cron logs.

## Swarm execution map (task → blocked by)

| Wave | Tasks (parallel-safe within wave) |
|---|---|
| A | T1 sweep-config, T2 backfill, T3 schema+verdict-store, T4 eBay parser extraction, T5 comps client, T6 valuation math, T7 geo |
| B | T8 LLM steps (needs T5 types), T9 render-cron (needs T1/T2 CLI names), T10 cloudflared tunnel (ops, independent) |
| C | T11 analyze orchestrator (needs T3,T5,T6,T8), T12 deals API extensions (needs T3,T7) |
| D | T13 Deals-browser frontend (needs T12), T14 saved-search alerts (needs T3,T12 params shape), T15 relist detection (needs T3), T16 Claude rank CLI (needs T3,T11) |

---

### Task 1: Whole-site sweep configuration

**Files:**
- Modify: `deals/cli.py`
- Test: `tests/deals/test_cli_sweep.py` (create)

**Interfaces:**
- Produces: `deals.cli.sweep_categories(arg: str | None, env: dict) -> list[str]` — returns category-id list; `[""]` means whole-site (empty `categoryIds` sweeps the entire close-sorted firehose — `GovDealsAdapter._search_page` already accepts `""`).
- Produces: CLI `discover` gains `--max-pages` (int, default 60) passed to `adapter.discover(max_pages=...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_cli_sweep.py
from deals.cli import sweep_categories, DEFAULT_CATEGORIES

def test_explicit_arg_wins():
    assert sweep_categories("372,47B", {}) == ["372", "47B"]

def test_env_var_used_when_no_arg():
    assert sweep_categories(None, {"DEALS_SWEEP_CATEGORIES": "22,90"}) == ["22", "90"]

def test_env_var_all_means_whole_site():
    assert sweep_categories(None, {"DEALS_SWEEP_CATEGORIES": "all"}) == [""]

def test_default_is_curated_cluster():
    assert sweep_categories(None, {}) == DEFAULT_CATEGORIES

def test_arg_all_means_whole_site():
    assert sweep_categories("all", {}) == [""]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_cli_sweep.py -q`
Expected: FAIL — `ImportError: cannot import name 'sweep_categories'`

- [ ] **Step 3: Implement**

In `deals/cli.py` add (above `main()`):

```python
def sweep_categories(arg: str | None, env: dict) -> list[str]:
    """Resolve which categories to sweep. 'all' (arg or env) = whole site,
    which the maestro API expresses as an empty categoryIds string."""
    raw = arg if arg is not None else env.get("DEALS_SWEEP_CATEGORIES", "")
    if raw.strip().lower() == "all":
        return [""]
    if raw.strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    return DEFAULT_CATEGORIES
```

Change the `discover` subparser + dispatch in `main()`:

```python
    d = sub.add_parser("discover")
    d.add_argument("--categories", default=None)
    d.add_argument("--max-pages", type=int, default=60)
```

```python
    elif a.cmd == "discover":
        import os
        cats = sweep_categories(a.categories, os.environ)
        rep = run_discovery(adapter, categories=cats, max_pages=a.max_pages)
        print(rep)
```

And thread `max_pages` through `deals/discover.py::run_discovery` (add keyword param, default 60, pass to `adapter.discover(category_ids=category, max_pages=max_pages)`).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_cli_sweep.py tests/deals/test_discover.py -q`
Expected: PASS (existing discover tests must stay green — `run_discovery` signature change is additive).

- [ ] **Step 5: Commit**

```bash
git add deals/cli.py deals/discover.py tests/deals/test_cli_sweep.py
git commit -m "feat(deals): whole-site sweep via DEALS_SWEEP_CATEGORIES / --categories all"
```

---

### Task 2: Outcome backfill for ended-unobserved lots

**Files:**
- Create: `deals/backfill.py`
- Modify: `deals/cli.py` (new subcommand)
- Test: `tests/deals/test_backfill.py` (create)

**Interfaces:**
- Produces: `deals.backfill.backfill_plan(rows: list[dict], now: datetime) -> list[dict]` — pure; input rows have keys `asset_id, account_id, auction_id, end_utc, bid_count, current_bid, snap_bid_count, snap_current_bid, snap_observed_at` (snap_* nullable); returns rows to close with keys `key, outcome, final_bid, final_bid_count, closed_at, complete(False)`.
- Produces: `deals.backfill.run_backfill(now=None) -> int` — queries, applies via `store.record_outcome`, returns count.
- Produces: CLI `python -m deals.cli backfill-outcomes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_backfill.py
from datetime import datetime, timedelta, timezone
from deals.backfill import backfill_plan

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

def _row(**kw):
    base = dict(asset_id=1, account_id=2, auction_id=3,
                end_utc=NOW - timedelta(days=3), bid_count=0, current_bid=5.0,
                snap_bid_count=None, snap_current_bid=None, snap_observed_at=None)
    base.update(kw)
    return base

def test_no_bid_from_live_state_when_no_snapshot():
    plan = backfill_plan([_row()], NOW)
    assert plan[0]["outcome"] == "no_bid"
    assert plan[0]["final_bid"] == 5.0
    assert plan[0]["complete"] is False          # honest: single observation

def test_snapshot_preferred_over_live_state():
    plan = backfill_plan([_row(snap_bid_count=4, snap_current_bid=120.0,
                                snap_observed_at=NOW - timedelta(days=3, hours=1))], NOW)
    assert plan[0]["outcome"] == "sold"
    assert plan[0]["final_bid"] == 120.0
    assert plan[0]["final_bid_count"] == 4

def test_one_bid_is_low_bid_not_no_bid():
    plan = backfill_plan([_row(bid_count=1)], NOW)
    assert plan[0]["outcome"] == "low_bid"

def test_recent_end_gets_grace_period():
    plan = backfill_plan([_row(end_utc=NOW - timedelta(minutes=30))], NOW)
    assert plan == []                            # watcher may still catch it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_backfill.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deals.backfill'`

- [ ] **Step 3: Implement**

```python
# deals/backfill.py
"""One-shot closer for lots that ended while the watcher wasn't running.

Uses the last snapshot when one exists, else the stored live state.
Everything closed here is outcome_complete = false — we observed the lot
before its close, not at it. Never overwrites an existing outcome.
"""
from datetime import datetime, timedelta
from automation import db
from deals.store import record_outcome

GRACE = timedelta(hours=1)   # leave truly-recent closes to the watcher

def backfill_plan(rows: list[dict], now: datetime) -> list[dict]:
    plan = []
    for r in rows:
        if r["end_utc"] > now - GRACE:
            continue
        bid_count = r["snap_bid_count"] if r["snap_bid_count"] is not None else r["bid_count"]
        final_bid = r["snap_current_bid"] if r["snap_current_bid"] is not None else r["current_bid"]
        if bid_count == 0:
            outcome = "no_bid"
        elif bid_count <= 2:
            outcome = "low_bid"
        else:
            outcome = "sold"
        plan.append({"key": (r["asset_id"], r["account_id"], r["auction_id"]),
                     "outcome": outcome, "final_bid": float(final_bid or 0),
                     "final_bid_count": int(bid_count or 0),
                     "closed_at": r["end_utc"], "complete": False})
    return plan

def run_backfill(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    rows = db.fetch_all("""
        SELECT l.asset_id, l.account_id, l.auction_id, l.end_utc,
               l.bid_count, l.current_bid,
               s.bid_count AS snap_bid_count, s.current_bid AS snap_current_bid,
               s.observed_at AS snap_observed_at
        FROM deal_lots l
        LEFT JOIN LATERAL (
            SELECT bid_count, current_bid, observed_at FROM deal_snapshots
            WHERE asset_id=l.asset_id AND account_id=l.account_id AND auction_id=l.auction_id
            ORDER BY observed_at DESC LIMIT 1) s ON TRUE
        WHERE l.outcome IS NULL AND l.end_utc < %s""", (now,))
    plan = backfill_plan(rows, now)
    for p in plan:
        record_outcome(p["key"], p["outcome"], p["final_bid"],
                       p["final_bid_count"], p["closed_at"], p["complete"])
    return len(plan)
```

Note: `low_bid` threshold `<= 2` mirrors `deals/watcher_logic.py::detect_outcome` — read that function first and reuse its exact threshold; if it differs, match the existing one and adjust the test.

CLI wiring in `deals/cli.py`:

```python
    sub.add_parser("backfill-outcomes")
```
```python
    elif a.cmd == "backfill-outcomes":
        from deals.backfill import run_backfill
        print(f"closed {run_backfill()} lots")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_backfill.py -q`
Expected: PASS

- [ ] **Step 5: Commit, then run the real backfill once**

```bash
git add deals/backfill.py deals/cli.py tests/deals/test_backfill.py
git commit -m "feat(deals): backfill-outcomes closes ended-unobserved lots honestly"
.venv/bin/python -m deals.cli backfill-outcomes   # expect ~6,800 closed
```

---

### Task 3: Schema + verdict store

**Files:**
- Create: `scripts/sql/deal_verdicts.sql`
- Create: `deals/verdict_store.py`
- Modify: `deals/store.py` (only: call new DDL from `init_schema`)
- Test: `tests/deals/test_verdict_store.py` (create)

**Interfaces:**
- Produces: `deals.verdict_store.VERDICT_COLUMNS: list[str]`, `verdict_row(v: dict) -> tuple` (pure), `insert_verdict(v: dict) -> None`, `lots_for_analysis(now, *, max_bid: float, window_h: int, limit: int) -> list[dict]` (rows include `raw`, `lat`, `lng`, `end_utc`, `current_bid`, `bid_count`), `latest_verdict(key: tuple[int,int,int]) -> dict | None`, `mark_alerted(key, analyzed_at) -> None`.
- Produces tables: `deal_verdicts`, `deal_lists`, `deal_list_items`, `deal_lot_tags`, `saved_searches`, and `ALTER TABLE deal_lots ADD COLUMN IF NOT EXISTS relist_of JSONB`.

- [ ] **Step 1: Write the SQL (DDL of record)**

```sql
-- scripts/sql/deal_verdicts.sql
-- Analysis verdicts + deal-browser saves/tags/searches (2026-07-17 spec).
CREATE TABLE IF NOT EXISTS deal_verdicts (
  asset_id INT, account_id INT, auction_id INT,
  analyzed_at TIMESTAMPTZ DEFAULT now(),
  identity JSONB, queries TEXT[],
  method TEXT,                    -- 'comps' | 'llm_estimate'
  comps JSONB, comp_count INT,
  per_unit REAL, recovery_tier REAL,
  est_resale REAL, piece_out_ceiling REAL,
  landed_cost REAL, margin REAL, margin_pct REAL,
  confidence TEXT,                -- 'low' | 'medium' | 'high'
  reasoning TEXT,
  rank_score REAL, rank_notes TEXT,
  alerted_at TIMESTAMPTZ,
  PRIMARY KEY (asset_id, account_id, auction_id, analyzed_at)
);
CREATE INDEX IF NOT EXISTS ix_verdicts_margin ON deal_verdicts(margin_pct DESC);

CREATE TABLE IF NOT EXISTS deal_lists (
  id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS deal_list_items (
  list_id BIGINT REFERENCES deal_lists(id) ON DELETE CASCADE,
  asset_id INT, account_id INT, auction_id INT,
  added_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (list_id, asset_id, account_id, auction_id));
CREATE TABLE IF NOT EXISTS deal_lot_tags (
  asset_id INT, account_id INT, auction_id INT, tag TEXT,
  added_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (asset_id, account_id, auction_id, tag));
CREATE TABLE IF NOT EXISTS saved_searches (
  id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  params JSONB NOT NULL, alert BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(), last_run_at TIMESTAMPTZ);

ALTER TABLE deal_lots ADD COLUMN IF NOT EXISTS relist_of JSONB;
```

- [ ] **Step 2: Write the failing test (pure parts)**

```python
# tests/deals/test_verdict_store.py
from deals.verdict_store import VERDICT_COLUMNS, verdict_row

def _v():
    return {c: None for c in VERDICT_COLUMNS} | {
        "asset_id": 1, "account_id": 2, "auction_id": 3,
        "identity": {"brand": "Steelcase"}, "queries": ["steelcase leap v2"],
        "method": "comps", "comps": [{"title": "x", "price": 100.0}],
        "comp_count": 5, "per_unit": 100.0, "recovery_tier": 0.4,
        "est_resale": 400.0, "landed_cost": 50.0,
        "margin": 350.0, "margin_pct": 700.0, "confidence": "medium"}

def test_row_matches_columns_order_and_serializes_json():
    row = verdict_row(_v())
    assert len(row) == len(VERDICT_COLUMNS)
    i_identity = VERDICT_COLUMNS.index("identity")
    assert isinstance(row[i_identity], str)          # json.dumps'd
    i_comps = VERDICT_COLUMNS.index("comps")
    assert isinstance(row[i_comps], str)

def test_queries_stays_a_list_for_text_array_binding():
    row = verdict_row(_v())
    assert row[VERDICT_COLUMNS.index("queries")] == ["steelcase leap v2"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_verdict_store.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Implement**

```python
# deals/verdict_store.py
"""Persistence for analysis verdicts + the analyze funnel query.

Same split as deals/store.py: verdict_row is pure/tested; the rest are thin
db wrappers exercised by live smoke."""
import json
from datetime import datetime, timedelta
from automation import db

DDL_FILE = "scripts/sql/deal_verdicts.sql"

VERDICT_COLUMNS = ["asset_id","account_id","auction_id","analyzed_at",
    "identity","queries","method","comps","comp_count","per_unit",
    "recovery_tier","est_resale","piece_out_ceiling","landed_cost",
    "margin","margin_pct","confidence","reasoning",
    "rank_score","rank_notes","alerted_at"]

_JSON_COLS = {"identity", "comps"}

def verdict_row(v: dict) -> tuple:
    out = []
    for c in VERDICT_COLUMNS:
        val = v.get(c)
        if c in _JSON_COLS and val is not None:
            val = json.dumps(val, default=str)
        out.append(val)
    return tuple(out)

def init_verdict_schema() -> None:
    with open(DDL_FILE) as f:
        sql = f.read()
    for stmt in filter(str.strip, sql.split(";")):
        db.execute(stmt)

def insert_verdict(v: dict) -> None:
    cols = ",".join(VERDICT_COLUMNS)
    ph = ",".join(["%s"] * len(VERDICT_COLUMNS))
    db.execute(f"INSERT INTO deal_verdicts ({cols}) VALUES ({ph})", verdict_row(v))

def lots_for_analysis(now: datetime, *, max_bid: float, window_h: int,
                      limit: int) -> list[dict]:
    """Cheap-close funnel: open lots ending inside the window, 0 bids or bid
    <= max_bid, not analyzed in the last 12h."""
    return db.fetch_all("""
        SELECT l.* FROM deal_lots l
        WHERE l.outcome IS NULL AND l.end_utc > %s AND l.end_utc <= %s
          AND (l.bid_count = 0 OR l.current_bid <= %s)
          AND l.is_free = false AND l.currency_code = 'USD'
          AND NOT EXISTS (SELECT 1 FROM deal_verdicts v
              WHERE v.asset_id=l.asset_id AND v.account_id=l.account_id
                AND v.auction_id=l.auction_id AND v.analyzed_at > %s)
        ORDER BY l.end_utc ASC LIMIT %s""",
        (now, now + timedelta(hours=window_h), max_bid,
         now - timedelta(hours=12), limit))

def latest_verdict(key: tuple[int, int, int]) -> dict | None:
    return db.fetch_one("""SELECT * FROM deal_verdicts
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s
        ORDER BY analyzed_at DESC LIMIT 1""", key)

def mark_alerted(key: tuple[int, int, int], analyzed_at: datetime) -> None:
    db.execute("""UPDATE deal_verdicts SET alerted_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND analyzed_at=%s""",
        (*key, analyzed_at))
```

Wire into `deals/store.py::init_schema` (append at end of function):

```python
    from deals.verdict_store import init_verdict_schema
    init_verdict_schema()
```

- [ ] **Step 5: Run tests, apply schema, commit**

Run: `.venv/bin/python -m pytest tests/deals/test_verdict_store.py tests/deals/test_store_rows.py -q` → PASS
Run: `.venv/bin/python -m deals.cli init-schema` → `schema ready`

```bash
git add scripts/sql/deal_verdicts.sql deals/verdict_store.py deals/store.py tests/deals/test_verdict_store.py
git commit -m "feat(deals): deal_verdicts + lists/tags/saved_searches schema and store"
```

---

### Task 4: Canonical eBay sold-page parser (repo-side, fixture-tested)

**Files:**
- Create: `deals/ebay_parse.py`
- Modify: `scripts/pi_comps_service.py` (import the canonical parser)
- Test: `tests/deals/test_ebay_parse.py` (create)
- Fixture (already committed): `tests/deals/fixtures/ebay_sold_sample_2026-07-17.html`

**Interfaces:**
- Produces: `deals.ebay_parse.parse_sold_page(html: str) -> dict` with keys `count:int, median:float|None, mean:float|None, items:list[dict{listing_id,title,price,condition,sold_note,url}]`. Pure, bs4-only.
- Dependency: add `beautifulsoup4` to `[project.dependencies]` in `pyproject.toml` (it is on the analyze query path via nothing else; the comps *client* consumes JSON — bs4 is needed repo-side only for this parser + tests, but keeping it a base dep is simpler than a new extras group).

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_ebay_parse.py
import pathlib
from deals.ebay_parse import parse_sold_page

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ebay_sold_sample_2026-07-17.html"

def test_parses_real_sold_page():
    data = parse_sold_page(FIXTURE.read_text())
    assert data["count"] >= 20                      # page had ~50 real cards
    assert data["median"] and data["median"] > 0
    first = data["items"][0]
    assert first["listing_id"] and first["title"] and first["price"] > 0
    assert first["url"].startswith("https://")

def test_placeholder_card_is_dropped():
    data = parse_sold_page(FIXTURE.read_text())
    assert all(i["title"].lower() != "shop on ebay" for i in data["items"])

def test_empty_html_yields_zero():
    assert parse_sold_page("<html></html>") == {
        "count": 0, "median": None, "mean": None, "items": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_ebay_parse.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deals.ebay_parse'`

- [ ] **Step 3: Implement**

Move `_parse` out of `scripts/pi_comps_service.py` verbatim into:

```python
# deals/ebay_parse.py
"""Parse eBay sold-search result HTML (2026 .s-card layout).

Canonical implementation — the Pi comps service deploys a copy of this file
(see scripts/deploy_pi_comps.sh). If eBay A/Bs back the legacy .s-item
layout, extend HERE and redeploy; the fixture test catches drift."""
import re
import statistics
from bs4 import BeautifulSoup

def parse_sold_page(body: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    items = []
    for card in soup.select(".s-card[data-listingid]"):
        title_el = card.select_one(".s-card__title")
        price_el = card.select_one(".s-card__price")
        if not title_el or not price_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if title.lower() in ("shop on ebay", ""):
            continue
        m = re.search(r"\$([\d,]+(?:\.\d{2})?)", price_el.get_text())
        if not m:
            continue
        link = card.select_one("a.s-card__link[href]")
        url = (link["href"].split("?")[0] if link else "")
        cond_el = card.select_one(".s-card__subtitle")
        cap_el = card.select_one(".s-card__caption")
        items.append({
            "listing_id": card.get("data-listingid"),
            "title": title,
            "price": float(m.group(1).replace(",", "")),
            "condition": cond_el.get_text(" ", strip=True) if cond_el else None,
            "sold_note": cap_el.get_text(" ", strip=True) if cap_el else None,
            "url": url,
        })
    prices = [i["price"] for i in items]
    return {
        "count": len(items),
        "median": round(statistics.median(prices), 2) if prices else None,
        "mean": round(statistics.mean(prices), 2) if prices else None,
        "items": items,
    }
```

In `scripts/pi_comps_service.py`: delete its `_parse` function and the now-unused `statistics`/`BeautifulSoup` imports; add at top `from ebay_parse import parse_sold_page` and change the one call site `return _parse(body)` → `return parse_sold_page(body)`. Add a deploy helper:

```bash
# scripts/deploy_pi_comps.sh
#!/bin/sh
# Deploy the comps service + canonical parser to the Pi and restart it.
set -e
scp scripts/pi_comps_service.py black-whole:~/comps/comps_service.py
scp deals/ebay_parse.py black-whole:~/comps/ebay_parse.py
ssh black-whole 'systemctl --user restart comps.service && sleep 2 && systemctl --user is-active comps.service'
```

`chmod +x scripts/deploy_pi_comps.sh`. Add `beautifulsoup4` to `[project.dependencies]` in `pyproject.toml`, then `.venv/bin/pip install -e . -q`.

- [ ] **Step 4: Run tests, deploy, verify Pi still healthy**

Run: `.venv/bin/python -m pytest tests/deals/test_ebay_parse.py -q` → PASS
Run: `./scripts/deploy_pi_comps.sh` → prints `active`
Run: `curl -s --max-time 10 http://100.99.195.81:8788/health` → `{"ok":true,...}`

- [ ] **Step 5: Commit**

```bash
git add deals/ebay_parse.py scripts/pi_comps_service.py scripts/deploy_pi_comps.sh tests/deals/test_ebay_parse.py pyproject.toml
git commit -m "feat(deals): canonical eBay sold-page parser, shared with Pi service"
```

---

### Task 5: Comps client (`CompsProvider`)

**Files:**
- Create: `deals/comps.py`
- Test: `tests/deals/test_comps.py` (create)

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class Comp: listing_id: str; title: str; price: float; condition: str | None; url: str
  @dataclass
  class CompsResult: query: str; count: int; median: float | None; items: list[Comp]; cached: bool
  class CompsUnavailable(Exception): ...   # Pi down / cooling / auth broken
  class PiCompsProvider:
      def __init__(self, base_url: str, key: str, timeout: float = 90.0): ...
      def fetch(self, query: str) -> CompsResult   # raises CompsUnavailable
  def comps_provider_from_env(env: dict | None = None) -> PiCompsProvider | None
  ```
- Env: `COMPS_URL` (e.g. `http://100.99.195.81:8788` locally, tunnel URL on Render), `COMPS_KEY`. Missing either → `comps_provider_from_env` returns `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_comps.py
import httpx
import pytest
from deals.comps import (Comp, CompsResult, CompsUnavailable,
                         PiCompsProvider, comps_provider_from_env)

def _provider(handler) -> PiCompsProvider:
    p = PiCompsProvider("http://pi.test", "k")
    p._client = httpx.Client(transport=httpx.MockTransport(handler),
                             base_url="http://pi.test")
    return p

def test_fetch_parses_result():
    def handler(request):
        assert request.headers["X-Comps-Key"] == "k"
        assert request.url.params["q"] == "steelcase leap v2"
        return httpx.Response(200, json={"query": "steelcase leap v2", "count": 1,
            "median": 80.0, "mean": 80.0, "cached": True, "fetched_at": 1,
            "items": [{"listing_id": "9", "title": "Leap V2", "price": 80.0,
                       "condition": "Pre-owned", "sold_note": None, "url": "https://ebay.com/itm/9"}]})
    r = _provider(handler).fetch("steelcase leap v2")
    assert isinstance(r, CompsResult) and r.items[0] == Comp("9", "Leap V2", 80.0,
                                                            "Pre-owned", "https://ebay.com/itm/9")

def test_503_raises_unavailable():
    def handler(request):
        return httpx.Response(503, json={"detail": {"error": "challenged"}})
    with pytest.raises(CompsUnavailable):
        _provider(handler).fetch("x chair")

def test_network_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("down")
    with pytest.raises(CompsUnavailable):
        _provider(handler).fetch("x chair")

def test_from_env_none_when_unconfigured():
    assert comps_provider_from_env({}) is None
    assert comps_provider_from_env({"COMPS_URL": "http://x"}) is None

def test_from_env_builds_provider():
    p = comps_provider_from_env({"COMPS_URL": "http://x", "COMPS_KEY": "k"})
    assert isinstance(p, PiCompsProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_comps.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# deals/comps.py
"""Client for the Pi sold-comps microservice (scripts/pi_comps_service.py).

Pluggable like automation/llm: comps_provider_from_env() returns None when
unconfigured and callers degrade to llm_estimate verdicts — comps failures
must never block the analyze pass."""
import os
from dataclasses import dataclass
import httpx

@dataclass
class Comp:
    listing_id: str
    title: str
    price: float
    condition: str | None
    url: str

@dataclass
class CompsResult:
    query: str
    count: int
    median: float | None
    items: list[Comp]
    cached: bool

class CompsUnavailable(Exception):
    pass

class PiCompsProvider:
    def __init__(self, base_url: str, key: str, timeout: float = 90.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._key = key

    def fetch(self, query: str) -> CompsResult:
        try:
            r = self._client.get("/comps", params={"q": query},
                                 headers={"X-Comps-Key": self._key})
        except httpx.HTTPError as e:
            raise CompsUnavailable(f"comps request failed: {e}") from e
        if r.status_code != 200:
            raise CompsUnavailable(f"comps status {r.status_code}: {r.text[:200]}")
        d = r.json()
        items = [Comp(i.get("listing_id") or "", i["title"], float(i["price"]),
                      i.get("condition"), i.get("url") or "") for i in d.get("items", [])]
        return CompsResult(d.get("query", query), d.get("count", len(items)),
                           d.get("median"), items, bool(d.get("cached")))

def comps_provider_from_env(env: dict | None = None) -> PiCompsProvider | None:
    env = env if env is not None else os.environ
    url, key = env.get("COMPS_URL"), env.get("COMPS_KEY")
    if not url or not key:
        return None
    return PiCompsProvider(url, key)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_comps.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deals/comps.py tests/deals/test_comps.py
git commit -m "feat(deals): CompsProvider client for the Pi sold-comps service"
```

---

### Task 6: Valuation math

**Files:**
- Create: `deals/valuation.py`
- Test: `tests/deals/test_valuation.py` (create)

**Interfaces:**
- Consumes: `deals.comps.Comp`, `deals.fees.FeeModel`, `deals.fees.landed_cost`.
- Produces:
  ```python
  @dataclass
  class Valuation:
      method: str               # 'comps' | 'llm_estimate'
      per_unit: float | None
      recovery_tier: float
      est_resale: float
      piece_out_ceiling: float | None
      landed_cost: float
      margin: float
      margin_pct: float
      confidence: str           # 'low' | 'medium' | 'high'
  def bulk_recovery_tier(quantity: int, env: dict | None = None) -> float
  def value_from_comps(kept: list[Comp], quantity: int, current_bid: float,
                       fees: FeeModel, env: dict | None = None) -> Valuation | None
  def value_from_estimate(est_resale: float, quantity: int, current_bid: float,
                          fees: FeeModel) -> Valuation
  ```
- Rules (from spec): tier 1.0 for `quantity <= 5`, else `DEALS_BULK_RECOVERY` (default 0.4). `piece_out_ceiling = per_unit * quantity * 0.8` for bulk lots, else None. Confidence: `high` ≥ 8 kept comps, `medium` ≥ 3, else the function returns `None` (caller degrades to estimate). `value_from_estimate` is always `confidence='low'`, `method='llm_estimate'`.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_valuation.py
import pytest
from deals.comps import Comp
from deals.fees import FeeModel
from deals.valuation import (Valuation, bulk_recovery_tier,
                             value_from_comps, value_from_estimate)

FEES = FeeModel(buyer_premium_pct=0.125, tax_pct=0.0, freight=0.0)

def _comps(prices):
    return [Comp(str(i), f"comp {i}", p, None, "") for i, p in enumerate(prices)]

def test_single_item_full_recovery():
    assert bulk_recovery_tier(1, {}) == 1.0
    assert bulk_recovery_tier(5, {}) == 1.0

def test_bulk_default_tier_and_env_override():
    assert bulk_recovery_tier(40, {}) == 0.4
    assert bulk_recovery_tier(40, {"DEALS_BULK_RECOVERY": "0.5"}) == 0.5

def test_median_times_qty_times_tier():
    v = value_from_comps(_comps([50, 100, 150]), 40, 10.0, FEES, {})
    assert v.per_unit == 100.0
    assert v.est_resale == pytest.approx(100.0 * 40 * 0.4)
    assert v.piece_out_ceiling == pytest.approx(100.0 * 40 * 0.8)
    assert v.method == "comps"
    # landed: 10 * 1.125 = 11.25 → margin ≈ 1588.75
    assert v.margin == pytest.approx(v.est_resale - 11.25)
    assert v.margin_pct == pytest.approx(v.margin / 11.25 * 100)

def test_confidence_scales_with_comp_count():
    assert value_from_comps(_comps([50] * 3), 1, 10, FEES, {}).confidence == "medium"
    assert value_from_comps(_comps([50] * 8), 1, 10, FEES, {}).confidence == "high"

def test_too_few_comps_returns_none():
    assert value_from_comps(_comps([50, 60]), 1, 10, FEES, {}) is None

def test_estimate_is_always_low_confidence():
    v = value_from_estimate(200.0, 1, 10.0, FEES)
    assert (v.method, v.confidence) == ("llm_estimate", "low")
    assert v.per_unit is None

def test_free_bid_zero_margin_pct_guard():
    v = value_from_estimate(200.0, 1, 0.0, FEES)   # landed cost 0
    assert v.margin_pct == 0.0                     # no div-by-zero
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_valuation.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# deals/valuation.py
"""Pure resale-valuation math. No LLM, no network, no DB.

The bid is not the cost (landed_cost adds premium/tax/freight) and the LLM
is never the price: comp-grounded valuations require >= 3 judged comps."""
import os
import statistics
from dataclasses import dataclass
from deals.comps import Comp
from deals.fees import FeeModel, landed_cost

BULK_QTY_THRESHOLD = 5
PIECE_OUT_FACTOR = 0.8
MIN_COMPS = 3
HIGH_COMPS = 8

@dataclass
class Valuation:
    method: str
    per_unit: float | None
    recovery_tier: float
    est_resale: float
    piece_out_ceiling: float | None
    landed_cost: float
    margin: float
    margin_pct: float
    confidence: str

def bulk_recovery_tier(quantity: int, env: dict | None = None) -> float:
    env = env if env is not None else os.environ
    if quantity <= BULK_QTY_THRESHOLD:
        return 1.0
    return float(env.get("DEALS_BULK_RECOVERY", "0.4"))

def _finish(method, per_unit, tier, est, ceiling, current_bid, quantity,
            fees, confidence) -> Valuation:
    lc = landed_cost(current_bid, quantity, fees).total
    margin = est - lc
    margin_pct = (margin / lc * 100) if lc > 0 else 0.0
    return Valuation(method, per_unit, tier, round(est, 2),
                     round(ceiling, 2) if ceiling is not None else None,
                     round(lc, 2), round(margin, 2), round(margin_pct, 1),
                     confidence)

def value_from_comps(kept: list[Comp], quantity: int, current_bid: float,
                     fees: FeeModel, env: dict | None = None) -> Valuation | None:
    if len(kept) < MIN_COMPS:
        return None
    per_unit = float(statistics.median(c.price for c in kept))
    tier = bulk_recovery_tier(quantity, env)
    est = per_unit * quantity * tier
    ceiling = per_unit * quantity * PIECE_OUT_FACTOR if tier < 1.0 else None
    confidence = "high" if len(kept) >= HIGH_COMPS else "medium"
    return _finish("comps", per_unit, tier, est, ceiling, current_bid,
                   quantity, fees, confidence)

def value_from_estimate(est_resale: float, quantity: int, current_bid: float,
                        fees: FeeModel) -> Valuation:
    return _finish("llm_estimate", None, 1.0, est_resale, None, current_bid,
                   quantity, fees, "low")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_valuation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deals/valuation.py tests/deals/test_valuation.py
git commit -m "feat(deals): comp-grounded valuation math with bulk recovery tiers"
```

---

### Task 7: Distance helper

**Files:**
- Create: `deals/geo.py`
- Test: `tests/deals/test_geo.py` (create)

**Interfaces:**
- Produces: `deals.geo.haversine_miles(lat1, lng1, lat2, lng2) -> float`; `deals.geo.distance_from_home(lat, lng, env: dict | None = None) -> float | None` (None when `DEALS_HOME_LAT`/`DEALS_HOME_LNG` unset or lot coords missing).

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_geo.py
import pytest
from deals.geo import haversine_miles, distance_from_home

def test_known_distance_dc_to_nyc():
    # Washington DC (38.9072, -77.0369) to NYC (40.7128, -74.0060) ≈ 204 mi
    assert haversine_miles(38.9072, -77.0369, 40.7128, -74.0060) == pytest.approx(204, abs=5)

def test_zero_distance():
    assert haversine_miles(38.9, -77.0, 38.9, -77.0) == 0.0

def test_home_unset_returns_none():
    assert distance_from_home(38.9, -77.0, {}) is None

def test_missing_lot_coords_returns_none():
    env = {"DEALS_HOME_LAT": "38.9", "DEALS_HOME_LNG": "-77.0"}
    assert distance_from_home(None, None, env) is None

def test_home_set_computes():
    env = {"DEALS_HOME_LAT": "38.9072", "DEALS_HOME_LNG": "-77.0369"}
    assert distance_from_home(40.7128, -74.0060, env) == pytest.approx(204, abs=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_geo.py -q` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# deals/geo.py
"""Great-circle distance for the deal browser's distance filter.
Distance is a filter knob, never a hard exclusion (spec decision)."""
import math
import os

EARTH_RADIUS_MILES = 3958.8

def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))

def distance_from_home(lat: float | None, lng: float | None,
                       env: dict | None = None) -> float | None:
    env = env if env is not None else os.environ
    hlat, hlng = env.get("DEALS_HOME_LAT"), env.get("DEALS_HOME_LNG")
    if not hlat or not hlng or lat is None or lng is None:
        return None
    return round(haversine_miles(float(hlat), float(hlng), lat, lng), 1)
```

- [ ] **Step 4: Run tests** → PASS

- [ ] **Step 5: Commit**

```bash
git add deals/geo.py tests/deals/test_geo.py
git commit -m "feat(deals): haversine distance-from-home helper"
```

---

### Task 8: LLM steps — identity extraction + comp judging

**Files:**
- Create: `deals/llm_steps.py`
- Test: `tests/deals/test_llm_steps.py` (create)

**Interfaces:**
- Consumes: `deals.comps.Comp`, `deals.models.Lot`, `automation.config.GEMINI_API_KEY`.
- Produces:
  ```python
  @dataclass
  class LotIdentity:
      brand: str | None; model: str | None; item_type: str
      quantity: int; condition: str | None
      queries: list[str]          # 2-3, most specific first
      est_resale_per_unit: float | None   # LLM guess, used ONLY in degraded mode
  def parse_identity_response(text: str) -> LotIdentity          # pure
  def extract_identity(lot: Lot) -> LotIdentity                  # Gemini call
  def parse_judge_response(text: str, comps: list[Comp]) -> list[Comp]  # pure
  def judge_comps(identity: LotIdentity, comps: list[Comp]) -> list[Comp]  # Gemini call
  ```
- Follow `deals/classify.py` exactly for the Gemini call shape (client, model `gemini-2.5-flash`, ```` ```json ```` stripping, fail-soft). `extract_identity` failure → raise `LlmStepError` (analyze counts it as an error for that lot). `judge_comps` failure → return `[]` (degrade). Quantity defaults to 1 when the LLM omits it or returns garbage.

- [ ] **Step 1: Write the failing test (pure parsers only — LLM calls are not unit-tested, same policy as classify.py)**

```python
# tests/deals/test_llm_steps.py
import pytest
from deals.comps import Comp
from deals.llm_steps import LotIdentity, parse_identity_response, parse_judge_response

def test_parse_identity_happy_path():
    text = '''```json
    {"brand": "Steelcase", "model": "Leap V2", "item_type": "office chair",
     "quantity": 40, "condition": "used",
     "queries": ["steelcase leap v2 chair", "steelcase leap chair used"],
     "est_resale_per_unit": 150}
    ```'''
    ident = parse_identity_response(text)
    assert ident.brand == "Steelcase" and ident.quantity == 40
    assert ident.queries[0] == "steelcase leap v2 chair"

def test_parse_identity_defaults_quantity_to_1():
    ident = parse_identity_response('{"item_type": "chair", "queries": ["chair"]}')
    assert ident.quantity == 1 and ident.brand is None

def test_parse_identity_garbage_raises():
    from deals.llm_steps import LlmStepError
    with pytest.raises(LlmStepError):
        parse_identity_response("I cannot help with that")

def test_parse_judge_keeps_by_index():
    comps = [Comp("1", "leap v2", 100, None, ""), Comp("2", "aeron", 900, None, ""),
             Comp("3", "leap v2 headrest only", 40, None, "")]
    kept = parse_judge_response('{"keep": [0]}', comps)
    assert [c.listing_id for c in kept] == ["1"]

def test_parse_judge_bad_indices_ignored():
    comps = [Comp("1", "x", 10, None, "")]
    assert parse_judge_response('{"keep": [0, 7, -2]}', comps) == [comps[0]]

def test_parse_judge_garbage_returns_empty():
    assert parse_judge_response("no json here", [Comp("1", "x", 10, None, "")]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_llm_steps.py -q` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# deals/llm_steps.py
"""Gemini steps of the analyze pipeline: identity extraction + comp judging.

Retrieval-then-reasoning: the LLM identifies and filters; prices come only
from retrieved sold comps (deals/valuation.py). est_resale_per_unit is the
degraded-mode fallback and is always confidence='low' downstream."""
import json
from dataclasses import dataclass, field
from deals.comps import Comp
from deals.models import Lot
from automation import config

class LlmStepError(Exception):
    pass

@dataclass
class LotIdentity:
    brand: str | None = None
    model: str | None = None
    item_type: str = "unknown"
    quantity: int = 1
    condition: str | None = None
    queries: list[str] = field(default_factory=list)
    est_resale_per_unit: float | None = None

_IDENTITY_PROMPT = """You are analyzing a government-surplus auction lot for resale.
Extract the product identity and give 2-3 eBay search queries (most specific first)
that would find SOLD listings of the same item. Also give your rough estimate of
the USED resale price per unit in USD (number only).
Respond ONLY as compact JSON:
{{"brand": <str|null>, "model": <str|null>, "item_type": <str>,
  "quantity": <int>, "condition": <str|null>,
  "queries": [<str>, ...], "est_resale_per_unit": <number|null>}}
Title: {title}
Description: {desc}"""

_JUDGE_PROMPT = """A surplus lot was identified as: {identity}
Below are eBay SOLD listings found for it, as "index: title ($price)".
Keep ONLY listings that are genuinely the same item (same product family and
whole-unit — not parts, accessories, or different models).
Respond ONLY as compact JSON: {{"keep": [<index>, ...]}}
{listings}"""

def _strip(text: str) -> str:
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

def parse_identity_response(text: str) -> LotIdentity:
    try:
        d = json.loads(_strip(text))
    except (json.JSONDecodeError, TypeError) as e:
        raise LlmStepError(f"unparseable identity response: {text[:120]!r}") from e
    if not d.get("queries"):
        raise LlmStepError("identity response has no queries")
    try:
        qty = max(1, int(d.get("quantity") or 1))
    except (TypeError, ValueError):
        qty = 1
    est = d.get("est_resale_per_unit")
    return LotIdentity(brand=d.get("brand"), model=d.get("model"),
                       item_type=d.get("item_type") or "unknown", quantity=qty,
                       condition=d.get("condition"),
                       queries=[q for q in d["queries"] if isinstance(q, str)][:3],
                       est_resale_per_unit=float(est) if est is not None else None)

def parse_judge_response(text: str, comps: list[Comp]) -> list[Comp]:
    try:
        d = json.loads(_strip(text))
        idx = d.get("keep", [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []
    return [comps[i] for i in idx
            if isinstance(i, int) and 0 <= i < len(comps)]

def _gemini(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return resp.text or ""

def extract_identity(lot: Lot) -> LotIdentity:
    try:
        text = _gemini(_IDENTITY_PROMPT.format(
            title=lot.title[:200], desc=(lot.description or "")[:1500]))
    except Exception as e:
        raise LlmStepError(f"gemini identity call failed: {e}") from e
    return parse_identity_response(text)

def judge_comps(identity: LotIdentity, comps: list[Comp]) -> list[Comp]:
    if not comps:
        return []
    listings = "\n".join(f"{i}: {c.title} (${c.price:.0f})" for i, c in enumerate(comps[:40]))
    ident_str = " ".join(filter(None, [identity.brand, identity.model, identity.item_type]))
    try:
        text = _gemini(_JUDGE_PROMPT.format(identity=ident_str, listings=listings))
    except Exception:
        return []
    return parse_judge_response(text, comps[:40])
```

- [ ] **Step 4: Run tests** → PASS

- [ ] **Step 5: Commit**

```bash
git add deals/llm_steps.py tests/deals/test_llm_steps.py
git commit -m "feat(deals): Gemini identity-extraction + comp-judging steps"
```

---

### Task 9: Render cron jobs + env plumbing

**Files:**
- Modify: `render.yaml`
- Create: `scripts/deals_cron.sh`
- Docs: append a "Deals cron" note to the `## deals/` section of `CLAUDE.md`

**Interfaces:**
- Consumes: CLI commands `discover` (T1), `watch-once` (existing), `analyze` (T11 — the cron entry can merge before T11 lands; the subcommand errors cleanly until then, so add this service LAST or set `schedule` after T11 merges. Preferred: land this task after T11 in merge order, or comment the analyze block until then).
- Produces: four Render cron services + `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`COMPS_URL`/`COMPS_KEY` env keys in the `blackwhole-secrets` group.

- [ ] **Step 1: Create the dispatch script**

```bash
# scripts/deals_cron.sh
#!/bin/sh
# Render cron entrypoint for the deals tracker: deals_cron.sh <subcommand>
# (committed script avoids the inline sh -c quote-mangling that broke
# run_discovery.sh's predecessor — see render.yaml history)
set -e
cd "$(dirname "$0")/.."
exec python -m deals.cli "$@"
```

`chmod +x scripts/deals_cron.sh`

- [ ] **Step 2: Add env keys to `render.yaml` `blackwhole-secrets`**

Append to `envVarGroups[0].envVars`:

```yaml
      - key: TELEGRAM_BOT_TOKEN     # deals digests + margin/relist alerts
        sync: false
      - key: TELEGRAM_CHAT_ID
        sync: false
      - key: COMPS_URL              # Pi sold-comps service (tunnel URL)
        sync: false
      - key: COMPS_KEY
        sync: false
```

- [ ] **Step 3: Add the cron services**

Append to `services:` in `render.yaml`:

```yaml
  # ── 3. Deals tracker — continuous loop (2026-07-17 spec) ────────────────
  - type: cron
    name: deals-discover
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "15 */6 * * *"     # every 6h
    autoDeploy: true
    dockerCommand: sh scripts/deals_cron.sh discover --categories all --max-pages 200
    envVars:
      - fromGroup: blackwhole-secrets
  - type: cron
    name: deals-watch
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "*/20 * * * *"     # every 20 min
    autoDeploy: true
    dockerCommand: sh scripts/deals_cron.sh watch-once
    envVars:
      - fromGroup: blackwhole-secrets
  - type: cron
    name: deals-analyze
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "45 * * * *"       # hourly
    autoDeploy: true
    dockerCommand: sh scripts/deals_cron.sh analyze
    envVars:
      - fromGroup: blackwhole-secrets
  - type: cron
    name: deals-digest
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "0 13 * * *"       # 13:00 UTC ≈ 9am ET
    autoDeploy: true
    dockerCommand: sh scripts/deals_cron.sh digest
    envVars:
      - fromGroup: blackwhole-secrets
```

- [ ] **Step 4: Verify + docs**

Run: `python3 -c "import yaml, io; yaml.safe_load(open('render.yaml')); print('yaml ok')"` → `yaml ok`
Run locally: `sh scripts/deals_cron.sh watch-once` (with repo `.env` loaded) → prints a `PollReport`.
Add to `CLAUDE.md` (deals section): one paragraph listing the four cron jobs, that `discover --categories all` sweeps the whole site, and that `TELEGRAM_*`+`COMPS_*` must be set in the Render dashboard (values live in the `blackwhole-secrets` group).

- [ ] **Step 5: Commit**

```bash
git add render.yaml scripts/deals_cron.sh CLAUDE.md
git commit -m "feat(deals): Render cron jobs for discover/watch/analyze/digest"
```

Operator follow-up (not automatable from here): set the four new secret values in the Render dashboard and confirm the Blueprint sync creates the cron services.

---

### Task 10: Expose the Pi comps service to Render (cloudflared tunnel)

**Files:**
- Create: `docs/PI_COMPS_SERVICE.md` (runbook)
- No repo code. Ops on the Pi over SSH (`ssh black-whole`).

**Interfaces:**
- Produces: `https://comps.black-whole.com` → Pi `localhost:8788`, protected by the existing `X-Comps-Key`. `COMPS_URL=https://comps.black-whole.com` works from Render; the tailnet URL `http://100.99.195.81:8788` keeps working locally.

- [ ] **Step 1: Install cloudflared on the Pi**

```bash
ssh black-whole 'curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o ~/cloudflared && chmod +x ~/cloudflared && ~/cloudflared --version'
```

- [ ] **Step 2: Authenticate + create the tunnel** (needs the Cloudflare account that owns black-whole.com; `cloudflared tunnel login` prints a URL for the operator to open)

```bash
ssh black-whole '~/cloudflared tunnel login'          # operator opens the printed URL
ssh black-whole '~/cloudflared tunnel create comps'
ssh black-whole '~/cloudflared tunnel route dns comps comps.black-whole.com'
```

- [ ] **Step 3: Config + systemd user unit**

```bash
ssh black-whole 'mkdir -p ~/.cloudflared && TUNNEL_ID=$(~/cloudflared tunnel list --output json | python3 -c "import json,sys; print(json.load(sys.stdin)[0][\"id\"])") && printf "tunnel: %s\ncredentials-file: /home/abdel/.cloudflared/%s.json\ningress:\n  - hostname: comps.black-whole.com\n    service: http://localhost:8788\n  - service: http_status:404\n" "$TUNNEL_ID" "$TUNNEL_ID" > ~/.cloudflared/config.yml'
ssh black-whole 'cat > ~/.config/systemd/user/cloudflared.service <<UNIT
[Unit]
Description=cloudflared tunnel (comps)
After=network-online.target

[Service]
ExecStart=%h/cloudflared tunnel run comps
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now cloudflared.service'
```

- [ ] **Step 4: Verify from the public internet**

```bash
curl -s https://comps.black-whole.com/health          # → {"ok":true,...}
curl -s -o /dev/null -w "%{http_code}\n" "https://comps.black-whole.com/comps?q=test+query"   # → 401 (no key)
```

- [ ] **Step 5: Write `docs/PI_COMPS_SERVICE.md`** covering: what runs on the Pi (`comps.service`, `cloudflared.service`, both systemd-user with linger), the fetch-discipline rules and why (from spec §Pi), redeploy via `scripts/deploy_pi_comps.sh`, health/cooldown checks, and key rotation (`~/comps/.env` + Render `COMPS_KEY`). Commit:

```bash
git add docs/PI_COMPS_SERVICE.md
git commit -m "docs: Pi comps service runbook (tunnel, redeploy, key rotation)"
```

---

### Task 11: Analyze orchestrator + Telegram verdict alerts

**Files:**
- Create: `deals/analyze.py`
- Modify: `deals/cli.py` (subcommand `analyze`)
- Test: `tests/deals/test_analyze.py` (create)

**Interfaces:**
- Consumes: `verdict_store.lots_for_analysis/insert_verdict/mark_alerted` (T3), `comps.comps_provider_from_env/CompsUnavailable` (T5), `llm_steps.extract_identity/judge_comps/LlmStepError` (T8), `valuation.value_from_comps/value_from_estimate` (T6), `mapping.asset_to_lot`, `fees.fee_model_from_env`, `telegram_alerts.send_message_sync`, `geo.distance_from_home` (T7).
- Produces:
  ```python
  @dataclass
  class AnalyzeReport: considered:int=0; analyzed:int=0; comp_grounded:int=0; degraded:int=0; alerted:int=0; errors:int=0
  def analyze_lot(lot: Lot, comps_provider, fees, env) -> dict          # one verdict dict
  def format_verdict_alert(lot: Lot, verdict: dict, distance: float | None) -> str  # pure
  def should_alert(verdict: dict, env: dict) -> bool                    # pure
  def run_analysis(now=None, env=None) -> AnalyzeReport
  ```
- Env: `DEALS_ANALYZE_MAX_BID` (25), `DEALS_ANALYZE_WINDOW_H` (24), `DEALS_ANALYZE_LIMIT` (50), `DEALS_ALERT_MIN_MARGIN_PCT` (100).

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_analyze.py
from datetime import datetime, timezone
from deals.analyze import should_alert, format_verdict_alert, analyze_lot
from deals.comps import Comp, CompsResult, CompsUnavailable
from deals.fees import FeeModel
from deals.models import Lot

def _lot(**kw):
    base = dict(asset_id=1, account_id=2, auction_id=3, title="40 Steelcase Leap V2 Chairs",
        description="lot of 40", native_category_id="372", native_category_name="Furniture",
        canonical_category="seating_furniture",
        end_utc=datetime(2026, 7, 18, tzinfo=timezone.utc), bid_count=0, opening_bid=5.0,
        current_bid=5.0, currency_code="USD", high_bidder=0, has_reserve=False,
        reserve_not_met=False, reserve_price=None, is_free=False, seller="GSA",
        city="Richmond", state="VA", zip="23220", lat=37.5, lng=-77.4,
        hero_image_url="", status="active", is_sold=False)
    base.update(kw)
    return Lot(**base)

def test_should_alert_gates_margin_confidence_and_method():
    env = {"DEALS_ALERT_MIN_MARGIN_PCT": "100"}
    good = {"margin_pct": 250.0, "confidence": "medium", "method": "comps"}
    assert should_alert(good, env)
    assert not should_alert(good | {"margin_pct": 50.0}, env)
    assert not should_alert(good | {"confidence": "low"}, env)
    assert not should_alert(good | {"method": "llm_estimate"}, env)

def test_alert_text_has_essentials():
    v = {"est_resale": 1600.0, "margin": 1500.0, "margin_pct": 700.0,
         "landed_cost": 100.0, "confidence": "medium", "comp_count": 5,
         "comps": [{"url": "https://ebay.com/itm/9", "price": 100.0, "title": "leap"}]}
    text = format_verdict_alert(_lot(), v, distance=104.2)
    assert "Steelcase" in text and "700" in text and "104" in text
    assert "govdeals.com" in text and "ebay.com/itm/9" in text

class FakeComps:
    def __init__(self, result): self.result = result
    def fetch(self, q):
        if isinstance(self.result, Exception): raise self.result
        return self.result

def _ident(monkeypatch, qty=40):
    from deals import analyze, llm_steps
    ident = llm_steps.LotIdentity(brand="Steelcase", model="Leap V2",
        item_type="office chair", quantity=qty,
        queries=["steelcase leap v2"], est_resale_per_unit=150.0)
    monkeypatch.setattr(analyze, "extract_identity", lambda lot: ident)
    return ident

def test_comp_grounded_verdict(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    comps = [Comp(str(i), "leap v2 chair", 100.0, None, "") for i in range(5)]
    monkeypatch.setattr(analyze, "judge_comps", lambda ident, c: comps)
    provider = FakeComps(CompsResult("q", 5, 100.0, comps, False))
    v = analyze_lot(_lot(), provider, FeeModel(), {})
    assert v["method"] == "comps" and v["comp_count"] == 5
    assert v["est_resale"] == 100.0 * 40 * 0.4

def test_degrades_when_comps_unavailable(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    v = analyze_lot(_lot(), FakeComps(CompsUnavailable("down")), FeeModel(), {})
    assert v["method"] == "llm_estimate" and v["confidence"] == "low"
    assert v["est_resale"] == 150.0 * 40          # est_per_unit × qty (no discount claim)

def test_degrades_when_no_provider(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    v = analyze_lot(_lot(), None, FeeModel(), {})
    assert v["method"] == "llm_estimate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_analyze.py -q` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# deals/analyze.py
"""The analyze pass: cheap-close funnel -> identity -> comps -> judge ->
valuation -> verdict row -> alert. Per-lot error isolation throughout."""
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from deals.comps import CompsUnavailable, comps_provider_from_env
from deals.fees import fee_model_from_env
from deals.geo import distance_from_home
from deals.llm_steps import LlmStepError, extract_identity, judge_comps
from deals.mapping import asset_to_lot
from deals.models import Lot
from deals.valuation import value_from_comps, value_from_estimate
from deals.verdict_store import insert_verdict, lots_for_analysis, mark_alerted
from automation.telegram_alerts import send_message_sync

@dataclass
class AnalyzeReport:
    considered: int = 0; analyzed: int = 0; comp_grounded: int = 0
    degraded: int = 0; alerted: int = 0; errors: int = 0

def analyze_lot(lot: Lot, comps_provider, fees, env: dict) -> dict:
    identity = extract_identity(lot)          # raises LlmStepError -> caller counts error
    kept, all_comps, val = [], [], None
    if comps_provider is not None:
        for q in identity.queries:
            try:
                result = comps_provider.fetch(q)
            except CompsUnavailable as e:
                print(f"[analyze] comps unavailable for {q!r}: {e}", file=sys.stderr)
                continue
            all_comps = result.items
            kept = judge_comps(identity, result.items)
            val = value_from_comps(kept, identity.quantity, lot.current_bid, fees, env)
            if val is not None:
                break
    if val is None:
        est_pu = identity.est_resale_per_unit or 0.0
        val = value_from_estimate(est_pu * identity.quantity, identity.quantity,
                                  lot.current_bid, fees)
    return {
        "asset_id": lot.asset_id, "account_id": lot.account_id,
        "auction_id": lot.auction_id, "analyzed_at": datetime.now().astimezone(),
        "identity": {"brand": identity.brand, "model": identity.model,
                     "item_type": identity.item_type, "quantity": identity.quantity,
                     "condition": identity.condition},
        "queries": identity.queries, "method": val.method,
        "comps": [{"listing_id": c.listing_id, "title": c.title, "price": c.price,
                   "url": c.url} for c in kept],
        "comp_count": len(kept), "per_unit": val.per_unit,
        "recovery_tier": val.recovery_tier, "est_resale": val.est_resale,
        "piece_out_ceiling": val.piece_out_ceiling, "landed_cost": val.landed_cost,
        "margin": val.margin, "margin_pct": val.margin_pct,
        "confidence": val.confidence,
        "reasoning": f"{len(kept)}/{len(all_comps)} comps kept for "
                     f"'{identity.queries[0] if identity.queries else ''}'",
        "rank_score": None, "rank_notes": None, "alerted_at": None,
    }

def should_alert(verdict: dict, env: dict) -> bool:
    min_pct = float(env.get("DEALS_ALERT_MIN_MARGIN_PCT", "100"))
    return (verdict["method"] == "comps"
            and verdict["confidence"] in ("medium", "high")
            and verdict["margin_pct"] >= min_pct)

def format_verdict_alert(lot: Lot, v: dict, distance: float | None) -> str:
    url = f"https://www.govdeals.com/en/asset/{lot.asset_id}/{lot.account_id}"
    dist = f" · {distance:.0f} mi away" if distance is not None else ""
    comp_urls = " ".join(c["url"] for c in v.get("comps", [])[:3] if c.get("url"))
    return (f"💰 {lot.title[:70]}\n"
            f"bid ${lot.current_bid:.0f} ({lot.bid_count} bids) → "
            f"est. resale ${v['est_resale']:.0f} "
            f"(margin {v['margin_pct']:.0f}%, {v['confidence']}, "
            f"{v['comp_count']} comps)\n"
            f"landed ~${v['landed_cost']:.0f} · {lot.city}, {lot.state}{dist}\n"
            f"{url}\ncomps: {comp_urls}")

def run_analysis(now: datetime | None = None, env: dict | None = None) -> AnalyzeReport:
    now = now or datetime.now().astimezone()
    env = env if env is not None else dict(os.environ)
    rep = AnalyzeReport()
    fees = fee_model_from_env()
    provider = comps_provider_from_env(env)
    rows = lots_for_analysis(now,
        max_bid=float(env.get("DEALS_ANALYZE_MAX_BID", "25")),
        window_h=int(env.get("DEALS_ANALYZE_WINDOW_H", "24")),
        limit=int(env.get("DEALS_ANALYZE_LIMIT", "50")))
    for row in rows:
        rep.considered += 1
        try:
            lot = asset_to_lot(row["raw"])
            verdict = analyze_lot(lot, provider, fees, env)
            insert_verdict(verdict)
            rep.analyzed += 1
            if verdict["method"] == "comps":
                rep.comp_grounded += 1
            else:
                rep.degraded += 1
            if should_alert(verdict, env):
                dist = distance_from_home(lot.lat, lot.lng, env)
                ok, _ = send_message_sync(format_verdict_alert(lot, verdict, dist))
                if ok:
                    mark_alerted((lot.asset_id, lot.account_id, lot.auction_id),
                                 verdict["analyzed_at"])
                    rep.alerted += 1
        except (LlmStepError, ValueError, KeyError) as e:
            rep.errors += 1
            print(f"[analyze] error on {row.get('asset_id')}: {e}", file=sys.stderr)
    return rep
```

CLI wiring in `deals/cli.py`:

```python
    sub.add_parser("analyze")
```
```python
    elif a.cmd == "analyze":
        from deals.analyze import run_analysis
        print(run_analysis())
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/deals/test_analyze.py tests/deals/ -q` → all PASS

- [ ] **Step 5: Commit + live smoke**

```bash
git add deals/analyze.py deals/cli.py tests/deals/test_analyze.py
git commit -m "feat(deals): analyze pass — funnel, comps pipeline, verdicts, alerts"
COMPS_URL=http://100.99.195.81:8788 COMPS_KEY=<from Pi ~/comps/.env> \
  .venv/bin/python -m deals.cli analyze    # live smoke: expect AnalyzeReport with analyzed > 0
```

Then check one verdict: `SELECT method, comp_count, est_resale, margin_pct, confidence FROM deal_verdicts ORDER BY analyzed_at DESC LIMIT 5;`

---

### Task 12: Deals API — verdicts, distance, lists, tags, saved searches

**Files:**
- Modify: `automation/web/deals_query.py`
- Modify: `automation/web/app.py` (extend `/api/deals`; new CRUD endpoints)
- Test: `tests/deals/test_deals_query_v2.py` (create)

**Interfaces:**
- Consumes: tables from T3, `deals.geo.distance_from_home` (T7).
- Produces (all admin-auth'd like existing `/api/deals`):
  - `/api/deals` gains query params `min_margin: float|None`, `list_id: int|None`, `tag: str|None`, `max_distance: float|None`, sort key `"margin"`; each row gains `verdict` (dict|None — latest verdict: `method, est_resale, margin_pct, confidence, comp_count, comps, rank_score`) and `distance_mi` (float|None).
  - `GET/POST/DELETE /api/deals/lists` — `{id, name, count}`; POST body `{name}`; DELETE `/api/deals/lists/{id}`.
  - `PUT/DELETE /api/deals/lists/{id}/items/{asset_id}/{account_id}/{auction_id}`.
  - `PUT/DELETE /api/deals/tags/{asset_id}/{account_id}/{auction_id}/{tag}`; `GET /api/deals/tags` (distinct tags + counts).
  - `GET/POST/DELETE /api/deals/searches` — saved searches; POST body `{name, params, alert}`.
- Implementation notes: latest-verdict join uses `LEFT JOIN LATERAL (... ORDER BY analyzed_at DESC LIMIT 1)`; `min_margin` filters on the joined verdict's `margin_pct`; `max_distance` is applied in Python after `enrich` (distance needs env home coords — do NOT put it in SQL); `list_id`/`tag` filter via `EXISTS` subqueries with `%s` binding.

- [ ] **Step 1: Write the failing test (pure query-builder parts)**

```python
# tests/deals/test_deals_query_v2.py
from automation.web.deals_query import build_where, order_clause, SORTS

def test_min_margin_adds_verdict_filter():
    where, args = build_where(status="active", min_margin=150.0)
    assert "margin_pct" in where and 150.0 in args

def test_list_filter_uses_exists_with_binding():
    where, args = build_where(status="active", list_id=7)
    assert "deal_list_items" in where and 7 in args

def test_tag_filter_uses_exists_with_binding():
    where, args = build_where(status="active", tag="pallet")
    assert "deal_lot_tags" in where and "pallet" in args

def test_margin_sort_available():
    assert "margin" in SORTS
    assert "v.margin_pct" in order_clause("margin", "desc")

def test_no_new_filters_no_new_sql():
    where, args = build_where(status="active")
    assert "deal_list_items" not in where and "margin_pct" not in where
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/deals/test_deals_query_v2.py -q` → FAIL

- [ ] **Step 3: Implement**

In `deals_query.py`: add params `min_margin=None, list_id=None, tag=None` to `build_where`; append fragments:

```python
    if min_margin is not None:
        where.append("v.margin_pct >= %s")
        args.append(min_margin)
    if list_id is not None:
        where.append("""EXISTS (SELECT 1 FROM deal_list_items li
            WHERE li.list_id = %s AND li.asset_id = deal_lots.asset_id
              AND li.account_id = deal_lots.account_id
              AND li.auction_id = deal_lots.auction_id)""")
        args.append(list_id)
    if tag is not None:
        where.append("""EXISTS (SELECT 1 FROM deal_lot_tags t
            WHERE t.tag = %s AND t.asset_id = deal_lots.asset_id
              AND t.account_id = deal_lots.account_id
              AND t.auction_id = deal_lots.auction_id)""")
        args.append(tag)
```

Add `"margin": "v.margin_pct"` to `SORTS` (and qualify existing sort columns with `deal_lots.` where ambiguity arises). In `app.py::list_deals`: change the FROM clause to

```sql
FROM deal_lots
LEFT JOIN LATERAL (
    SELECT method, est_resale, margin_pct, confidence, comp_count, comps,
           rank_score, analyzed_at
    FROM deal_verdicts v0
    WHERE v0.asset_id = deal_lots.asset_id AND v0.account_id = deal_lots.account_id
      AND v0.auction_id = deal_lots.auction_id
    ORDER BY v0.analyzed_at DESC LIMIT 1) v ON TRUE
```

select `deal_lots.*, row_to_json(v.*) AS verdict`, thread the three new query params through the endpoint signature into `build_where`, compute `row["distance_mi"] = distance_from_home(row.get("lat"), row.get("lng"))` inside the row loop, and apply `max_distance` filtering post-enrich (`rows = [r for r in rows if max_distance is None or (r["distance_mi"] is not None and r["distance_mi"] <= max_distance)]`). Add the CRUD endpoints as thin `db.execute`/`db.fetch_all` wrappers following the style of the existing `/api/favorites` handlers in `app.py` (same auth decorator, same error shape). NULLs: `min_margin`/`margin` sort exclude never-analyzed lots by SQL semantics — that is the intended behavior.

- [ ] **Step 4: Run tests + existing suite**

Run: `.venv/bin/python -m pytest tests/deals/ -q` → PASS (especially any existing `deals_query`/viewer tests)
Manual: restart `python -m automation.web`, hit `http://127.0.0.1:8765/api/deals?sort=margin&min_margin=50` and confirm `verdict` + `distance_mi` keys appear.

- [ ] **Step 5: Commit**

```bash
git add automation/web/deals_query.py automation/web/app.py tests/deals/test_deals_query_v2.py
git commit -m "feat(web): deals API v2 — verdicts, distance, lists, tags, saved searches"
```

---

### Task 13: Deal Browser frontend (Zillow-model)

**Files:**
- Modify: `automation/web/templates/index.html` (Deals tab markup)
- Modify: `automation/web/static/app.js`, `automation/web/static/app.css`

**Interfaces:**
- Consumes: every endpoint from T12.
- Produces (UI only, no new APIs): on the `10 Deals` tab —
  - verdict columns in the table: `est. resale`, `margin %` (color-scaled), `conf`, `comps` (link count opens a detail drawer listing comp title/price/url), `rank`;
  - a `♥` save control per row → popover with list checkboxes + "new list" inline input (PUT/DELETE list items);
  - tag chips per row (click × to remove, `+` to add) and a tag filter dropdown fed by `GET /api/deals/tags`;
  - filter bar additions: `min margin %`, `max distance mi`, list selector;
  - a "★ Save this search" button → names current filter state, `POST /api/deals/searches` with `alert` checkbox; saved searches render as chips above the filters (click = apply params, × = delete).
- Style: existing dark terminal admin theme (`app.css` conventions — same class naming as current Deals tab). No framework; vanilla JS matching current `app.js` patterns. GovAuctions-style verdict presentation: margin % is the loudest element of the row.

- [ ] **Step 1: Implement markup + JS + CSS** (single step — frontend here is not unit-tested; follow existing Deals-tab code in `app.js` for fetch/render patterns and reuse its state object for filter params)

- [ ] **Step 2: Manual verification checklist** (restart `python -m automation.web`, open `/admin` → `10 Deals`):
  - rows show verdict columns; un-analyzed lots show `—`
  - sort by margin works; min-margin + max-distance filters change the row set
  - heart a lot into a new list "watch"; filter by that list; unheart removes
  - add tag `pallet`, filter by it, remove it
  - save a search with alert ON; chip appears; re-applying it restores filters; delete works
  - `04 Auctions` and other tabs unaffected (regression eyeball)

- [ ] **Step 3: Commit**

```bash
git add automation/web/templates/index.html automation/web/static/app.js automation/web/static/app.css
git commit -m "feat(web): deal browser — verdict columns, lists, tags, saved searches"
```

---

### Task 14: Saved-search alert evaluation

**Files:**
- Create: `deals/saved_search_alerts.py`
- Modify: `deals/analyze.py` (call at end of `run_analysis`)
- Test: `tests/deals/test_saved_search_alerts.py` (create)

**Interfaces:**
- Consumes: `saved_searches` table (T3), `deals_query.build_where` (T12), `telegram_alerts.send_message_sync`.
- Produces: `deals.saved_search_alerts.run_saved_search_alerts(now=None) -> int` (searches with `alert=true`: run their stored `params` through `build_where` plus `first_seen_at > last_run_at` so only NEW lots match; send one Telegram message per search listing up to 10 new matches; update `last_run_at`); pure helper `format_search_alert(name: str, rows: list[dict]) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_saved_search_alerts.py
from deals.saved_search_alerts import format_search_alert

def test_format_lists_lots_with_urls():
    rows = [{"title": "40 chairs", "current_bid": 5.0, "bid_count": 0,
             "asset_id": 1, "account_id": 2, "city": "Richmond", "state": "VA"}]
    text = format_search_alert("cheap chairs", rows)
    assert "cheap chairs" in text and "40 chairs" in text
    assert "govdeals.com/en/asset/1/2" in text

def test_format_caps_at_ten():
    rows = [{"title": f"lot {i}", "current_bid": 1, "bid_count": 0,
             "asset_id": i, "account_id": 1, "city": "X", "state": "Y"} for i in range(25)]
    text = format_search_alert("s", rows)
    assert "lot 9" in text and "lot 11" not in text and "+15 more" in text
```

- [ ] **Step 2: Run to verify FAIL**, **Step 3: Implement**

```python
# deals/saved_search_alerts.py
"""Saved searches with alert=true fire a Telegram message when NEW lots
(first_seen_at after the search's last_run_at) match their stored params."""
import sys
from datetime import datetime
from automation import db
from automation.telegram_alerts import send_message_sync
from automation.web.deals_query import build_where

def format_search_alert(name: str, rows: list[dict]) -> str:
    lines = [f"🔎 saved search “{name}”: {len(rows)} new match(es)"]
    for r in rows[:10]:
        url = f"https://www.govdeals.com/en/asset/{r['asset_id']}/{r['account_id']}"
        lines.append(f"• {r['title'][:55]} — ${float(r['current_bid'] or 0):.0f} "
                     f"({r['bid_count']} bids), {r['city']}, {r['state']} — {url}")
    if len(rows) > 10:
        lines.append(f"…+{len(rows) - 10} more")
    return "\n".join(lines)

_ALLOWED = {"q", "category", "native", "state", "max_bids", "ending_within",
            "status", "min_margin", "list_id", "tag"}

def run_saved_search_alerts(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    sent = 0
    for s in db.fetch_all("SELECT * FROM saved_searches WHERE alert = true"):
        try:
            params = {k: v for k, v in (s["params"] or {}).items() if k in _ALLOWED}
            where, args = build_where(**params)
            if s["last_run_at"]:
                where += " AND deal_lots.first_seen_at > %s"
                args = list(args) + [s["last_run_at"]]
            rows = db.fetch_all(f"""SELECT deal_lots.* FROM deal_lots
                LEFT JOIN LATERAL (SELECT margin_pct FROM deal_verdicts v0
                    WHERE v0.asset_id=deal_lots.asset_id AND v0.account_id=deal_lots.account_id
                      AND v0.auction_id=deal_lots.auction_id
                    ORDER BY v0.analyzed_at DESC LIMIT 1) v ON TRUE
                WHERE {where} ORDER BY end_utc ASC LIMIT 100""", args)
            if rows:
                ok, _ = send_message_sync(format_search_alert(s["name"], rows))
                sent += 1 if ok else 0
            db.execute("UPDATE saved_searches SET last_run_at=%s WHERE id=%s", (now, s["id"]))
        except Exception as e:
            print(f"[saved-search] {s.get('name')}: {e}", file=sys.stderr)
    return sent
```

Call it at the end of `run_analysis` (before `return rep`):

```python
    try:
        run_saved_search_alerts(now)
    except Exception as e:
        print(f"[analyze] saved-search pass failed: {e}", file=sys.stderr)
```

(import at top: `from deals.saved_search_alerts import run_saved_search_alerts`)

- [ ] **Step 4: Run tests** → `.venv/bin/python -m pytest tests/deals/test_saved_search_alerts.py tests/deals/test_analyze.py -q` PASS

- [ ] **Step 5: Commit**

```bash
git add deals/saved_search_alerts.py deals/analyze.py tests/deals/test_saved_search_alerts.py
git commit -m "feat(deals): saved-search Telegram alerts on new matching lots"
```

---

### Task 15: Relist detection

**Files:**
- Create: `deals/relist.py`
- Modify: `deals/discover.py` (hook after each category loop)
- Test: `tests/deals/test_relist.py` (create)

**Interfaces:**
- Consumes: `deal_lots.relist_of` column (T3), `telegram_alerts.send_message_sync`.
- Produces: `deals.relist.title_similarity(a: str, b: str) -> float` (token-set Jaccard, 0..1, pure); `find_relist(lot_row: dict, closed_rows: list[dict]) -> dict | None` (pure; match = same `account_id`, similarity ≥ 0.6, different `auction_id`); `scan_for_relists(now=None) -> int` (recent unmatched lots vs closed no-bid lots from same accounts; stamps `relist_of` JSON `{asset_id, account_id, auction_id, final_bid, closed_at}`; one Telegram alert per detection).

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_relist.py
from deals.relist import title_similarity, find_relist

def test_similarity_ignores_order_and_case():
    assert title_similarity("Steelcase Leap V2 Chairs (40)",
                            "chairs steelcase LEAP v2 40") > 0.8

def test_similarity_disjoint_is_zero():
    assert title_similarity("forklift", "office chairs") == 0.0

CLOSED = [{"asset_id": 9, "account_id": 5, "auction_id": 100,
           "title": "Lot of 40 Steelcase Leap V2 Chairs", "final_bid": 0.0,
           "closed_at": "2026-07-10"}]

def test_find_relist_matches_same_account_similar_title():
    new = {"asset_id": 77, "account_id": 5, "auction_id": 200,
           "title": "40 Steelcase Leap V2 Chairs — RELISTED"}
    m = find_relist(new, CLOSED)
    assert m and m["auction_id"] == 100

def test_no_match_across_accounts():
    new = {"asset_id": 77, "account_id": 6, "auction_id": 200,
           "title": "Lot of 40 Steelcase Leap V2 Chairs"}
    assert find_relist(new, CLOSED) is None

def test_same_auction_is_not_a_relist():
    new = {"asset_id": 9, "account_id": 5, "auction_id": 100,
           "title": "Lot of 40 Steelcase Leap V2 Chairs"}
    assert find_relist(new, CLOSED) is None
```

- [ ] **Step 2: Run to verify FAIL**, **Step 3: Implement**

```python
# deals/relist.py
"""Detect no-bid lots reappearing under a new auction (same seller account).
A relist is the second chance to buy at opening price — alert immediately."""
import json
import re
import sys
from datetime import datetime, timedelta
from automation import db
from automation.telegram_alerts import send_message_sync

SIM_THRESHOLD = 0.6

def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1}

def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def find_relist(lot_row: dict, closed_rows: list[dict]) -> dict | None:
    best, best_sim = None, 0.0
    for c in closed_rows:
        if c["account_id"] != lot_row["account_id"]:
            continue
        if c["auction_id"] == lot_row["auction_id"]:
            continue
        sim = title_similarity(lot_row["title"], c["title"])
        if sim >= SIM_THRESHOLD and sim > best_sim:
            best, best_sim = c, sim
    return best

def scan_for_relists(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    fresh = db.fetch_all("""SELECT asset_id, account_id, auction_id, title, current_bid
        FROM deal_lots WHERE relist_of IS NULL AND outcome IS NULL
          AND first_seen_at > %s""", (now - timedelta(days=2),))
    if not fresh:
        return 0
    accounts = tuple({r["account_id"] for r in fresh})
    closed = db.fetch_all("""SELECT asset_id, account_id, auction_id, title,
               final_bid, closed_at
        FROM deal_lots WHERE outcome = 'no_bid' AND account_id = ANY(%s)""",
        (list(accounts),))
    hits = 0
    for lot in fresh:
        try:
            m = find_relist(lot, closed)
            if not m:
                continue
            db.execute("""UPDATE deal_lots SET relist_of=%s, updated_at=now()
                WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
                (json.dumps({"asset_id": m["asset_id"], "account_id": m["account_id"],
                             "auction_id": m["auction_id"],
                             "final_bid": m["final_bid"],
                             "closed_at": str(m["closed_at"])}, default=str),
                 lot["asset_id"], lot["account_id"], lot["auction_id"]))
            url = f"https://www.govdeals.com/en/asset/{lot['asset_id']}/{lot['account_id']}"
            send_message_sync(f"♻️ RELIST: {lot['title'][:60]}\n"
                              f"previously closed no-bid — now ${float(lot['current_bid'] or 0):.0f}\n{url}")
            hits += 1
        except Exception as e:
            print(f"[relist] error on {lot['asset_id']}: {e}", file=sys.stderr)
    return hits
```

Hook into `deals/discover.py::run_discovery` — after the categories loop, before `return rep`:

```python
    try:
        from deals.relist import scan_for_relists
        scan_for_relists(now)
    except Exception as e:
        print(f"[discover] relist scan failed: {e}", file=sys.stderr)
```

- [ ] **Step 4: Run tests** → `.venv/bin/python -m pytest tests/deals/test_relist.py tests/deals/test_discover.py -q` PASS

- [ ] **Step 5: Commit**

```bash
git add deals/relist.py deals/discover.py tests/deals/test_relist.py
git commit -m "feat(deals): relist detection with Telegram alert"
```

---

### Task 16: Claude local re-rank (`deals.cli rank`)

**Files:**
- Create: `deals/rank.py`
- Modify: `deals/cli.py` (subcommand `rank`)
- Test: `tests/deals/test_rank.py` (create)

**Interfaces:**
- Consumes: `deal_verdicts` (T3, written by T11).
- Produces: `deals.rank.build_rank_prompt(verdicts: list[dict]) -> str` (pure), `parse_rank_response(text: str) -> list[dict]` (pure; `[{"index": int, "score": float, "notes": str}]`), `run_rank(top_n: int = 20) -> int` (loads today's top verdicts by `margin_pct`, calls `claude -p <prompt> --output-format text` via `subprocess.run`, writes `rank_score`/`rank_notes` back). Runs LOCALLY only (needs the `claude` CLI on PATH); on Render it simply never runs.

- [ ] **Step 1: Write the failing test**

```python
# tests/deals/test_rank.py
from deals.rank import build_rank_prompt, parse_rank_response

VERDICTS = [{"asset_id": 1, "account_id": 2, "auction_id": 3,
             "identity": {"brand": "Steelcase", "item_type": "chair", "quantity": 40},
             "est_resale": 1600.0, "margin_pct": 700.0, "landed_cost": 100.0,
             "confidence": "medium", "comp_count": 5,
             "comps": [{"title": "leap v2", "price": 100.0, "url": "u"}]}]

def test_prompt_contains_lot_facts_and_json_contract():
    p = build_rank_prompt(VERDICTS)
    assert "Steelcase" in p and "700" in p and '"index"' in p

def test_parse_happy_path():
    out = parse_rank_response('[{"index": 0, "score": 7.5, "notes": "solid comps"}]')
    assert out[0]["score"] == 7.5

def test_parse_fenced_and_garbage():
    assert parse_rank_response('```json\n[{"index":0,"score":1,"notes":""}]\n```')[0]["index"] == 0
    assert parse_rank_response("sorry") == []
```

- [ ] **Step 2: Run to verify FAIL**, **Step 3: Implement**

```python
# deals/rank.py
"""Claude-CLI adversarial re-rank of the day's top verdicts. Local-only
(TTY machine with `claude` on PATH); second brain on the shortlist."""
import json
import subprocess
import sys
from automation import db

_CONTRACT = ('Respond ONLY as compact JSON: [{"index": <int>, "score": <0-10>, '
             '"notes": "<risk notes, <=120 chars>"}] — one entry per lot, '
             'score = how confident you are this is a real profitable flip.')

def build_rank_prompt(verdicts: list[dict]) -> str:
    lines = ["You are auditing resale-arbitrage verdicts on government-surplus "
             "auction lots. Judge each skeptically: comp relevance, liquidity, "
             "condition risk, freight reality.", ""]
    for i, v in enumerate(verdicts):
        ident = v.get("identity") or {}
        comps = ", ".join(f"${c['price']:.0f} {c['title'][:40]}"
                          for c in (v.get("comps") or [])[:5])
        lines.append(f"{i}: {ident.get('quantity', 1)}x {ident.get('brand') or '?'} "
                     f"{ident.get('item_type', '?')} — est ${v['est_resale']:.0f}, "
                     f"margin {v['margin_pct']:.0f}%, landed ${v['landed_cost']:.0f}, "
                     f"{v['comp_count']} comps [{comps}] ({v['confidence']})")
    lines += ["", _CONTRACT]
    return "\n".join(lines)

def parse_rank_response(text: str) -> list[dict]:
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        out = json.loads(t)
        return [r for r in out if isinstance(r.get("index"), int)
                and isinstance(r.get("score"), (int, float))]
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []

def run_rank(top_n: int = 20) -> int:
    verdicts = db.fetch_all("""SELECT * FROM deal_verdicts
        WHERE analyzed_at > now() - interval '24 hours' AND method = 'comps'
        ORDER BY margin_pct DESC LIMIT %s""", (top_n,))
    if not verdicts:
        print("no comp-grounded verdicts in the last 24h"); return 0
    proc = subprocess.run(["claude", "-p", build_rank_prompt(verdicts),
                           "--output-format", "text"],
                          capture_output=True, text=True, timeout=300)
    ranks = parse_rank_response(proc.stdout)
    if not ranks:
        print(f"claude produced no parseable ranking: {proc.stdout[:200]!r}",
              file=sys.stderr)
        return 0
    for r in ranks:
        v = verdicts[r["index"]]
        db.execute("""UPDATE deal_verdicts SET rank_score=%s, rank_notes=%s
            WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND analyzed_at=%s""",
            (float(r["score"]), (r.get("notes") or "")[:300],
             v["asset_id"], v["account_id"], v["auction_id"], v["analyzed_at"]))
    return len(ranks)
```

CLI wiring: `sub.add_parser("rank")` and

```python
    elif a.cmd == "rank":
        from deals.rank import run_rank
        print(f"ranked {run_rank()} verdicts")
```

- [ ] **Step 4: Run tests** → PASS. Live smoke (local, after T11 has produced verdicts): `.venv/bin/python -m deals.cli rank`

- [ ] **Step 5: Commit**

```bash
git add deals/rank.py deals/cli.py tests/deals/test_rank.py
git commit -m "feat(deals): Claude-CLI re-rank of top verdicts"
```

---

## Final verification (after all waves)

1. `.venv/bin/python -m pytest tests/ -q` — full suite green.
2. Live smoke sequence (local, with repo `.env` + `COMPS_*` set): `init-schema` → `backfill-outcomes` → `discover --categories all --max-pages 5` → `watch-once` → `analyze` → `rank` — each prints a sane report; spot-check `deal_verdicts` rows and one Telegram alert (temporarily set `DEALS_ALERT_MIN_MARGIN_PCT=0` to force one, then restore).
3. Dashboard: Deals tab shows verdicts, lists/tags/saved-search flows work end-to-end.
4. Render: blueprint sync creates 4 cron services; check each service's first run log in the Render dashboard; `https://comps.black-whole.com/health` returns ok.
5. Update `CLAUDE.md` deals section + memory (`deals-tracker-v1.md`) with the new state.

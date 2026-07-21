# Site Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Public Surplus data loss + visibility, remove the dead budget question, show recently-sold lots as social proof (with an internal `lost_sold_out` status), and reorganize the listing photo folders by status.

**Architecture:** Four independent tickets against the existing FastAPI + Supabase-Postgres app in `automation/web/`. No new services. Ticket 1 is pure data-integrity + read-path fixes. Ticket 2 is a template + one line of Python. Ticket 3 adds one nullable column and one status value, then branches the public read path/templates on status. Ticket 4 adds a physical-folder resolver so disk layout can change without breaking the flat `folder_name` URL/key contract.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, psycopg (Supabase Postgres via `automation/db.py`), SQLite (upstream scrape cache), pytest, vanilla JS/CSS.

## Global Constraints

- All new DB code goes through `from automation import db` (psycopg, dict rows). timestamptz reads back as `datetime`, not ISO string.
- Machine-facing outputs stay untouched: sitemap `<lastmod>`, `/catalog/facebook.csv`, all `/api/*` JSON. Only human-facing surfaces change.
- The public "sold" label is the single constant `SOLD_PUBLIC_LABEL = "SOLD OUT"`. Internally `sold_out` (genuinely sold) and `lost_sold_out` (lost at auction) are distinct; the public treats them identically.
- `folder_name` stays a flat basename (e.g. `Fresno_California_Banquet_Chairs_700`) — it is the `/image/` URL segment AND the Supabase Storage key prefix (BLACKWHOLE-6). Ticket 4 must NOT change it.
- Schema changes ship as staged, guarded `ops/pending-db-updates-*.sql` files (NOT applied by code) — same convention as existing `ops/` files. The Supabase status CHECK is the enforcing mirror; the SQLite schema does not enforce the status set.
- Run tests with `.venv/bin/python -m pytest` (this venv has no `pytest` console script).
- Date formatting (Ticket 5) is **backlogged** — do not touch date rendering.

---

## Ticket 1 — Public Surplus: stop data loss, restore visibility

### Task 1.1: Preserve quantity in the Supabase transfer upsert

**Files:**
- Modify: `scripts/transfer_listings_to_supabase.py:75-80` (the `UPSERT` string)
- Test: `tests/test_transfer_upsert_guard.py` (create)

**Interfaces:**
- Produces: unchanged public surface. The `UPSERT` module-level string now contains `COALESCE(EXCLUDED.quantity, auction_listings.quantity)` and a `WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at` guard.

**Why:** A failed LLM run sets `quantity = NULL`; the current `quantity = EXCLUDED.quantity` overwrites yesterday's verified count with NULL. The `last_seen_at` guard also stops the dashboard-triggered stale-file clobber (reading the checked-in `state/listings.db` with old timestamps over fresh Supabase rows) — the unfixed per-source clobber from the `transfer-clobbers-fresh-govdeals-rows` note.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transfer_upsert_guard.py
"""Guard: a failed-LLM NULL quantity must never erase a verified count, and a
stale re-transfer must never push last_seen_at backward. Pure string checks on
the module-level UPSERT — no DB. Run: .venv/bin/python -m pytest tests/test_transfer_upsert_guard.py -v
"""
from scripts.transfer_listings_to_supabase import UPSERT


def test_quantity_upsert_coalesces_incoming_null():
    # incoming NULL quantity must fall back to the stored value
    assert "COALESCE(EXCLUDED.quantity, auction_listings.quantity)" in UPSERT
    # and must NOT be a bare overwrite
    assert "quantity = EXCLUDED.quantity" not in UPSERT


def test_upsert_guards_against_stale_last_seen():
    assert "WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at" in UPSERT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_transfer_upsert_guard.py -v`
Expected: FAIL — `COALESCE(...)` not found in current UPSERT.

- [ ] **Step 3: Implement**

Replace the `UPSERT` definition (`scripts/transfer_listings_to_supabase.py:75-80`) with:

```python
# quantity: a failed-LLM run sends NULL; COALESCE keeps the last verified count
# instead of erasing it. Every other column takes the fresh value.
_UPDATE_SET = ", ".join(
    (
        "quantity = COALESCE(EXCLUDED.quantity, auction_listings.quantity)"
        if c == "quantity"
        else f"{c} = EXCLUDED.{c}"
    )
    for c in COLS
    if c != "asset_id"
)

UPSERT = f"""
INSERT INTO auction_listings ({", ".join(COLS)})
VALUES ({", ".join(["%s"] * len(COLS))})
ON CONFLICT (asset_id) DO UPDATE SET
{_UPDATE_SET}
WHERE EXCLUDED.last_seen_at > auction_listings.last_seen_at
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_transfer_upsert_guard.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/transfer_listings_to_supabase.py tests/test_transfer_upsert_guard.py
git commit -m "fix(transfer): COALESCE quantity + guard last_seen_at so failed LLM runs can't erase counts"
```

### Task 1.2: Preserve quantity in the local SQLite update

**Files:**
- Modify: `auction_extractors/listings_db.py:200-244` (the `UPDATE listings` call)
- Test: `tests/test_listings_db_quantity_preserve.py` (create)

**Interfaces:**
- Consumes: existing `listings_db` module functions (`ensure_db`, `upsert_listing` — confirm exact names when implementing).
- Produces: a fresh listing dict with `quantity=None` must not overwrite an existing non-null quantity.

**Why:** `listing.get("quantity", existing["quantity"])` (line 228) returns `None` because the key is always present set to `None`. Same NULL-clobber as 1.1, one layer up.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_listings_db_quantity_preserve.py
"""A re-scrape whose LLM failed (quantity=None) must keep the stored quantity.
Uses a temp SQLite file via LISTINGS_DB_PATH. Run:
  .venv/bin/python -m pytest tests/test_listings_db_quantity_preserve.py -v
"""
import importlib
from pathlib import Path


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LISTINGS_DB_PATH", str(tmp_path / "listings.db"))
    import auction_extractors.listings_db as ldb
    importlib.reload(ldb)
    ldb.ensure_db()
    return ldb


def _base_listing(**over):
    row = {
        "asset_id": "ps:123", "link": "https://publicsurplus.com/x",
        "title": "200 Chairs", "description": "", "quantity": 200,
        "quantity_source": "llm", "quantity_confidence": "high",
        "price": "$1", "location": "Tulsa, OK", "lot_number": "1",
        "end_date": "", "time_left": "", "image_url": "",
        "pickup_zip": "", "contact_email": "", "contact_phone": "",
    }
    row.update(over)
    return row


def test_null_quantity_does_not_clobber_stored_count(tmp_path, monkeypatch):
    ldb = _fresh_db(tmp_path, monkeypatch)
    ldb.upsert_listing(_base_listing())                       # store 200
    ldb.upsert_listing(_base_listing(quantity=None,
                                     quantity_source="llm_failed"))  # failed re-scrape
    row = ldb.get_listing("ps:123")
    assert row["quantity"] == 200          # preserved, not None
```

(Confirm the real function names `upsert_listing` / `get_listing` / `ensure_db` while implementing; adjust the test to match. If `get_listing` doesn't exist, read the row with a direct `sqlite3` query on `LISTINGS_DB_PATH`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_listings_db_quantity_preserve.py -v`
Expected: FAIL — `row["quantity"]` is `None`.

- [ ] **Step 3: Implement**

In `auction_extractors/listings_db.py`, just above the `conn.execute("UPDATE listings ...")` block (~line 200), add:

```python
    # A failed-LLM re-scrape sends quantity=None; keep the last good count
    # (and its source/confidence) rather than nulling a verified value.
    incoming_qty = listing.get("quantity")
    if incoming_qty is None:
        final_quantity = existing["quantity"]
        final_quantity_source = existing["quantity_source"]
        final_quantity_confidence = existing["quantity_confidence"]
    else:
        final_quantity = incoming_qty
        final_quantity_source = listing.get("quantity_source", existing["quantity_source"])
        final_quantity_confidence = listing.get("quantity_confidence", existing["quantity_confidence"])
```

Then replace the three bound values at lines 228-230:

```python
            final_quantity,
            final_quantity_source,
            final_quantity_confidence,
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_listings_db_quantity_preserve.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auction_extractors/listings_db.py tests/test_listings_db_quantity_preserve.py
git commit -m "fix(listings_db): keep stored quantity when a re-scrape's LLM count is null"
```

### Task 1.3: Surface NULL-quantity rows on the Auctions tab instead of hiding them

**Files:**
- Modify: `automation/auctions_supabase.py:43-64` (`_load_from_supabase`), `:135-157` (output dict)
- Test: `tests/test_auctions_supabase_null_qty.py` (create)

**Interfaces:**
- Produces: `get_top_chairs(...)` returns rows whose stored quantity is NULL, each carrying `"quantity": 0` and a new `"quantity_unknown": True` flag; rows with a real quantity keep `"quantity_unknown": False`. Existing keys unchanged.

**Why:** `WHERE quantity >= %s` drops NULLs (`NULL >= 50` is false), so a total LLM outage looks identical to "no chairs." Show them as `qty unknown` degraded cards instead.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auctions_supabase_null_qty.py
"""NULL-quantity rows must surface as degraded 'qty unknown' cards, not vanish.
db.fetch_all is monkeypatched — no DB, no LLM. Run:
  .venv/bin/python -m pytest tests/test_auctions_supabase_null_qty.py -v
"""
from automation import auctions_supabase as A


def test_null_quantity_row_is_surfaced_as_unknown(monkeypatch):
    fake_rows = [
        {"asset_id": "ps:1", "link": "https://publicsurplus.com/a", "title": "Stack of banquet chairs",
         "description": "", "quantity": None, "quantity_source": "llm_failed",
         "quantity_confidence": None, "price": "$5", "location": "Tulsa, OK",
         "pickup_zip": "", "contact_email": "", "contact_phone": "",
         "end_date": "", "time_left": "", "image_url": "", "last_seen_at": None},
    ]
    monkeypatch.setattr(A.db, "fetch_all", lambda *a, **k: [dict(r) for r in fake_rows])
    out = A.get_top_chairs(source="ps", include_condition=False, active_only=False)
    assert len(out) == 1
    assert out[0]["quantity_unknown"] is True
    assert out[0]["quantity"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_auctions_supabase_null_qty.py -v`
Expected: FAIL — current SQL/filter drops the row (empty list) and no `quantity_unknown` key.

- [ ] **Step 3: Implement**

In `_load_from_supabase` (`automation/auctions_supabase.py:46-54`), change the WHERE so NULL survives and only over-large real quantities are capped:

```python
    rows = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE (quantity IS NULL OR (quantity >= %s AND quantity <= %s))
          AND link ILIKE %s
        ORDER BY quantity DESC NULLS LAST
        """,
        (min_quantity, _SANE_MAX_QUANTITY, f"%{frag}%"),
    )
```

In the output loop (`:136-157`), add the flag and coerce NULL to 0:

```python
        qty_raw = it.get("quantity")
        out.append(
            {
                "rank": i,
                "quantity": int(qty_raw) if qty_raw is not None else 0,
                "quantity_unknown": qty_raw is None,
                "title": en["title"],
                # ... rest unchanged ...
```

Also update the in-Python sort key at `:62` so NULL sorts last without raising:

```python
    rows.sort(
        key=lambda x: (-(int(x["quantity"]) if x.get("quantity") is not None else -1),
                       _price_to_float(x.get("price")))
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_auctions_supabase_null_qty.py -v`
Expected: PASS.

- [ ] **Step 5: Render the degraded state (JS)**

In `automation/web/static/app.js` where auction cards render the quantity (search for the `it.quantity` interpolation in the auctions-tab card builder — near line 952/987), show `qty unknown` when `it.quantity_unknown`:

```javascript
const qtyLabel = it.quantity_unknown ? 'QTY UNKNOWN' : `${it.quantity} chairs`;
```

Use `qtyLabel` in the card markup where the raw quantity was printed. Add a muted CSS class if one is handy; otherwise plain text is fine.

- [ ] **Step 6: Commit**

```bash
git add automation/auctions_supabase.py automation/web/static/app.js tests/test_auctions_supabase_null_qty.py
git commit -m "fix(auctions): surface null-quantity lots as 'qty unknown' instead of hiding them"
```

### Task 1.4: Reorder discovery so Public Surplus isn't starved of LLM quota

**Files:**
- Modify: `scripts/run_discovery.sh:11-25`

**Why:** GovDeals runs first and burns the shared Groq quota, so PS (second) gets 429'd on every chunk → 100% NULL. Running PS first, and relying on the now-graceful degraded-visibility from 1.3 for whichever source runs second, stops the total blackout.

- [ ] **Step 1: Swap the order**

Edit `scripts/run_discovery.sh` so the Public Surplus block runs before the GovDeals block. Keep the non-fatal `|| echo ...` guard on the PS run and the comment. Result order:

```sh
echo "[discovery] Public Surplus scrape -> staging DB"
PUBLICSURPLUS_USE_API=1 PUBLICSURPLUS_ALLOW_BROWSER=0 \
  python auction_extractors/public_surplus_automation.py \
  || echo "[discovery] Public Surplus scrape FAILED — continuing with GovDeals rows only"

echo "[discovery] GovDeals scrape -> staging DB"
python auction_extractors/govdeals_chairs_extraction.py

echo "[discovery] transfer staged listings -> Supabase"
python scripts/transfer_listings_to_supabase.py
```

Update the stale comment block to reflect "PS now runs first to avoid quota starvation."

- [ ] **Step 2: Sanity-check the script parses**

Run: `sh -n scripts/run_discovery.sh && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/run_discovery.sh
git commit -m "fix(discovery): run Public Surplus before GovDeals so it isn't last for shared LLM quota"
```

- [ ] **Step 4: Operator config note (no code)**

Record in the final report for the operator to set in the Render `black-whole-secrets` group / `black-whole-discovery` env: **`GEMINI_API_KEY`** (so `quantity_llm._provider_chain` has a real fallback when Groq 429s) and confirm **`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`** are present (so `_alert_on_quantity_degradation` actually sends). These are dashboard settings, not commits.

---

## Ticket 2 — Remove the budget question

### Task 2.1: Delete the budget field and its Telegram line

**Files:**
- Modify: `automation/web/templates/_subscribe_form.html:63-83`
- Modify: `automation/web/app.py:928` (drop `budget_per_chair` from the ping)
- Test: `tests/test_subscribe_form_no_budget.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: the subscribe form no longer emits `name="budget_per_chair"`. `inventory.create_subscriber` keeps accepting the kwarg (column stays for history); the `/subscribe` handler keeps passing it through harmlessly (payload just won't contain it). No DB change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subscribe_form_no_budget.py
"""The alert-signup form must not render a budget question. Static file check.
Run: .venv/bin/python -m pytest tests/test_subscribe_form_no_budget.py -v
"""
from pathlib import Path

FORM = Path("automation/web/templates/_subscribe_form.html").read_text()


def test_no_budget_field_in_subscribe_form():
    assert "budget_per_chair" not in FORM
    assert "BUDGET" not in FORM.upper()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_subscribe_form_no_budget.py -v`
Expected: FAIL — `budget_per_chair` present.

- [ ] **Step 3: Implement**

In `_subscribe_form.html`, replace the two-column row at lines 63-83 (timeline + budget) with a single-column timeline row:

```html
      <div class="mf-row">
        <label>
          <span>WHEN DO YOU NEED THEM?</span>
          <select name="timeline">
            <option value="">—</option>
            <option value="asap">ASAP</option>
            <option value="month">Within a month</option>
            <option value="flexible">Flexible / watching</option>
          </select>
        </label>
      </div>
```

In `automation/web/app.py:928`, remove `row.get("budget_per_chair"),` from the `prefs` tuple in `_notify_new_subscriber` (leave the rest of the tuple intact).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_subscribe_form_no_budget.py tests/test_subscribers.py -v`
Expected: PASS. (If `tests/test_subscribers.py:103` asserts budget round-trips through a direct `create_subscriber` call, that still passes — the column and kwarg remain. Only fix that test if it drove the value through the *form*, which it does not.)

- [ ] **Step 5: Commit**

```bash
git add automation/web/templates/_subscribe_form.html automation/web/app.py tests/test_subscribe_form_no_budget.py
git commit -m "feat(subscribe): remove unused budget question from the alert-signup form"
```

---

## Ticket 3 — Sold-out lots as social proof

### Task 3.1: Stage the schema migration (sold_at + lost_sold_out)

**Files:**
- Create: `ops/pending-db-updates-sold-social-proof.sql`

**Why:** Public site needs a `sold_at` timestamp to bound "recently" and a new `lost_sold_out` status value. Staged + guarded, applied by the operator after review — matches the `ops/pending-db-updates-*` convention.

- [ ] **Step 1: Write the staged SQL**

```sql
-- Sold-out social proof (2026-07-21).
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.
--
-- Adds `sold_at` (bounds the public "recently sold" window) and a new
-- `lost_sold_out` status: lots we bid on and LOST. Publicly they read
-- identically to `sold_out`; the separate value keeps the truth on the
-- back end. `lost` (unshown) is untouched.

BEGIN;

ALTER TABLE inventory ADD COLUMN IF NOT EXISTS sold_at timestamptz;

-- Extend the status CHECK to allow lost_sold_out. The constraint name may
-- differ — inspect with:
--   SELECT conname FROM pg_constraint WHERE conrelid = 'inventory'::regclass AND contype = 'c';
-- then drop/recreate with the full allowed set:
-- ALTER TABLE inventory DROP CONSTRAINT <status_check_name>;
-- ALTER TABLE inventory ADD CONSTRAINT <status_check_name>
--   CHECK (status IN ('draft','listed','hidden','sold_out','owned',
--                     'won_pickup','active_bid','lost','lost_sold_out'));

COMMIT;
-- ROLLBACK;
```

- [ ] **Step 2: Commit**

```bash
git add ops/pending-db-updates-sold-social-proof.sql
git commit -m "ops: stage sold_at column + lost_sold_out status migration"
```

### Task 3.2: Teach `inventory.py` about sold lots

**Files:**
- Modify: `automation/inventory.py:36-40` (constants), `:75-95` (`list_public`), `:98-117` (`stats`), `:346` (status validation)
- Test: `tests/test_inventory_sold.py` (create)

**Interfaces:**
- Produces:
  - `SOLD_PUBLIC_STATUSES = ("sold_out", "lost_sold_out")`, `SOLD_PUBLIC_LABEL = "SOLD OUT"`, `SOLD_RECENT_LIMIT = 8`, `SOLD_RECENT_DAYS = 90`.
  - `list_public(include_sold: bool = False) -> list[dict]` — every row carries `is_sold: bool`. When `include_sold=True`, up to `SOLD_RECENT_LIMIT` sold rows (status in `SOLD_PUBLIC_STATUSES`, `sold_at >= now()-90d`, ordered `sold_at DESC`) are appended after the available rows.
  - `ALL_STATUSES` includes `"lost_sold_out"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inventory_sold.py
"""list_public(include_sold=True) appends recently-sold rows flagged is_sold,
after available rows. connect() is stubbed — no DB. Run:
  .venv/bin/python -m pytest tests/test_inventory_sold.py -v
"""
import contextlib
from automation import inventory


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0] if self._rows else None


class _FakeConn:
    """Returns available rows for the first query, sold rows for the second."""
    def __init__(self, available, sold):
        self._queues = [available, sold]
    def execute(self, sql, params=None):
        # crude router: the sold query filters on SOLD_PUBLIC_STATUSES / sold_at
        rows = self._queues.pop(0) if self._queues else []
        return _FakeCursor(rows)
    def commit(self): pass


def _stub(monkeypatch, available, sold):
    @contextlib.contextmanager
    def fake_connect():
        yield _FakeConn(available, sold)
    monkeypatch.setattr(inventory, "connect", fake_connect)


def test_lost_sold_out_is_a_valid_status():
    assert "lost_sold_out" in inventory.ALL_STATUSES


def test_include_sold_appends_flagged_rows(monkeypatch):
    available = [{"lot_id": "A", "status": "listed", "quantity_remaining": 5}]
    sold = [{"lot_id": "B", "status": "lost_sold_out", "quantity_remaining": 700}]
    _stub(monkeypatch, available, sold)
    rows = inventory.list_public(include_sold=True)
    assert [r["lot_id"] for r in rows] == ["A", "B"]      # available first
    assert rows[0]["is_sold"] is False
    assert rows[1]["is_sold"] is True


def test_default_excludes_sold(monkeypatch):
    available = [{"lot_id": "A", "status": "listed", "quantity_remaining": 5}]
    _stub(monkeypatch, available, [])
    rows = inventory.list_public()
    assert all(r["is_sold"] is False for r in rows)
    assert [r["lot_id"] for r in rows] == ["A"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_inventory_sold.py -v`
Expected: FAIL — `list_public()` takes no `include_sold`, no `is_sold` key, `lost_sold_out` not in `ALL_STATUSES`.

- [ ] **Step 3: Implement**

Add to the constants block (`automation/inventory.py:36-40`):

```python
PUBLIC_STATUSES = ("listed", "draft", "owned", "won_pickup")
SOLD_PUBLIC_STATUSES = ("sold_out", "lost_sold_out")
SOLD_PUBLIC_LABEL = "SOLD OUT"        # both sold_out and lost_sold_out show this
SOLD_RECENT_LIMIT = 8
SOLD_RECENT_DAYS = 90
ALL_STATUSES = (
    "draft", "listed", "hidden", "sold_out",
    "owned", "won_pickup", "active_bid", "lost", "lost_sold_out",
)
```

Replace `list_public` (`:75-95`):

```python
def list_public(include_sold: bool = False) -> list[dict]:
    """Rows customers see on /listings. Available lots first (flagged
    is_sold=False). When include_sold=True, up to SOLD_RECENT_LIMIT recently
    sold/lost lots (status in SOLD_PUBLIC_STATUSES, sold within
    SOLD_RECENT_DAYS) are appended, flagged is_sold=True — social proof.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM inventory
            WHERE status = ANY(%s)
              AND (quantity_remaining IS NULL OR quantity_remaining > 0)
            ORDER BY
              CASE status WHEN 'listed' THEN 0 ELSE 1 END,
              COALESCE(quantity_remaining, 0) DESC,
              updated_at DESC
            """,
            (list(PUBLIC_STATUSES),),
        ).fetchall()
        available = [dict(r) for r in rows]
        for r in available:
            r["is_sold"] = False

        sold: list[dict] = []
        if include_sold:
            srows = conn.execute(
                """
                SELECT * FROM inventory
                WHERE status = ANY(%s)
                  AND sold_at IS NOT NULL
                  AND sold_at >= now() - make_interval(days => %s)
                ORDER BY sold_at DESC
                LIMIT %s
                """,
                (list(SOLD_PUBLIC_STATUSES), SOLD_RECENT_DAYS, SOLD_RECENT_LIMIT),
            ).fetchall()
            sold = [dict(r) for r in srows]
            for r in sold:
                r["is_sold"] = True
    return available + sold
```

Fix `stats` (`:107-116`) so `chairs` and `cities` apply the same qty filter as `lots` (a qty=0 public-status row must not inflate counts):

```python
        chairs = conn.execute(
            "SELECT COALESCE(SUM(quantity_remaining), 0) AS n FROM inventory "
            "WHERE status = ANY(%s) "
            "AND (quantity_remaining IS NULL OR quantity_remaining > 0)",
            (statuses,),
        ).fetchone()["n"]
        cities = conn.execute(
            "SELECT COUNT(DISTINCT city) AS n FROM inventory "
            "WHERE city IS NOT NULL AND city != '' AND status = ANY(%s) "
            "AND (quantity_remaining IS NULL OR quantity_remaining > 0)",
            (statuses,),
        ).fetchone()["n"]
```

(`set_fields`'s validation at `:346` already accepts anything in `ALL_STATUSES`, so `lost_sold_out` becomes settable automatically. The auto-sold-out rule at `:338-345` still flips to `sold_out` — that's correct for genuinely-sold; `lost_sold_out` is set explicitly.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_inventory_sold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/inventory.py tests/test_inventory_sold.py
git commit -m "feat(inventory): list_public(include_sold) + lost_sold_out status + stats qty fix"
```

### Task 3.3: Wire sold lots into the public routes + sold detail view

**Files:**
- Modify: `automation/web/app.py:404-408` (JSON-LD availability), `:467-500` (`/` + `/listings`), `:503-518` (detail route)
- Test: `tests/test_sold_detail.py` (create)

**Interfaces:**
- Consumes: `inventory.list_public(include_sold=True)`, `inventory.SOLD_PUBLIC_STATUSES`, `inventory.SOLD_PUBLIC_LABEL`.
- Produces: sold lots render on `/` and `/listings` with `is_sold=True` + `hero_src`. A sold lot's `/listings/{id}` returns 200 with `schema.org/SoldOut` and a template flag `is_sold=True`. Sitemap + catalog feed stay `include_sold=False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sold_detail.py
"""A sold lot's detail page returns 200, emits SoldOut, and flags is_sold.
inventory is monkeypatched (pattern from tests/test_seo.py). Run:
  .venv/bin/python -m pytest tests/test_sold_detail.py -v
"""
import importlib
from fastapi.testclient import TestClient

web_app = importlib.import_module("automation.web.app")

SOLD_ROW = {
    "lot_id": "5003", "title": "Burgundy Vinyl Banquet Chairs",
    "description": "Lost at auction.", "city": "Fresno", "state": "CA",
    "zip_code": "93650", "chair_type": "banquet", "quantity_remaining": 700,
    "quantity_original": 700, "price_per_chair": 12.0, "status": "lost_sold_out",
    "folder_name": None, "hero_image": None, "hero_image_url": None,
    "image_urls": None, "facebook_url": None, "ebay_url": None,
}


def test_sold_lot_detail_is_soldout(monkeypatch):
    monkeypatch.setattr(web_app.inventory, "get", lambda lid: dict(SOLD_ROW))
    client = TestClient(web_app.app)
    resp = client.get("/listings/5003")
    assert resp.status_code == 200
    assert "schema.org/SoldOut" in resp.text
    assert "InStock" not in resp.text
    assert "SOLD OUT" in resp.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sold_detail.py -v`
Expected: FAIL — page emits `InStock` (availability keys off quantity>0) and no "SOLD OUT".

- [ ] **Step 3: Implement**

JSON-LD availability (`app.py:404-408`) — key off status, not just quantity:

```python
        "availability": (
            "https://schema.org/SoldOut"
            if row.get("status") in inventory.SOLD_PUBLIC_STATUSES
            or (row.get("quantity_remaining") or 0) <= 0
            else "https://schema.org/InStock"
        ),
```

`/` route (`:477-479`): change to `include_sold=True` (available lots lead via `_idaho_first`, sold appended by the query order — but `sorted(..., key=_idaho_first)` would interleave; instead sort only the available head). Simplest correct form:

```python
        public = inventory.list_public(include_sold=True)
        avail = [r for r in public if not r.get("is_sold")]
        sold = [r for r in public if r.get("is_sold")]
        featured = (sorted(avail, key=_idaho_first) + sold)[:12]
        for r in featured:
            r["hero_src"] = _hero_src(r)
```

`/listings` route (`:491`): `items = inventory.list_public(include_sold=True)` (query already returns available-then-sold; keep that order — do not re-sort).

Detail route (`:503-518`): allow sold lots (they already don't 404 — only `hidden` does), and pass an `is_sold` flag:

```python
    is_sold = row.get("status") in inventory.SOLD_PUBLIC_STATUSES
    return templates.TemplateResponse(
        request, "listing_detail.html",
        _public_ctx({
            "item": row,
            "hero": hero,
            "images": images,
            "is_sold": is_sold,
            "sold_label": inventory.SOLD_PUBLIC_LABEL,
            **_detail_seo(row, hero, images),
        }),
    )
```

Sitemap (`:868`) and catalog feed already call `list_public()` / `list_catalog_feed()` with no `include_sold` → default `False`. Leave them.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_sold_detail.py tests/test_seo.py -v`
Expected: PASS (test_seo still green — available lots still emit InStock).

- [ ] **Step 5: Commit**

```bash
git add automation/web/app.py tests/test_sold_detail.py
git commit -m "feat(web): render recently-sold lots on public pages + SoldOut detail view"
```

### Task 3.4: Templates — stamp + card branch on sold, subscribe block on sold detail

**Files:**
- Modify: `automation/web/templates/listings.html:50-87`, `landing.html:74-103`, `listing_detail.html:23-105`
- Test: covered by `tests/test_sold_detail.py` (extend with a grid render if practical) + manual.

- [ ] **Step 1: listings.html card** — add sold modifier + status-first stamp. Replace the `<a class="lot-card" ...>` open tag and the `.lot-stamp` line:

```html
  <a class="lot-card{% if item.is_sold %} lot-card--sold{% endif %}"
     href="/listings/{{ item.lot_id }}"
     data-type="{{ item.chair_type or '' }}"
     data-city="{{ item.city or '' }}"
     data-qty="{{ item.quantity_remaining or item.quantity_original or 0 }}"
     data-sold="{{ '1' if item.is_sold else '0' }}"
     data-search="{{ (item.title or '') }} {{ item.lot_id }} {{ item.chair_type or '' }} {{ item.city or '' }}">
```

```html
      <div class="lot-stamp{% if item.is_sold %} lot-stamp--sold{% endif %}">
        {% if item.is_sold %}SOLD OUT{% elif item.quantity_remaining and item.quantity_remaining > 0 %}AVAILABLE{% else %}INQUIRE{% endif %}
      </div>
```

- [ ] **Step 2: landing.html card** — same two edits at `:74` (card open) and `:81` (stamp). Featured cards from `/` now include sold rows flagged `is_sold`.

- [ ] **Step 3: listing_detail.html** — sold stamp + swap the quote form for the subscribe block when `is_sold`.

Big stamp (`:28-30`):

```html
        <div class="lot-stamp lot-stamp--big{% if is_sold %} lot-stamp--sold{% endif %}">
          {% if is_sold %}SOLD OUT{% elif item.quantity_remaining and item.quantity_remaining > 0 %}AVAILABLE{% else %}INQUIRE{% endif %}
        </div>
```

Replace the CTA block (`:68-72`) and the whole `#contact` quote section (`:76-105`) with a conditional:

```html
    {% if is_sold %}
    <div class="detail-ctas">
      <div class="sold-note mono">◉ THIS LOT HAS SOLD.</div>
    </div>
    {% else %}
    <div class="detail-ctas">
      <a href="#contact-form" class="btn btn-primary">I WANT THIS LOT →</a>
      {% if item.facebook_url %}<a class="btn btn-ghost" target="_blank" rel="noopener" href="{{ item.facebook_url }}">VIEW ON FACEBOOK</a>{% endif %}
      {% if item.ebay_url %}<a class="btn btn-ghost" target="_blank" rel="noopener" href="{{ item.ebay_url }}">VIEW ON EBAY</a>{% endif %}
    </div>
    {% endif %}
  </div>
</section>

{% if is_sold %}
  {% set subscribe_source = 'site_detail' %}
  {% include "_subscribe_form.html" %}
{% else %}
<section class="contact contact--slim" id="contact">
  ... (existing REQUEST A QUOTE section unchanged) ...
</section>
{% endif %}
```

Keep the existing quote `<section>` markup verbatim inside the `{% else %}`. This makes `source='site_detail'` reachable (previously dead).

- [ ] **Step 4: Verify render**

Run: `.venv/bin/python -m pytest tests/test_sold_detail.py -v`
Expected: PASS (asserts "SOLD OUT" + SoldOut in the rendered detail HTML).

- [ ] **Step 5: Commit**

```bash
git add automation/web/templates/listings.html automation/web/templates/landing.html automation/web/templates/listing_detail.html
git commit -m "feat(templates): SOLD OUT stamp + dimmed cards + subscribe block on sold detail"
```

### Task 3.5: Styles — sold stamp + dimmed card

**Files:**
- Modify: `automation/web/static/public.css` (after `.lot-stamp--big`, ~line 330)

- [ ] **Step 1: Add styles**

```css
/* Recently-sold social proof: red stamp, dimmed photo, struck price. */
.lot-stamp--sold {
  background: var(--accent);
  color: var(--paper);
  border-color: var(--accent);
}
.lot-card--sold .lot-img img { filter: grayscale(0.4); opacity: 0.6; }
.lot-card--sold .lot-price-num { text-decoration: line-through; opacity: 0.7; }
.sold-note {
  font-size: 12px; letter-spacing: 0.16em; color: var(--accent);
  border: 1.5px solid var(--accent); padding: 10px 14px; display: inline-block;
}
```

(Confirm `--accent` renders red in `:root` at the top of `public.css`; it is the site accent used by `.lot-price-num` and `.lot-stamp` border. If it's not red enough for a "sold" signal, add `--sold: #c02626;` to `:root` and use it here.)

- [ ] **Step 2: Commit**

```bash
git add automation/web/static/public.css
git commit -m "style(public): sold-out stamp + dimmed card treatment"
```

### Task 3.6: Stage the data updates for the real lots

**Files:**
- Create: `ops/pending-db-updates-sold-data.sql`

**Why:** The three named lots aren't marked sold in the DB (Fresno=`lost`, Tulsa/NC=`listed`), and lot 7126 is `sold_out` with 360 remaining (renders "AVAILABLE"). Staged + guarded; operator applies after confirming.

- [ ] **Step 1: Write the staged SQL**

```sql
-- Sold-out social-proof data (2026-07-21).
-- STATUS: STAGED — NOT APPLIED. Requires ops/pending-db-updates-sold-social-proof.sql first.
-- Review each row before running against Supabase `blackwhole`.
--
-- Operator: set a real sold_at per lot (roughly when the auction closed).
-- Placeholder now() used below — REPLACE with the true close dates.

BEGIN;

-- Lost at auction — we never owned these. lost_sold_out = shows SOLD OUT
-- publicly, but flagged internally as a loss.
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '5003';    -- Fresno CA, Burgundy Vinyl, 700
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '28505';   -- Fort Sill OK, Saffron Stacking, 250
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '334';     -- Wilmington NC, Tan Banquet, 500

-- Genuinely sold — keep sold_out, just stamp a sold_at so it enters the
-- 90-day "recently sold" window and stops rendering "AVAILABLE".
UPDATE inventory SET sold_at = now(), updated_at = now()
  WHERE lot_id = '7126';    -- North Miami FL, Red & Gold, 360 (qty/status mismatch)

COMMIT;
-- ROLLBACK;
```

- [ ] **Step 2: Commit**

```bash
git add ops/pending-db-updates-sold-data.sql
git commit -m "ops: stage lost_sold_out/sold_at data updates for Fresno/Fort Sill/Wilmington/North Miami"
```

---

## Ticket 4 — Reorganize listing photo folders by status

### Task 4.1: Make image serving resilient to bucketed folders

**Files:**
- Modify: `automation/web/app.py:440-464` (`_hero_src`/`_gallery_srcs`), `:1135-1149` (`_list_listing_folders`), `:1223-1234` (`serve_image`), `:1235-1243` (`serve_screenshot`)
- Test: `tests/web/test_folder_resolver.py` (create)

**Interfaces:**
- Produces: `_resolve_folder_dir(folder_name: str) -> Path | None` — returns `DOWNLOAD_ROOT/folder_name` if it exists (legacy flat), else the first `DOWNLOAD_ROOT/<bucket>/folder_name` that exists, else None. `_list_listing_folders()` descends into `_active`/`_sold`/`_lost`/`_archive` buckets and skips `_docs`.

**Why:** After reorg, a lot folder physically lives at `DOWNLOAD_ROOT/_lost/<name>`, but `folder_name` stays the flat basename (URL + Supabase key contract). The `/image/` route must find it either place.

- [ ] **Step 1: Write the failing test**

```python
# tests/web/test_folder_resolver.py
"""_resolve_folder_dir finds a lot folder whether it's flat or bucketed.
Run: .venv/bin/python -m pytest tests/web/test_folder_resolver.py -v
"""
import importlib

web_app = importlib.import_module("automation.web.app")


def test_resolves_flat_then_bucketed(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DOWNLOAD_ROOT", tmp_path)
    (tmp_path / "FlatLot").mkdir()
    (tmp_path / "_lost").mkdir()
    (tmp_path / "_lost" / "LostLot").mkdir()

    assert web_app._resolve_folder_dir("FlatLot") == tmp_path / "FlatLot"
    assert web_app._resolve_folder_dir("LostLot") == tmp_path / "_lost" / "LostLot"
    assert web_app._resolve_folder_dir("NopeLot") is None


def test_list_listing_folders_descends_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DOWNLOAD_ROOT", tmp_path)
    (tmp_path / "_active").mkdir(); (tmp_path / "_active" / "A").mkdir()
    (tmp_path / "_lost").mkdir(); (tmp_path / "_lost" / "B").mkdir()
    (tmp_path / "_docs").mkdir(); (tmp_path / "_docs" / "readme").mkdir()
    (tmp_path / "Flat").mkdir()
    names = {p.name for p in web_app._list_listing_folders()}
    assert names == {"A", "B", "Flat"}      # _docs contents excluded
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_folder_resolver.py -v`
Expected: FAIL — `_resolve_folder_dir` doesn't exist.

- [ ] **Step 3: Implement**

Add near `_hero_src` (`app.py:~440`):

```python
_FOLDER_BUCKETS = ("_active", "_sold", "_lost", "_archive")


def _resolve_folder_dir(folder_name: str):
    """Physical dir for a lot folder — flat (legacy) or inside a status bucket.

    folder_name stays a flat basename (URL + Supabase key contract); the file
    on disk may have moved under _active/_sold/_lost/_archive (Ticket 4).
    """
    if not folder_name:
        return None
    flat = DOWNLOAD_ROOT / folder_name
    if flat.is_dir():
        return flat
    for bucket in _FOLDER_BUCKETS:
        cand = DOWNLOAD_ROOT / bucket / folder_name
        if cand.is_dir():
            return cand
    return None
```

Update `_gallery_srcs` (`:461-463`) to resolve the dir for the disk-scan branch:

```python
    folder = row.get("folder_name")
    if folder:
        fdir = _resolve_folder_dir(folder)
        if fdir:
            return [f"/image/{folder}/{n}" for n in _folder_images(fdir)]
    return []
```

Update `serve_image` (`:1223-1231`):

```python
@app.get("/image/{folder}/{name}")
async def serve_image(folder: str, name: str):
    folder_path = _resolve_folder_dir(folder)
    if folder_path is None:
        raise HTTPException(404, "not found")
    target = (folder_path / name).resolve()
    if not str(target).startswith(str(folder_path.resolve())):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))
```

Update `serve_screenshot` (`:1235`) the same way — resolve the base dir first:

```python
@app.get("/screenshot/{folder}/{name}")
async def serve_screenshot(folder: str, name: str):
    fdir = _resolve_folder_dir(folder)
    if fdir is None:
        raise HTTPException(404, "not found")
    base = (fdir / "_screenshots").resolve()
    target = (base / name).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))
```

Rewrite `_list_listing_folders` (`:1135-1149`) to be bucket-aware:

```python
_SKIP_FOLDERS = {"General listing", "Lost biddings", "Listing Automations",
                 "Listing_automation_html"}


def _list_listing_folders() -> list[Path]:
    if not DOWNLOAD_ROOT.exists():
        return []
    folders: list[Path] = []
    for p in DOWNLOAD_ROOT.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        if p.name == "_docs":
            continue
        if p.name in _FOLDER_BUCKETS:
            for child in p.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    folders.append(child)
            continue
        if p.name in _SKIP_FOLDERS:
            continue
        folders.append(p)          # legacy flat lot
    folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return folders
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/web/test_folder_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/web/app.py tests/web/test_folder_resolver.py
git commit -m "feat(web): resolve listing folders across status buckets (Ticket 4 prep)"
```

### Task 4.2: The reorganize script (dry-run default)

**Files:**
- Create: `scripts/reorganize_listing_folders.py`
- Test: `tests/test_reorganize_folders.py` (create)

**Interfaces:**
- Consumes: `inventory.list_all()` (real lot_id→status→folder_name), `_FOLDER_BUCKETS` semantics.
- Produces: `plan_moves(rows, existing_names) -> tuple[list[Move], list[str]]` — pure function returning `(moves, unmatched)`. `Move = (folder_name, bucket)`. A CLI wrapper prints the plan (dry-run) and, with `--apply`, moves dirs + updates `inventory.folder_path` in one transaction.

**Bucket mapping:**
```
listed, draft, owned, won_pickup   -> _active
sold_out                           -> _sold
lost_sold_out, lost                -> _lost
hidden + zero-qty stubs            -> _archive
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reorganize_folders.py
"""plan_moves buckets folders by status and flags unmatched ones. Pure. Run:
  .venv/bin/python -m pytest tests/test_reorganize_folders.py -v
"""
from scripts.reorganize_listing_folders import plan_moves, bucket_for_status


def test_bucket_mapping():
    assert bucket_for_status("listed") == "_active"
    assert bucket_for_status("sold_out") == "_sold"
    assert bucket_for_status("lost_sold_out") == "_lost"
    assert bucket_for_status("lost") == "_lost"
    assert bucket_for_status("hidden") == "_archive"


def test_plan_moves_buckets_and_flags_unmatched():
    rows = [
        {"folder_name": "Fresno_700", "status": "lost_sold_out"},
        {"folder_name": "FortSill_250", "status": "listed"},
        {"folder_name": None, "status": "listed"},          # no folder → skipped
    ]
    on_disk = ["Fresno_700", "FortSill_250", "General listing"]
    moves, unmatched = plan_moves(rows, on_disk)
    assert ("Fresno_700", "_lost") in moves
    assert ("FortSill_250", "_active") in moves
    assert "General listing" in unmatched      # on disk, no ledger row
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reorganize_folders.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# scripts/reorganize_listing_folders.py
"""Reorganize ~/Desktop/Banquet chiars Pictures/ into status buckets.

Dry-run by default; --apply moves folders AND updates inventory.folder_path in
one transaction. folder_name (the URL/Supabase-key basename) is never changed —
only the physical location and the folder_path column.

    .venv/bin/python scripts/reorganize_listing_folders.py            # dry-run
    .venv/bin/python scripts/reorganize_listing_folders.py --apply
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from automation import inventory                      # noqa: E402
from automation.config import DOWNLOAD_ROOT           # noqa: E402

DOC_FILES = {".md", ".html", ".htm"}
LEGACY_SKIP = {"General listing", "Lost biddings", "Listing Automations",
               "Listing_automation_html"}
BUCKETS = ("_active", "_sold", "_lost", "_archive", "_docs")

_STATUS_BUCKET = {
    "listed": "_active", "draft": "_active", "owned": "_active", "won_pickup": "_active",
    "sold_out": "_sold",
    "lost_sold_out": "_lost", "lost": "_lost",
    "hidden": "_archive", "active_bid": "_archive",
}


def bucket_for_status(status: str | None) -> str:
    return _STATUS_BUCKET.get((status or "").strip(), "_archive")


def plan_moves(rows, on_disk):
    """(moves, unmatched). moves = [(folder_name, bucket)]; unmatched = disk
    folders with no ledger row."""
    by_folder = {r["folder_name"]: r for r in rows if r.get("folder_name")}
    moves = []
    for name, row in by_folder.items():
        if name in on_disk:
            moves.append((name, bucket_for_status(row.get("status"))))
    matched = {m[0] for m in moves}
    unmatched = [d for d in on_disk if d not in matched and d not in BUCKETS]
    return moves, unmatched


def _disk_lot_names(root: Path) -> list[str]:
    """Top-level dirs that look like lots (skip buckets + known non-lots)."""
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith(".") or p.name in BUCKETS:
            continue
        if p.name in LEGACY_SKIP:
            continue
        out.append(p.name)
    return out


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    root = Path(DOWNLOAD_ROOT)
    rows = inventory.list_all()
    on_disk = _disk_lot_names(root)
    moves, unmatched = plan_moves(rows, on_disk)

    print(f"root: {root}")
    print(f"planned moves: {len(moves)}   unmatched (left in place): {len(unmatched)}")
    for name, bucket in sorted(moves, key=lambda m: (m[1], m[0])):
        print(f"  {bucket:9s} <- {name}")
    if unmatched:
        print("UNMATCHED (no ledger row — place by hand):")
        for u in sorted(unmatched):
            print(f"  ? {u}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to move folders + update folder_path.")
        return 0

    for bucket in BUCKETS:
        (root / bucket).mkdir(exist_ok=True)

    with inventory.connect() as conn:
        for name, bucket in moves:
            src = root / name
            dst = root / bucket / name
            if not src.is_dir():
                continue                 # already moved (idempotent)
            if dst.exists():
                print(f"  skip (exists): {dst}")
                continue
            shutil.move(str(src), str(dst))
            conn.execute(
                "UPDATE inventory SET folder_path = %s, updated_at = now() "
                "WHERE folder_name = %s",
                (str(dst), name),
            )
        conn.commit()
    print(f"applied {len(moves)} moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reorganize_folders.py -v`
Expected: PASS.

- [ ] **Step 5: Dry-run against the real folder (no writes)**

Run: `.venv/bin/python scripts/reorganize_listing_folders.py`
Expected: prints the move plan + the unmatched list (`General listing`, `Access_Denied_Unknown_NA`, `Unknown_Unknown_Email_Auction_to_a_Friend_`, loose docs). Changes nothing. **Show this output to the operator before applying.**

- [ ] **Step 6: Commit**

```bash
git add scripts/reorganize_listing_folders.py tests/test_reorganize_folders.py
git commit -m "feat(scripts): reorganize listing folders into status buckets (dry-run default)"
```

---

## Final verification

- [ ] Run the whole suite: `.venv/bin/python -m pytest tests/ -q` — expect green (or pre-existing failures unrelated to this work, noted explicitly).
- [ ] Launch the app (`.venv/bin/python -m automation.web`), open `/listings`: available lots first, then ≤8 SOLD OUT lots (dimmed, red stamp) once Task 3.6's SQL is applied. Confirm a sold lot's detail page shows SoldOut + the subscribe block.
- [ ] Report to the operator the two manual steps that are NOT code: (a) apply the three `ops/pending-db-updates-*.sql` files in order after review; (b) set `GEMINI_API_KEY` and confirm `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in the `black-whole-discovery` Render env group.

## Self-review notes

- **Spec coverage:** T1.1-1.4 cover Ticket 1 (upsert, sqlite, visibility, order, config note). T2.1 covers Ticket 2. T3.1-3.6 cover schema, inventory, routes, templates, styles, data. T4.1-4.2 cover Ticket 4 (resolver + script). Date formatting is explicitly backlogged — no task, by design.
- **Type consistency:** `list_public(include_sold=...)`, `is_sold`, `SOLD_PUBLIC_STATUSES`, `SOLD_PUBLIC_LABEL`, `_resolve_folder_dir`, `plan_moves`/`bucket_for_status` are named identically across the tasks that define and consume them.
- **Guarded destructive steps:** all schema/data changes are staged `ops/` SQL applied by the operator; the folder script is dry-run by default; the transfer upsert is guarded so a failed run can't erase data.

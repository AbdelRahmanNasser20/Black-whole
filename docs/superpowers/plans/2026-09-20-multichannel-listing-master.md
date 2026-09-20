# Multichannel Listing Engine — Master Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Phase 1 is fully detailed here. Phases 2–7 are task lists with interfaces; write a detailed sub-plan (same folder, `2026-09-20-phaseN-*.md`) when a phase starts, after reading the files it names.

**Goal:** One `inventory` row drives every sales channel (site, FB Business catalog, Google Shopping, eBay, FB Marketplace, Craigslist). A change in admin fans out automatically; delist and relist happen without the operator, except FB Marketplace which queues for one-click approval and ships **disabled**.

**Architecture:** `inventory` stays the single source of truth. A new `listing_channels` table holds one row per lot × channel (state, external id, url, payload hash, last error). A sync loop in the web process computes desired state from `inventory` and pushes to channel adapters (`automation/channels/`). Feed channels (site, FB catalog, Google) are pull-based CSV routes and only get state bookkeeping. Push channels (eBay API, Craigslist Playwright, FB Marketplace Playwright) get real adapters behind per-channel switches in `site_settings`. Admin gets a **Channels** tab (switches, queue, matrix) and the rail shrinks to six tabs.

**Tech Stack:** Python 3.12, FastAPI, psycopg pool (`automation/db.py`), Supabase Postgres, Playwright (non-headless, per-identity Chrome profiles), eBay Sell Inventory API (OAuth), Google Merchant Center scheduled CSV fetch, MapLibre (CRM map, reused), pytest (`.venv/bin/python -m pytest tests/web -q`).

**Spec:** the decision log below (grill session 2026-09-20). No separate spec file.

## Decisions (from the grill, 2026-09-20)

| # | Decision | Why |
|---|---|---|
| D1 | `inventory` is the master. Spreadsheet = CSV export/import view, never a second store. | 262 tests + preserved-edit rules already guard it. |
| D2 | Feed/API channels fully automatic (list, delist, relist). Browser channels auto-create but paced, each with a kill switch. **FB Marketplace ships OFF.** Relist on FB Marketplace = approval queue. | Renew/Relist are FB spam triggers; the family account is already shadowbanned. |
| D3 | Admin tab **Leads** = one view over `subscribers` + `inquiries`, add optional city/state/zip to the contact form, map pins from zip using the CRM's MapLibre map. Two tables stay underneath. | No migration risk, same screen. |
| D4 | Six-tab rail: Inventory (+Drafts folded, +channel matrix, +FB queue) · Leads · Source (Launcher+Auctions+Listings DB+Test Scrape) · Radar (Deals+Tracking) · Deposits · **Channels**. Drafts tab dies. | 11 tabs → 6. |
| D5 | "Relist when available again" = a previously listed GovDeals asset gets a **new auction** (detector already in `deals/relist.py`). Restock of our own sold stock stays a manual admin edit. | Operator confirmed A, not B. |
| D6 | eBay = lead magnet. Local Pickup + Freight, per-chair price, qty = remaining, Promoted Listings on. Sell Inventory API (item → offer → publish). Developer account under `listings@black-whole.com`; user token from the existing seller account. | Free exposure until a sale; listing stays honest and sellable. |
| D7 | Google Merchant Center free listings via `/catalog/google.csv` + Product JSON-LD + sitemap. | Feed-based like FB catalog; ranks in the Shopping tab in days. |
| D8 | Craigslist via Playwright on a dedicated `chrome_profile_misc` (Craigslist + Nextdoor share it). By-dealer furniture, $5/post, one post per metro per lot, hours apart. No bulk sheet exists for us. | CL TOS bans automation; low volume is the mitigation. |
| D9 | One Chrome profile per identity. Never two FB accounts in one profile. Mohamed's account only from the Phoenix IP. Pi login for Mohamed not needed until unattended posting. | Fingerprint linking is how bans spread. |
| D10 | Deals: keep crons (they feed favorites/tracking), freeze new public `/deals` work, add a **Promote to inventory** button on favorites. | Three keys, no bridge, was the confusion. |
| D11 | Bitwarden (official MCP + CLI) for shared credentials; one "Automation" collection for bot accounts only. | Sharing with Mohamed later; Bitwarden MCP exists. |
| D12 | Partner Item API is not attainable for a one-person reseller. Ignore. | Approved Meta partners only. |

## Operator gates (you, not the agent)

- [ ] Create Bitwarden account + 2FA; tell the agent the email. Agent installs `bw` CLI + MCP.
- [ ] Google Merchant Center: create account, verify+claim black-whole.com, business info, shipping/return pages, add scheduled fetch of `https://black-whole.com/catalog/google.csv`.
- [ ] eBay: developer account (`listings@black-whole.com`), production keyset, OAuth consent from the seller account, opt in to Business Policies, create one fulfillment policy (Local Pickup + Freight), one payment, one return policy. Paste `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REFRESH_TOKEN`, `EBAY_FULFILLMENT_POLICY_ID`, `EBAY_PAYMENT_POLICY_ID`, `EBAY_RETURN_POLICY_ID` into `.env` and Render.
- [ ] Craigslist: log into `~/.listing_automation/chrome_profile_misc` once (agent opens the window); card on file for $5 by-dealer posts.
- [ ] Apply `scripts/sql/011_listing_channels.sql` and `012_inquiries_geo.sql` in Supabase SQL Editor (agent prints them).
- [ ] Say "enable fb_marketplace" when the heat dies down. Until then the switch stays off.

## Parked (written down so it is not lost)

- Rehome account emails to `listings@black-whole.com` (Craigslist, Nextdoor, Merchant Center, eBay dev). Not now.
- Nextdoor For Sale adapter (shares `chrome_profile_misc`). After Craigslist works.
- OfferUp: operator does by hand.
- Mohamed's account on the Pi: fresh manual login on the Pi, never copy the profile folder.
- FB groups discovery (churches, event planners) on Mohamed's account: re-run when his Chrome profile has the extension.
- 1,200 vs 1,300 Atlanta count: ledger says 1,200 (`31225-atl`). 09-03 notes said 1,300. Operator to confirm.
- `lbs_per_chair` still 13 lb placeholder (LST-LTL).

## Global Constraints

- All DB access through `automation/db.py` (pooled). No `psycopg.connect` per call. Route handlers touching the DB are plain `def` or `asyncio.to_thread`. `tests/web/test_event_loop_hygiene.py` enforces it.
- Schema changes = SQL file under `scripts/sql/` + Supabase SQL Editor + DDL note in `docs/claude-reference/data-model.md`. Never created at runtime.
- `inventory.upsert_from_run()` must keep preserving `quantity_remaining`, `status`, `price_per_chair`, `hero_image`, `*_url`.
- `CATALOG_FEED_STATUSES` gating stays: an archived lot is never offered as stock on any channel.
- Every photo that goes to a channel has been through `automation.dewatermark` first; only R2 URLs, never Supabase Storage.
- Playwright always non-headless. One Chrome profile per identity. `MAX_SENDS_PER_DAY` / `MIN_SECONDS_BETWEEN_SENDS` analogues apply to every browser channel.
- `inventory.storage_note` never leaves the backend.
- Public routes are not under `/api/` (auth wall). New feeds live under `/catalog/`.
- Read-only admin JSON handlers get `@readcache.cached()`; every write drops the memo.
- Never invent a number on a listing (price, quantity, freight). Missing → skip the row, log why.
- Tests: `.venv/bin/python -m pytest tests/web -q` green before every commit. Relaunch `python -m automation.web` after touching `app.py`, templates, static.

## Phase order

| Phase | Deliverable | Needs from operator |
|---|---|---|
| 0 | Verify Atlanta split on site; Bitwarden CLI+MCP | Bitwarden account |
| 1 | `listing_channels` + sync loop + Channels tab + FB approval queue (disabled) | migration 011 |
| 2 | Google feed + JSON-LD + sitemap | Merchant Center clicks |
| 3 | eBay Sell API adapter, replaces Playwright | eBay keys |
| 4 | Craigslist adapter, post owned lots | CL login + card |
| 5 | Relist detector → inventory + queue | — |
| 6 | Admin rail: Leads + map, Source, Radar, Drafts folded | migration 012 |
| 7 | Promote-to-inventory bridge; deals freeze note | — |

---

## Phase 0: Ops (same day)

### Task 0.1: Confirm the Atlanta split renders

**Files:** none (verification).

- [ ] Run `.venv/bin/python -m automation.web` and open `http://127.0.0.1:8765/listings`.
- [ ] Confirm two cards: "Brown Convention Chairs — Boise, ID" qty 2,500 and "Brown Convention Chairs — Atlanta, GA" qty 1,200. Confirm `/listings/31225-atl` renders with city Atlanta.
- [ ] If the Atlanta card is missing, check `inventory.list_public()` includes `won_pickup` (it does per `PUBLIC_STATUSES`) and that `hero_image` is set on `31225-atl`. Copy the hero from `31225` via the admin Inventory tab if empty.

### Task 0.2: Bitwarden CLI + MCP

- [ ] `brew install bitwarden-cli`; `bw login <operator email>` (operator types the password; agent never sees it).
- [ ] Add to `~/.claude/settings.json` mcpServers: `{"bitwarden": {"command": "npx", "args": ["-y", "@bitwarden/mcp-server"], "env": {"BW_SESSION": "<from bw unlock --raw>"}}}`.
- [ ] Create collection "Automation" in the vault; only bot accounts go in it.

---

## Phase 1: Channel state, sync loop, Channels tab, FB approval queue

### File structure

- Create `scripts/sql/011_listing_channels.sql` — table + seed of `site_settings` channel switches.
- Create `automation/channels/__init__.py` — `CHANNELS`, `STATES`, `FEED_CHANNELS`, `PUSH_CHANNELS` constants.
- Create `automation/channels/store.py` — CRUD over `listing_channels` (get/upsert/list/matrix/queue).
- Create `automation/channels/sync.py` — `desired_state(row, channel)`, `payload_hash(row, channel)`, `plan(rows)`, `run_once()`; adapter dispatch via `automation/publish/registry`.
- Modify `automation/site_settings.py` — add channel switch keys to `SPEC`.
- Modify `automation/web/app.py` — `/api/channels*` routes + `_channel_sync_loop` startup task.
- Create `automation/web/static/admin/channels.js`, `channels.css`; modify `shell.js`, `templates/index.html`.
- Tests: `tests/test_channels_store.py`, `tests/test_channels_sync.py`, `tests/web/test_admin_tab_channels.py`, `tests/web/test_channels_api.py`; modify `tests/web/test_admin_modules.py`.

### Task 1.1: Migration + constants

**Files:**
- Create: `scripts/sql/011_listing_channels.sql`
- Create: `automation/channels/__init__.py`
- Modify: `docs/claude-reference/data-model.md` (append DDL)
- Test: `tests/test_channels_constants.py`

**Produces:** `CHANNELS: tuple[str,...]`, `FEED_CHANNELS`, `PUSH_CHANNELS`, `STATES`, `APPROVAL_CHANNELS`.

- [ ] **Step 1: failing test**

```python
# tests/test_channels_constants.py
from automation import channels

def test_channel_sets_partition():
    assert set(channels.FEED_CHANNELS) | set(channels.PUSH_CHANNELS) == set(channels.CHANNELS)
    assert not set(channels.FEED_CHANNELS) & set(channels.PUSH_CHANNELS)

def test_fb_marketplace_needs_approval():
    assert "fb_marketplace" in channels.APPROVAL_CHANNELS
    assert "ebay" not in channels.APPROVAL_CHANNELS

def test_states():
    assert channels.STATES == ("off", "queued", "pending_approval", "live", "delisted", "error")
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_channels_constants.py -q` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: implement**

```python
# automation/channels/__init__.py
"""Channel state model. One row per lot × channel lives in `listing_channels`.

Feed channels are pull-based CSV routes (Commerce Manager / Merchant Center fetch
them) so their "sync" is bookkeeping only. Push channels have adapters that
create/update/delete a listing on the far side.
"""
CHANNELS: tuple[str, ...] = ("site", "fb_catalog", "google", "ebay", "fb_marketplace", "craigslist")
FEED_CHANNELS: tuple[str, ...] = ("site", "fb_catalog", "google")
PUSH_CHANNELS: tuple[str, ...] = ("ebay", "fb_marketplace", "craigslist")
# Push channels whose (re)list must be approved by the operator in admin.
APPROVAL_CHANNELS: frozenset[str] = frozenset({"fb_marketplace"})
STATES: tuple[str, ...] = ("off", "queued", "pending_approval", "live", "delisted", "error")
```

```sql
-- scripts/sql/011_listing_channels.sql  (PENDING — apply in Supabase SQL Editor)
CREATE TABLE IF NOT EXISTS listing_channels (
  id             bigserial PRIMARY KEY,
  lot_id         text NOT NULL REFERENCES inventory(lot_id) ON DELETE CASCADE,
  channel        text NOT NULL CHECK (channel IN ('site','fb_catalog','google','ebay','fb_marketplace','craigslist')),
  state          text NOT NULL DEFAULT 'off' CHECK (state IN ('off','queued','pending_approval','live','delisted','error')),
  external_id    text,
  url            text,
  payload_hash   text,
  last_synced_at timestamptz,
  last_error     text,
  approved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (lot_id, channel)
);
CREATE INDEX IF NOT EXISTS listing_channels_state_idx ON listing_channels (channel, state);

-- Channel switches. 0/1 ints so site_settings' int coercion applies.
INSERT INTO site_settings (key, value) VALUES
  ('channels_master_enabled', '1'),
  ('channel_site_enabled', '1'),
  ('channel_fb_catalog_enabled', '1'),
  ('channel_google_enabled', '1'),
  ('channel_ebay_enabled', '0'),
  ('channel_fb_marketplace_enabled', '0'),
  ('channel_craigslist_enabled', '0'),
  ('browser_channel_daily_cap', '4'),
  ('browser_channel_spacing_s', '1200')
ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 4:** tests pass. Append the DDL block to `docs/claude-reference/data-model.md` under a `listing_channels` heading.
- [ ] **Step 5:** `git add` those four files; `git commit -m "channels: listing_channels DDL + channel constants (Phase 1.1)"`.

### Task 1.2: site_settings switches

**Files:** Modify `automation/site_settings.py:22-40`; Test `tests/test_site_settings_channels.py`.

**Produces:** keys `channels_master_enabled`, `channel_<c>_enabled` for every `c` in `CHANNELS`, `browser_channel_daily_cap`, `browser_channel_spacing_s`; helper `site_settings.channel_enabled(channel: str) -> bool`.

- [ ] **Step 1: failing test**

```python
from automation import site_settings, channels

def test_every_channel_has_a_switch():
    for c in channels.CHANNELS:
        assert f"channel_{c}_enabled" in site_settings.SPEC

def test_defaults_ship_browser_channels_off():
    d = site_settings.defaults()
    assert d["channel_fb_marketplace_enabled"] == 0
    assert d["channel_craigslist_enabled"] == 0
    assert d["channel_site_enabled"] == 1

def test_channel_enabled_respects_master(monkeypatch):
    monkeypatch.setattr(site_settings, "get_all",
        lambda: {**site_settings.defaults(), "channels_master_enabled": 0, "channel_site_enabled": 1})
    assert site_settings.channel_enabled("site") is False
```

- [ ] **Step 2:** run → FAIL (`KeyError`).
- [ ] **Step 3: implement** — extend `SPEC` in a loop and add the helper:

```python
from . import channels as _channels

_SWITCH = {"type": int, "min": 0, "max": 1}
SPEC["channels_master_enabled"] = {**_SWITCH, "default": 1}
for _c in _channels.CHANNELS:
    SPEC[f"channel_{_c}_enabled"] = {**_SWITCH, "default": 1 if _c in _channels.FEED_CHANNELS else 0}
SPEC["browser_channel_daily_cap"] = {"type": int, "min": 0, "max": 50, "default": 4}
SPEC["browser_channel_spacing_s"] = {"type": int, "min": 60, "max": 86_400, "default": 1200}


def channel_enabled(channel: str) -> bool:
    """Master switch AND the channel's own switch. Never raises."""
    values = get_all()
    return bool(values.get("channels_master_enabled", 0)) and bool(values.get(f"channel_{channel}_enabled", 0))
```

- [ ] **Step 4:** tests pass; also `.venv/bin/python -m pytest tests/web/test_deposits_admin.py -q` still green (settings API round-trips new keys).
- [ ] **Step 5:** commit `channels: per-channel switches in site_settings (Phase 1.2)`.

### Task 1.3: `listing_channels` store

**Files:** Create `automation/channels/store.py`; Test `tests/test_channels_store.py` (monkeypatch `automation.db.fetch_all/fetch_one/execute`, no live DB).

**Produces:**
```python
def get(lot_id: str, channel: str) -> dict | None
def upsert(lot_id: str, channel: str, *, state: str, external_id: str | None = None,
           url: str | None = None, payload_hash: str | None = None, last_error: str | None = None) -> dict
def set_state(lot_id: str, channel: str, state: str, *, last_error: str | None = None) -> dict | None
def list_for_lot(lot_id: str) -> list[dict]
def list_by_state(channel: str, state: str) -> list[dict]
def matrix() -> list[dict]        # one dict per lot: {lot_id, title, status, qty, channels: {c: {state,url}}}
def queue() -> list[dict]         # all rows in state pending_approval, newest first
```

- [ ] **Step 1: failing tests**

```python
import pytest
from automation.channels import store

def test_upsert_rejects_unknown_channel():
    with pytest.raises(ValueError):
        store.upsert("gd-1-2", "myspace", state="live")

def test_upsert_rejects_unknown_state():
    with pytest.raises(ValueError):
        store.upsert("gd-1-2", "ebay", state="maybe")

def test_upsert_issues_on_conflict_sql(monkeypatch):
    seen = {}
    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"lot_id": "gd-1-2", "channel": "ebay", "state": "live"}
    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    row = store.upsert("gd-1-2", "ebay", state="live", url="https://ebay.com/itm/1")
    assert "ON CONFLICT (lot_id, channel)" in seen["sql"]
    assert row["state"] == "live"

def test_matrix_shape(monkeypatch):
    monkeypatch.setattr(store.inventory, "list_all", lambda: [{"lot_id": "a", "title": "t", "status": "owned", "quantity_remaining": 5}])
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [{"lot_id": "a", "channel": "ebay", "state": "live", "url": "u"}])
    m = store.matrix()
    assert m[0]["channels"]["ebay"] == {"state": "live", "url": "u"}
    assert m[0]["channels"]["site"] == {"state": "off", "url": None}
```

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: implement** — parameterized SQL only; `upsert` uses `INSERT ... ON CONFLICT (lot_id, channel) DO UPDATE SET state=EXCLUDED.state, external_id=COALESCE(EXCLUDED.external_id, listing_channels.external_id), url=COALESCE(EXCLUDED.url, listing_channels.url), payload_hash=COALESCE(EXCLUDED.payload_hash, listing_channels.payload_hash), last_error=EXCLUDED.last_error, last_synced_at=now(), updated_at=now() RETURNING *`. Validate `channel in CHANNELS` and `state in STATES` before any SQL. `matrix()` fills every channel with `{"state": "off", "url": None}` when no row exists.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `channels: listing_channels store (Phase 1.3)`.

### Task 1.4: Sync engine (desired state + hash + plan + run_once)

**Files:** Create `automation/channels/sync.py`; Test `tests/test_channels_sync.py`.

**Consumes:** `store.*`, `site_settings.channel_enabled`, `inventory.CATALOG_FEED_STATUSES`, `inventory.list_all()`, `automation.publish.registry.get(platform)`.

**Produces:**
```python
def desired_state(row: dict, channel: str) -> str           # 'live' | 'delisted'
def payload_hash(row: dict, channel: str) -> str            # sha1 of the fields that channel renders
def plan(rows: list[dict], current: dict[tuple[str,str], dict]) -> list[Action]
@dataclass class Action: lot_id: str; channel: str; op: str  # 'list' | 'update' | 'delist' | 'noop'
def run_once(*, now=None, dry_run: bool = False) -> dict      # {"planned": n, "applied": n, "skipped": {...}}
```

Rules encoded:
- desired `live` iff `row["status"] in inventory.CATALOG_FEED_STATUSES and (row["quantity_remaining"] or 0) > 0 and not row.get("fake_sold_out")`; else `delisted`.
- Feed channels: `list`/`delist` just write state (feeds are pull); `update` writes the new hash.
- Push channels: `list` on a channel in `APPROVAL_CHANNELS` → state `pending_approval` (no adapter call). Others → adapter `publish`, on success `live` + url + external_id, on exception `error` + `last_error`.
- `delist` on a push channel → adapter `unpublish` if the adapter has it, else state `delisted` with `last_error="manual delist required"`.
- A channel with `channel_enabled(c) is False` is skipped entirely and counted in `skipped`.
- Browser channels (`fb_marketplace`, `craigslist`): at most `browser_channel_daily_cap` list actions per UTC day, spaced `browser_channel_spacing_s`; count from `listing_channels.last_synced_at`.

- [ ] **Step 1: failing tests** (cover: desired_state sold/zero-qty/fake_sold_out → delisted; hash changes when price changes and not when `updated_at` changes; `plan` yields `list` when desired live and current off, `update` when live and hash differs, `noop` when equal; approval channel yields `pending_approval` not adapter call; disabled channel skipped; daily cap honoured).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: implement** per rules above. Hash fields: `title, price_per_chair, quantity_remaining, city, state, hero_image, chair_type, description` (`description` if present).
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `channels: sync engine (Phase 1.4)`.

### Task 1.5: API routes + background loop

**Files:** Modify `automation/web/app.py` (after the `/api/settings` block ~line 3740; startup block ~3187-3211); Test `tests/web/test_channels_api.py` (same fixture shape as `tests/web/test_deposits_admin.py`: `TestClient(app, base_url="https://testserver")`, no `with`, monkeypatch every DB call).

**Produces:**
- `GET /api/channels` → `{"switches": {...}, "matrix": store.matrix(), "queue": store.queue()}` (`@readcache.cached()`, plain `def`).
- `PATCH /api/channels/switches` body `{key: 0|1, ...}` → `site_settings.set_many` restricted to keys starting `channel` / `browser_channel`; 400 on anything else.
- `POST /api/channels/queue/{id}/approve` → sets state `queued`, `approved_at=now()`; the loop lists it next tick. Refuses (409) if `channel_enabled(channel)` is False with body `{"reason": "channel disabled"}`.
- `POST /api/channels/queue/{id}/reject` → state `off`.
- `POST /api/channels/sync` → `asyncio.to_thread(sync.run_once)` returns its dict (manual "Sync now").
- Startup: `_channel_sync_loop` every `CHANNEL_SYNC_SEC` (env, default 300) calling `await asyncio.to_thread(sync.run_once)`; never raises out of the loop; skipped entirely when `SKIP_BACKGROUND_LOOPS=1` (same guard the existing loops use — check `_alerts_tick` registration for the exact flag name and reuse it).

- [ ] **Step 1: failing tests** — routes 401 behind auth when enabled; `GET` shape; switch PATCH rejects `deposit_pct`; approve on disabled channel → 409; approve on enabled channel → state queued.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** pass, plus `tests/web/test_event_loop_hygiene.py` green.
- [ ] **Step 5:** commit `channels: /api/channels routes + sync loop (Phase 1.5)`.

### Task 1.6: Channels admin tab

**Files:** Create `automation/web/static/admin/channels.js`, `channels.css`; Modify `shell.js:19` (add `channels`), `templates/index.html` rail (12th tab for now; the rail collapses to six in Phase 6) + a `data-pane="channels"` panel with skeleton twin; Test `tests/web/test_admin_tab_channels.py` (asserts the pane, the module import, and the strings `Sync now`, `Approve`, `Reject`, one toggle per channel); Modify `tests/web/test_admin_modules.py:15` tab list.

Tab layout (ES module, `mount()`/`load()` like `deposits.js`):
1. **Switches** row: master toggle + one toggle per channel; FB Marketplace toggle shows a red "OFF — waiting for operator" pill; changes PATCH `/api/channels/switches`.
2. **Approval queue** table: lot title, channel, since, `Approve` / `Reject` buttons (`pending()` on click).
3. **Matrix** table: one row per lot, one cell per channel colored by state (`off` grey, `queued` amber, `pending_approval` amber outline, `live` green with link, `delisted` slate, `error` red with `title=last_error`).
4. `Sync now` button → `POST /api/channels/sync`, toast result.

- [ ] Steps: failing tab test → implement → `.venv/bin/python -m pytest tests/web -q` green → relaunch web, eyeball `/admin?tab=channels` → commit `admin: Channels tab (Phase 1.6)`.

### Task 1.7: Wire existing channel writers into the store

**Files:** Modify `automation/lot_channels.py` (`post_to_facebook` ~616, `remove_lot` ~676, `restore_lot` ~727), `automation/inventory.py::set_platform_url` (~500).

- [ ] After a successful Marketplace post: `store.upsert(lot_id, "fb_marketplace", state="live", url=url, external_id=item_id)`.
- [ ] `remove_lot`: for every channel in its `channels` arg → `store.set_state(lot_id, ch, "delisted")` (`site`→`site`, `business`→`fb_catalog`, `fb`→`fb_marketplace`).
- [ ] `restore_lot`: `store.set_state(lot_id, "fb_marketplace", "pending_approval")` instead of the printed "relist by hand" note.
- [ ] `set_platform_url(lot_id, "ebay", url)` mirrors into `store.upsert(lot_id, "ebay", state="live", url=url)`; `"facebook"` → `fb_marketplace`.
- [ ] Tests: extend `tests/test_lot_channels*.py` (find existing) with monkeypatched `store`; commit `channels: existing writers record into listing_channels (Phase 1.7)`.

### Task 1.8: Backfill + docs

- [ ] `scripts/backfill_listing_channels.py`: for each inventory row set `site`/`fb_catalog` state from `desired_state`, `fb_marketplace` live where `facebook_url` contains `marketplace/item/`, `ebay` live where `ebay_url`. `--dry-run` default, `--apply` to write.
- [ ] Docs: new `docs/claude-reference/channels.md` (model, states, switches, loop, how to enable FB Marketplace); add row to the CLAUDE.md "Read before working on X" table; HARD RULE line: "FB Marketplace channel ships disabled; only the operator flips `channel_fb_marketplace_enabled`."
- [ ] Commit `channels: backfill script + docs (Phase 1.8)`.

---

## Phase 2: Google Merchant feed + JSON-LD + sitemap

**Files:** Create `automation/google_feed.py`, `tests/test_google_feed.py`, `tests/web/test_google_feed_endpoint.py`; Modify `app.py` (routes `/catalog/google.csv`, `/sitemap.xml`), `templates/listing_detail.html` (JSON-LD block).

**Interfaces:**
- `google_feed.FEED_COLUMNS = ("id","title","description","link","image_link","additional_image_link","availability","price","condition","brand","google_product_category","product_type","shipping","identifier_exists")`
- `google_feed.build_rows(rows: list[dict]) -> list[dict]` — same input as `catalog_feed.build_feed_rows` (`inventory.list_catalog_feed()`); `condition="used"`, `availability="in_stock"`, `price=f"{price_per_chair:.2f} USD"`, `google_product_category="Furniture > Chairs"`, `identifier_exists="no"`, `link=f"https://black-whole.com/listings/{lot_id}"`; rows with no price or no R2 image are dropped and logged like the FB feed.
- `GET /catalog/google.csv` → `text/csv`, tab-delimited is NOT used (comma, quoted), `Cache-Control: max-age=900`.
- `GET /sitemap.xml` → landing, `/listings`, every public lot, `/map`, `/deals`.
- JSON-LD `Product` with `offers.availability`, `itemCondition: UsedCondition`, `priceCurrency: USD`, `price`, `image` list, `areaServed` city/state. Never include `storage_note`.

Tasks: 2.1 feed builder (TDD on row shape + drop rules) · 2.2 route + endpoint test · 2.3 sitemap · 2.4 JSON-LD + test asserting it parses and has no storage note · 2.5 `sync.py`: `google` marked `live` alongside `fb_catalog` · 2.6 docs (`docs/google_merchant_runbook.md`: operator clicks). Commit per task.

## Phase 3: eBay Sell API adapter

**Files:** Create `automation/ebay_api.py` (the ONLY module importing `requests` for eBay; token refresh, `create_or_replace_inventory_item(sku, item)`, `create_offer(sku, offer)`, `publish_offer(offer_id) -> listing_id`, `withdraw_offer(offer_id)`, `update_offer_quantity(offer_id, qty)`); rewrite `automation/publish/adapters/ebay.py` to call it (drop Playwright); Tests with `responses`-style monkeypatch of `requests.Session.request`.

**Env:** `EBAY_ENV=sandbox|production`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REFRESH_TOKEN`, `EBAY_FULFILLMENT_POLICY_ID`, `EBAY_PAYMENT_POLICY_ID`, `EBAY_RETURN_POLICY_ID`, `EBAY_MERCHANT_LOCATION_KEY`. Missing any → adapter reports `error` "ebay not configured", never guesses.

**Listing shape:** `sku=lot_id`, `condition=USED_GOOD`, `availability.shipToLocationAvailability.quantity=quantity_remaining`, `product.title` ≤80 chars, `aspects` Brand/Type/Color/Material from `chair_type`, images = R2 URLs (≤12), offer `format=FIXED_PRICE`, `pricingSummary.price = price_per_chair`, `listingDescription` with min-order note and "local pickup or freight quote", `merchantLocationKey`, `categoryId` from env `EBAY_CATEGORY_ID` (default 20490 "Business & Industrial > Chairs" — verify at exec with `getCategorySuggestions`), `listingPolicies` from env. Promoted Listings: set via Marketing API `createCampaign` once, then `bulkCreateAdsByListingId` at `EBAY_AD_RATE_PCT` (default 10.0) — separate task, off unless env set.

Tasks: 3.1 token client · 3.2 item/offer/publish · 3.3 adapter + `unpublish` (withdraw) + quantity update on hash change · 3.4 `channel_ebay_enabled` end-to-end in sandbox · 3.5 promoted ads · 3.6 runbook `docs/ebay_api_runbook.md`. Remove `LST-E1` from the all-time list when 3.4 passes in production.

## Phase 4: Craigslist adapter

**Files:** Revive `automation/craigslist.py` (keep its post model), Create `automation/publish/adapters/craigslist.py` (Playwright, profile `LISTING_CL_PROFILE` default `~/.listing_automation/chrome_profile_misc`, non-headless), `scripts/cl_post.py --lot <id> --metro <slug> [--publish]`, tests for URL/metro mapping and the form filler's field map (fixture HTML captured once with Claude-in-Chrome).

**Rules:** category `furniture - by dealer` (`fud`), one post per (lot, metro), metro from `inventory.city/state` via `automation/craigslist.py` metro table, images ≤ 24 from R2 (download to scratch, upload), body from `lot_channels.fb_description` minus any Facebook wording, capture the `post.craigslist.org/manage/<id>` URL → `store.upsert(..., "craigslist", state="live", url=public_url, external_id=manage_id)`. Payment step: if CL shows the $5 checkout, stop with state `pending_approval` and `last_error="payment step"` — operator pays once, card is remembered, next run publishes. Never automate renew or repost. `unpublish` = open manage URL, click Delete.

Tasks: 4.1 form map + fixture · 4.2 filler (dry-run stops before Publish) · 4.3 publish + URL capture · 4.4 adapter into sync (`browser_channel_daily_cap` honoured) · 4.5 first live posts: owned lots `9006` (Phoenix), `folder:ATL_Grey_blueish_chairs_399` + `31225-atl` (Atlanta), `folder:Orange_Red_Banquet_Chairs_Cypress_242` (Orange County), `gd-32876-2` (Nashville) · 4.6 runbook.

## Phase 5: Relist detector → inventory

**Files:** Create `automation/channels/relist.py`; Modify `deals/relist.py::scan_for_relists` (after the `UPDATE deal_lots SET relist_of` write, call `channels.relist.adopt(lot)`), Tests `tests/test_channels_relist.py`.

**Interface:** `adopt(deal_lot: dict) -> dict | None` — find `inventory` row where `govdeals_url` matches `/asset/{asset_id}/{account_id}` OR `lot_id LIKE 'gd-{asset_id}-{account_id}%'` and `status IN ('lost','lost_sold_out','sold_out')` with `fake_sold_out` or never owned. If found: `inventory.set_fields(status='active_bid', fake_sold_out=False)`, `UPDATE inventory SET crm_offerable=true, govdeals_url=<new auction url>`, `store.set_state(lot, c, 'queued')` for enabled push channels, `pending_approval` for `APPROVAL_CHANNELS`, feed channels fall out of `sync.run_once`; Telegram `leads` ping "♻️ relisted on GovDeals → back on site/feeds, FB queued". Returns the inventory row. Never touches `quantity_remaining` or `price_per_chair` (operator's numbers).

Tasks: 5.1 matcher (TDD both key shapes) · 5.2 adopt + state writes · 5.3 hook into `deals/relist.py` · 5.4 doc line in `channels.md`.

## Phase 6: Admin rail consolidation

**Files:** Create `scripts/sql/012_inquiries_geo.sql` (`ALTER TABLE inquiries ADD COLUMN city text, ADD COLUMN state text, ADD COLUMN zip_code text`), `automation/web/static/admin/leads.js`, `leads.css`, `source.js`, `radar.js`; Modify `inventory.create_inquiry` (+3 optional fields), `templates/_contact_form.html` (or wherever `POST /contact` form lives; optional City / State / ZIP inputs), `app.py` (`GET /api/leads` = union view `{kind: 'subscriber'|'inquiry', id, name, email, phone, city, state, zip, lot_id, status, created_at, lat, lng}` with lat/lng from `automation/zip_centroids.py` 3-digit centroids), `shell.js` TABS, `index.html` rail (six tabs), `tests/web/test_admin_modules.py`, per-tab tests.

**Leads map:** port the CRM map init from `facebook_scraper_Claude` (CRM-R300, MapLibre) into `static/ui/map.js` shared module; pins colored by kind; click → row; filter chips `subscriber | inquiry | new | contacted`.

**Source tab:** one pane with a segmented control `Launcher · Auctions · Listings DB · Test Scrape`; existing modules keep their code, `source.js` mounts them into sub-panes. **Radar tab:** same pattern for Deals · Tracking. **Inventory tab:** add the Drafts photo grid as a per-row expander (reuse `drafts.js` renderer) and a Channels column reading `/api/channels` matrix; delete `drafts.js` tab registration.

Tasks: 6.1 migration + inquiry fields + contact form (TDD on `create_inquiry`) · 6.2 `/api/leads` · 6.3 shared map module · 6.4 Leads tab · 6.5 Source · 6.6 Radar · 6.7 Inventory absorbs Drafts + channel column · 6.8 rail → six, tests updated · 6.9 docs (`repo-layout.md` admin section).

## Phase 7: Promote-to-inventory bridge + deals freeze

- 7.1 `POST /api/favorites/{asset_id}/promote` → `BackgroundTask(lot_channels.add_lot, govdeals_url(asset, account), publish=False)`; returns `{lot_id}`; 409 if `find_existing_lot` hits. Button on the favorites list in Source tab. Test with monkeypatched `add_lot`.
- 7.2 `docs/claude-reference/deals.md`: "Public /deals frozen 2026-09-20 (D10). Crons stay. New work only via the Promote bridge." Update `DEAL-CUTS` line in the all-time list.

---

## Self-review

- **Coverage:** D1→1.x, D2→1.2/1.4/1.5, D3→6.1-6.4, D4→6.5-6.8, D5→5, D6→3, D7→2, D8→4, D9→ops + 4 profile rule, D10→7, D11→0.2, D12→no task (nothing to build). Atlanta split→0.1. Spreadsheet export view: **gap** — add to Phase 6.7: `GET /api/inventory/export.csv` (all `inventory` columns minus `storage_note`) and `POST /api/inventory/import.csv` (whitelisted user-edit columns only, dry-run response first). Added as Task 6.10.
- **Placeholders:** Phases 2–7 are intentionally interface-level; each gets a detailed sub-plan at start (stated in the header).
- **Type consistency:** `store.upsert(lot_id, channel, *, state, external_id, url, payload_hash, last_error)` and `store.set_state(lot_id, channel, state, *, last_error)` are used with those names in 1.4, 1.5, 1.7, 4, 5. `site_settings.channel_enabled(channel)` used in 1.4, 1.5, 5. `sync.run_once()` returns a dict used by 1.5.

### Task 6.10: Inventory CSV export/import (the "spreadsheet")
- `GET /api/inventory/export.csv` — every `inventory` column except `storage_note`; `Content-Disposition: attachment`.
- `POST /api/inventory/import.csv` — multipart CSV; only `quantity_remaining, status, price_per_chair, hero_image, city, state, locations, chair_type, title` may change; response lists diffs; apply only with `?apply=1`. Uses `inventory.set_fields` row by row (so the `quantity_remaining=0 → sold_out` rule still fires). Tests: export excludes storage note; import rejects unknown column; dry-run writes nothing.

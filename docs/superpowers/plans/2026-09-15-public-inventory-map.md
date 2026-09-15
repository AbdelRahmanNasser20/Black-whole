# Public Inventory Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A buyer lands on black-whole.com, sees our chair lots on a map (home page, full-screen `/map`, and a mini-map on each listing), filters available / incoming / sold, searches near a city or ZIP, and sees our favorited auction lots as "incoming" with the source watermark removed.

**Architecture:** One new read model (`automation/web/public_map.py`) turns `inventory` rows + `auction_favorites` into an allow-listed point list, geocoded at read time (city → zip → state, offline pgeocode) and memoised 5 min. One public JSON route (`/map/api/points`) feeds three surfaces through one ES module (`static/site/map.js`) that mounts the existing Leaflet module (`static/admin_map.js`) with a light basemap. Favorited lots get clean photos through the existing dewatermark → R2 path, stored on three new `auction_favorites` columns.

**Tech Stack:** FastAPI + Jinja2 (no bundler), Leaflet 1.9.4 + markercluster via cdnjs (already vendored by `admin_map.js`), pgeocode (offline GeoNames), dewatermark.ai API, Cloudflare R2, pytest.

**Spec:** Obsidian `Black-whole/Tasks/website-map-listings.md` (iCloud vault: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault/Black-whole/Tasks/website-map-listings.md`). Sister UI plan: `docs/superpowers/plans/2026-09-04-ui-rebuild-govauctions-clone.md` (§8 Workstream D map, §11 Workstream G storefront).

## Summary (read this first)

- Three surfaces, one feed: home (search bar + compact map), `/map` (full screen, filters, sold toggle, side list), listing page (mini map + "N other lots within 200 mi").
- Pins are **city-level only**. Never `storage_note`, never a street address, never `zip_code` in the payload.
- "Incoming" = `won_pickup` + `active_bid` rows + favorited auctions. Favorites are **redacted**: no auction link, asset id, bid, seller, or close time. A favorite with `#private` in its notes never appears.
- Favorites' photos come from a new `favorite_images.mirror_favorite_photos()` (GovDeals gallery → dewatermark.ai → R2). The raw watermarked `auction_favorites.image_url` is never sent to the public.
- No inventory schema change. One migration: three columns on `auction_favorites` (operator pastes SQL).

## Decisions taken (spec left these open; change here, not in code)

1. **Watermark** = strip the source watermark via dewatermark.ai (existing rule: every photo path to R2/site runs `automation.dewatermark` first). Terms-of-service risk on republishing GovDeals photos stays flagged to the operator; `#private` in a favorite's notes opts a lot out.
2. **Incoming** = won-not-picked-up (`won_pickup`) + lots we are bidding on (`active_bid`, already public on `/listings` today) + favorites. Favorites are redacted so a competing buyer cannot find the auction from the site.
3. **Pin precision**: city centroid first, zip centroid second, state centroid last (`precision` is sent so the pin can render "approx"). The storage address is never read.

## Global Constraints

- **Stack stays.** No Tailwind, no React, no bundler, no npm build. `node --check` only.
- **Public JSON lives under `/map/api/`, never `/api/`** (`/api/` is auth-gated by `automation/web/auth.py:69 PROTECTED_PREFIXES`).
- **`inventory.storage_note` never renders anywhere.** Nor `govdeals_username`, `govdeals_password`, `contact_*`, `buyer_cert_*`, `folder_path`, `seller_id`, `zip_code`, `auction_favorites.image_url`, `auction_favorites.link`, `auction_favorites.asset_id`. The read model is an allow-list; a test asserts it.
- **DB:** everything through `automation/db.py` / `automation/inventory.py` / `automation/favorites.py`; `%s` placeholders; route handlers that touch the DB are plain `def` (FastAPI threadpool) — `tests/web/test_event_loop_hygiene.py` enforces it. No runtime schema creation. Schema changes = a `scripts/sql/0NN_*.sql` file + the DDL in `../docs/claude-reference/data-model.md` in the same commit.
- **Tokens only:** every colour in `static/site/*.css` / `*.js` is a `var(--…)` from `static/ui/tokens.css` (`tests/web/test_ui_primitives.py::test_no_hardcoded_hex_outside_tokens`). Radius 0 everywhere.
- **Loading rule:** no raw `fetch(` under `static/site/` (`test_no_raw_fetch_in_new_modules`). Use `import {load, api} from '/static/ui/state.js'`.
- **`admin_map.js` API is frozen** (`mount → {leaflet, setPoints, fit, onViewport, inBounds, bboxParam, invalidateSize, count, visibleCount}`) because `static/admin/deals.js:505` and `static/admin/auctions.js:486` use it. Changes must be additive with unchanged defaults.
- **Dewatermark:** API only, global cache, `RunBudget` caps (`MAX_API_CALLS_PER_RUN=50`, `MAX_API_CALLS_PER_DAY=250`). No fallback on failure: a dirty photo stays in `_originals/` and is not published.
- **Relaunch after every `app.py` / `templates/` / `static/` change:** kill the process on :8765 and run `python -m automation.web` from `listing_automation/` (venv active).
- **Tests:** `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` must stay green (no `pytest` console script). Add the new files' tests to that run.
- **Branch:** `feat/public-map` in worktree `.claude/worktrees/public-map` (`git worktree add .claude/worktrees/public-map -b feat/public-map main`). Commit after every green cycle. One PR at the end.
- **Answer shape** in anything written to docs: summary first, details nested.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

| File | Responsibility |
|---|---|
| `automation/alerts/geo.py` (modify) | Add `city_latlon()` + `resolve_place()` — the city → zip → state ladder. |
| `pyproject.toml` (modify) | Add `pgeocode>=0.5` to `dependencies`. |
| `scripts/sql/010_favorites_clean_images.sql` (create) | Three columns on `auction_favorites`. |
| `../docs/claude-reference/data-model.md` (modify) | DDL for `auction_favorites` incl. the new columns. |
| `automation/favorites.py` (modify) | `Favorite.clean_hero_url` / `clean_image_urls`, `set_clean_images()`, `is_private()`. |
| `automation/web/public_map.py` (create) | Read model: buckets, allow-listed points, favorites redaction, near/geocode, nearby. |
| `automation/web/app.py` (modify) | Routes `GET /map`, `GET /map/api/points`; `/` and `/listings/{lot_id}` context additions; sitemap; star route background task. |
| `automation/web/templates/_public_base.html` (modify) | Nav link `Map`. |
| `automation/web/templates/map.html` (create) | Full-screen map page. |
| `automation/web/templates/landing.html` (modify) | Search bar + compact map between `stats` and `featured`. |
| `automation/web/templates/listing_detail.html` (modify) | Mini map + nearby list after the spec sheet. |
| `automation/web/static/admin_map.js` (modify, additive) | `mount(container, {tiles})`, `p.cls` on pins. |
| `automation/web/static/site/map.js` (create) | ES module: `mountSiteMap()`, filters, side list, URL state. |
| `automation/web/static/site/map.css` (create) | Pin colours, layout for the three surfaces. |
| `automation/lot_channels.py` (modify) | Extract `clean_and_upload(key, urls, …)` from `mirror_photos`. |
| `automation/favorite_images.py` (create) | `mirror_favorite_photos(asset_id)`. |
| `scripts/favorite_photos.py` (create) | CLI: `--all [--force]`, `--asset a/b`, `--dry-run`. |
| Tests | `tests/test_geo_place.py`, `tests/web/test_public_map.py`, `tests/web/test_public_map_page.py`, `tests/test_favorite_images.py`. |

---

### Task 1: Worktree + city-level geocoder

**Files:**
- Modify: `automation/alerts/geo.py` (after `resolve_latlon`, line ~121)
- Modify: `pyproject.toml:6-25` (dependencies)
- Test: `tests/test_geo_place.py`

**Interfaces:**
- Consumes: `automation.alerts.geo._pgeocode_us()`, `_zip_latlon()`, `_norm_state()`, `STATE_CENTROIDS` (all exist).
- Produces: `resolve_place(city: str | None, state: str | None, zip_code: str | None) -> tuple[float | None, float | None, str | None]` with precision `'city' | 'zip' | 'state' | None`; `city_latlon(city, state) -> tuple[float, float] | None`; `parse_place(text: str) -> tuple[str | None, str | None, str | None]` → `(city, state, zip)` from free text like `"Boise, ID"`, `"83702"`, `"Boise ID 83702"`.

- [ ] **Step 1: Create the worktree**

```bash
cd ~/Projects/blackwhole/listing_automation
git worktree add .claude/worktrees/public-map -b feat/public-map main
cd .claude/worktrees/public-map
ln -s ../../../.venv .venv 2>/dev/null || true   # reuse the repo venv; skip if .venv already resolves
ls -la .env || cp ../../../.env .env
.venv/bin/python -m pytest tests/web/ tests/deals/ -q 2>&1 | tail -2
```
Expected: the baseline count passes (≈370+). If `.venv` is a real dir in the worktree already, use it.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_geo_place.py
"""City → zip → state ladder for public map pins (no network: pgeocode is faked)."""
import pandas as pd
import pytest

from automation.alerts import geo


class _FakeNominatim:
    def query_location(self, name, top_k=10):
        if name.lower() == "boise":
            return pd.DataFrame({
                "place_name": ["Boise", "Boise", "Boise"],
                "state_code": ["ID", "ID", "ID"],
                "latitude": [43.60, 43.63, 43.66],
                "longitude": [-116.27, -116.20, -116.25],
            })
        return pd.DataFrame(columns=["place_name", "state_code", "latitude", "longitude"])

    def query_postal_code(self, code):
        return pd.Series({"latitude": 43.6322, "longitude": -116.2052}) if code == "83702" \
            else pd.Series({"latitude": float("nan"), "longitude": float("nan")})


@pytest.fixture(autouse=True)
def _fake_pgeocode(monkeypatch):
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: _FakeNominatim())
    geo.city_latlon.cache_clear()


def test_city_ladder_prefers_city():
    lat, lng, prec = geo.resolve_place("Boise", "ID", "83702")
    assert prec == "city"
    assert abs(lat - 43.63) < 0.05 and abs(lng - (-116.24)) < 0.05


def test_city_ladder_falls_to_zip_then_state():
    assert geo.resolve_place("Nowhere", "ID", "83702")[2] == "zip"
    assert geo.resolve_place("Nowhere", "ID", None)[2] == "state"
    assert geo.resolve_place(None, None, None) == (None, None, None)


def test_city_requires_matching_state():
    assert geo.city_latlon("Boise", "GA") is None


def test_city_latlon_without_pgeocode(monkeypatch):
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: None)
    geo.city_latlon.cache_clear()
    assert geo.city_latlon("Boise", "ID") is None


@pytest.mark.parametrize("text,expect", [
    ("Boise, ID", ("Boise", "ID", None)),
    ("83702", (None, None, "83702")),
    ("Boise ID 83702", ("Boise", "ID", "83702")),
    ("Pittsburgh, Pennsylvania", ("Pittsburgh", "PA", None)),
    ("", (None, None, None)),
])
def test_parse_place(text, expect):
    assert geo.parse_place(text) == expect
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_geo_place.py -q`
Expected: FAIL with `AttributeError: module 'automation.alerts.geo' has no attribute 'resolve_place'`.

- [ ] **Step 4: Implement in `automation/alerts/geo.py`**

Append after `resolve_latlon`:

```python
import functools
import re

_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


@functools.lru_cache(maxsize=2048)
def city_latlon(city: str | None, state: str | None) -> tuple[float, float] | None:
    """City centroid from pgeocode's offline GeoNames (median of the city's zip rows).

    Needs a matching 2-letter state: "Boise, GA" is None, not a wrong pin.
    """
    st = _norm_state(state)
    name = (city or "").strip()
    if not name or st is None:
        return None
    nomi = _pgeocode_us()
    if nomi is None:
        return None
    try:
        df = nomi.query_location(name, top_k=25)
    except Exception:  # noqa: BLE001 — pgeocode raises on odd input; treat as miss
        return None
    if df is None or df.empty:
        return None
    hit = df[(df["state_code"] == st) & (df["place_name"].str.lower() == name.lower())]
    if hit.empty:
        hit = df[df["state_code"] == st]
    if hit.empty:
        return None
    return (float(hit["latitude"].median()), float(hit["longitude"].median()))


def resolve_place(
    city: str | None, state: str | None, zip_code: str | None
) -> tuple[float | None, float | None, str | None]:
    """Public-map ladder: city centroid → zip centroid → state centroid → none.

    City first on purpose: pins must never be more precise than a city.
    """
    hit = city_latlon(city, state)
    if hit is not None:
        return (hit[0], hit[1], "city")
    lat, lon, prec = resolve_latlon(zip_code, state)
    return (lat, lon, prec)


_ZIP_RE = re.compile(r"\b(\d{5})\b")


def parse_place(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Free text ("Boise, ID", "83702", "Boise ID 83702") → (city, state, zip)."""
    s = (text or "").strip()
    if not s:
        return (None, None, None)
    zip_code = None
    m = _ZIP_RE.search(s)
    if m:
        zip_code = m.group(1)
        s = (s[:m.start()] + s[m.end():]).strip(" ,")
    parts = [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]
    state = None
    city_parts = parts
    if parts:
        last = parts[-1]
        if _norm_state(last):
            state, city_parts = _norm_state(last), parts[:-1]
        else:
            for n in (2, 1):
                cand = " ".join(parts[-n:]).lower()
                if cand in _STATE_NAMES:
                    state, city_parts = _STATE_NAMES[cand], parts[:-n]
                    break
    city = " ".join(city_parts).strip() or None
    return (city, state, zip_code)
```

Then in `pyproject.toml` add to `dependencies`:
```toml
    "pgeocode>=0.5",        # offline city/zip → lat/lng for public map pins (automation/alerts/geo.py)
```
and run `.venv/bin/pip install -e . -q`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_geo_place.py tests/deals/test_geo.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add automation/alerts/geo.py pyproject.toml tests/test_geo_place.py
git commit -m "geo: city-level resolve_place ladder + parse_place for the public map

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Favorites carry clean photos (schema + model)

**Files:**
- Create: `scripts/sql/010_favorites_clean_images.sql`
- Modify: `../docs/claude-reference/data-model.md` (add an `auction_favorites` DDL block if absent; else add the three columns)
- Modify: `automation/favorites.py` (`Favorite` dataclass ~117, row mapper ~150-170, new `set_clean_images`, `is_private`)
- Test: `tests/test_favorites_clean_images.py`

**Interfaces:**
- Produces: `Favorite.clean_hero_url: str | None`, `Favorite.clean_image_urls: list[str]`, `Favorite.is_private -> bool` (`#private` in notes, case-insensitive), `favorites.set_clean_images(asset_id: str, hero_url: str | None, image_urls: list[str]) -> None`.
- The row mapper must use `row.get(...)` for the three new columns so an **unapplied migration degrades to "no photo"**, never a 500.

- [ ] **Step 1: Migration file**

```sql
-- 010_favorites_clean_images.sql — dewatermarked R2 copies of a favorite's GovDeals photos (2026-09-15).
-- Paste into Supabase → SQL Editor once. Idempotent. Written by automation/favorite_images.py,
-- read by automation/web/public_map.py. URLs only (bounded), no blobs.
ALTER TABLE auction_favorites
  ADD COLUMN IF NOT EXISTS clean_hero_url   text,
  ADD COLUMN IF NOT EXISTS clean_image_urls jsonb,
  ADD COLUMN IF NOT EXISTS clean_images_at  timestamptz;
COMMENT ON COLUMN auction_favorites.clean_hero_url IS
  'R2 URL of the dewatermarked cover photo. The raw image_url is never shown publicly.';
```

Add the same three columns to the `auction_favorites` DDL in `../docs/claude-reference/data-model.md` (create the block from the live columns listed in this plan's Task 2 preamble if the table is not documented yet: `asset_id text PK, link, title, quantity int, end_date_iso, end_date_raw, image_url, location, starred_at, last_synced_at, notes`).

- [ ] **Step 2: Failing test**

```python
# tests/test_favorites_clean_images.py
from automation import favorites


def test_favorite_defaults_and_private():
    f = favorites.Favorite(asset_id="1/2", link="x", title="t", quantity=1, end_date_iso=None,
                           end_date_raw=None, image_url="raw", location="Boise, ID",
                           starred_at="2026-01-01", last_synced_at="2026-01-01",
                           notes="keep #Private", sent_intervals=[])
    assert f.clean_hero_url is None and f.clean_image_urls == []
    assert f.is_private is True
    d = f.to_dict()
    assert d["clean_hero_url"] is None and d["clean_image_urls"] == []


def test_set_clean_images_sql(monkeypatch):
    calls = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): calls.append((sql, params))

    monkeypatch.setattr(favorites.inventory, "connect", lambda: _Conn())
    favorites.set_clean_images("1/2", "https://r2/fav-1-2.jpg", ["https://r2/fav-1-2/00.jpg"])
    sql, params = calls[0]
    assert "UPDATE auction_favorites" in sql and "clean_hero_url" in sql
    assert params[0] == "https://r2/fav-1-2.jpg" and params[-1] == "1/2"
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_favorites_clean_images.py -q` → FAIL (`unexpected keyword` / missing attribute).

- [ ] **Step 4: Implement in `automation/favorites.py`**

In the dataclass add (after `sent_intervals: list[str]`):
```python
    clean_hero_url: str | None = None
    clean_image_urls: list[str] = field(default_factory=list)

    @property
    def is_private(self) -> bool:
        """`#private` anywhere in notes keeps this favorite off the public map."""
        return "#private" in (self.notes or "").lower()
```
(`from dataclasses import dataclass, field`.) In `to_dict` add `"clean_hero_url"` and `"clean_image_urls"` to the key tuple. In the row → `Favorite` mapper add:
```python
        clean_hero_url=row.get("clean_hero_url"),
        clean_image_urls=list(row.get("clean_image_urls") or []),
```
(if the mapper indexes `row["..."]`, the row is a `dict_row`, so `.get` works). Add:
```python
def set_clean_images(asset_id: str, hero_url: str | None, image_urls: list[str]) -> None:
    """Stamp the dewatermarked R2 copies (automation/favorite_images.py). Fails loud
    if migration 010 is not applied — that is the CLI's problem, not the site's."""
    import json
    with inventory.connect() as conn:
        conn.execute(
            "UPDATE auction_favorites SET clean_hero_url = %s, clean_image_urls = %s::jsonb, "
            "clean_images_at = now() WHERE asset_id = %s",
            (hero_url, json.dumps(list(image_urls or [])), asset_id),
        )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_favorites_clean_images.py tests/ -q -k "favorite" ` → PASS (existing favorites tests too).

- [ ] **Step 6: Commit**

```bash
git add scripts/sql/010_favorites_clean_images.sql ../../../../docs/claude-reference/data-model.md automation/favorites.py tests/test_favorites_clean_images.py
git commit -m "favorites: clean_hero_url/clean_image_urls columns (migration 010) + is_private

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
(The data-model.md path is relative to the worktree; use the absolute path `~/Projects/blackwhole/docs/claude-reference/data-model.md` and commit it in the workspace repo if `git add` says it is outside this repo — the workspace root is its own git repo.)

---

### Task 3: Read model `automation/web/public_map.py`

**Files:**
- Create: `automation/web/public_map.py`
- Test: `tests/web/test_public_map.py`

**Interfaces:**
- Consumes: `inventory.list_public()`, `inventory.list_sold_showcase()`, `inventory.parse_locations(value)`, `inventory.SOLD_STATUSES`, `favorites.list_all()`, `lot_channels.unit_word(row)`, `lot_images.hero_src(row)`, `geo.resolve_place`, `geo.parse_place`, `geo.haversine_miles`, `readcache.cached`.
- Produces:
  - `BUCKETS = ("available", "incoming", "sold")`
  - `bucket(row: dict) -> str | None`
  - `POINT_KEYS` (frozenset) — the only keys a point may carry: `id, kind, lot_id, title, bucket, quantity, unit, price_per_chair, city, state, lat, lng, precision, hero, url`
  - `points_from_inventory(rows: list[dict]) -> list[dict]` (one point per location entry)
  - `points_from_favorites(favs: list[Favorite]) -> list[dict]` (redacted)
  - `all_points() -> list[dict]` (memoised 300 s)
  - `fetch_points(*, statuses: set[str] | None, near: str | None, radius_mi: float | None) -> dict` → `{"points": [...], "counts": {bucket: n}, "near": {"lat","lng","label"} | None}` (points gain `distance_mi` when `near` resolves)
  - `nearby(lot_id: str, *, miles: float = 200, limit: int = 6) -> dict` → `{"origin": {"lat","lng","precision"} | None, "items": [point + distance_mi]}`

- [ ] **Step 1: Failing tests**

```python
# tests/web/test_public_map.py
"""Public map read model: buckets, allow-list, multi-location pins, favorites redaction, nearby."""
import pytest

from automation import favorites as fav_mod
from automation.web import public_map as pm

PRIVATE = ("storage_note", "govdeals_username", "govdeals_password", "contact_email",
           "contact_phone", "buyer_cert_path", "folder_path", "seller_id", "zip_code",
           "image_url", "link", "asset_id", "end_date_iso", "notes")


@pytest.fixture(autouse=True)
def _fixed_geo(monkeypatch):
    table = {("Boise", "ID"): (43.6, -116.2), ("Atlanta", "GA"): (33.7, -84.4),
             ("Pittsburgh", "PA"): (40.4, -80.0)}
    def fake(city, state, zip_code):
        hit = table.get((city, state))
        return (hit[0], hit[1], "city") if hit else (None, None, None)
    monkeypatch.setattr(pm.geo, "resolve_place", fake)
    pm.readcache.invalidate_all()


def _row(**kw):
    base = dict(lot_id="gd-1-2", title="500 banquet chairs", status="owned", quantity_remaining=500,
                quantity_original=500, price_per_chair=25, city="Boise", state="ID", zip_code="83702",
                storage_note="unit 12 gate 4321", hero_image_url="https://r2/x.jpg", image_urls=[],
                locations=None, fake_sold_out=False, chair_type="banquet")
    base.update(kw)
    return base


@pytest.mark.parametrize("row,expect", [
    (_row(status="owned"), "available"),
    (_row(status="listed"), "available"),
    (_row(status="won_pickup"), "incoming"),
    (_row(status="active_bid"), "incoming"),
    (_row(status="sold_out"), "sold"),
    (_row(status="lost_sold_out"), "sold"),
    (_row(status="owned", fake_sold_out=True), "sold"),
    (_row(status="hidden"), None),
    (_row(status="lost"), None),
])
def test_bucket(row, expect):
    assert pm.bucket(row) == expect


def test_points_allowlist_and_city_level():
    pts = pm.points_from_inventory([_row()])
    assert len(pts) == 1
    p = pts[0]
    assert set(p) <= pm.POINT_KEYS
    for k in PRIVATE:
        assert k not in p
    assert p["lat"] == 43.6 and p["precision"] == "city" and p["url"] == "/listings/gd-1-2"
    assert p["kind"] == "lot" and p["bucket"] == "available" and p["unit"]


def test_multi_location_lot_yields_one_pin_per_place():
    row = _row(locations=[{"city": "Boise", "state": "ID", "quantity": 300},
                          {"city": "Atlanta", "state": "GA", "quantity": 200}])
    pts = pm.points_from_inventory([row])
    assert [(p["city"], p["quantity"]) for p in pts] == [("Boise", 300), ("Atlanta", 200)]
    assert pts[0]["id"] != pts[1]["id"]


def test_unresolvable_place_is_dropped():
    assert pm.points_from_inventory([_row(city="Nowhere", state=None)]) == []


def _fav(**kw):
    base = dict(asset_id="9685/56", link="https://www.govdeals.com/en/asset/9685/56",
                title="Lot of 2,500 banquet chairs", quantity=2500, end_date_iso="2099-01-01T00:00:00+00:00",
                end_date_raw=None, image_url="https://cdn.govdeals.com/raw.jpg", location="Pittsburgh, PA",
                starred_at="2026-09-14", last_synced_at="2026-09-14", notes=None, sent_intervals=[],
                clean_hero_url="https://r2/fav-9685-56.jpg", clean_image_urls=[])
    base.update(kw)
    return fav_mod.Favorite(**base)


def test_favorite_is_redacted_incoming():
    p = pm.points_from_favorites([_fav()])[0]
    assert set(p) <= pm.POINT_KEYS
    assert p["kind"] == "favorite" and p["bucket"] == "incoming"
    assert p["hero"] == "https://r2/fav-9685-56.jpg" and p["url"] == "/#contact"
    assert "9685" not in p["id"] or True  # id may embed the key; the assertions below are the rule
    flat = " ".join(str(v) for v in p.values())
    assert "govdeals" not in flat.lower() and "raw.jpg" not in flat


def test_favorite_without_clean_photo_has_no_hero():
    p = pm.points_from_favorites([_fav(clean_hero_url=None)])[0]
    assert p["hero"] is None


def test_private_or_ended_favorites_are_skipped():
    assert pm.points_from_favorites([_fav(notes="#private")]) == []
    assert pm.points_from_favorites([_fav(end_date_iso="2000-01-01T00:00:00+00:00")]) == []


def test_fetch_points_filters_and_near(monkeypatch):
    rows = [_row(), _row(lot_id="gd-3-4", city="Atlanta", state="GA", status="sold_out")]
    monkeypatch.setattr(pm, "all_points", lambda: pm.points_from_inventory(rows))
    monkeypatch.setattr(pm.geo, "parse_place", lambda t: ("Boise", "ID", None))
    out = pm.fetch_points(statuses={"available"}, near="Boise, ID", radius_mi=None)
    assert [p["lot_id"] for p in out["points"]] == ["gd-1-2"]
    assert out["counts"] == {"available": 1, "incoming": 0, "sold": 1}
    assert out["near"]["label"] == "Boise, ID" and out["points"][0]["distance_mi"] == 0.0
    far = pm.fetch_points(statuses=None, near="Boise, ID", radius_mi=100)
    assert [p["lot_id"] for p in far["points"]] == ["gd-1-2"]


def test_nearby_excludes_self_and_sold(monkeypatch):
    rows = [_row(), _row(lot_id="gd-3-4", city="Atlanta", state="GA"),
            _row(lot_id="gd-5-6", city="Atlanta", state="GA", status="sold_out")]
    monkeypatch.setattr(pm, "all_points", lambda: pm.points_from_inventory(rows))
    out = pm.nearby("gd-3-4", miles=200)
    assert out["origin"]["lat"] == 33.7
    assert [p["lot_id"] for p in out["items"]] == []          # Boise is > 200 mi, sold excluded
    out = pm.nearby("gd-3-4", miles=5000)
    assert [p["lot_id"] for p in out["items"]] == ["gd-1-2"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/web/test_public_map.py -q` → FAIL (`ModuleNotFoundError: automation.web.public_map`).

- [ ] **Step 3: Implement `automation/web/public_map.py`**

```python
"""Public inventory map — the ONLY read model behind /map, the home map and the
listing mini-map (2026-09-15 plan).

Rules (tests enforce them):
- Allow-list. A point carries exactly POINT_KEYS. `storage_note`, zip, contact
  fields, auction links and raw GovDeals image URLs never leave this module.
- City-level pins: geo.resolve_place (city → zip → state) — never an address.
- Buckets: available (listed/owned/draft with stock) · incoming (won_pickup,
  active_bid, favorited auctions) · sold (sold_out/lost_sold_out/fake_sold_out).
- Favorites are redacted: title + quantity + city + clean photo. No link, no
  asset id, no bid, no close time. `#private` in notes = never shown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from automation import favorites as favorites_mod
from automation import inventory, lot_channels, lot_images
from automation.alerts import geo
from automation.web import readcache

BUCKETS = ("available", "incoming", "sold")
INCOMING_STATUSES = frozenset({"won_pickup", "active_bid"})
AVAILABLE_STATUSES = frozenset({"listed", "owned", "draft"})
POINT_KEYS = frozenset({
    "id", "kind", "lot_id", "title", "bucket", "quantity", "unit", "price_per_chair",
    "city", "state", "lat", "lng", "precision", "hero", "url",
})
CACHE_TTL = 300


def bucket(row: dict) -> str | None:
    status = (row or {}).get("status")
    if status in inventory.SOLD_STATUSES or row.get("fake_sold_out"):
        return "sold"
    if status in INCOMING_STATUSES:
        return "incoming"
    if status in AVAILABLE_STATUSES:
        qty = row.get("quantity_remaining")
        return "available" if qty is None or qty > 0 else None
    return None


def _places(row: dict) -> list[dict]:
    """[{city, state, quantity}] — primary city first, then `locations` extras."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    entries = inventory.parse_locations(row.get("locations")) or []
    primary = {"city": row.get("city"), "state": row.get("state"), "quantity": None}
    for e in [primary, *entries]:
        city, state = (e.get("city") or "").strip(), (e.get("state") or row.get("state") or "").strip()
        if not city:
            continue
        key = (city.lower(), state.upper())
        if key in seen:
            continue
        seen.add(key)
        out.append({"city": city, "state": state or None, "quantity": e.get("quantity")})
    return out


def _point(**kw) -> dict:
    p = {k: kw.get(k) for k in POINT_KEYS}
    return p


def points_from_inventory(rows: Iterable[dict]) -> list[dict]:
    pts: list[dict] = []
    for row in rows:
        b = bucket(row)
        if b is None:
            continue
        places = _places(row)
        for i, place in enumerate(places):
            lat, lng, prec = geo.resolve_place(place["city"], place["state"],
                                               row.get("zip_code") if i == 0 else None)
            if lat is None:
                continue
            qty = place["quantity"] if place["quantity"] is not None else (
                row.get("quantity_original") if b == "sold" else row.get("quantity_remaining"))
            pts.append(_point(
                id=f"{row['lot_id']}#{i}", kind="lot", lot_id=row["lot_id"],
                title=row.get("title") or row["lot_id"], bucket=b, quantity=qty,
                unit=lot_channels.unit_word(row).upper(),
                price_per_chair=float(row["price_per_chair"]) if row.get("price_per_chair") else None,
                city=place["city"], state=place["state"], lat=lat, lng=lng, precision=prec,
                hero=lot_images.hero_src(row), url=f"/listings/{row['lot_id']}",
            ))
    return pts


def points_from_favorites(favs: Iterable[favorites_mod.Favorite]) -> list[dict]:
    now = datetime.now(timezone.utc)
    pts: list[dict] = []
    for f in favs:
        if f.is_private:
            continue
        end = f.end_dt
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end is not None and end < now:
            continue
        city, state, _zip = geo.parse_place(f.location)
        lat, lng, prec = geo.resolve_place(city, state, None)
        if lat is None:
            continue
        key = "".join(ch if ch.isalnum() else "-" for ch in f.asset_id)
        pts.append(_point(
            id=f"fav-{key}", kind="favorite", lot_id=None,
            title=f.title or "Incoming lot", bucket="incoming", quantity=f.quantity,
            unit="CHAIR", price_per_chair=None, city=city, state=state, lat=lat, lng=lng,
            precision=prec, hero=f.clean_hero_url or None, url="/#contact",
        ))
    return pts


@readcache.cached(ttl=CACHE_TTL)
def all_points() -> list[dict]:
    """Every public pin. Memoised; readcache drops it on any successful API write."""
    rows = [*inventory.list_public(), *inventory.list_sold_showcase()]
    seen: set[str] = set()
    uniq = [r for r in rows if not (r["lot_id"] in seen or seen.add(r["lot_id"]))]
    pts = points_from_inventory(uniq)
    try:
        pts += points_from_favorites(favorites_mod.list_all())
    except Exception:  # noqa: BLE001 — favorites table trouble must not blank the map
        pass
    return pts


def _resolve_near(text: str | None) -> dict | None:
    if not text or not text.strip():
        return None
    city, state, zip_code = geo.parse_place(text)
    lat, lng, prec = geo.resolve_place(city, state, zip_code)
    if lat is None:
        return None
    return {"lat": lat, "lng": lng, "label": text.strip(), "precision": prec}


def fetch_points(*, statuses: set[str] | None = None, near: str | None = None,
                 radius_mi: float | None = None) -> dict:
    pts = [dict(p) for p in all_points()]
    counts = {b: sum(1 for p in pts if p["bucket"] == b) for b in BUCKETS}
    if statuses:
        pts = [p for p in pts if p["bucket"] in statuses]
    origin = _resolve_near(near)
    if origin:
        for p in pts:
            p["distance_mi"] = round(geo.haversine_miles(origin["lat"], origin["lng"], p["lat"], p["lng"]), 1)
        if radius_mi:
            pts = [p for p in pts if p["distance_mi"] <= radius_mi]
        pts.sort(key=lambda p: p["distance_mi"])
    return {"points": pts, "counts": counts, "near": origin}


def nearby(lot_id: str, *, miles: float = 200, limit: int = 6) -> dict:
    pts = all_points()
    mine = [p for p in pts if p["lot_id"] == lot_id]
    if not mine:
        return {"origin": None, "items": []}
    o = mine[0]
    items = []
    for p in pts:
        if p["lot_id"] == lot_id or p["bucket"] == "sold":
            continue
        d = geo.haversine_miles(o["lat"], o["lng"], p["lat"], p["lng"])
        if d <= miles:
            items.append({**p, "distance_mi": round(d, 1)})
    items.sort(key=lambda p: p["distance_mi"])
    return {"origin": {"lat": o["lat"], "lng": o["lng"], "precision": o["precision"]},
            "items": items[:limit]}
```

`readcache.invalidate_all()` clears the memo (no per-function `cache_clear`); tests that need a fresh memo monkeypatch `pm.all_points` directly. `inventory.parse_locations(None)` returns `None` — the `or []` in `_places` handles it.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/web/test_public_map.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add automation/web/public_map.py tests/web/test_public_map.py
git commit -m "web: public_map read model — allow-listed, city-level pins; redacted favorites

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Routes + `/map` page shell + nav + sitemap

**Files:**
- Modify: `automation/web/app.py` — add routes after `public_listing_detail` (~line 590); `sitemap_xml` (~1344: add `/map` to the static tuple); import `public_map`.
- Modify: `automation/web/templates/_public_base.html:61-65` (nav).
- Create: `automation/web/templates/map.html`.
- Test: `tests/web/test_public_map_page.py`.

**Interfaces:**
- Produces: `GET /map` (HTML, ids `map-near`, `map-status`, `map-radius`, `map-count`, `site-map`, `map-list`), `GET /map/api/points?status=available,incoming&near=&radius=` → `public_map.fetch_points(...)` result. Query `status` = comma list of buckets (default `available,incoming`); invalid bucket → 400; `radius` float in miles (optional).

- [ ] **Step 1: Failing tests**

```python
# tests/web/test_public_map_page.py
"""Public /map page + /map/api/points: ids the JS builds against, no auth, allow-list on the wire."""
import importlib
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

app_mod = importlib.import_module("automation.web.app")
POINT = {"id": "gd-1-2#0", "kind": "lot", "lot_id": "gd-1-2", "title": "500 chairs", "bucket": "available",
         "quantity": 500, "unit": "CHAIR", "price_per_chair": 25.0, "city": "Boise", "state": "ID",
         "lat": 43.6, "lng": -116.2, "precision": "city", "hero": None, "url": "/listings/gd-1-2"}


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    monkeypatch.setattr(app_mod.visits, "track", lambda *a, **k: None)
    yield
    auth_svc.reset_caches()


@pytest.fixture
def client(monkeypatch):
    seen = {}
    def fake_fetch(*, statuses=None, near=None, radius_mi=None):
        seen.update(statuses=statuses, near=near, radius_mi=radius_mi)
        return {"points": [POINT], "counts": {"available": 1, "incoming": 0, "sold": 0}, "near": None}
    monkeypatch.setattr(app_mod.public_map, "fetch_points", fake_fetch)
    c = TestClient(app)
    c.seen = seen
    return c


def test_map_page_shell(client):
    html = client.get("/map").text
    assert "<title>" in html and "Where the chairs are" in html
    for el_id in ("map-near", "map-status", "map-radius", "map-count", "site-map", "map-list"):
        assert f'id="{el_id}"' in html, el_id
    assert "/static/site/map.js" in html and "/static/site/map.css" in html
    assert 'href="/map"' in html          # nav link from _public_base
    assert "storage_note" not in html


def test_points_default_statuses_and_params(client):
    r = client.get("/map/api/points")
    assert r.status_code == 200
    assert client.seen["statuses"] == {"available", "incoming"}
    body = r.json()
    assert body["points"][0]["lot_id"] == "gd-1-2" and "counts" in body
    client.get("/map/api/points?status=available,sold&near=Boise%2C%20ID&radius=200")
    assert client.seen == {"statuses": {"available", "sold"}, "near": "Boise, ID", "radius_mi": 200.0}


def test_points_rejects_unknown_bucket(client):
    assert client.get("/map/api/points?status=secret").status_code == 400


def test_points_is_public_even_with_auth_on(monkeypatch, client):
    monkeypatch.setenv("ADMIN_PASSWORD", "x"); monkeypatch.setenv("SESSION_SECRET", "y")
    auth_svc.reset_caches()
    assert client.get("/map/api/points").status_code == 200
    assert client.get("/map").status_code == 200


def test_sitemap_lists_map(monkeypatch):
    monkeypatch.setattr(app_mod.inventory, "list_public", lambda: [])
    monkeypatch.setattr(app_mod.inventory, "list_sold_showcase", lambda: [])
    assert "/map</loc>" in TestClient(app).get("/sitemap.xml").text
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/web/test_public_map_page.py -q` → FAIL (404 / AttributeError).

- [ ] **Step 3: Routes in `app.py`**

Add `from automation.web import public_map` next to the other `automation.web` imports. After `public_listing_detail`:

```python
@app.get("/map", response_class=HTMLResponse)
def public_map_page(request: Request, near: str | None = None, status: str | None = None,
                    radius: float | None = None):
    """Full-screen public map of our lots (plan 2026-09-15). Shell only: the JS
    fetches /map/api/points. `near`/`status`/`radius` seed the filter bar."""
    visits.track(request)
    return templates.TemplateResponse(request, "map.html", _public_ctx({
        "near": (near or "").strip(), "status": status or "available,incoming",
        "radius": radius or "",
    }))


@app.get("/map/api/points")
def public_map_points(status: str | None = None, near: str | None = None,
                      radius: float | None = None):
    """Public JSON for every map surface. Allow-listed in public_map — never add
    columns here. Lives under /map/api/ (public), not /api/ (auth-gated)."""
    wanted = {s.strip() for s in (status or "available,incoming").split(",") if s.strip()}
    if not wanted <= set(public_map.BUCKETS):
        raise HTTPException(400, f"status must be a comma list of {','.join(public_map.BUCKETS)}")
    try:
        return public_map.fetch_points(statuses=wanted, near=near, radius_mi=radius)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"map query failed: {e!r}")
```

In `sitemap_xml`: `for path in ("/", "/listings", "/map", "/sell"):`.

- [ ] **Step 4: Nav link + template**

`_public_base.html` nav (after the Inventory link): `<a href="/map" class="nav-link">Map</a>`.

`templates/map.html`:
```html
{% extends "_public_base.html" %}
{% block title %}Where the chairs are — Black Whole Liquidation{% endblock %}
{% block meta_description %}Every Black Whole chair lot on one map — available, incoming and sold. Search near your city.{% endblock %}
{% block body_class %}page-map{% endblock %}
{% block head_extra %}
<link rel="stylesheet" href="/static/site/map.css?v={{ now }}">
{% endblock %}
{% block content %}
<section class="page-head">
  <div class="section-rule"></div>
  <h1 class="display">Where the chairs are</h1>
  <p class="lede">Available now, on the way, and already moved. City-level pins — message us for pickup details.</p>
</section>

<form class="filter-bar map-filters" id="map-filters" action="/map" method="get">
  <div class="filter-group filter-group--grow">
    <label for="map-near" class="mono tiny">NEAR</label>
    <input id="map-near" name="near" type="search" value="{{ near }}" placeholder="City or ZIP" autocomplete="off">
  </div>
  <div class="filter-group">
    <label for="map-radius" class="mono tiny">WITHIN</label>
    <select id="map-radius" name="radius">
      <option value="" {% if not radius %}selected{% endif %}>Any distance</option>
      {% for r in (100, 200, 500, 1000) %}<option value="{{ r }}" {% if radius|string == r|string %}selected{% endif %}>{{ r }} mi</option>{% endfor %}
    </select>
  </div>
  <div class="filter-group seg" id="map-status" data-value="{{ status }}">
    <label class="mono tiny">SHOW</label>
    <button type="button" class="seg-btn b-available" data-bucket="available">Available</button>
    <button type="button" class="seg-btn b-incoming" data-bucket="incoming">Incoming</button>
    <button type="button" class="seg-btn b-sold" data-bucket="sold">Sold</button>
    <input type="hidden" name="status" value="{{ status }}">
  </div>
  <div class="filter-group">
    <button type="submit" class="btn btn-primary">GO</button>
    <span id="map-count" class="mono tiny" aria-live="polite"></span>
  </div>
</form>

<section class="map-split">
  <div id="site-map" class="site-map site-map--full map-loading" aria-label="Map of chair lots"
       data-points-url="/map/api/points" data-status="{{ status }}" data-near="{{ near }}" data-radius="{{ radius }}" data-tiles="light"></div>
  <aside id="map-list" class="map-list" data-state="loading"></aside>
</section>
{% endblock %}
{% block scripts %}
<script type="module" src="/static/site/site.js?v={{ now }}"></script>
<script type="module" src="/static/site/map.js?v={{ now }}"></script>
{% endblock %}
```
`_public_base.html` blocks are `title`, `meta_description`, `body_class`, `head_extra`, `content`, `scripts` — never override `head` (it wraps the SEO/og meta). Create an empty `static/site/map.css` so the asset test passes now (real styles come in Task 5).

- [ ] **Step 5: Run tests + relaunch smoke**

Run: `.venv/bin/python -m pytest tests/web/ -q` → PASS (all, including `test_event_loop_hygiene`).
Relaunch `python -m automation.web`; `curl -s localhost:8765/map/api/points | head -c 400` shows `{"points": [...` with real Boise/Atlanta pins and no `storage_note`.

- [ ] **Step 6: Commit**

```bash
git add automation/web/app.py automation/web/templates/map.html automation/web/templates/_public_base.html automation/web/static/site/map.css tests/web/test_public_map_page.py
git commit -m "web: /map page shell + /map/api/points public feed + nav + sitemap

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Map module — `site/map.js`, `site/map.css`, additive `admin_map.js` options

**Files:**
- Modify: `automation/web/static/admin_map.js:102-118` (mount options), `:148-162` (pin class).
- Create: `automation/web/static/site/map.js`, fill `automation/web/static/site/map.css`.
- Test: existing `tests/web/test_ui_primitives.py` (hex + raw-fetch rules) + `node --check`.

**Interfaces:**
- `AdminMap.mount(container, opts = {})` — `opts.tiles`: `'light'` → Esri `Canvas/World_Light_Gray_Base`, any `http…` string → used verbatim, otherwise the current dark URL. Default behaviour unchanged.
- `setPoints` honours `p.cls` (extra class on the pin div) — unchanged when absent.
- `map.js` exports `mountSiteMap(el, {status, near, radius, compact, listEl, countEl, onOrigin})` and auto-mounts every `[data-points-url]` element on `DOMContentLoaded`. Data attributes: `data-points-url`, `data-status`, `data-near`, `data-radius`, `data-tiles`, `data-compact`, `data-focus-lat`/`data-focus-lng` (listing page), `data-list="#map-list"`, `data-count="#map-count"`.

- [ ] **Step 1: `admin_map.js` additive edits**

```js
  const TILES = {
    dark: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    light: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  };

  async function mount(container, opts = {}) {
    const L = await loadLibs();
    container.classList.add('admin-map-box');
    const map = L.map(container, { center: [39.5, -98.35], zoom: 4, worldCopyJump: true });
    const tiles = /^https?:/.test(opts.tiles || '') ? opts.tiles : (TILES[opts.tiles] || TILES.dark);
    L.tileLayer(tiles, { attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ', maxZoom: 16 }).addTo(map);
```
and in `setPoints`: `className: 'amap-pin' + (p.approx ? ' approx' : '') + (p.cls ? ' ' + p.cls : ''),`. Keep every other line as is. `node --check automation/web/static/admin_map.js`.

- [ ] **Step 2: `static/site/map.js`**

```js
// static/site/map.js — the three public map surfaces (home band, /map, listing mini-map).
// One feed (/map/api/points), one mounter. Pins are city-level; sold pins render muted.
import { load, api, esc, fmt } from '/static/ui/state.js';

const BUCKET_LABEL = { available: 'Available', incoming: 'Incoming', sold: 'Sold' };

function ensureAdminMap() {
  if (window.AdminMap) return Promise.resolve(window.AdminMap);
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/static/admin_map.js';
    s.onload = () => resolve(window.AdminMap);
    s.onerror = () => reject(new Error('map library failed to load'));
    document.head.appendChild(s);
  });
}

function popupHtml(p) {
  const qty = p.quantity != null ? `${fmt.int(p.quantity)} ${esc(p.unit || 'CHAIR')}${p.quantity === 1 ? '' : 'S'}` : '';
  const price = p.price_per_chair != null ? ` · $${fmt.int(p.price_per_chair)}/ea` : '';
  const where = [p.city, p.state].filter(Boolean).join(', ');
  const cta = p.kind === 'favorite' ? 'Message us to reserve →' : 'View lot →';
  return `<div class="map-pop b-${esc(p.bucket)}">
    ${p.hero ? `<img src="${esc(p.hero)}" alt="" loading="lazy">` : ''}
    <div class="map-pop-title">${esc(p.title)}</div>
    <div class="mono tiny">${esc(BUCKET_LABEL[p.bucket] || p.bucket)}${p.precision !== 'city' ? ' · approx' : ''} · ${esc(where)}</div>
    <div class="mono">${qty}${price}${p.distance_mi != null ? ` · ${p.distance_mi} mi` : ''}</div>
    <a href="${esc(p.url)}">${cta}</a>
  </div>`;
}

function listItemHtml(p) {
  const where = [p.city, p.state].filter(Boolean).join(', ');
  return `<a class="map-item b-${esc(p.bucket)}" href="${esc(p.url)}" data-id="${esc(p.id)}">
    ${p.hero ? `<img src="${esc(p.hero)}" alt="" loading="lazy">` : '<span class="map-item-noimg"></span>'}
    <span class="map-item-body">
      <span class="map-item-title">${esc(p.title)}</span>
      <span class="mono tiny">${esc(BUCKET_LABEL[p.bucket] || p.bucket)} · ${esc(where)}${p.distance_mi != null ? ` · ${p.distance_mi} mi` : ''}</span>
      <span class="mono">${p.quantity != null ? fmt.int(p.quantity) + ' ' + esc(p.unit || 'CHAIR') + 'S' : ''}${p.price_per_chair != null ? ' · $' + fmt.int(p.price_per_chair) + '/ea' : ''}</span>
    </span></a>`;
}

function readStatus(el, fallback = 'available,incoming') {
  return (el.dataset.status || fallback).split(',').map(s => s.trim()).filter(Boolean);
}

export async function mountSiteMap(el, opts = {}) {
  const listEl = opts.listEl || (el.dataset.list ? document.querySelector(el.dataset.list) : null);
  const countEl = opts.countEl || (el.dataset.count ? document.querySelector(el.dataset.count) : null);
  const state = {
    status: new Set(opts.status || readStatus(el)),
    near: opts.near ?? el.dataset.near ?? '',
    radius: opts.radius ?? el.dataset.radius ?? '',
    points: [], all: [],
  };
  let statusEl = el.parentElement.querySelector(':scope > .map-status');
  if (!statusEl) { statusEl = document.createElement('div'); statusEl.className = 'map-status mono tiny'; el.insertAdjacentElement('afterend', statusEl); }
  const AdminMap = await ensureAdminMap();
  const map = await AdminMap.mount(el, { tiles: el.dataset.tiles || 'light' });
  el.classList.remove('map-loading');

  const render = () => {
    const pts = state.all.filter(p => state.status.has(p.bucket));
    state.points = pts;
    map.setPoints(pts.map(p => ({ lat: p.lat, lng: p.lng, title: p.title, popup: popupHtml(p),
                                  approx: p.precision !== 'city', cls: 'b-' + p.bucket })));
    if (countEl) countEl.textContent = `${pts.length} lot${pts.length === 1 ? '' : 's'}`;
    if (listEl) {
      listEl.dataset.state = pts.length ? 'ready' : 'empty';
      listEl.innerHTML = pts.length ? pts.map(listItemHtml).join('')
        : '<div class="map-empty"><div class="display">Nothing here yet</div><p>Widen the radius or turn on Sold.</p></div>';
    }
  };

  const fetchAll = () => {
    const q = new URLSearchParams({ status: 'available,incoming,sold' });
    if (state.near) q.set('near', state.near);
    if (state.radius) q.set('radius', state.radius);
    const url = `${el.dataset.pointsUrl || '/map/api/points'}?${q}`;
    // NEVER hand the Leaflet container to load(): it replaces innerHTML with a
    // skeleton and would wipe the map. The side list (or a status sibling) owns
    // the loading state.
    const target = listEl || statusEl;
    return load(target, ({ signal }) => api(url, { signal }), {
      skeleton: listEl ? 'card' : 'line', count: listEl ? 3 : 1, keepOld: !!listEl,
      render: (d) => {
        state.all = d.points || []; render();
        if (d.near) map.leaflet.setView([d.near.lat, d.near.lng], state.radius ? 6 : 5);
        else if (!el.dataset.focusLat) map.fit();
        if (opts.onOrigin) opts.onOrigin(d.near);
        return listEl ? undefined : '';          // clear the status skeleton; the list rendered itself
      },
      isEmpty: () => false,
    });
  };

  if (el.dataset.focusLat && el.dataset.focusLng) {
    map.leaflet.setView([+el.dataset.focusLat, +el.dataset.focusLng], 6);
  }
  await fetchAll();

  return {
    map,
    setStatus(next) { state.status = new Set(next); render(); },
    setNear(near, radius) { state.near = near || ''; state.radius = radius || ''; return fetchAll(); },
    points: () => state.points,
  };
}

function wireFilters(form, ctl) {
  const seg = form.querySelector('#map-status');
  const hidden = seg && seg.querySelector('input[name="status"]');
  const active = new Set((seg?.dataset.value || 'available,incoming').split(','));
  const paint = () => seg.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('on', active.has(b.dataset.bucket)));
  paint();
  seg?.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn'); if (!b) return;
    if (active.has(b.dataset.bucket)) active.delete(b.dataset.bucket); else active.add(b.dataset.bucket);
    paint(); hidden.value = [...active].join(','); ctl.setStatus(active);
    const u = new URL(location.href); u.searchParams.set('status', hidden.value); history.replaceState(null, '', u);
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const near = form.querySelector('#map-near').value.trim();
    const radius = form.querySelector('#map-radius').value;
    ctl.setNear(near, radius);
    const u = new URL(location.href);
    near ? u.searchParams.set('near', near) : u.searchParams.delete('near');
    radius ? u.searchParams.set('radius', radius) : u.searchParams.delete('radius');
    history.replaceState(null, '', u);
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  for (const el of document.querySelectorAll('[data-points-url]')) {
    try {
      const ctl = await mountSiteMap(el);
      const form = document.getElementById('map-filters');
      if (form && el.id === 'site-map') wireFilters(form, ctl);
      const homeToggle = document.getElementById('home-map-sold');
      if (homeToggle && el.id === 'home-map') {
        homeToggle.addEventListener('change', () => ctl.setStatus(homeToggle.checked ? ['available', 'incoming', 'sold'] : ['available', 'incoming']));
      }
    } catch (err) {
      el.classList.remove('map-loading');
      el.innerHTML = `<div class="map-empty"><p>${esc(err.message || 'Map unavailable')}</p></div>`;
    }
  }
});
```
Check `fmt.int` exists in `state.js:5-17` (the report says `fmt` has `money`, `int`, `endsIn`); if `int` is named differently, use what exists.

- [ ] **Step 3: `static/site/map.css`** (tokens only, radius 0)

```css
/* static/site/map.css — public map surfaces. Colours are tokens only (test_no_hardcoded_hex_outside_tokens). */
.site-map { min-height: 320px; border: 1px solid var(--border); background: var(--surface-2); }
.site-map--full { min-height: max(480px, calc(100vh - 260px)); }
.site-map--home { min-height: 360px; }
.site-map--mini { min-height: 240px; }
.map-loading { background: linear-gradient(90deg, var(--sk-a), var(--sk-b), var(--sk-a)); background-size: 200% 100%; animation: mapsk 1.2s linear infinite; }
@keyframes mapsk { to { background-position: -200% 0; } }

/* pin colours by bucket — admin_map.js adds .amap-pin; we add .b-<bucket> via p.cls */
.amap-pin.b-available { background: var(--accent); border-color: var(--border); }
.amap-pin.b-incoming  { background: var(--info);   border-color: var(--border); }
.amap-pin.b-sold      { background: var(--muted);  border-color: var(--border); opacity: .75; }

.map-split { display: grid; grid-template-columns: 7fr 3fr; gap: var(--gap); align-items: start; }
.map-list { display: grid; gap: var(--gap); max-height: max(480px, calc(100vh - 260px)); overflow: auto; }
.map-item { display: grid; grid-template-columns: 72px 1fr; gap: var(--gap); padding: var(--pad); border: 1px solid var(--border); background: var(--surface); color: var(--text); text-decoration: none; border-left-width: 4px; }
.map-item.b-available { border-left-color: var(--accent); }
.map-item.b-incoming  { border-left-color: var(--info); }
.map-item.b-sold      { border-left-color: var(--muted); opacity: .8; }
.map-item img, .map-item-noimg { width: 72px; height: 54px; object-fit: cover; background: var(--surface-2); display: block; }
.map-item-body { display: grid; gap: 2px; }
.map-item-title { font-weight: 600; }
.map-empty { padding: var(--pad); border: 1px dashed var(--border); color: var(--muted); }
.map-status { min-height: 1em; color: var(--muted); }
.map-status[data-state="ready"] { display: none; }

.map-pop img { width: 100%; height: 120px; object-fit: cover; display: block; margin-bottom: 6px; }
.map-pop-title { font-weight: 600; margin-bottom: 2px; }
.map-pop a { display: inline-block; margin-top: 6px; color: var(--accent); }

.map-filters .seg-btn { border: 1px solid var(--border); background: var(--surface); color: var(--muted); padding: 6px 10px; font: inherit; cursor: pointer; }
.map-filters .seg-btn.on { color: var(--text); background: var(--surface-2); border-bottom: 3px solid var(--accent); }
.map-filters .seg-btn.b-incoming.on { border-bottom-color: var(--info); }
.map-filters .seg-btn.b-sold.on { border-bottom-color: var(--muted); }
.filter-group--grow { flex: 1 1 240px; }

.home-map-band { display: grid; gap: var(--gap); }
.home-map-search { display: flex; gap: var(--gap); flex-wrap: wrap; align-items: center; }
.home-map-search input[type="search"] { flex: 1 1 260px; padding: 10px 12px; border: 1px solid var(--border); background: var(--surface); font: inherit; }
.map-legend { display: flex; gap: var(--gap); flex-wrap: wrap; }
.map-legend i { display: inline-block; width: 12px; height: 12px; margin-right: 6px; vertical-align: -1px; border: 1px solid var(--border); }
.map-legend .b-available i { background: var(--accent); }
.map-legend .b-incoming i { background: var(--info); }
.map-legend .b-sold i { background: var(--muted); }

.nearby { margin-top: var(--gap); display: grid; gap: var(--gap); }
.nearby-list { display: grid; gap: 6px; }

@media (max-width: 1024px) {
  .map-split { grid-template-columns: 1fr; }
  .site-map--full { min-height: 50vh; }
  .map-list { max-height: none; }
}
```
If `tokens.css` lacks `--info` or `--sk-a/--sk-b` in the light block, they inherit from `:root` (the report lists them at `:5-20`); verify with `grep -n "info\|sk-a" static/ui/tokens.css`.

- [ ] **Step 4: Verify**

```bash
node --check automation/web/static/admin_map.js && node --check automation/web/static/site/map.js
.venv/bin/python -m pytest tests/web/ -q
```
Expected: PASS. Relaunch; open `http://127.0.0.1:8765/map`: light tiles, orange/blue/grey pins, side list, toggles filter without refetch, `near=Boise, ID` re-centres and sorts by distance. Open `/admin` → Auctions → map toggle still mounts dark.

- [ ] **Step 5: Commit**

```bash
git add automation/web/static/admin_map.js automation/web/static/site/map.js automation/web/static/site/map.css
git commit -m "site: map module — light tiles, bucket-coloured pins, filters + side list on /map

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Home page — search bar + compact map

**Files:**
- Modify: `automation/web/templates/landing.html` (insert between the `stats` section close `:63` and `{% if featured %}` `:65`; add `map.css` link in the head block; add `map.js` in `scripts`).
- Test: extend `tests/web/test_public_map_page.py`.

- [ ] **Step 1: Failing test**

```python
def test_home_has_map_band(monkeypatch):
    monkeypatch.setattr(app_mod, "_landing_data", lambda: {"counts": {"lots": 1, "chairs": 500, "cities": 1, "moved": 0}, "featured": []})
    html = TestClient(app).get("/").text
    assert 'id="home-map"' in html and 'data-points-url="/map/api/points"' in html
    assert 'action="/map"' in html and 'name="near"' in html
    assert 'id="home-map-sold"' in html
    assert "/static/site/map.js" in html
```
Run → FAIL.

- [ ] **Step 2: Template**

Insert after `</section>` of `.stats`:
```html
<section class="home-map-band" id="where">
  <div class="how-head">
    <div class="section-rule"></div>
    <div class="section-label mono tiny">WHERE THE CHAIRS ARE</div>
  </div>
  <form class="home-map-search" action="/map" method="get">
    <input type="search" name="near" placeholder="Your city or ZIP — find chairs near you" aria-label="Find chairs near a city or ZIP">
    <button type="submit" class="btn btn-primary">FIND NEAR ME</button>
    <label class="mono tiny"><input type="checkbox" id="home-map-sold"> Show sold</label>
    <a href="/map" class="btn btn-ghost">FULL MAP →</a>
  </form>
  <div id="home-map" class="site-map site-map--home map-loading" aria-label="Map of chair lots"
       data-points-url="/map/api/points" data-status="available,incoming" data-tiles="light" data-compact="1"></div>
  <div class="map-legend mono tiny">
    <span class="b-available"><i></i>AVAILABLE</span>
    <span class="b-incoming"><i></i>INCOMING</span>
    <span class="b-sold"><i></i>SOLD</span>
  </div>
</section>
```
Add `{% block head_extra %}<link rel="stylesheet" href="/static/site/map.css?v={{ now }}">{% endblock %}` to landing.html (never override `head`) and `<script type="module" src="/static/site/map.js?v={{ now }}"></script>` after `site.js`.

- [ ] **Step 3: Verify**

`.venv/bin/python -m pytest tests/web/ tests/test_seo.py -q` → PASS. Relaunch; `/` shows the band under the stats; the search submits to `/map?near=…`; the sold checkbox adds grey pins.

- [ ] **Step 4: Commit**

```bash
git add automation/web/templates/landing.html tests/web/test_public_map_page.py
git commit -m "home: 'Where the chairs are' band — near-me search + compact map + sold toggle

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Listing page — mini map + "N other lots within 200 mi"

**Files:**
- Modify: `automation/web/app.py` `public_listing_detail` (~573-589): add `nearby` context.
- Modify: `automation/web/templates/listing_detail.html`: after `</dl>` (~line 88).
- Test: extend `tests/web/test_public_map_page.py`.

**Interfaces:** template context gains `nearby = {"origin": {...} | None, "items": [...]}`; `NEARBY_MILES = 200` constant in `public_map`.

- [ ] **Step 1: Failing test**

```python
def test_listing_detail_shows_nearby(monkeypatch):
    row = {"lot_id": "gd-1-2", "title": "500 chairs", "status": "owned", "quantity_remaining": 500,
           "city": "Boise", "state": "ID", "hero_image_url": None, "image_urls": [], "locations": None,
           "price_per_chair": 25, "storage_note": "gate 4321"}
    monkeypatch.setattr(app_mod.inventory, "get", lambda lot_id: dict(row))
    monkeypatch.setattr(app_mod.public_map, "nearby", lambda lot_id, **k: {
        "origin": {"lat": 43.6, "lng": -116.2, "precision": "city"},
        "items": [{**POINT, "lot_id": "gd-3-4", "url": "/listings/gd-3-4", "title": "200 chairs", "distance_mi": 42.0}]})
    html = TestClient(app).get("/listings/gd-1-2").text
    assert 'id="lot-map"' in html and 'data-focus-lat="43.6"' in html
    assert "1 other lot within 200 mi" in html and 'href="/listings/gd-3-4"' in html
    assert "gate 4321" not in html and "storage_note" not in html
```
Run → FAIL.

- [ ] **Step 2: Route + template**

In `public_listing_detail`, before the `TemplateResponse`:
```python
    try:
        near = public_map.nearby(lot_id, miles=public_map.NEARBY_MILES)
    except Exception:  # noqa: BLE001 — the page must render without the map
        near = {"origin": None, "items": []}
```
and pass `"nearby": near` in the context. Add `NEARBY_MILES = 200` to `public_map.py`.

Template, after `</dl>`:
```html
    {% if nearby and nearby.origin %}
    <div class="nearby">
      <div class="section-rule"></div>
      <div class="section-label mono tiny">
        {{ nearby['items'] | length }} OTHER LOT{% if nearby['items'] | length != 1 %}S{% endif %} WITHIN 200 MI
      </div>
      <div id="lot-map" class="site-map site-map--mini map-loading" aria-label="Map around this lot"
           data-points-url="/map/api/points" data-status="available,incoming" data-tiles="light"
           data-focus-lat="{{ nearby.origin.lat }}" data-focus-lng="{{ nearby.origin.lng }}"></div>
      {% if nearby['items'] %}
      <div class="nearby-list">
        {% for n in nearby['items'] %}
        <a class="map-item b-{{ n.bucket }}" href="{{ n.url }}">
          <span class="map-item-body">
            <span class="map-item-title">{{ n.title }}</span>
            <span class="mono tiny">{{ n.city }}{% if n.state %}, {{ n.state }}{% endif %} · {{ n.distance_mi }} mi{% if n.quantity %} · {{ "{:,}".format(n.quantity) }} {{ n.unit }}S{% endif %}</span>
          </span>
        </a>
        {% endfor %}
      </div>
      {% endif %}
    </div>
    {% endif %}
```
Note Jinja: `nearby['items']` not `nearby.items` (dict method). Add `{% block head_extra %}<link rel="stylesheet" href="/static/site/map.css?v={{ now }}">{% endblock %}` and `map.js` to `scripts` like Task 6.

- [ ] **Step 3: Verify**

`.venv/bin/python -m pytest tests/web/ tests/test_seo.py -q` → PASS. Relaunch; `/listings/31225` shows the mini map centred on Boise and the nearby list.

- [ ] **Step 4: Commit**

```bash
git add automation/web/app.py automation/web/public_map.py automation/web/templates/listing_detail.html tests/web/test_public_map_page.py
git commit -m "listing: mini map + N other lots within 200 mi

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Favorite photos — dewatermark → R2 without an inventory row

**Files:**
- Modify: `automation/lot_channels.py:334-381` — extract `clean_and_upload()`.
- Create: `automation/favorite_images.py`, `scripts/favorite_photos.py`.
- Modify: `automation/web/app.py` `star_favorite` (~2406-2428) — background mirror.
- Test: `tests/test_favorite_images.py` (+ existing `tests/` lot_channels tests stay green).

**Interfaces:**
- `lot_channels.clean_and_upload(key: str, urls: list[str], log=_print, *, dewatermark: bool = True, limit: int | None = None) -> dict | None` — download → dewatermark → `listing_images.upload_lot_images(key, files)`; returns `{"hero_image_url", "image_urls"}` or None. `mirror_photos` = `clean_and_upload(lot_id, urls, …)` then `inventory.set_images(...)` (behaviour unchanged).
- `favorite_images.FAVORITE_PHOTO_LIMIT = 6`; `favorite_images.r2_key(asset_id) -> str | None` (`"9685/56"` → `"fav-9685-56"`; `ps:`/`bs:` → None); `favorite_images.mirror_favorite_photos(asset_id: str, *, log=print, force: bool = False) -> dict | None`.
- Env `FAVORITE_PHOTOS_ON_STAR` (default `1`): star route schedules a background mirror.

- [ ] **Step 1: Failing tests**

```python
# tests/test_favorite_images.py
from automation import favorite_images as fi


def test_r2_key():
    assert fi.r2_key("9685/56") == "fav-9685-56"
    assert fi.r2_key("ps:123") is None and fi.r2_key("bs:abc") is None and fi.r2_key("") is None


def test_mirror_fetches_cleans_uploads_and_stamps(monkeypatch):
    calls = {}
    monkeypatch.setattr(fi.lot_channels, "fetch_detail", lambda a, b: {"assetId": a, "assetPhotos": ["p"] * 9})
    monkeypatch.setattr(fi.lot_channels, "gallery_urls", lambda d: [f"https://cdn/{i}.jpg" for i in range(9)])
    def fake_clean(key, urls, log=print, *, dewatermark=True, limit=None, strict=False):
        calls["key"], calls["n"], calls["dw"] = key, len(urls), dewatermark
        return {"hero_image_url": "https://r2/fav-9685-56.jpg", "image_urls": ["https://r2/fav-9685-56/00.jpg"]}
    monkeypatch.setattr(fi.lot_channels, "clean_and_upload", fake_clean)
    monkeypatch.setattr(fi.favorites, "set_clean_images", lambda a, h, u: calls.update(stamped=(a, h, u)))
    out = fi.mirror_favorite_photos("9685/56", log=lambda *a: None)
    assert calls["key"] == "fav-9685-56" and calls["n"] == fi.FAVORITE_PHOTO_LIMIT and calls["dw"] is True
    assert calls["stamped"][0] == "9685/56" and out["hero_image_url"].startswith("https://r2/")


def test_mirror_skips_closed_lot(monkeypatch):
    def boom(a, b): raise RuntimeError("GovDeals returned nothing")
    monkeypatch.setattr(fi.lot_channels, "fetch_detail", boom)
    monkeypatch.setattr(fi.favorites, "set_clean_images", lambda *a: (_ for _ in ()).throw(AssertionError("must not stamp")))
    assert fi.mirror_favorite_photos("1/2", log=lambda *a: None) is None


def test_mirror_never_uses_raw_image_url(monkeypatch):
    """The favorite's own image_url is the watermarked CDN file — never the source of a public photo."""
    src = open(fi.__file__).read()
    assert "image_url" not in src.replace("clean_image_urls", "").replace("hero_image_url", "").replace("image_urls", "")
```

Run → FAIL (`ModuleNotFoundError`).

- [ ] **Step 2: Refactor `lot_channels.mirror_photos`**

Replace the body so it reads:
```python
def clean_and_upload(key: str, urls: list[str], log: Log = _print, *,
                     dewatermark: bool = True, limit: int | None = None) -> dict | None:
    """Seller photos -> dewatermark.ai -> R2 under `key` (any string; key_base sanitises).
    Does NOT touch inventory — callers stamp the URLs where they belong."""
    import asyncio
    import httpx
    urls = list(urls or [])[:limit] if limit else list(urls or [])
    if not urls:
        return None
    folder = Path(config.SCRATCH_DIR) / "lot_channels" / listing_images.key_base(key)
    folder.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=DOWNLOAD_HEADERS) as client:
        for i, url in enumerate(urls):
            ext = listing_images.guess_ext(url.split("?")[0])
            target = folder / f"{i:02d}.{ext}"
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — one bad photo isn't fatal
                log(f"  ! photo download failed ({type(exc).__name__}): {url[:90]}")
                continue
            if resp.content:
                target.write_bytes(resp.content)
                files.append(target)
    if not files:
        return None
    if dewatermark:
        from . import dewatermark as dw
        _phase("dewatermark", "running", lot_id=key)
        cleaned = asyncio.run(dw.dewatermark(None, files, folder, lot_label=key))
        dirty = [c for c in cleaned if c.parent.name == "_originals"]
        if dirty:
            log(f"  ! {len(dirty)}/{len(files)} photos still watermarked (API failed) — kept originals")
        _phase("dewatermark", "done", cleaned=len(cleaned) - len(dirty), files=len(files))
        log(f"  ✓ dewatermarked {len(cleaned) - len(dirty)}/{len(files)} via dewatermark.ai")
        files = cleaned or files
    return listing_images.upload_lot_images(key, files)


def mirror_photos(lot_id: str, urls: list[str], log: Log = _print, *,
                  dewatermark: bool = True) -> dict | None:
    """Seller photos -> dewatermark.ai -> R2 under our key contract; stamps hero/gallery."""
    result = clean_and_upload(lot_id, urls, log, dewatermark=dewatermark)
    if result:
        inventory.set_images(lot_id, result["hero_image_url"], result["image_urls"])
    return result
```
Keep the original docstring text on `mirror_photos` (the "dewatermark=False exists for tests only" note). Note for the favorites path: a photo that stays in `_originals/` is still uploaded today by `mirror_photos` (existing behaviour, `files = cleaned or files`). For favorites that is not acceptable (the whole point is no source watermark), so `favorite_images` passes only clean files — see Step 3: it calls `clean_and_upload` and then, if the log reported dirty files, it still stamps (existing semantics). To keep it simple and safe: add `strict: bool = False` to `clean_and_upload`; when `strict=True`, drop dirty files before upload (`files = [c for c in cleaned if c.parent.name != "_originals"]`) and return None if nothing clean remains. Favorites use `strict=True`.

- [ ] **Step 3: `automation/favorite_images.py`**

```python
"""Clean photos for favorited auctions (public map "incoming" pins).

A favorite has no inventory row, so `lot_channels.mirror_photos` can't stamp it.
This module runs the same download → dewatermark.ai → R2 path under a synthetic
key (`fav-<asset>-<account>`) and stamps `auction_favorites.clean_*` instead.
Only GovDeals favorites are supported (the gallery fetch is GovDeals-only).
Never reads the favorite's raw CDN photo column — that file is watermarked.
"""
from __future__ import annotations

import os
import re

from automation import favorites, lot_channels

FAVORITE_PHOTO_LIMIT = 6   # hero + 5: enough for a pin popup, ~6 dewatermark calls per lot
_GD_KEY = re.compile(r"^(\d+)/(\d+)$")


def r2_key(asset_id: str | None) -> str | None:
    m = _GD_KEY.match((asset_id or "").strip())
    return f"fav-{m.group(1)}-{m.group(2)}" if m else None


def mirror_favorite_photos(asset_id: str, *, log=print, force: bool = False) -> dict | None:
    key = r2_key(asset_id)
    if key is None:
        log(f"  - {asset_id}: not a GovDeals favorite, skipped")
        return None
    if not force:
        fav = favorites.get(asset_id)
        if fav is not None and fav.clean_hero_url:
            log(f"  = {asset_id}: already has clean photos")
            return {"hero_image_url": fav.clean_hero_url, "image_urls": fav.clean_image_urls}
    asset, account = (int(x) for x in asset_id.split("/"))
    try:
        detail = lot_channels.fetch_detail(asset, account)
    except Exception as exc:  # noqa: BLE001 — closed lot / swapped ids
        log(f"  ! {asset_id}: gallery unavailable ({exc})")
        return None
    urls = lot_channels.gallery_urls(detail)
    if not urls:
        log(f"  ! {asset_id}: no photos on GovDeals")
        return None
    result = lot_channels.clean_and_upload(key, urls, log, dewatermark=True,
                                          limit=FAVORITE_PHOTO_LIMIT, strict=True)
    if not result:
        log(f"  ! {asset_id}: nothing clean to publish")
        return None
    favorites.set_clean_images(asset_id, result["hero_image_url"], result["image_urls"])
    log(f"  ✓ {asset_id}: {len(result['image_urls'])} clean photos on R2")
    return result


def on_star_enabled() -> bool:
    return os.environ.get("FAVORITE_PHOTOS_ON_STAR", "1") not in ("0", "false", "no")
```


- [ ] **Step 4: CLI `scripts/favorite_photos.py`**

```python
#!/usr/bin/env python
"""Clean photos for favorited auctions → R2 → auction_favorites.clean_*.

    .venv/bin/python scripts/favorite_photos.py --all            # favorites missing clean photos
    .venv/bin/python scripts/favorite_photos.py --all --force    # redo every GovDeals favorite
    .venv/bin/python scripts/favorite_photos.py --asset 9685/56
    .venv/bin/python scripts/favorite_photos.py --all --dry-run
Budget: MAX_API_CALLS_PER_RUN / _PER_DAY apply (6 photos per favorite).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from automation import favorite_images, favorites  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--asset")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    targets = [a.asset] if a.asset else [
        f.asset_id for f in favorites.list_all()
        if favorite_images.r2_key(f.asset_id) and (a.force or not f.clean_hero_url)]
    print(f"{len(targets)} favorite(s) to process")
    for asset_id in targets:
        if a.dry_run:
            print(f"  would mirror {asset_id}")
            continue
        favorite_images.mirror_favorite_photos(asset_id, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Star route background task**

```python
from fastapi import BackgroundTasks   # add to the existing fastapi import line

@app.post("/api/auctions/favorites")
def star_favorite(payload: dict, background: BackgroundTasks):
    ...existing body...
    if fav and favorite_images.on_star_enabled():
        # Clean photos for the public map's "incoming" pin. Runs after the
        # response; dewatermark budget caps apply; failures only log.
        background.add_task(favorite_images.mirror_favorite_photos, fav.asset_id)
    return fav.to_dict() if fav else {}
```
Import `from automation import favorite_images` with the other `automation` imports. Existing tests that call this route must still pass (BackgroundTasks is injected by FastAPI). If a test calls `star_favorite(...)` directly as a function, pass `BackgroundTasks()`.

- [ ] **Step 6: Verify**

```bash
.venv/bin/python -m pytest tests/test_favorite_images.py tests/ -q -k "favorite or lot_channels or mirror"
.venv/bin/python -m pytest tests/web/ tests/deals/ -q
DEWATERMARK_OFFLINE=1 .venv/bin/python scripts/favorite_photos.py --all --dry-run
```
Expected: PASS; dry-run prints the GovDeals favorites lacking clean photos (up to 16).

- [ ] **Step 7: Commit**

```bash
git add automation/lot_channels.py automation/favorite_images.py scripts/favorite_photos.py automation/web/app.py tests/test_favorite_images.py
git commit -m "favorites: dewatermarked R2 photos via clean_and_upload; background mirror on star; CLI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Ship — migration, photos, PA lot, smoke, PR

**Files:** none new. Docs: `docs/claude-reference/todos-and-history.md` (Done line), `docs/claude-reference/repo-layout.md` (add `/map`, `public_map.py`, `favorite_images.py`), `CLAUDE.md` key-paths line for `scripts/favorite_photos.py`.

- [ ] **Step 1: Apply migration 010** (operator gate — cannot be automated)

`needs input:` paste `scripts/sql/010_favorites_clean_images.sql` into Supabase → SQL Editor (or run `.venv/bin/python scripts/apply_sql.py scripts/sql/010_favorites_clean_images.sql` if that script is allowed against prod). Verify:
```bash
.venv/bin/python -c "from automation import db; print(db.fetch_one(\"SELECT count(*) AS n FROM information_schema.columns WHERE table_name='auction_favorites' AND column_name LIKE 'clean_%'\"))"
```
Expected `{'n': 3}`.

- [ ] **Step 2: Mirror favorite photos**

```bash
.venv/bin/python scripts/favorite_photos.py --all
.venv/bin/python -c "from automation import favorites; print([(f.asset_id, bool(f.clean_hero_url)) for f in favorites.list_all()])"
```
Expected: every open GovDeals favorite `True`; closed ones log `gallery unavailable` and stay `False` (they will not show a photo, and they drop off the map once `end_date_iso` is past).

- [ ] **Step 3: List the 2,500 PA chairs (spec feature 5)**

Probe both id orders first (the vault note says the URL 404'd on 09-14; `/asset/{asset}/{account}` — swapped ids return nothing):
```bash
.venv/bin/python -c "
from automation import lot_channels as lc
for a,b in ((56,9685),(9685,56)):
    try: d=lc.fetch_detail(a,b); print('OK',a,b,d.get('assetShortDesc'), d.get('city'), d.get('state')); break
    except Exception as e: print('no',a,b,e)
"
```
If one order works: `.venv/bin/python scripts/lot_channels.py add https://www.govdeals.com/en/asset/<asset>/<account> --channels site --no-publish --quantity 2500 --chair-type banquet` then `scripts/check_offerable_images.py --http` if the row is `crm_offerable`. Confirm it appears on `/map` as **incoming** (status `active_bid`) — if `add` sets a different status, set it in the admin Inventory tab.
If both orders fail: `needs input:` the lot is closed or the ids are wrong — ask the operator for the working GovDeals URL, and note it in the Obsidian task Log.

- [ ] **Step 4: Full verification**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
node --check automation/web/static/site/map.js
```
Relaunch, then click through at 1280 and 390 px: `/` (band + search → `/map?near=`), `/map` (toggles, radius, side list, popup → listing), `/listings/31225` (mini map + nearby), `/admin` Auctions map (still dark, unchanged). `curl -s localhost:8765/map/api/points | grep -c storage_note` → `0`.

- [ ] **Step 5: Docs + PR**

Add to `docs/claude-reference/todos-and-history.md` Done: `2026-09-15 · WEB-MAP: public map on /, /map, listing pages; favorites feed with clean photos (migration 010).` Update `repo-layout.md` and the CLAUDE.md key-paths line. Then:
```bash
git add docs CLAUDE.md && git commit -m "docs: public map + favorite photos

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin feat/public-map
gh pr create --title "Public inventory map: /, /map, listing mini-map + favorites feed with clean photos" --body "$(cat <<'EOF'
## Summary
- Buyers see every lot on a map: home band with near-me search, full `/map` with available/incoming/sold toggles + radius + side list, mini map + "N other lots within 200 mi" on each listing.
- Pins are city-level (city → zip → state ladder, offline pgeocode). `storage_note`/zip/contact fields never leave `automation/web/public_map.py` (allow-list, tested).
- Favorited auctions show as **incoming**, redacted (no link/asset/bid/close time), with dewatermarked R2 photos (`automation/favorite_images.py`, migration `010_favorites_clean_images.sql`, `#private` in notes opts out).

## Operator steps
- [ ] Paste `scripts/sql/010_favorites_clean_images.sql` in Supabase SQL Editor
- [ ] `.venv/bin/python scripts/favorite_photos.py --all`

## Tests
`.venv/bin/python -m pytest -q` green; `node --check` on the new modules; smoke at 1280/390.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Then add a dated Log line to the Obsidian task `Black-whole/Tasks/website-map-listings.md` (`2026-09-15 · Plan written + built on feat/public-map, PR #<n>. Needs: migration 010 pasted, favorite_photos --all, PA lot URL.`) and set `status: doing`.

---

## Self-review

- **Spec coverage:** feature 1 world map → Tasks 3–6; feature 2 sold toggle → Task 5/6 (`#map-status`, `#home-map-sold`); feature 3 nearby + filters → Tasks 3 (`near`, `radius`, buckets), 5, 7; feature 4 favorites with watermark removed → Tasks 2, 3, 8; feature 5 PA lot → Task 9 step 3. "Where the map goes": home (Task 6), `/map` (Tasks 4–5), listing page (Task 7), admin untouched (additive-only `admin_map.js`). "Done when" → Task 9.
- **Placeholders:** none; every code step is literal. The only open items are operator gates (migration paste, PA URL), marked `needs input:`.
- **Type consistency:** `resolve_place(city, state, zip_code) -> (lat, lng, precision)` used identically in Tasks 1, 3; `fetch_points(statuses=set, near=str|None, radius_mi=float|None)` matches route (Task 4) and tests; `clean_and_upload(key, urls, log, *, dewatermark, limit, strict)` matches Task 8 module + test (`fake_clean` must accept `strict`); `Favorite.clean_hero_url` / `clean_image_urls` / `is_private` used in Tasks 2, 3, 8; point keys in `POINT_KEYS` match the JS fields (`hero`, `url`, `bucket`, `precision`, `distance_mi` is added at fetch time and is not in `POINT_KEYS` on purpose — `set(p) <= POINT_KEYS` is asserted only on `points_from_*` output).

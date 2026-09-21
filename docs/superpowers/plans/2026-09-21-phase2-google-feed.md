# Phase 2 — Google Merchant feed + traffic attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a Google Merchant Center product feed at `/catalog/google.csv` from the same `inventory` rows that feed Facebook, so the `google` channel in the Channels tab means something real, and give the operator a zero-code way to see where site visitors come from.

**Architecture:** `automation/google_feed.py` is a pure serializer (dict rows in, CSV out) that reuses the price/image/description helpers already in `automation/catalog_feed.py`. One new FastAPI route, public and unauthenticated like the FB feed. No DB changes, no sync-loop changes (the sync loop already treats `google` as a feed channel). Traffic attribution is Cloudflare Web Analytics (operator toggle, the site is already proxied) plus the UTM tags every feed link already carries.

**Tech Stack:** Python 3.12, FastAPI, csv/io stdlib, pytest with the offline `TestClient` monkeypatch pattern from `tests/web/test_catalog_feed_endpoint.py`.

**Spec:** `docs/superpowers/plans/2026-09-20-multichannel-listing-master.md` §"Phase 2" (D7). Sitemap, Product JSON-LD, and sync-loop Google handling in that section **already exist** on main (`app.py::sitemap_xml`, `app.py` product JSON-LD builder, `sync.py::_apply_feed`) and are NOT rebuilt here; Task 3 only adds a regression test.

## Global Constraints

- Feed is public and read-only, no auth, no secrets (same as `/catalog/facebook.csv`).
- Never emit `inventory.storage_note` anywhere (facility address + gate code).
- Never invent a product fact: no GTIN/MPN (`identifier_exists=no`), no shipping price, no weight, no dimensions.
- Rows with no positive price or no durable (non-Supabase) image are dropped, not shipped broken (same rule as the FB feed).
- Route handler that touches the DB is a plain `def` (FastAPI threadpool). `tests/web/test_event_loop_hygiene.py` enforces it.
- Tests run offline: `.venv/bin/python -m pytest tests/test_google_feed.py tests/web/test_google_feed_endpoint.py tests/test_seo.py -q`.
- Commit per task. Branch `feat/google-feed` off `main`. Push after every task.

## Discovery (read before starting)

| Already on main | Where | Consequence |
|---|---|---|
| FB feed serializer with `_price`, `_image_link`, `_description`, `google_category`, `state_code`, `site_base_url` | `automation/catalog_feed.py` | Import and reuse; do not copy. |
| FB feed link already has UTM (`utm_source=facebook&utm_medium=catalog&utm_campaign=fb_shop`) | `catalog_feed.UTM_QUERY` | Google feed gets its own UTM string, same shape. |
| `inventory.list_catalog_feed()` = status-gated + `quantity_remaining > 0` | `automation/inventory.py:336` | Same SQL feeds both channels. Do not add a second query. |
| `/sitemap.xml`, `/robots.txt` + tests | `app.py:1416-1453`, `tests/test_seo.py` | Done. |
| Product JSON-LD with offers/availability/UsedCondition/availableAtOrFrom | `app.py` ~L470-541, `tests/test_seo.py::test_detail_page_meta_and_jsonld` | Done except the storage-note leak test. |
| `GOOGLE_SITE_VERIFICATION` env → `<meta name="google-site-verification">` | `config.py:190`, `_public_base.html:19` | Merchant Center website verification = set this env on Render. |
| `google` channel marked live by sync loop alongside `fb_catalog` | `channels/sync.py:286` `_apply_feed` | Nothing to do. Channels tab already shows it. |
| Apollo website-visitor pixel on every public page | `_public_base.html:36-50`, tracker id `6986af4f7ce37e001d9745cc`, receiving data | Shows *which companies* visit, not *where traffic comes from*. |
| black-whole.com proxied by Cloudflare (104.21.x / 172.67.x, ns lisa/wells) | DNS | Cloudflare Web Analytics can be switched on with no code. |

---

### Task 1: `automation/google_feed.py` serializer

**Files:**
- Create: `automation/google_feed.py`
- Test: `tests/test_google_feed.py`

**Interfaces:**
- Consumes: `catalog_feed._price(row) -> str|None` ("28.00 USD"), `catalog_feed._image_link(row) -> str|None`, `catalog_feed._description(row, title) -> str`, `catalog_feed.google_category(row) -> str`, `catalog_feed.state_code(value) -> str`, `catalog_feed.site_base_url() -> str`, `catalog_feed.BRAND`, `lot_images.resolve(row).urls`.
- Produces: `FEED_COLUMNS: list[str]`, `UTM_QUERY: str`, `feed_row(row, base_url=None) -> dict|None`, `build_feed_rows(rows, base_url=None) -> list[dict]`, `rows_to_csv(rows, base_url=None) -> str`.

Google Merchant column contract (checked against the Merchant Center product data spec; free listings + Shopping ads share it):

| column | value |
|---|---|
| `id` | `lot_id` |
| `title` | title, ≤150 chars |
| `description` | `catalog_feed._description`, ≤5000 |
| `link` | `{base}/listings/{lot_id}?utm_source=google&utm_medium=feed&utm_campaign=merchant` |
| `image_link` | `catalog_feed._image_link` |
| `additional_image_link` | up to 10 more durable URLs, comma-joined, excluding `image_link` |
| `availability` | `in_stock` (underscore, unlike FB's "in stock") |
| `price` | `"28.00 USD"` |
| `condition` | `used` |
| `brand` | `catalog_feed.BRAND` |
| `identifier_exists` | `no` |
| `google_product_category` | `catalog_feed.google_category(row)` |
| `product_type` | `Seating > Banquet Chairs` or `Tables > Banquet Tables` (from `google_category`) |
| `custom_label_0` | `state_code(state)` |
| `custom_label_1` | city |

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_google_feed.py
"""Offline tests for the Google Merchant Center feed serializer (multichannel Phase 2).

Pure dict fixtures — no DB, no FastAPI, no network. Mirrors tests/test_catalog_feed.py.
"""
import csv
import io

import pytest

from automation import google_feed

R2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev"
BASE = "https://black-whole.com"


def _lot(**over):
    row = {
        "lot_id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "1,200 stackable banquet chairs, brown fabric, metal frame.",
        "price_per_chair": 25.0,
        "quantity_remaining": 1200,
        "status": "won_pickup",
        "city": "Atlanta",
        "state": "Georgia",
        "hero_image_url": f"{R2}/31225-atl/hero.jpg",
        "image_urls": [f"{R2}/31225-atl/hero.jpg", f"{R2}/31225-atl/2.jpg", f"{R2}/31225-atl/3.jpg"],
        "storage_note": "Unit 12, gate code 4455",
    }
    row.update(over)
    return row


def test_columns_match_google_spec():
    assert google_feed.FEED_COLUMNS == [
        "id", "title", "description", "link", "image_link", "additional_image_link",
        "availability", "price", "condition", "brand", "identifier_exists",
        "google_product_category", "product_type", "custom_label_0", "custom_label_1",
    ]


def test_eligible_row_maps_all_columns():
    fr = google_feed.feed_row(_lot(), base_url=BASE)
    assert fr == {
        "id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "1,200 stackable banquet chairs, brown fabric, metal frame.",
        "link": f"{BASE}/listings/31225-atl?{google_feed.UTM_QUERY}",
        "image_link": f"{R2}/31225-atl/hero.jpg",
        "additional_image_link": f"{R2}/31225-atl/2.jpg,{R2}/31225-atl/3.jpg",
        "availability": "in_stock",
        "price": "25.00 USD",
        "condition": "used",
        "brand": "BLACKWHOLE Liquidation",
        "identifier_exists": "no",
        "google_product_category": "Furniture > Chairs",
        "product_type": "Seating > Banquet Chairs",
        "custom_label_0": "GA",
        "custom_label_1": "Atlanta",
    }


def test_utm_marks_google_feed():
    assert google_feed.UTM_QUERY == "utm_source=google&utm_medium=feed&utm_campaign=merchant"


@pytest.mark.parametrize("over", [
    {"price_per_chair": None},
    {"price_per_chair": 0},
    {"hero_image_url": None, "image_urls": []},
    {"hero_image_url": "https://nihgzltpjriekyqqucbd.supabase.co/storage/v1/object/public/x.jpg", "image_urls": []},
    {"title": ""},
])
def test_incomplete_rows_are_dropped(over):
    assert google_feed.feed_row(_lot(**over), base_url=BASE) is None


def test_additional_images_cap_at_ten_and_exclude_hero():
    urls = [f"{R2}/31225-atl/{i}.jpg" for i in range(14)]
    fr = google_feed.feed_row(_lot(hero_image_url=urls[0], image_urls=urls), base_url=BASE)
    extra = fr["additional_image_link"].split(",")
    assert len(extra) == 10
    assert urls[0] not in extra


def test_tables_get_table_product_type():
    fr = google_feed.feed_row(_lot(title="Round Banquet Tables (Augusta, GA)", chair_type="table"), base_url=BASE)
    assert fr["google_product_category"] == google_feed.catalog_feed.GOOGLE_CATEGORY_TABLES
    assert fr["product_type"] == "Tables > Banquet Tables"


def test_title_truncated_to_150():
    fr = google_feed.feed_row(_lot(title="x" * 200), base_url=BASE)
    assert len(fr["title"]) == 150


def test_storage_note_never_appears():
    csv_text = google_feed.rows_to_csv([_lot()], base_url=BASE)
    assert "gate code" not in csv_text
    assert "Unit 12" not in csv_text


def test_csv_has_header_and_eligible_rows_only():
    text = google_feed.rows_to_csv([_lot(), _lot(lot_id="bad", price_per_chair=None)], base_url=BASE)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["id"] for r in rows] == ["31225-atl"]
    assert text.splitlines()[0] == ",".join(google_feed.FEED_COLUMNS)


def test_site_base_url_env_override(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com/")
    fr = google_feed.feed_row(_lot())
    assert fr["link"].startswith("https://staging.example.com/listings/31225-atl?")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_google_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'automation.google_feed'`

- [ ] **Step 3: Write the serializer**

```python
# automation/google_feed.py
"""Google Merchant Center product feed serialization (multichannel Phase 2, D7).

Pure, DB-free rendering of `inventory` rows into the CSV Merchant Center
fetches on a schedule. The SQL that selects sellable lots is the same one the
Facebook feed uses (`inventory.list_catalog_feed()`); this module only decides
the Google column shape and per-row eligibility. Price/image/description
helpers are imported from `catalog_feed` so the two feeds can never disagree
about a lot's price or photo.

Column names follow the Merchant Center product data specification. Google
maps by header name. `availability` is `in_stock` (underscore) — Meta's feed
uses "in stock" with a space; the two are not interchangeable.

Deliberately NOT emitted (no trustworthy value in the ledger, and a guessed
value puts a wrong fact in front of a buyer): gtin, mpn, shipping,
shipping_weight, product_dimensions. `identifier_exists=no` tells Google we
have no GTIN/MPN, which is expected for used liquidation stock.

Operator setup: docs/google_merchant_runbook.md.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from . import catalog_feed, lot_images

FEED_COLUMNS = [
    "id", "title", "description", "link", "image_link", "additional_image_link",
    "availability", "price", "condition", "brand", "identifier_exists",
    "google_product_category", "product_type", "custom_label_0", "custom_label_1",
]

UTM_QUERY = "utm_source=google&utm_medium=feed&utm_campaign=merchant"
AVAILABILITY = "in_stock"
CONDITION = "used"
IDENTIFIER_EXISTS = "no"
PRODUCT_TYPE_CHAIRS = "Seating > Banquet Chairs"
PRODUCT_TYPE_TABLES = "Tables > Banquet Tables"

_TITLE_MAX = 150          # Merchant Center hard limit
_EXTRA_IMAGES_MAX = 10    # additional_image_link limit


def _product_type(category: str) -> str:
    return PRODUCT_TYPE_TABLES if category == catalog_feed.GOOGLE_CATEGORY_TABLES else PRODUCT_TYPE_CHAIRS


def _additional_images(row: dict, hero: str) -> str:
    """Up to 10 durable gallery URLs after the hero, comma-joined (Google's format)."""
    resolved = lot_images.resolve(row)
    extra: list[str] = []
    for url in resolved.urls:
        if not url or url == hero or lot_images.storage_backend(url) == "supabase":
            continue
        if url not in extra:
            extra.append(url)
        if len(extra) == _EXTRA_IMAGES_MAX:
            break
    return ",".join(extra)


def feed_row(row: dict, base_url: str | None = None) -> dict | None:
    """Map one inventory row to a Merchant Center row, or None if Google would reject it."""
    base = (base_url or catalog_feed.site_base_url()).rstrip("/")
    lot_id = catalog_feed._clean(row.get("lot_id"))
    title = catalog_feed._clean(row.get("title"))[:_TITLE_MAX]
    price = catalog_feed._price(row)
    image = catalog_feed._image_link(row)
    if not (lot_id and title and price and image):
        return None
    category = catalog_feed.google_category(row)
    return {
        "id": lot_id,
        "title": title,
        "description": catalog_feed._description(row, title),
        "link": f"{base}/listings/{lot_id}?{UTM_QUERY}",
        "image_link": image,
        "additional_image_link": _additional_images(row, image),
        "availability": AVAILABILITY,
        "price": price,
        "condition": CONDITION,
        "brand": catalog_feed.BRAND,
        "identifier_exists": IDENTIFIER_EXISTS,
        "google_product_category": category,
        "product_type": _product_type(category),
        "custom_label_0": catalog_feed.state_code(row.get("state")),
        "custom_label_1": catalog_feed._clean(row.get("city")),
    }


def build_feed_rows(rows: Iterable[dict], base_url: str | None = None) -> list[dict]:
    base = (base_url or catalog_feed.site_base_url()).rstrip("/")
    return [fr for r in rows if (fr := feed_row(r, base))]


def rows_to_csv(rows: Iterable[dict], base_url: str | None = None) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FEED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(build_feed_rows(rows, base_url))
    return buf.getvalue()
```

If `lot_images.resolve(row).urls` does not read an `image_urls` key in the fixture, open `automation/lot_images.py::resolve` and use whichever column name it reads (the FB test fixture in `tests/test_catalog_feed.py` shows the working keys). Adjust the fixture, not the resolver.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_google_feed.py -q`
Expected: all PASS

- [ ] **Step 5: Commit + push**

```bash
git add automation/google_feed.py tests/test_google_feed.py
git commit -m "google_feed: Merchant Center CSV serializer reusing catalog_feed helpers (Phase 2.1)"
git push -u origin feat/google-feed
```

---

### Task 2: `GET /catalog/google.csv`

**Files:**
- Modify: `automation/web/app.py` (next to `facebook_catalog_feed`, ~L1455)
- Test: `tests/web/test_google_feed_endpoint.py`

**Interfaces:**
- Consumes: `google_feed.rows_to_csv`, `inventory.list_catalog_feed`.
- Produces: route `GET /catalog/google.csv` → `text/csv; charset=utf-8`, `Cache-Control: public, max-age=900`, public even when `ADMIN_PASSWORD` is set.

- [ ] **Step 1: Write the failing tests**

```python
# tests/web/test_google_feed_endpoint.py
"""HTTP tests for GET /catalog/google.csv (multichannel Phase 2).

DB-free: `inventory.list_catalog_feed` is monkeypatched. Mirrors
tests/web/test_catalog_feed_endpoint.py — the feed must stay public so
Merchant Center's scheduled fetch works with no login.
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from automation import google_feed, inventory
from automation.web import auth as auth_svc
from automation.web.app import app

R2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev"


def _lot(**over):
    row = {
        "lot_id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "Bulk used banquet chairs.",
        "price_per_chair": 25.0,
        "quantity_remaining": 1200,
        "status": "won_pickup",
        "city": "Atlanta",
        "state": "GA",
        "hero_image_url": f"{R2}/31225-atl/hero.jpg",
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _clean_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SITE_BASE_URL", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


def test_feed_returns_csv_rows(monkeypatch):
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/google.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "public, max-age=900"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    assert rows[0]["id"] == "31225-atl"
    assert rows[0]["availability"] == "in_stock"
    assert rows[0]["link"].endswith(f"/listings/31225-atl?{google_feed.UTM_QUERY}")


def test_feed_drops_ineligible_rows(monkeypatch):
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot(), _lot(lot_id="x", price_per_chair=None)])
    r = _client().get("/catalog/google.csv")
    assert [row["id"] for row in csv.DictReader(io.StringIO(r.text))] == ["31225-atl"]


def test_feed_is_public_with_admin_auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    auth_svc.reset_caches()
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/google.csv")
    assert r.status_code == 200


def test_feed_uses_site_base_url(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com")
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/google.csv")
    assert "https://staging.example.com/listings/31225-atl?" in r.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/web/test_google_feed_endpoint.py -q`
Expected: FAIL, 404 on `/catalog/google.csv`

- [ ] **Step 3: Add the route**

In `automation/web/app.py`, change the import line `from .. import catalog_feed, lot_channels` to `from .. import catalog_feed, google_feed, lot_channels`, then add directly under `facebook_catalog_feed`:

```python
@app.get("/catalog/google.csv")
def google_catalog_feed():
    """Google Merchant Center product feed (multichannel Phase 2, D7).

    Public, read-only, no secrets — Merchant Center fetches this URL on a
    daily schedule. Same status/quantity gate as the FB feed
    (`inventory.list_catalog_feed`); `google_feed` drops rows Google would
    reject. Operator setup: docs/google_merchant_runbook.md.
    """
    body = google_feed.rows_to_csv(inventory.list_catalog_feed())
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Cache-Control": "public, max-age=900"},
    )
```

Check `tests/web/test_event_loop_hygiene.py` still passes (plain `def`, so it will). Check the auth middleware's public-path allowlist: if `/catalog/facebook.csv` is listed explicitly rather than by prefix, add `/catalog/google.csv` beside it (`grep -n "catalog" automation/web/auth.py`).

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/web/test_google_feed_endpoint.py tests/web/test_event_loop_hygiene.py -q`
Expected: all PASS

- [ ] **Step 5: Commit + push**

```bash
git add automation/web/app.py tests/web/test_google_feed_endpoint.py automation/web/auth.py
git commit -m "web: GET /catalog/google.csv Merchant Center feed, public, 15 min cache (Phase 2.2)"
git push
```

---

### Task 3: JSON-LD storage-note regression test

**Files:**
- Test: `tests/test_seo.py` (append)

The Product JSON-LD builder already exists and never reads `storage_note`. Lock that in so a future "add address to JSON-LD" edit cannot leak the gate code.

- [ ] **Step 1: Write the test**

```python
def test_detail_jsonld_never_leaks_storage_note(client, monkeypatch):
    row = dict(ROW, storage_note="Unit 12, gate code 4455", description="Chairs.")
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: row)
    r = client.get("/listings/10340")
    assert r.status_code == 200
    assert "gate code" not in r.text
    assert "Unit 12" not in r.text
```

- [ ] **Step 2: Run**

Run: `.venv/bin/python -m pytest tests/test_seo.py -q`
Expected: PASS on first run (this is a guard, not a fix). If it FAILS, the detail template is rendering `storage_note` somewhere — that is a HARD RULE violation; find and remove it before continuing.

- [ ] **Step 3: Commit + push**

```bash
git add tests/test_seo.py
git commit -m "seo: guard that listing pages never render storage_note (Phase 2.3)"
git push
```

---

### Task 4: Runbook + docs

**Files:**
- Create: `docs/google_merchant_runbook.md`
- Modify: `docs/claude-reference/channels.md` (google row), `CLAUDE.md` (Key paths line + Channels rule), `docs/seo.md` (Traffic section)

- [ ] **Step 1: Write `docs/google_merchant_runbook.md`**

```markdown
# Google Merchant Center — operator runbook

Feed URL: `https://black-whole.com/catalog/google.csv` (public, refreshes every 15 min from `inventory`).

## One-time setup (≈20 min, operator clicks)
1. merchants.google.com → Create account. Business name "Black Whole Liquidation", country US, address = business address on file. Use the business Google account once it exists; personal is fine to start (rehoming accounts is parked).
2. **Verify website**: Business info → Website → choose "HTML tag". Copy the `content="..."` value. Set `GOOGLE_SITE_VERIFICATION=<value>` in Render → blackwhole-secrets → redeploy. Return to Merchant Center → Verify. (The meta tag renders on every public page when the env is set.)
3. **Shipping**: Shipping & returns → Add shipping service → name "Local pickup / freight quote", country US, rate = flat $0, delivery time 1–14 business days. Google requires *a* shipping entry for listings to show; the site's freight widget quotes the real number. Do not enter a made-up freight price anywhere else.
4. **Add the feed**: Products → Feeds → Add primary feed → country US, language English → name `black-whole inventory` → method **Scheduled fetch** → URL `https://black-whole.com/catalog/google.csv`, fetch daily 06:00 America/Phoenix → Create → Fetch now.
5. **Free listings**: Growth → Manage programs → Free listings → enable. Shopping ads only if a budget is decided later.
6. Wait for "Diagnostics" to go green (up to 3 days). Common warnings and what they mean:
   - `Missing value [shipping]` → step 3 not done.
   - `Invalid value [availability]` → the feed sends `in_stock`; if Google shows this, the fetch hit an old deploy.
   - `Missing value [gtin]` → expected; `identifier_exists=no` suppresses it once processed.
   - `Image too small` → the lot's hero on R2 is under 100×100; redo photos via `/list-lot redo-photos`.

## How it ties into Channels
- The Channels tab `GOOGLE` switch only tells the sync loop to keep recording `google` as live/delisted; the feed itself is always served. Turning the switch OFF does not remove products from Google — pause the feed in Merchant Center for that.
- A lot leaves the feed when `status` leaves `CATALOG_FEED_STATUSES` or `quantity_remaining` hits 0, same as Facebook. Google re-fetches daily, so expect up to 24 h lag; "Fetch now" forces it.

## Checks
- `curl -s https://black-whole.com/catalog/google.csv | head -3`
- Row count should equal the `live` count in the GOOGLE column of `/admin?tab=channels`.
```

- [ ] **Step 2: Add the Traffic section to `docs/seo.md`**

```markdown
## Where visitors come from (traffic attribution)

Two tools, no code:
1. **Cloudflare Web Analytics** (free, cookieless). black-whole.com is already proxied by Cloudflare, so: dash.cloudflare.com → black-whole.com → Analytics & Logs → Web Analytics → Enable. Cloudflare injects the beacon itself. Shows referrers (facebook.com, google.com, craigslist.org, ebay.com), countries, top pages, devices, Core Web Vitals. Data starts within an hour.
2. **UTM tags** on every channel link: FB catalog links carry `utm_source=facebook&utm_medium=catalog`, Google feed links `utm_source=google&utm_medium=feed`. Cloudflare and Apollo both group on these, so a click from a feed is attributed even when the app strips the Referer.

Already installed: **Apollo website-visitor pixel** (`_public_base.html`, tracker `6986af4f7ce37e001d9745cc`) — identifies *which companies* visited (Apollo → Website Visitors). It does not show traffic source; use Cloudflare for that.

Not installed on purpose: Google Analytics (cookie banner + consent work for no extra signal we would act on).
```

- [ ] **Step 3: Update `docs/claude-reference/channels.md`** — in the channel table, google row: "`google` — Merchant Center scheduled fetch of `/catalog/google.csv` (`automation/google_feed.py`); runbook `docs/google_merchant_runbook.md`; switch only affects bookkeeping, pause the feed in MC to actually pull products."

- [ ] **Step 4: Update `CLAUDE.md`** — Key paths: add "`GET /catalog/google.csv` = Merchant Center feed (`automation/google_feed.py`, reuses `catalog_feed` helpers; setup `docs/google_merchant_runbook.md`)". Channels HARD RULE block: add "Both catalog feeds (`facebook.csv`, `google.csv`) read `inventory.list_catalog_feed()` — never add a second sellable-lots query; price/image helpers live in `catalog_feed` and are shared."

- [ ] **Step 5: Commit + push, open PR**

```bash
git add docs/google_merchant_runbook.md docs/seo.md docs/claude-reference/channels.md CLAUDE.md
git commit -m "docs: Google Merchant runbook, traffic attribution section, channels/CLAUDE pointers (Phase 2.4)"
git push
gh pr create --base main --head feat/google-feed --title "Google Merchant Center feed + traffic attribution docs (multichannel Phase 2)" --body "..."
```

Run the whole suite before the PR: `.venv/bin/python -m pytest tests -q -x --ignore=tests/deals` (1 pre-existing env-dependent failure in `tests/web/test_sold_showcase.py` when `STRIPE_SECRET_KEY` is in local `.env` is known).

---

## Operator gates after merge (Abdel)

1. Cloudflare → Web Analytics → Enable (2 clicks, answers "where do visitors come from" from today on).
2. Merchant Center account + verification env + feed URL — runbook steps 1–5.
3. Nothing else. No migration, no backfill, no relaunch beyond the normal deploy.

## Self-review

- **Coverage vs master plan Phase 2:** 2.1 feed builder → Task 1; 2.2 route → Task 2; 2.3 sitemap → already on main (tested in `test_seo.py`); 2.4 JSON-LD → exists, guard added in Task 3; 2.5 sync marks google live → already on main (`_apply_feed`); 2.6 runbook → Task 4. Visitor-source question → Task 4 Step 2 + gate 1.
- **Placeholders:** none; the PR body "..." is the standard summary + test counts.
- **Type consistency:** `feed_row/build_feed_rows/rows_to_csv(rows, base_url=None)` match `catalog_feed`'s signatures; `UTM_QUERY` used identically in Tasks 1, 2, 4.

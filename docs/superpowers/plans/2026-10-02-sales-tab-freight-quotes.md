# Sales Tab + Freight Quote Capture — Implementation Plan

> **For agentic workers:** executed natively in one session (operator said "start building", 2026-10-02). Steps use checkbox (`- [x]`) syntax. One fresh reviewer checks the whole branch at the end.

**Goal:** Every freight request is stored with ZIP, email and phone and shown in one admin Sales tab with real carrier prices beside it.

**Architecture:** The public endpoint validates contact details, runs the pure estimator, and writes one row for every outcome (quoted or unquotable). A background thread asks Warp's multi-carrier feed for prices and writes a small summary on the row. The admin rail gets one Sales tab that groups four existing/new panes; no pane is rewritten.

**Tech stack:** FastAPI, psycopg via `automation/db.py`, stdlib `urllib`, vanilla ES modules, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-sales-tab-freight-quotes-design.md`

## Global Constraints

- Never invent a freight number. An unquotable lane shows no price.
- The price shown to the buyer comes from the estimator only. Warp never drives it.
- `freight_estimate.py` stays stdlib-only with no DB access.
- No handler runs DB or network work on the event loop (`tests/web/test_event_loop_hygiene.py`).
- No full provider response is stored. `carrier_options` holds at most 5 entries.
- Buyer phone and email never render on a public page and never leave through a public response.
- The code must work before migration 021 is applied: quotes still save through the old columns.
- No commit, push or deploy without the operator's word.

## Review Focus

1. **Migration not applied yet** → a quote must still be saved and priced. Test: `test_legacy_schema_insert_keeps_phone_in_raw`.
2. **Warp answers slowly, with an empty list, or with a dedicated-truck substitution** → the buyer's price is untouched and no wrong carrier price is stored. Tests in `tests/test_warp_rates.py`.
3. **Buyer types a phone with punctuation, a country code, or 9 digits** → normalised to 10 digits or rejected with a clear message. Test: `test_phone_normalisation`.
4. **Old cached page posts without email/phone** → `400` with a message the old widget shows as an error, never a silent price. Test: `test_contact_required`.
5. **Operator types in a field and presses `[` or `1`** → no tab change. Guard in `shell.js`, checked by hand.

## File Structure

| File | Change |
|---|---|
| `scripts/sql/021_sales_quotes.sql` | new — columns on `freight_quotes` + `inventory` |
| `automation/freight_log.py` | insert for every outcome, schema check, list / status / carrier writers |
| `automation/warp_rates.py` | new — Warp market-options client + summary |
| `automation/freight_estimate.py` | per-lot calibration; Warp provider removed |
| `automation/inventory.py` | chair fields editable through `set_fields` |
| `automation/web/app.py` | public endpoint, carrier check, admin routes |
| `automation/web/templates/listing_detail.html`, `static/site/site.js` | widget asks ZIP + email + phone first |
| `automation/web/templates/index.html` | Sales rail tab, sub-nav, Quotes pane, chair fields |
| `automation/web/static/admin/shell.js` | Sales group + shortcuts + badge |
| `automation/web/static/admin/quotes.js`, `quotes.css` | new — Quotes view |
| `automation/web/static/admin/inventory.js` | chair fields in the lot editor |
| `tests/…` | see each task |
| `CLAUDE.md`, `docs/claude-reference/*` | rules + runbook lines |

## Tasks

### Task 1 — Migration 021
- [x] `freight_quotes`: `buyer_phone`, `status` (default `new`, CHECK `new|answered|won|lost|junk`), `status_changed_at`, `note`, `unquotable_reason`, `lot_quantity_remaining`, `carrier_status`, `carrier_low`, `carrier_name`, `carrier_count`, `carrier_options`, `carrier_checked_at`; drop NOT NULL on `mode`, `origin_zip`; index `(status, quoted_at DESC)`.
- [x] `inventory`: `chair_weight_lb`, `chair_frame`, `chairs_per_pallet`, `pallet_height_in`.
- [x] Header stamp says PENDING. Idempotent (`IF NOT EXISTS`).

### Task 2 — `freight_log.py`
**Produces:**
- `schema_ready() -> bool` (cached; `reset_schema_cache()` for tests)
- `insert_storefront_quote(*, lot_id, origin_zip, dest_zip, quantity, quote=None, buyer_email=None, buyer_phone=None, client_ip=None, unquotable_reason=None, lot_quantity_remaining=None) -> int | None`
- `get_quote(quote_id) -> dict | None`, `list_quotes(status=None, limit=200) -> list[dict]`, `count_new() -> int`
- `set_quote_status(quote_id, *, status=None, note=None) -> dict | None` (raises `ValueError` on a bad status)
- `set_carrier_result(quote_id, summary: dict) -> bool`
- [x] Tests `tests/test_freight_log.py`: SQL + params for new and legacy schema, unquotable row has NULL mode, legacy schema refuses an unquotable row and returns None, status validation.

### Task 3 — `warp_rates.py`
**Produces:**
- `WarpUnavailable(Exception)`, `MAX_LTL_PALLETS = 12`
- `pallets_for(quantity, chairs_per_pallet) -> int`
- `pickup_date(today=None) -> str` (3+ days out, never a weekend)
- `market_options(origin_zip, dest_zip, *, pallets, weight_lbs_per_pallet, height_in, pickup=None, api_key=None, timeout=60) -> list[dict]`
- `summarize(options, limit=5) -> dict` with `carrier_status`, `carrier_low`, `carrier_name`, `carrier_count`, `carrier_options`
- `price_check(shown_low, shown_high, carrier_low) -> str | None` → `site_low` / `site_high` / `ok`
- [x] Tests `tests/test_warp_rates.py` with canned payloads: body shape, Bearer header only with a key, cheapest pick, substituted and non-positive options dropped, empty list → `none`, HTTP error → `WarpUnavailable`, price-check thresholds.

### Task 4 — `freight_estimate.py`
- [x] `LotCalibration` gains `chairs_per_pallet=35.0`, `pallet_height_in=80`.
- [x] `calibration_from_row(row) -> LotCalibration` reads `chair_weight_lb`, `chairs_per_pallet`, `pallet_height_in`; missing values fall back to the default.
- [x] `get_freight_estimate(..., cal=None)` passes the calibration to the estimator.
- [x] Remove `WarpProvider`, `_CarrierProviderBase`, `_first_number`, `_map_carrier_rates`; `select_provider()` returns the estimator.
- [x] Tests: lot weight changes the range; pinned lanes do not move; old Warp tests replaced by "a key never changes the provider".

### Task 5 — Public endpoint + widget
- [x] `_clean_phone(value) -> str` (10 US digits, raises `ValueError`).
- [x] `POST /freight-estimate`: contact required → estimate → one insert → alert → background carrier check. Responses: `ok` + estimate, `unquotable`, `not_saved`.
- [x] Alert carries email, phone and the over-stock note; Telegram failure is logged.
- [x] Widget: ZIP, chairs, email, phone, one button; ZIP+4 trimmed; messages for each response.
- [x] Tests in `tests/web/test_freight_endpoint.py` and `test_public_ui.py`.

### Task 6 — Admin API
- [x] `GET /api/freight-quotes?status=`, `PATCH /api/freight-quotes/{id}`, `POST /api/freight-quotes/{id}/carrier-check`, `GET /api/sales/counts`.
- [x] Tests `tests/web/test_admin_freight_quotes.py`.

### Task 7 — Sales tab + shortcuts
- [x] Rail: `sales` link replaces `inquiries`, `subscribers`, `deposits`. Panes keep their ids; a new `quotes` pane is added.
- [x] `shell.js`: Sales group (`quotes|inquiries|deposits|subscribers`), sub-nav, last view remembered, badge, `[` `]` digits and `q i d s`.
- [x] `quotes.js` + `quotes.css`.
- [x] Update `tests/web/test_admin_tabs.py` (10 rail tabs) and add `tests/web/test_admin_tab_sales.py`.

### Task 8 — Chair data per lot
- [x] `inventory.set_fields` accepts and validates the four fields.
- [x] Lot editor shows them. Test in `tests/web/test_admin_tab_inventory.py` or `tests/test_inventory_*.py`.

### Task 9 — Docs + whole-suite run + review
- [x] `CLAUDE.md` freight rules, `docs/claude-reference/repo-layout.md`, spec amendments.
- [x] Full `pytest -q`.
- [x] Fresh reviewer on the branch diff (6 confirmed bugs fixed — see the spec, section 10).

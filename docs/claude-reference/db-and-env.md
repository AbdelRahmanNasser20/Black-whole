<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Connecting to the shared DB

Workspace-level decision (see `workspace/CLAUDE.md §14`): all new DB code goes through Supabase Postgres. This repo uses a **self-contained, vendored** helper `automation/db.py` (psycopg over Supabase) rather than the workspace `core/db.py`, so it runs in standalone web/CI clones where `core/` isn't present. Same documented surface:

```python
from automation import db   # db.connect, db.fetch_one, db.fetch_all, db.execute, db.executemany
```

`BLACKWHOLE_DB_URL` is read from this repo's `.env` (gitignored) by `automation/config.py`. Rows come back as plain dicts (psycopg `dict_row`), and timestamptz columns read back as `datetime` objects (not ISO strings).

**Current adoption: `inventory.py` + `favorites.py` + the A/B compare feature are cut over to Supabase** (project `blackwhole` / `nihgzltpjriekyqqucbd`) as of 2026-05-29. `inventory.connect()` is an alias for `db.connect()`. The local SQLite file at `~/.listing_automation/inventory.db` is no longer read or written, and `~/.listing_automation/compare_ratings.json` / `llm_compare_*.json` are no longer read or written (existing logs were imported via `scripts/migrate_compare_logs.py`; the files can be deleted any time). Schema lives in Supabase (managed via migrations), not created at runtime; the cutover added `auction_favorites` / `auction_alerts_sent` / `llm_compare_logs`. Note: Supabase has RLS **disabled** on all tables — the server connects as the pooler `postgres` role (bypasses RLS), but enabling RLS + policies for the anon key is a separate, still-open task.

`auction_extractors/state/listings.db` stays SQLite-only and read-only to this repo (upstream scrape cache).

## Connection pool (2026-09-11)

`db.connect()` hands out connections from a lazy, process-wide `psycopg_pool.ConnectionPool` (`psycopg[pool]` extra). Diagnosis that motivated it: every admin query averaged <1 ms in Postgres, but each `fetch_*` opened a fresh TLS + SCRAM handshake to the Supabase session pooler — ~1.4 s from the operator's laptop, ~0.4 s from Render — and several handlers ran that on the asyncio event loop, freezing the whole server. `/api/inventory-stats` (one row) took 8-22 s; `/api/drafts` 56 s.

- `with db.connect() as conn:` — pooled checkout; commits on clean exit, returns the connection to the pool. Same lifecycle as before, minus the handshake.
- `db.connect(pooled=False)` / `db.connect(autocommit=True)` — private connection, exactly the old behaviour (advisory locks, LISTEN/NOTIFY, VACUUM, long backfills).
- Reads (`fetch_one`/`fetch_all`) retry once on `OperationalError` (a stale pooled socket); writes never retry.
- Env: `BLACKWHOLE_DB_POOL=0` (kill switch), `BLACKWHOLE_DB_POOL_MIN`/`_MAX` (1/4), `_MAX_IDLE` (600 s), `_MAX_LIFETIME` (3600 s), `_WAIT` (30 s). Keep `_MAX` small: the free-tier pooler slot count is shared with every Render cron.
- The pool is keyed on the DSN and rebuilt if `BLACKWHOLE_DB_URL` changes; `db.reset_pool()` closes it (tests, app shutdown).

## Key environment
- macOS only paths assumed (`~/Desktop/Banquet chiars Pictures/`).
- `.env` carries `DEWATERMARK_API_KEY` and (optional) `GEMINI_API_KEY`. Already gitignored.
- Persistent Playwright profile lives at `~/.listing_automation/chrome_profile/`. Logged into FB + eBay there.
- A/B compare logs and ratings live in Supabase table `llm_compare_logs` (one row per dual-extractor run, `id` = unix-ts). The old `~/.listing_automation/logs/llm_compare_*.json` + `compare_ratings.json` files are dormant — kept on disk as a backup but not read or written.

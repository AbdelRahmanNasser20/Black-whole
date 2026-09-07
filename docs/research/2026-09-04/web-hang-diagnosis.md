# Web hang diagnosis — `python -m automation.web` wedges on `/api/health`

**Root cause:** the admin server runs synchronous psycopg (Supabase pooler) calls *on the asyncio event loop* — `_alerts_tick` every 30 s and ~60 `async def` route handlers — so each ~2–4 s handshake freezes every request, and one black-holed handshake (socket stuck in `SYN_SENT`, no `connect_timeout`) freezes the whole server for the OS TCP timeout (minutes) or, when it recurs every tick, for hours.

- Why prod is fine: Render sits next to the pooler (ms handshakes) → the same blocking is invisible. From Egypt over Tailscale (`utun4`, MTU 1280) a handshake is 2.2–3.9 s and sometimes never completes.
- Why `/api/health` hangs: it is `async def health(): return "ok"` — no DB, no threadpool. Only a blocked loop can stall it. That rules out the SSE streams and the threadpool.

## Evidence

- Repro (server PID 20423, 19:44 EEST): `/deals`, `/api/profiles`, `/api/health` and 5 more `/api/health` → all `000` after exactly 25 s (`curl -m 25`). No log line for any of them. Ten minutes earlier `/api/health` answered in 12 ms — the wedge is intermittent, tied to DB handshakes.
- Native stack (`sample 20423 3`): main thread = uvloop `run_forever → task_step → coroutine → __pyx_pw_14psycopg_binary… → poll` in **2557 of 2560 samples**. A coroutine task is sitting inside psycopg's socket wait. All other threads are idle (`PyThread_acquire_lock_timed` = threadpool workers waiting for work).
- Sockets (`lsof -p 20423`): fd 27 `100.66.125.9:63451 -> 18.213.155.45:5432 SYN_SENT` — a pooler handshake that never completed; fd 26 an older ESTABLISHED pooler conn. Both from the loop thread.
- Earlier wedge log (`~/.listing_automation/logs/web.relaunch-2026-09-04.log`): `[favorites] tick error: OperationalError('… could not receive data from server: Operation timed out')`, `failed to resolve host 'aws-1-us-east-1.pooler.supabase.com'`, and `/api/auctions/favorites` 500s with the same error — each one is the loop thread sitting in a stalled socket until the OS gave up. 1,275 `/api/auctions/favorites` hits in that log: the admin tab polls it every 30 s (`app.js:1293`), and that handler is `async def` calling `favorites.list_all()` directly.
- Code: `automation/web/app.py:2411-2478` `_alerts_tick` (async) → `favorites.list_all()` ×2, `favorites.upsert()`, `favorites.mark_sent()` → `inventory.connect()` → `automation/db.py:53` `psycopg.connect(dsn)` with **no `connect_timeout`**. Same pattern in `list_favorites` (`app.py:2323`) and ~60 other `async def` handlers (`inventory.*`, `db.fetch_*`, `profiles.*`). `_tracking_tick` is already correct (`asyncio.to_thread(_tracking_pass)`).
- Controlled repro (`$CLAUDE_JOB_DIR/tmp`, real DSN): a 1 ms `await asyncio.sleep` probe while one tick runs — **sync-on-loop: worst stall 3,906 ms; via `asyncio.to_thread`: 10 ms.** Handshake timings from this machine: 3.71 s, 2.16 s, 2.37 s, 2.47 s.
- py-spy 0.4.2 installed into `.venv` but `py-spy dump` needs `sudo` on macOS (password prompt) — run `sudo .venv/bin/py-spy dump --pid <worker pid>` next time it wedges to see the Python frames; expect `_alerts_tick` / `list_favorites` → `favorites.list_all` → `psycopg.connect`.

## Fix (smallest; not applied — `git apply --check` passes, `py_compile` ok)

1. `_alerts_tick`: move the blocking half into `_alerts_collect_due()` and run it with `asyncio.to_thread`; `mark_sent` via `to_thread` too.
2. `list_favorites` (`/api/auctions/favorites`, polled every 30 s by the admin tab): `await asyncio.to_thread(favorites.list_all)`.
3. `automation/db.py`: `connect_timeout=10` (env `BLACKWHOLE_DB_CONNECT_TIMEOUT`) so a black-holed SYN can never hold a thread — or the loop — for minutes. Defense in depth, one line.

```diff
--- a/automation/web/app.py
+++ b/automation/web/app.py
@@ -2320,7 +2320,7 @@
     """All starred auctions, newest first. Each item carries a derived
     ``seconds_until_end`` and a ``sent_intervals`` list so the UI can render
     a checklist of which alerts have already fired."""
-    favs = favorites.list_all()
+    favs = await asyncio.to_thread(favorites.list_all)
     return {
         "items": [f.to_dict() for f in favs],
         "intervals": [label for label, _ in favorites.ALERT_INTERVALS],
@@ -2402,65 +2402,73 @@
         f"{qty_line}\n"
         f"{fav_dict.get('link') or ''}"
     ).strip()
+
+
+def _alerts_collect_due() -> list:
+    """Blocking half of the scheduler tick: every favorites.* call opens a
+    fresh Supabase pooler connection (sync psycopg), so this must run in a
+    worker thread, never on the event loop — one stalled handshake would
+    freeze every request (including /api/health) for the whole TCP timeout."""
+    favs = favorites.list_all()
+    if not favs:
+        return []
+
+    # Re-sync end_date from auction_extractors cache so we catch relists.
+    # Cheap: one indexed lookup per favorite. If listings.db is gone we
+    # silently skip the sync — alerts still fire off the snapshot.
+    try:
+        import sqlite3
+        db_path = AUCTION_EXTRACTORS_DIR / "state" / "listings.db"
+        if db_path.exists():
+            conn = sqlite3.connect(str(db_path))
+            conn.row_factory = sqlite3.Row
+            try:
+                for f in favs:
+                    row = conn.execute(
+                        "SELECT end_date, time_left, image_url, title, "
+                        "quantity, location FROM listings WHERE asset_id = ?",
+                        (f.asset_id,),
+                    ).fetchone()
+                    if row is None:
+                        continue
+                    # ONLY the absolute end_date — never time_left. A
+                    # relative "2 days left" string re-parses to a new
+                    # instant every tick, which re-armed alerts endlessly
+                    # (the alert flood). No absolute date → keep snapshot.
+                    fresh_end = (row["end_date"] or "").strip()
+                    if not fresh_end:
+                        continue
+                    fresh_dt = favorites._parse_end_date(fresh_end)
+                    if fresh_dt is None:
+                        continue
+                    # Compare PARSED times, not raw strings: formatting
+                    # drift must not trigger a needless re-sync/re-arm.
+                    if f.end_dt and abs((fresh_dt - f.end_dt).total_seconds()) <= 120:
+                        continue
+                    favorites.upsert(
+                        asset_id=f.asset_id,
+                        link=f.link,
+                        title=row["title"] or f.title,
+                        quantity=row["quantity"] or f.quantity,
+                        end_date_raw=fresh_end or f.end_date_raw,
+                        image_url=row["image_url"] or f.image_url,
+                        location=row["location"] or f.location,
+                    )
+            finally:
+                conn.close()
+    except Exception as e:
+        print(f"[favorites] sync from listings.db failed: {e!r}")
+
+    # Re-read after sync.
+    favs = favorites.list_all()
+    return favorites.due_alerts(favs)
 
 
 async def _alerts_tick() -> None:
     """One scheduler pass. Re-syncs end_date from listings.db where possible
     (catches relists with fresh end_date), then ships any due alerts."""
     try:
-        favs = favorites.list_all()
-        if not favs:
-            return
-
-        # Re-sync end_date from auction_extractors cache so we catch relists.
-        # Cheap: one indexed lookup per favorite. If listings.db is gone we
-        # silently skip the sync — alerts still fire off the snapshot.
-        try:
-            import sqlite3
-            db_path = AUCTION_EXTRACTORS_DIR / "state" / "listings.db"
-            if db_path.exists():
-                conn = sqlite3.connect(str(db_path))
-                conn.row_factory = sqlite3.Row
-                try:
-                    for f in favs:
-                        row = conn.execute(
-                            "SELECT end_date, time_left, image_url, title, "
-                            "quantity, location FROM listings WHERE asset_id = ?",
-                            (f.asset_id,),
-                        ).fetchone()
-                        if row is None:
-                            continue
-                        # ONLY the absolute end_date — never time_left. A
-                        # relative "2 days left" string re-parses to a new
-                        # instant every tick, which re-armed alerts endlessly
-                        # (the alert flood). No absolute date → keep snapshot.
-                        fresh_end = (row["end_date"] or "").strip()
-                        if not fresh_end:
-                            continue
-                        fresh_dt = favorites._parse_end_date(fresh_end)
-                        if fresh_dt is None:
-                            continue
-                        # Compare PARSED times, not raw strings: formatting
-                        # drift must not trigger a needless re-sync/re-arm.
-                        if f.end_dt and abs((fresh_dt - f.end_dt).total_seconds()) <= 120:
-                            continue
-                        favorites.upsert(
-                            asset_id=f.asset_id,
-                            link=f.link,
-                            title=row["title"] or f.title,
-                            quantity=row["quantity"] or f.quantity,
-                            end_date_raw=fresh_end or f.end_date_raw,
-                            image_url=row["image_url"] or f.image_url,
-                            location=row["location"] or f.location,
-                        )
-                finally:
-                    conn.close()
-        except Exception as e:
-            print(f"[favorites] sync from listings.db failed: {e!r}")
-
-        # Re-read after sync.
-        favs = favorites.list_all()
-        due = favorites.due_alerts(favs)
+        due = await asyncio.to_thread(_alerts_collect_due)
         if not due:
             return
         if not telegram_alerts.is_configured():
@@ -2475,7 +2483,7 @@
             text = _format_alert(fav.to_dict(), label)
             ok, err = await telegram_alerts.send_message(text, topic="deals")
             if ok:
-                favorites.mark_sent(fav.asset_id, label)
+                await asyncio.to_thread(favorites.mark_sent, fav.asset_id, label)
                 print(f"[favorites] alert sent: {fav.asset_id} {label}")
             else:
                 print(f"[favorites] alert FAILED: {fav.asset_id} {label}: {err}")
--- a/automation/db.py
+++ b/automation/db.py
@@ -50,7 +50,14 @@
     surface (workspace CLAUDE.md §14) but missing here until 2026-08-28, so
     `connect(autocommit=True)` raised TypeError in this repo only.
     """
-    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=autocommit)
+    # connect_timeout caps a black-holed TCP handshake to the pooler (seen as a
+    # socket stuck in SYN_SENT for minutes over the Egypt/Tailscale path).
+    # Without it libpq waits for the OS TCP timeout, and whoever called us —
+    # a worker thread, or worse the event loop — is frozen for that long.
+    return psycopg.connect(
+        _dsn(), row_factory=dict_row, autocommit=autocommit,
+        connect_timeout=int(os.getenv("BLACKWHOLE_DB_CONNECT_TIMEOUT", "10")),
+    )
 
 
 def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
```

Patch file: `~/.claude/jobs/2a0b5935/tmp/webfix/fix.diff` (`cd listing_automation && git apply ~/.claude/jobs/2a0b5935/tmp/webfix/fix.diff`).

## How to verify

1. Apply, kill + relaunch `python -m automation.web` (app.py change needs a relaunch).
2. `for i in $(seq 1 120); do curl -m 5 -s -o /dev/null -w "%{http_code} %{time_total}\n" http://127.0.0.1:8765/api/health; sleep 2; done` — 4 minutes = 8 scheduler ticks. Before: intermittent `000 5.0`; after: every line `200` under ~50 ms even while `[favorites]`/`[tracking]` ticks run.
3. `sample <worker pid> 3 -file /tmp/s.txt; grep -c psycopg /tmp/s.txt` on the **main** thread section → 0 (main thread should sit in `uv__io_poll`/`kevent`).
4. `lsof -nP -p <worker pid> | grep 5432` — any `SYN_SENT` now belongs to a worker thread and clears within 10 s (`connect_timeout`).
5. Rerun the controlled repro with `_alerts_collect_due` swapped in: worst stall ≤ ~10 ms.

## Follow-up (not in this diff)

- ~60 other `async def` handlers still call sync psycopg on the loop (`/`, `/listings`, `/api/inventory*`, `/api/deals/*`, `/sitemap.xml`, catalog feed, …). Each admin click = a 2–4 s freeze of *every* request from Egypt. Cheapest wholesale fix: handlers with no `await` inside → change `async def` → `def` (Starlette runs plain `def` routes in the threadpool, anyio default 40 threads); handlers that do `await` → wrap the DB call in `asyncio.to_thread`. Same rule as CLAUDE.md's `default_extractors()` no-hang rule: **never block the loop on the pooler.**
- `--reload` (WatchFiles) and the two SSE streams were checked and are not involved: SSE generators only `await queue.get()` with a 15 s ping, and the reloader is a separate parent process (PID 20406).

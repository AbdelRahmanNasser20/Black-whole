"""Orchestration CLI for the closing-price recorder.

`python -m recorder.cli <command>` — the single entrypoint every cron (Render
+ interim launchd) drives. Four commands:

- `discover [--source S]`  — sweep active + recently-closed lots (all 6
  sources, or one via `--source`); INSERT-only, per-source error isolation.
- `poll-once`               — re-check tracked lots due for a poll right now
  (per `recorder.schedule.is_due`), grouped by source.
- `coverage [--days N]`     — print the coverage report (the Phase-0
  done-metric: closed lots vs. how many got a post-close observation).
- `run [--discover-stale-hours H]` — poll-once always, the GovDeals 7-day
  SOA re-check, plus discover for any source whose newest observation is
  stale (or nonexistent). This is the one command cron calls.
- `finals-backfill --source govdeals [--since-days N] [--limit N] [--apply]`
  — read the bidbox for recently-ended GovDeals lots that never got a
  bidbox final; dry-run by default.
- `archive-backfill --source govdeals [--since-days 30] [--limit N] [--apply]`
  — permanent private archive (detail + gallery + bidbox + our timeline +
  photos) of recently closed GovDeals lots into R2; dry-run by default.
  `run` archives a bounded batch of just-closed lots every tick.
- `archive-analyze [--limit N] [--lot a/b/c] [--force]` — the LLM analysis
  of archived lots, cached next to each archive (runs once per lot).

Every command (except `--help`, which argparse short-circuits before any of
our code runs) starts with a startup guard: if `listing_snapshots` doesn't
exist yet, exit 3 with a clear message instead of a raw stack trace — cron
logs should read "schema not applied", not a psycopg traceback.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

# Importing automation.config triggers its .env-load side effect (same
# pattern deals/classify.py and deals/llm_steps.py use) — recorder/store.py
# only imports `automation.db`, which never loads .env itself, so something
# in this process's import chain has to. MINOR fix (whole-branch review):
# this runs at MODULE IMPORT time, i.e. before argparse ever gets a chance
# to short-circuit on --help — all top-level imports happen before main()
# is called. It's harmless there (and safe to run for --help too) only
# because loading a .env file is a side-effect-free read with no failure
# mode of its own; the thing that must stay AFTER argparse's --help
# short-circuit is `_check_schema()`'s actual DB query, called from inside
# main() below, not this import.
from automation import config  # noqa: F401
from automation import db

from recorder import health, lot_archive, schedule, store
from recorder.sources import govdeals as govdeals_source
from recorder.sources.allsurplus import AllSurplusSource
from recorder.sources.govdeals import GovDealsSource
from recorder.sources.gsa import GSASource
from recorder.sources.mibid import MiBidSource
from recorder.sources.municibid import MunicibidSource
from recorder.sources.public_surplus import PublicSurplusSource
from recorder.sources.purple_wave import PurpleWaveSource

# Canonical source-name order — single source of truth for both the CLI's
# `--source` choices (needed before any adapter is instantiated, so --help
# never touches the network) and `build_registry()`'s dict.
SOURCE_NAMES = ("govdeals", "allsurplus", "public_surplus", "purple_wave", "municibid", "mibid", "gsa")
# Sources on the maestro API (bidbox finals, the 7-day re-check, the lot archive).
MAESTRO_SOURCES = govdeals_source.MAESTRO_SOURCES


def build_registry() -> dict:
    """Fresh adapter instances, one per source. All 6 constructors take no
    required args (confirmed against Tasks 2-4)."""
    return {
        "govdeals": GovDealsSource(),
        "allsurplus": AllSurplusSource(),
        "public_surplus": PublicSurplusSource(),
        "purple_wave": PurpleWaveSource(),
        "municibid": MunicibidSource(),
        "mibid": MiBidSource(),
        "gsa": GSASource(),
    }


# --- discover ----------------------------------------------------------

# Whole-site GovDeals is ~27.6k live lots (2026-09-29) and ~125k new lots a
# month, each stored with its sacred ~2.7 KB search `raw` — hundreds of MB a
# month into `listing_snapshots`. The project moved to Supabase Pro on
# 2026-09-29 (8 GB disk included, $0.125/GB after), so the whole-site sweep
# refuses to run above 6 GB — headroom before the included disk runs out —
# and falls back to the furniture scope, loudly.
DB_MAX_MB_FOR_SCOPE_ALL_DEFAULT = 6000


def _govdeals_scope_for_run() -> str:
    want = govdeals_source.scope()
    if want != govdeals_source.SCOPE_ALL:
        return want
    try:
        cap = float(os.getenv("RECORDER_DB_MAX_MB_FOR_SCOPE_ALL") or DB_MAX_MB_FOR_SCOPE_ALL_DEFAULT)
    except ValueError:
        cap = DB_MAX_MB_FOR_SCOPE_ALL_DEFAULT
    size = store.database_size_mb()
    if size > cap:
        print(f"RECORDER ERROR source=govdeals scope=all refused: database is {size:.0f} MB "
              f"(> RECORDER_DB_MAX_MB_FOR_SCOPE_ALL={cap:.0f}) — sweeping furniture only. "
              "Reclaim space (scripts/reclaim_db_space.py) or archive listing_snapshots.raw first.",
              file=sys.stderr)
        return govdeals_source.SCOPE_FURNITURE
    return want


def _discover_counts(adapter) -> tuple[int, int]:
    """discover() + sold_sweep() for one source, inserted as one batch.
    Returns (inserted, observed). Raises on failure — the caller isolates
    per-source."""
    if getattr(adapter, "SOURCE", None) == "govdeals" and isinstance(adapter, GovDealsSource):
        found = adapter.discover(scope_override=_govdeals_scope_for_run())
    else:
        found = adapter.discover()
    observations = list(found) + list(adapter.sold_sweep())
    # discover() re-reports every active lot on every sweep; without this the
    # table grew ~2/3 pure duplicates (recorder/README.md "Storage").
    return store.insert_observations(store.filter_changed(observations)), len(observations)


def _discover_one(adapter) -> int:
    return _discover_counts(adapter)[0]


# --- source health (recorder/health.py) ---------------------------------------

def _load_health() -> "health.Registry":
    """Once per run. Table absent (018 PENDING) or unreadable → an in-memory
    breaker for this run only, with a NOTE."""
    try:
        rows = store.load_source_health()
    except Exception as exc:  # noqa: BLE001 - health is never a reason to skip a run
        print(f"RECORDER NOTE: source health unreadable ({exc!r}) — in-memory breaker this run",
              file=sys.stderr)
        return health.Registry(persisted=False)
    if rows is None:
        path = health.state_file()
        if path:
            print(f"RECORDER NOTE: recorder_source_health missing (migration 018 PENDING) — "
                  f"breaker kept in {path} (RECORDER_HEALTH_FILE)", file=sys.stderr)
            reg = health.Registry(health.load_file(path), persisted=False)
            reg.file = path
            return reg
        print("RECORDER NOTE: recorder_source_health missing (migration 018 PENDING) — "
              "in-memory breaker this run", file=sys.stderr)
        return health.Registry(persisted=False)
    return health.Registry(rows)


def _save_health(reg: "health.Registry") -> None:
    """Once per run: persist changed rows, then ping Telegram on open/close
    transitions. Never raises."""
    if reg.persisted and reg.dirty:
        try:
            store.save_source_health(reg.dirty_rows())
        except Exception as exc:  # noqa: BLE001
            print(f"RECORDER NOTE: source health not saved ({exc!r})", file=sys.stderr)
    elif getattr(reg, "file", None) and reg.dirty:
        try:
            health.save_file(reg.file, reg)
        except Exception as exc:  # noqa: BLE001
            print(f"RECORDER NOTE: source health file not saved ({exc!r})", file=sys.stderr)
    for t in reg.transitions:
        print(f"health: {health.format_transition(t)}")
    health.notify(reg.transitions)


def _open_until(reg: "health.Registry", name: str) -> str:
    nxt = reg.get(name).next_attempt_at
    return nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else "?"


def cmd_discover(registry: dict, source: str | None = None,
                 health_reg: "health.Registry | None" = None,
                 now: datetime | None = None) -> int:
    """`health_reg` given (from `run`): open sources are skipped and every
    outcome is recorded. Standalone `discover` passes none — an operator
    asking for a source by name always gets it."""
    names = [source] if source else list(registry.keys())
    failed: list[str] = []
    for name in names:
        adapter = registry.get(name)
        if adapter is None:
            print(f"RECORDER ERROR source={name} discover failed: unknown source", file=sys.stderr)
            failed.append(name)
            continue
        t = now or datetime.now(timezone.utc)
        if health_reg is not None and not health_reg.begin_attempt(name, t):
            print(f"discover source={name} skipped: circuit open until {_open_until(health_reg, name)}")
            continue
        try:
            n, seen = _discover_counts(adapter)
        except Exception as exc:  # noqa: BLE001 - one bad source must never kill the sweep
            print(f"RECORDER ERROR source={name} discover failed: {exc!r}", file=sys.stderr)
            failed.append(name)
            if health_reg is not None:
                health_reg.record_failure(name, t, f"discover raised: {exc!r}")
            continue
        if health_reg is not None:
            if seen:
                health_reg.record_success(name, t, kind="discover")
            else:
                # Every adapter "aborts" by printing a RECORDER ERROR and
                # returning [] — that is a failed attempt, not a quiet day.
                health_reg.record_failure(name, t, "discover returned 0 observations (aborted)")
        print(f"discover source={name} inserted={n} observed={seen}")
    if failed:
        print(f"discover: {len(failed)} source(s) failed: {','.join(failed)}", file=sys.stderr)
        return 1
    return 0


# --- poll-once -----------------------------------------------------------

POLL_ABANDON_DAYS_DEFAULT = 14.0


def _abandon_after() -> timedelta:
    try:
        days = float(os.getenv("RECORDER_POLL_ABANDON_DAYS") or POLL_ABANDON_DAYS_DEFAULT)
    except ValueError:
        days = POLL_ABANDON_DAYS_DEFAULT
    return timedelta(days=days)


def cmd_poll_once(registry: dict, now: datetime | None = None,
                  health_reg: "health.Registry | None" = None) -> int:
    now = now or datetime.now(timezone.utc)
    tracked = store.tracked_active()
    due = [row for row in tracked if schedule.is_due(now, row["observed_at"], row["end_date"])]

    # A lot still 'active' two weeks past its clock is not going to answer:
    # every source purges or walls it long before. Polling it forever was most
    # of the Public Surplus/Municibid timeout storm. No row is written — the
    # lot just stops costing a request; its last snapshot stays the record.
    cutoff = now - _abandon_after()
    abandoned: dict[str, int] = {}
    kept = []
    for row in due:
        end = row.get("end_date")
        if end is not None and end < cutoff:
            abandoned[row["source"]] = abandoned.get(row["source"], 0) + 1
        else:
            kept.append(row)
    due = kept

    # IMPORTANT 3: sort by end_date ascending, NULLs last, BEFORE grouping by
    # source — final-hour/confirming lots (soonest end_date, or already past)
    # always poll first if a run overruns and can't finish everything due.
    due.sort(key=lambda row: (row["end_date"] is None, row["end_date"]))

    by_source: dict[str, list[dict]] = {}
    for row in due:
        by_source.setdefault(row["source"], []).append(row)

    total_polled = total_inserted = total_gone = total_closed = 0
    failed: list[str] = []
    for name, rows in by_source.items():
        adapter = registry.get(name)
        if adapter is None:
            print(f"RECORDER ERROR source={name} poll failed: unknown source", file=sys.stderr)
            failed.append(name)
            continue
        if health_reg is not None and not health_reg.begin_attempt(name, now):
            print(f"poll source={name} skipped: circuit open until {_open_until(health_reg, name)} "
                  f"(due={len(rows)})")
            continue
        if hasattr(adapter, "last_poll_stats"):
            adapter.last_poll_stats = None
        try:
            observations = adapter.poll(rows)
            n = store.insert_observations(store.filter_changed(observations))
        except Exception as exc:  # noqa: BLE001 - one bad source must never kill the poll
            print(f"RECORDER ERROR source={name} poll failed: {exc!r}", file=sys.stderr)
            failed.append(name)
            if health_reg is not None:
                health_reg.record_failure(name, now, f"poll raised: {exc!r}")
            continue
        stats = getattr(adapter, "last_poll_stats", None) or {}
        lot_failed = int(stats.get("failed") or 0)
        aborted = bool(stats.get("aborted"))
        if health_reg is not None:
            outcome = health.poll_outcome(int(stats.get("attempted") or len(rows)), lot_failed,
                                          aborted, n)
            if outcome == "failure":
                health_reg.record_failure(
                    name, now, f"poll batch: {lot_failed}/{stats.get('attempted')} lots failed"
                    + (" (aborted)" if aborted else ""))
            elif outcome == "success":
                health_reg.record_success(name, now)
            else:
                health_reg.record_neutral(name, now)
        gone = sum(1 for o in observations if o.status == "gone")
        closed = sum(1 for o in observations if o.status == "closed")
        total_polled += len(rows)
        total_inserted += n
        total_gone += gone
        total_closed += closed
        extra = f" failed={lot_failed}" + (" aborted=1" if aborted else "") if stats else ""
        print(f"poll source={name} due={len(rows)} inserted={n} gone={gone} closed={closed}{extra}")

    for name, k in sorted(abandoned.items()):
        print(f"poll source={name} abandoned={k} (past end_date > {_abandon_after().days} d, not polled)")
    print(
        f"poll-once polled={total_polled} inserted={total_inserted} "
        f"gone={total_gone} closed={total_closed} abandoned={sum(abandoned.values())}"
    )
    if failed:
        print(f"poll-once: {len(failed)} source(s) failed: {','.join(failed)}", file=sys.stderr)
        return 1
    return 0


# --- coverage --------------------------------------------------------------

_COVERAGE_COLUMNS = ("source", "closed_lots", "covered", "missed", "pct")


def _format_coverage_table(rows: list[dict]) -> str:
    if not rows:
        return "(no coverage data yet)"
    widths = {c: len(c) for c in _COVERAGE_COLUMNS}
    for row in rows:
        for c in _COVERAGE_COLUMNS:
            widths[c] = max(widths[c], len(str(row[c])))
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in _COVERAGE_COLUMNS)]
    lines.append("  ".join("-" * widths[c] for c in _COVERAGE_COLUMNS))
    for row in rows:
        lines.append("  ".join(str(row[c]).ljust(widths[c]) for c in _COVERAGE_COLUMNS))
    return "\n".join(lines)


def cmd_coverage(days: int) -> int:
    rows = store.coverage(days=days)
    print(_format_coverage_table(rows))
    print(f"listing_snapshots table size: {store.table_size_pretty()}")
    return 0


# --- GovDeals bidbox: 7-day SOA re-check + finals backfill -------------------

def cmd_recheck_finals(registry: dict) -> int:
    """One bounded pass of the 7-day SOA re-check (payment default / relist).
    Re-check rows skip `filter_changed` on purpose: an unchanged re-check is
    still the marker that the lot has been re-checked."""
    rc = 0
    for name in MAESTRO_SOURCES:
        adapter = registry.get(name)
        if adapter is None or not hasattr(adapter, "recheck_finals"):
            continue
        try:
            rows = store.soa_recheck_due(
                sorted(govdeals_source.SOLD_CODES),
                govdeals_source.SOA_RECHECK_AFTER.total_seconds(),
                govdeals_source.SOA_RECHECK_LIMIT_PER_RUN,
                source=name,
            )
            if not rows:
                continue
            observations = adapter.recheck_finals(rows)
            n = store.insert_observations(observations)
        except Exception as exc:  # noqa: BLE001 - never kill the run over the re-check
            print(f"RECORDER ERROR source={name} recheck failed: {exc!r}", file=sys.stderr)
            rc = 1
            continue
        changed = sum(1 for o in observations if o.raw["recorder_capture"].get("changed"))
        print(f"recheck source={name} due={len(rows)} inserted={n} changed={changed}")
    return rc


def _money(v) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def cmd_finals_backfill(source: str, since_days: int, limit: int, apply: bool,
                        now: datetime | None = None, adapter=None) -> int:
    """Bidbox finals for GovDeals lots that ended in the window and were
    recorded `gone` (i.e. `last_snapshot`). Dry-run unless `apply`."""
    if source not in MAESTRO_SOURCES:
        print(f"finals-backfill: only {'/'.join(MAESTRO_SOURCES)} have a post-close bidbox "
              f"(got {source!r})", file=sys.stderr)
        return 2
    now = now or datetime.now(timezone.utc)
    adapter = adapter or govdeals_source.adapter_for_source(source)
    rows = store.finals_backfill_candidates(source, since_days, limit)
    counts = {"checked": 0, "final": 0, "purged": 0, "extended": 0, "grace": 0, "error": 0}
    outcomes: dict[str, int] = {}
    to_write = []
    sample = []
    higher = lower = same = 0
    for row in rows:
        parsed = govdeals_source._parse_lot_key(row["source_lot_id"])
        if parsed is None:
            continue
        counts["checked"] += 1
        result, obs = govdeals_source.resolve_with_bidbox(adapter, parsed, now)
        counts[result] = counts.get(result, 0) + 1
        if obs is None:
            continue
        to_write.append(obs)
        if result != "final":
            continue
        outcome = obs.raw["recorder_capture"]["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        last = row.get("last_price")
        if last is not None and obs.current_bid is not None:
            if obs.current_bid > last:
                higher += 1
            elif obs.current_bid < last:
                lower += 1
            else:
                same += 1
        sample.append((row["source_lot_id"], last, row.get("last_bid_count"), obs.current_bid,
                       obs.bid_count, obs.raw["recorder_capture"]["status_code"], outcome))

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"finals-backfill [{mode}] source={source} since_days={since_days} limit={limit}")
    print(f"  checked={counts['checked']} with_final={counts['final']} "
          f"purged(204)={counts['purged']} extended={counts['extended']} "
          f"grace={counts['grace']} errors={counts['error']}")
    print(f"  outcomes: " + (", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())) or "—"))
    print(f"  final vs last_snapshot: higher={higher} lower={lower} same={same}")
    if sample:
        print("  sample (lot | last_snapshot price/bids -> bidbox final price/bids | code outcome):")
        for lot_id, last, last_n, fin, fin_n, code, outcome in sample[:15]:
            print(f"    {lot_id:<20} {_money(last):>11} / {last_n if last_n is not None else '—':<4}"
                  f" -> {_money(fin):>11} / {fin_n if fin_n is not None else '—':<4} | {code} {outcome}")
    if apply:
        n = store.insert_observations(store.filter_changed(to_write))
        print(f"  inserted={n}")
    else:
        print(f"  would insert={len(to_write)} (re-run with --apply to write)")
    return 0


# --- lot archive -------------------------------------------------------------

ARCHIVE_SINCE_DAYS = 30
ARCHIVE_PER_RUN_DEFAULT = 15
ARCHIVE_TIME_BUDGET_S_DEFAULT = 90.0
# Whole-site GovDeals closes, for the storage projection: ~29k/week measured in
# deal_lots (Jul-Aug 2026) ≈ 125k/month.
MONTHLY_LOTS_ALL_SCOPE = 125_000
R2_FREE_GB = 10.0
R2_USD_PER_GB_MONTH = 0.015


def _index_fn():
    """The lot_archive index writer, or None until migration 015 is applied."""
    try:
        return store.upsert_archive_index if store.lot_archive_index_exists() else None
    except Exception as exc:  # noqa: BLE001 - the index is optional
        print(f"lot archive: index check failed ({exc}) — archiving without it", file=sys.stderr)
        return None


def _archive_http_get(url, timeout=30):
    from recorder.sources.base import polite_get
    return polite_get(url, timeout=timeout)


def storage_projection(bytes_per_lot: float, monthly_lots: int = MONTHLY_LOTS_ALL_SCOPE,
                       months: int = 12) -> dict:
    """Pure. Cumulative R2 GB and monthly bill after `months` of archiving."""
    gb_month = bytes_per_lot * monthly_lots / 1e9
    stored = gb_month * months
    bill = max(0.0, stored - R2_FREE_GB) * R2_USD_PER_GB_MONTH
    total = sum(max(0.0, gb_month * m - R2_FREE_GB) * R2_USD_PER_GB_MONTH
                for m in range(1, months + 1))
    return {"gb_per_month": round(gb_month, 2), "gb_after": round(stored, 1),
            "usd_per_month_after": round(bill, 2), "usd_total": round(total, 2),
            "months": months, "monthly_lots": monthly_lots}


def _print_archive_meter(meter: dict, mode: str, source: str, since_days: int, limit: int) -> None:
    print(f"archive-backfill [{mode}] source={source} since_days={since_days} limit={limit}")
    print(f"  considered={meter['considered']} archived={meter['archived']} "
          f"already={meter['skipped_exists']} not_ready={meter['not_ready']} "
          f"errors={meter['error']} would_archive={meter['would_archive']}")
    if meter["archived"]:
        n = meter["archived"]
        per = (meter["doc_bytes"] + meter["photo_bytes"]) / n
        print(f"  bytes: docs={meter['doc_bytes']:,} photos={meter['photo_bytes']:,} "
              f"({meter['photos']} photos) — {per / 1e3:,.0f} KB/lot, "
              f"doc {meter['doc_bytes'] / n / 1e3:,.1f} KB/lot, requests={meter['requests']}")
        print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(meter["outcomes"].items())))
        p = storage_projection(per)
        print(f"  projection (whole site, ~{p['monthly_lots']:,} lots/month): "
              f"{p['gb_per_month']} GB/month; after {p['months']} months {p['gb_after']} GB "
              f"→ ${p['usd_per_month_after']}/month (${p['usd_total']} over the year; "
              f"first {R2_FREE_GB:.0f} GB free, ${R2_USD_PER_GB_MONTH}/GB-mo)")
    for r in meter["results"][:40]:
        if r.result == "archived":
            price = "—" if r.final_price is None else f"${r.final_price:,.2f}"
            print(f"    {r.lot_key:<20} {r.outcome or '?':<16} {price:>12}  "
                  f"{r.photos} photo(s)  {r.total_bytes / 1e3:,.0f} KB")
        elif r.result != "skipped_exists":
            print(f"    {r.lot_key:<20} {r.result}: {r.reason}")


def cmd_archive_backfill(source: str, since_days: int, limit: int, apply: bool,
                         lots: list[str] | None = None, archive_store=None, adapter=None,
                         http_get=None, force: bool = False) -> int:
    if source not in lot_archive.SOURCES:
        print(f"archive-backfill: only {'/'.join(lot_archive.SOURCES)} are archived "
              f"(got {source!r})", file=sys.stderr)
        return 2
    if apply:
        try:
            archive_store = lot_archive.require_store(archive_store)
        except lot_archive.StoreNotConfigured as exc:
            print(f"RECORDER ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        archive_store = archive_store or lot_archive.store_from_env()
    if lots:
        rows = [{"source_lot_id": k, "end_date": None} for k in lots]
    else:
        use_index = False
        try:
            use_index = store.lot_archive_index_exists()
        except Exception:  # noqa: BLE001
            pass
        rows = store.archive_candidates(since_days, max(limit * 4, limit + 50),
                                        lot_archive.MIN_AGE.total_seconds(), use_index=use_index,
                                        source=source)
    already = lot_archive.archived_slugs(archive_store, source) if archive_store else set()
    meter = lot_archive.run_archive(
        rows, store=archive_store, adapter=adapter or govdeals_source.adapter_for_source(source),
        http_get=http_get or _archive_http_get, timeline_fn=store.lot_timeline,
        limit=limit, apply=apply, already=already,
        index_fn=_index_fn() if apply else None, force=bool(force and lots), source=source)
    _print_archive_meter(meter, "APPLY" if apply else "DRY-RUN", source, since_days, limit)
    print(f"  store={getattr(archive_store, 'kind', 'none')}")
    if not apply:
        print("  (dry-run: no request sent — re-run with --apply to archive)")
    return 0


def cmd_archive_pending(registry: dict) -> int:
    """The per-tick "archive at close" pass inside `run`: a bounded batch of
    just-closed lots. No store → loud note, never a failed run (nothing is lost
    yet; GovDeals keeps the page for weeks)."""
    if (os.getenv("RECORDER_ARCHIVE_ENABLED") or "1").strip().lower() in ("0", "false", "no", "off"):
        return 0
    archive_store = lot_archive.store_from_env()
    if archive_store is None:
        why = lot_archive.last_store_error()
        print(f"RECORDER NOTE: lot archive skipped — R2 not configured" + (f" ({why})" if why else ""),
              file=sys.stderr)
        return 0
    limit = lot_archive.env_int("RECORDER_ARCHIVE_MAX_PER_RUN", ARCHIVE_PER_RUN_DEFAULT)
    try:
        budget = float(os.getenv("RECORDER_ARCHIVE_TIME_BUDGET_S") or ARCHIVE_TIME_BUDGET_S_DEFAULT)
    except ValueError:
        budget = ARCHIVE_TIME_BUDGET_S_DEFAULT
    index_fn = _index_fn()
    rc = 0
    t0 = time.monotonic()
    for source in lot_archive.SOURCES:     # one shared time budget across sources
        left = budget - (time.monotonic() - t0)
        if left <= 0:
            break
        try:
            rows = store.archive_candidates(ARCHIVE_SINCE_DAYS, limit * 4,
                                            lot_archive.MIN_AGE.total_seconds(),
                                            use_index=index_fn is not None, source=source)
            if not rows:
                continue
            meter = lot_archive.run_archive(
                rows, store=archive_store, adapter=govdeals_source.adapter_for_source(source),
                http_get=_archive_http_get, timeline_fn=store.lot_timeline, limit=limit,
                apply=True, time_budget_s=left, index_fn=index_fn, source=source)
        except Exception as exc:  # noqa: BLE001 - never kill the run over the archive
            print(f"RECORDER ERROR source={source} archive failed: {exc!r}", file=sys.stderr)
            rc = 1
            continue
        print(f"archive source={source} archived={meter['archived']} not_ready={meter['not_ready']} "
              f"errors={meter['error']} bytes={meter['doc_bytes'] + meter['photo_bytes']:,}")
        rc = rc or (1 if meter["error"] else 0)
    return rc


def cmd_archive_analyze(limit: int, lot: str | None = None, force: bool = False,
                        archive_store=None, source: str = "govdeals") -> int:
    from recorder import lot_analysis
    try:
        archive_store = lot_archive.require_store(archive_store)
    except lot_archive.StoreNotConfigured as exc:
        print(f"RECORDER ERROR: {exc}", file=sys.stderr)
        return 1
    keys = [lot] if lot else sorted(lot_archive.archived_slugs(archive_store, source), reverse=True)
    counts = {"ok": 0, "unavailable": 0, "cached": 0}
    done = 0
    for k in keys:
        if done >= limit:
            break
        key = k if "/" in k else "/".join(map(str, lot_archive.parse_slug(k) or ()))
        if not key:
            continue
        cached = lot_analysis.load(archive_store, key, source)
        if (cached and cached.get("status") == "ok" and not force
                and (cached.get("version") or 0) >= lot_analysis.ANALYSIS_VERSION):
            counts["cached"] += 1
            continue
        a = lot_analysis.analyze_and_store(archive_store, key, source)
        done += 1
        counts[a["status"]] = counts.get(a["status"], 0) + 1
        v = a.get("deal") or {}
        print(f"  {key:<20} {a['status']:<12} {(a.get('category') or {}).get('llm') or '—':<22} "
              f"qty={(a.get('quantity') or {}).get('value')} verdict={v.get('verdict') or '—'} "
              f"{a.get('error') or ''}")
    print("archive-analyze: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


# --- run (the cron entrypoint) ---------------------------------------------

# Advisory lock key for `run` (IMPORTANT 3). Render cron fires every 5 min;
# a slow run (network hiccups, a stuck source) must never overlap the next
# invocation and double-poll/double-discover concurrently.
_RUN_LOCK_KEY = "recorder_run"


def _run_budget_s() -> float:
    """RECORDER_RUN_BUDGET_S (default 0 = off): once a run has spent this long,
    the remaining stale discovers wait for the next run — polls (closes) first."""
    try:
        return max(0.0, float(os.getenv("RECORDER_RUN_BUDGET_S") or 0))
    except ValueError:
        return 0.0


def _connect_lock_conn(attempts: int = 4, base_delay: float = 2.0):
    """The run's private lock connection, backing off while the Supabase
    session pooler is full ("max clients reached", 15-client cap) instead of
    dying with a traceback (seen in the 2026-10-02 soak)."""
    for i in range(attempts):
        try:
            return db.connect(pooled=False)
        except psycopg.OperationalError as e:
            if i == attempts - 1 or not any(m in str(e).lower() for m in store._POOL_FULL_MARKERS):
                raise
            delay = base_delay * (2 ** i)
            print(f"recorder run: pooler full, retrying the lock connection in {delay:.0f}s")
            time.sleep(delay)


def cmd_run(registry: dict, discover_stale_hours: float = 6.0, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    started = time.monotonic()

    # Session-level advisory lock on a dedicated connection held for the
    # whole run — NOT the short-lived per-query connections `db.fetch_*`
    # opens. `pg_try_advisory_lock` never blocks: it returns False instantly
    # if another `run` already holds the lock, so an overrunning previous
    # invocation just makes this one a clean no-op exit(0), never a pile-up.
    conn = _connect_lock_conn()
    try:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (_RUN_LOCK_KEY,)
        ).fetchone()["locked"]
        conn.commit()
        if not locked:
            print("recorder run skipped — previous run still active")
            return 0

        health_reg = _load_health()
        try:
            poll_rc = cmd_poll_once(registry, now=now, health_reg=health_reg)
            poll_rc = cmd_recheck_finals(registry) or poll_rc
            poll_rc = cmd_archive_pending(registry) or poll_rc

            # Stale = no new row AND no clean DISCOVER for H hours. A discover
            # that saw every lot unchanged inserts nothing (change-gating), so
            # newest_observed_at alone re-fired discover for a quiet source on
            # every 5-minute run. Poll successes do NOT count: clean polls
            # that insert nothing must not hold discover off forever.
            stale: list[str] = []
            for name in registry:
                newest = store.newest_observed_at(name)
                last_disc = health_reg.get(name).last_discover_at
                ref = max((t for t in (newest, last_disc) if t is not None), default=None)
                if ref is None or (now - ref) >= timedelta(hours=discover_stale_hours):
                    stale.append(name)

            discover_rc = 0
            budget_s = _run_budget_s()
            if stale:
                print(f"run: discover due for stale source(s): {','.join(stale)}")
                for name in stale:
                    spent = time.monotonic() - started
                    if budget_s and spent > budget_s:
                        print(f"discover source={name} skipped: run budget spent "
                              f"({spent:.0f}s > RECORDER_RUN_BUDGET_S={budget_s:.0f})")
                        continue
                    rc = cmd_discover(registry, source=name, health_reg=health_reg, now=now)
                    discover_rc = discover_rc or rc
            else:
                print("run: no source is stale, skipping discover")

            return 1 if (poll_rc or discover_rc) else 0
        finally:
            _save_health(health_reg)
            print(f"run: elapsed={time.monotonic() - started:.1f}s")
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_RUN_LOCK_KEY,))
            conn.commit()
        except Exception:  # noqa: BLE001 - best-effort release; connection close below is the backstop
            pass
        conn.close()


# --- health -----------------------------------------------------------------

_HEALTH_COLUMNS = ("source", "state", "consecutive_failures", "last_success_at",
                   "next_attempt_at", "last_error")


def _fmt_ts(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else ("—" if v is None else str(v))


def cmd_health(now: datetime | None = None) -> int:
    """The breaker table. Exit 1 when any source has been open (no success)
    for more than 24 h — that one needs a human."""
    now = now or datetime.now(timezone.utc)
    rows = store.load_source_health()
    if rows is None and health.state_file():
        rows = health.load_file(health.state_file())
        print(f"(migration 018 PENDING — reading {health.state_file()})")
    if rows is None:
        print("recorder_source_health missing — migration 018 is PENDING; the breaker is "
              "in-memory per run until it is applied.")
        return 0
    states = sorted((health.SourceHealth.from_row({**r, "source": s}) for s, r in rows.items()),
                    key=lambda h: h.source)
    if not states:
        print("(no source health recorded yet)")
        return 0
    cells = [[h.source, h.state, str(h.consecutive_failures), _fmt_ts(h.last_success_at),
              _fmt_ts(h.next_attempt_at), (h.last_error or "—")[:80]] for h in states]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(_HEALTH_COLUMNS)]
    print("  ".join(c.upper().ljust(widths[i]) for i, c in enumerate(_HEALTH_COLUMNS)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    stuck = [h.source for h in states if health.open_too_long(h, now)]
    if stuck:
        print(f"health: open > 24h: {','.join(stuck)}", file=sys.stderr)
        return 1
    return 0


# --- startup guard + argparse wiring ----------------------------------------

def _check_schema() -> None:
    """Exit 3 with a clear message if `listing_snapshots` hasn't been created
    yet, instead of letting the first real query blow up with a raw
    psycopg traceback in the cron log.

    IMPORTANT 5 fix (BLACKWHOLE-28 whole-branch review): a DB *outage*
    (unreachable host, pooler down, network blip) is a different failure
    from "schema not applied" — catch it separately so cron logs read one
    clean line instead of a raw psycopg.OperationalError traceback, and exit
    a distinct code (4, not 3) so the two causes are distinguishable from
    the exit status alone.
    """
    try:
        row = db.fetch_one("SELECT to_regclass('listing_snapshots') AS reg")
    except psycopg.OperationalError as exc:
        print(f"RECORDER ERROR: database unreachable: {exc}", file=sys.stderr)
        sys.exit(4)
    if not row or row.get("reg") is None:
        print(
            "RECORDER ERROR: listing_snapshots table not found. Apply "
            "scripts/sql/004_listing_snapshots.sql in Supabase Studio's SQL "
            "Editor before running the recorder.",
            file=sys.stderr,
        )
        sys.exit(3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recorder",
        description="Closing-price recorder: sweep + poll 6 auction sources into listing_snapshots.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_discover = sub.add_parser(
        "discover", help="sweep active + recently-closed lots from every source (or one via --source)"
    )
    p_discover.add_argument("--source", default=None, choices=sorted(SOURCE_NAMES))

    sub.add_parser("poll-once", help="re-check tracked lots due for a poll right now")

    sub.add_parser("health", help="per-source circuit breaker table (exit 1 if any open > 24h)")

    p_coverage = sub.add_parser(
        "coverage", help="print the coverage report (Phase-0 done-metric: >90%% target)"
    )
    p_coverage.add_argument("--days", type=int, default=7)

    p_run = sub.add_parser(
        "run", help="poll-once + conditional discover for stale sources — the single cron entrypoint"
    )
    p_run.add_argument("--discover-stale-hours", type=float, default=6.0)

    p_backfill = sub.add_parser(
        "finals-backfill",
        help="read the post-close bidbox for recently-ended GovDeals lots (dry-run unless --apply)",
    )
    p_backfill.add_argument("--source", default="govdeals", choices=list(MAESTRO_SOURCES))
    p_backfill.add_argument("--since-days", type=int, default=8)
    p_backfill.add_argument("--limit", type=int, default=500)
    p_backfill.add_argument("--apply", action="store_true",
                            help="write the terminal observations (default: print only)")

    p_arch = sub.add_parser(
        "archive-backfill",
        help="archive recently closed GovDeals lots (detail, photos, bidbox, timeline) to R2; "
             "dry-run unless --apply",
    )
    p_arch.add_argument("--source", default="govdeals", choices=list(lot_archive.SOURCES))
    p_arch.add_argument("--since-days", type=int, default=ARCHIVE_SINCE_DAYS)
    p_arch.add_argument("--limit", type=int, default=100)
    p_arch.add_argument("--lot", action="append", default=None, metavar="ASSET/ACCOUNT/AUCTION",
                        help="archive these lots instead of the window (repeatable)")
    p_arch.add_argument("--apply", action="store_true", help="write to the store (default: count only)")
    p_arch.add_argument("--force", action="store_true",
                        help="with --lot: re-archive even if a document exists (overwrites it)")

    p_an = sub.add_parser("archive-analyze", help="LLM analysis of archived lots, cached per lot")
    p_an.add_argument("--limit", type=int, default=30)
    p_an.add_argument("--lot", default=None, metavar="ASSET/ACCOUNT/AUCTION")
    p_an.add_argument("--force", action="store_true", help="re-run even when a cached analysis exists")
    p_an.add_argument("--source", default="govdeals", choices=list(lot_archive.SOURCES))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Schema guard runs for every real command but never for --help, which
    # argparse already exited on above.
    _check_schema()

    registry = build_registry()

    if args.cmd == "discover":
        return cmd_discover(registry, source=args.source)
    if args.cmd == "poll-once":
        health_reg = _load_health()
        try:
            return cmd_poll_once(registry, health_reg=health_reg)
        finally:
            _save_health(health_reg)
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "coverage":
        return cmd_coverage(args.days)
    if args.cmd == "run":
        return cmd_run(registry, discover_stale_hours=args.discover_stale_hours)
    if args.cmd == "finals-backfill":
        return cmd_finals_backfill(args.source, args.since_days, args.limit, args.apply)
    if args.cmd == "archive-backfill":
        return cmd_archive_backfill(args.source, args.since_days, args.limit, args.apply,
                                    lots=args.lot, force=args.force)
    if args.cmd == "archive-analyze":
        return cmd_archive_analyze(args.limit, lot=args.lot, force=args.force, source=args.source)

    parser.error(f"unknown command {args.cmd!r}")  # pragma: no cover - argparse prevents this
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())

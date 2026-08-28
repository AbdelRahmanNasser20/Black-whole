"""Orchestration CLI for the closing-price recorder.

`python -m recorder.cli <command>` — the single entrypoint every cron (Render
+ interim launchd) drives. Four commands:

- `discover [--source S]`  — sweep active + recently-closed lots (all 6
  sources, or one via `--source`); INSERT-only, per-source error isolation.
- `poll-once`               — re-check tracked lots due for a poll right now
  (per `recorder.schedule.is_due`), grouped by source.
- `coverage [--days N]`     — print the coverage report (the Phase-0
  done-metric: closed lots vs. how many got a post-close observation).
- `run [--discover-stale-hours H]` — poll-once always, plus discover for any
  source whose newest observation is stale (or nonexistent). This is the one
  command cron calls.

Every command (except `--help`, which argparse short-circuits before any of
our code runs) starts with a startup guard: if `listing_snapshots` doesn't
exist yet, exit 3 with a clear message instead of a raw stack trace — cron
logs should read "schema not applied", not a psycopg traceback.
"""
from __future__ import annotations

import argparse
import sys
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

from recorder import schedule, store
from recorder.sources.govdeals import GovDealsSource
from recorder.sources.gsa import GSASource
from recorder.sources.mibid import MiBidSource
from recorder.sources.municibid import MunicibidSource
from recorder.sources.public_surplus import PublicSurplusSource
from recorder.sources.purple_wave import PurpleWaveSource

# Canonical source-name order — single source of truth for both the CLI's
# `--source` choices (needed before any adapter is instantiated, so --help
# never touches the network) and `build_registry()`'s dict.
SOURCE_NAMES = ("govdeals", "public_surplus", "purple_wave", "municibid", "mibid", "gsa")


def build_registry() -> dict:
    """Fresh adapter instances, one per source. All 6 constructors take no
    required args (confirmed against Tasks 2-4)."""
    return {
        "govdeals": GovDealsSource(),
        "public_surplus": PublicSurplusSource(),
        "purple_wave": PurpleWaveSource(),
        "municibid": MunicibidSource(),
        "mibid": MiBidSource(),
        "gsa": GSASource(),
    }


# --- discover ----------------------------------------------------------

def _discover_one(adapter) -> int:
    """discover() + sold_sweep() for one source, inserted as one batch.
    Raises on failure — the caller isolates per-source."""
    observations = list(adapter.discover()) + list(adapter.sold_sweep())
    # discover() re-reports every active lot on every sweep; without this the
    # table grew ~2/3 pure duplicates (recorder/README.md "Storage").
    return store.insert_observations(store.filter_changed(observations))


def cmd_discover(registry: dict, source: str | None = None) -> int:
    names = [source] if source else list(registry.keys())
    failed: list[str] = []
    for name in names:
        adapter = registry.get(name)
        if adapter is None:
            print(f"RECORDER ERROR source={name} discover failed: unknown source", file=sys.stderr)
            failed.append(name)
            continue
        try:
            n = _discover_one(adapter)
        except Exception as exc:  # noqa: BLE001 - one bad source must never kill the sweep
            print(f"RECORDER ERROR source={name} discover failed: {exc!r}", file=sys.stderr)
            failed.append(name)
            continue
        print(f"discover source={name} inserted={n}")
    if failed:
        print(f"discover: {len(failed)} source(s) failed: {','.join(failed)}", file=sys.stderr)
        return 1
    return 0


# --- poll-once -----------------------------------------------------------

def cmd_poll_once(registry: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    tracked = store.tracked_active()
    due = [row for row in tracked if schedule.is_due(now, row["observed_at"], row["end_date"])]

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
        try:
            observations = adapter.poll(rows)
            n = store.insert_observations(store.filter_changed(observations))
        except Exception as exc:  # noqa: BLE001 - one bad source must never kill the poll
            print(f"RECORDER ERROR source={name} poll failed: {exc!r}", file=sys.stderr)
            failed.append(name)
            continue
        gone = sum(1 for o in observations if o.status == "gone")
        closed = sum(1 for o in observations if o.status == "closed")
        total_polled += len(rows)
        total_inserted += n
        total_gone += gone
        total_closed += closed
        print(f"poll source={name} due={len(rows)} inserted={n} gone={gone} closed={closed}")

    print(
        f"poll-once polled={total_polled} inserted={total_inserted} "
        f"gone={total_gone} closed={total_closed}"
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


# --- run (the cron entrypoint) ---------------------------------------------

# Advisory lock key for `run` (IMPORTANT 3). Render cron fires every 5 min;
# a slow run (network hiccups, a stuck source) must never overlap the next
# invocation and double-poll/double-discover concurrently.
_RUN_LOCK_KEY = "recorder_run"


def cmd_run(registry: dict, discover_stale_hours: float = 6.0, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)

    # Session-level advisory lock on a dedicated connection held for the
    # whole run — NOT the short-lived per-query connections `db.fetch_*`
    # opens. `pg_try_advisory_lock` never blocks: it returns False instantly
    # if another `run` already holds the lock, so an overrunning previous
    # invocation just makes this one a clean no-op exit(0), never a pile-up.
    conn = db.connect()
    try:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (_RUN_LOCK_KEY,)
        ).fetchone()["locked"]
        conn.commit()
        if not locked:
            print("recorder run skipped — previous run still active")
            return 0

        poll_rc = cmd_poll_once(registry, now=now)

        stale: list[str] = []
        for name in registry:
            newest = store.newest_observed_at(name)
            if newest is None or (now - newest) >= timedelta(hours=discover_stale_hours):
                stale.append(name)

        discover_rc = 0
        if stale:
            print(f"run: discover due for stale source(s): {','.join(stale)}")
            for name in stale:
                rc = cmd_discover(registry, source=name)
                discover_rc = discover_rc or rc
        else:
            print("run: no source is stale, skipping discover")

        return 1 if (poll_rc or discover_rc) else 0
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_RUN_LOCK_KEY,))
            conn.commit()
        except Exception:  # noqa: BLE001 - best-effort release; connection close below is the backstop
            pass
        conn.close()


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

    p_coverage = sub.add_parser(
        "coverage", help="print the coverage report (Phase-0 done-metric: >90%% target)"
    )
    p_coverage.add_argument("--days", type=int, default=7)

    p_run = sub.add_parser(
        "run", help="poll-once + conditional discover for stale sources — the single cron entrypoint"
    )
    p_run.add_argument("--discover-stale-hours", type=float, default=6.0)

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
        return cmd_poll_once(registry)
    if args.cmd == "coverage":
        return cmd_coverage(args.days)
    if args.cmd == "run":
        return cmd_run(registry, discover_stale_hours=args.discover_stale_hours)

    parser.error(f"unknown command {args.cmd!r}")  # pragma: no cover - argparse prevents this
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())

"""Postgres access layer — the single door to the Supabase `blackwhole` DB.

Why this exists: the inventory ledger (and the auction-favorites store that
shares its file) moved off local SQLite onto Supabase Postgres so every surface
— run.py, the dashboard, the public site — reads and writes one source of truth.

This is a thin, self-contained helper (no dependency on the workspace `core/`
package, which isn't vendored into this repo) so the project still runs in
standalone web/CI clones. It mirrors the documented `core.db` surface:
`connect`, `fetch_one`, `fetch_all`, `execute`, `executemany`.

Connection string comes from `BLACKWHOLE_DB_URL` (loaded from `.env` by
`automation.config`). Rows come back as plain dicts via psycopg's `dict_row`,
so callers can keep the `row["col"]` / `dict(row)` patterns from the SQLite era.

Connections are POOLED (psycopg_pool) as of 2026-09-11. Before that every
fetch_one/fetch_all opened a brand-new TLS + SCRAM handshake to the Supabase
session pooler — ~1.4 s from the operator's machine, ~0.4 s from Render — while
the queries themselves run in <1 ms. That handshake, multiplied by 2-10 calls
per request, was the entire "admin loads slow" cost. The pool is lazy (nothing
opens at import), process-wide, and small (`BLACKWHOLE_DB_POOL_MAX`, default 4,
so several web workers + crons stay well under the pooler's client limit).
`BLACKWHOLE_DB_POOL=0` restores the old connect-per-call behaviour.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Iterable, Sequence, TypeVar

import psycopg
from psycopg.rows import dict_row

T = TypeVar("T")


def _dsn() -> str:
    dsn = os.getenv("BLACKWHOLE_DB_URL")
    if not dsn:
        raise RuntimeError(
            "BLACKWHOLE_DB_URL is not set — add it to the repo-root .env "
            "(see automation/config.py)."
        )
    return dsn


def _conn_kwargs(*, autocommit: bool = False) -> dict:
    # connect_timeout caps a black-holed TCP handshake to the pooler (seen as a
    # socket stuck in SYN_SENT for minutes over the Egypt/Tailscale path).
    # Without it libpq waits for the OS TCP timeout, and whoever called us —
    # a worker thread, or worse the event loop — is frozen for that long.
    # TCP keepalives stop a NAT / the pooler from silently dropping an idle
    # pooled socket, which would otherwise surface as a stale-connection error
    # on the next request after a quiet spell.
    return dict(
        row_factory=dict_row,
        autocommit=autocommit,
        connect_timeout=int(os.getenv("BLACKWHOLE_DB_CONNECT_TIMEOUT", "10")),
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=15,
        keepalives_count=3,
    )


# ───────────────────────────── connection pool ─────────────────────────────

_pool = None
_pool_dsn: str | None = None   # DSN the live pool was built for
_pool_lock = threading.Lock()
_pool_unavailable = False   # psycopg_pool missing / pool construction failed


def pool_enabled() -> bool:
    return os.getenv("BLACKWHOLE_DB_POOL", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _make_pool():
    """Build the process-wide pool. Separate so tests can swap it."""
    from psycopg_pool import ConnectionPool  # optional extra: psycopg[pool]

    return ConnectionPool(
        _dsn(),
        kwargs=_conn_kwargs(),
        min_size=int(os.getenv("BLACKWHOLE_DB_POOL_MIN", "1")),
        max_size=int(os.getenv("BLACKWHOLE_DB_POOL_MAX", "4")),
        # Close connections idle > 10 min so a quiet dashboard doesn't pin
        # pooler slots overnight; the next request just reconnects once.
        max_idle=float(os.getenv("BLACKWHOLE_DB_POOL_MAX_IDLE", "600")),
        max_lifetime=float(os.getenv("BLACKWHOLE_DB_POOL_MAX_LIFETIME", "3600")),
        timeout=float(os.getenv("BLACKWHOLE_DB_POOL_WAIT", "30")),
        open=True,
        name="blackwhole",
    )


def get_pool():
    """The lazy singleton pool, or None when pooling is off/unavailable."""
    global _pool, _pool_dsn, _pool_unavailable
    if not pool_enabled() or _pool_unavailable:
        return None
    dsn = _dsn()
    if _pool is None or _pool_dsn != dsn:
        with _pool_lock:
            if _pool is not None and _pool_dsn != dsn:
                # Env changed under us (tests, a re-pointed .env): a pool built
                # for the old DSN would hand out connections to the wrong DB —
                # or hang 30 s on a dead one. Rebuild.
                stale, _pool = _pool, None
                try:
                    stale.close()
                except Exception:
                    pass
            if _pool is None:
                try:
                    _pool = _make_pool()
                    _pool_dsn = dsn
                except ImportError:
                    # psycopg_pool not installed: degrade to connect-per-call,
                    # loudly, once — the slow path is a regression worth seeing.
                    _pool_unavailable = True
                    print("[db] psycopg_pool not installed — pooling disabled "
                          "(pip install 'psycopg[pool]')")
                    return None
    return _pool


def reset_pool() -> None:
    """Close and forget the pool (tests, and the web app's shutdown hook)."""
    global _pool, _pool_dsn, _pool_unavailable
    with _pool_lock:
        p, _pool, _pool_dsn = _pool, None, None
        _pool_unavailable = False
    if p is not None:
        try:
            p.close()
        except Exception:
            pass


def connect(*, autocommit: bool = False, pooled: bool | None = None) -> Any:
    """A connection to the Supabase Postgres DB.

    Usable as a context manager: ``with connect() as conn:`` commits on a clean
    exit, rolls back on exception, and hands the connection back — to the pool
    when pooled, closed otherwise — the same per-op lifecycle the old sqlite3
    ``connect()`` had. Rows are dicts (`dict_row`). Use this directly when a
    single logical operation spans several statements in one transaction
    (e.g. inventory.upsert_from_run); use the helpers below for one-shot
    queries.

    **Pooled by default.** A pooled checkout is only valid inside the ``with``
    block. Pass ``pooled=False`` when you need a private connection object you
    hold for a long time outside a ``with`` (session advisory locks, LISTEN/
    NOTIFY, VACUUM) — that path returns a plain ``psycopg.Connection`` exactly
    as before.

    ``autocommit=True`` commits each statement as it runs and always implies a
    private (unpooled) connection: a long batched backfill that should keep
    completed batches even if a later one fails, `LISTEN/NOTIFY` listeners, and
    `VACUUM`, which Postgres refuses to run inside a transaction block.
    """
    if autocommit or pooled is False:
        return psycopg.connect(_dsn(), **_conn_kwargs(autocommit=autocommit))
    pool = get_pool()
    if pool is None:
        return psycopg.connect(_dsn(), **_conn_kwargs())
    return pool.connection()


def _read(fn: Callable[[Any], T]) -> T:
    """Run a read on a pooled connection; retry ONCE on a stale checkout.

    A pooled connection the pooler quietly dropped (idle timeout, failover)
    raises OperationalError on first use. The pool discards it on return, so a
    second checkout is a fresh socket. Reads are idempotent, so the caller
    never sees the hiccup. Writes deliberately do not retry (a statement that
    committed just before the socket died would run twice)."""
    for attempt in (1, 2):
        try:
            with connect() as conn:
                return fn(conn)
        except psycopg.OperationalError:
            if attempt == 2 or get_pool() is None:
                raise
    raise AssertionError("unreachable")


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    return _read(lambda conn: conn.execute(sql, params or ()).fetchone())


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    return _read(lambda conn: conn.execute(sql, params or ()).fetchall())


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Run a single write statement; return the affected row count."""
    with connect() as conn:
        cur = conn.execute(sql, params or ())
        return cur.rowcount


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with connect() as conn:
        conn.cursor().executemany(sql, list(seq))

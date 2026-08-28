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
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row


def _dsn() -> str:
    dsn = os.getenv("BLACKWHOLE_DB_URL")
    if not dsn:
        raise RuntimeError(
            "BLACKWHOLE_DB_URL is not set — add it to the repo-root .env "
            "(see automation/config.py)."
        )
    return dsn


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    """Open a fresh connection to the Supabase Postgres DB.

    Usable as a context manager: ``with connect() as conn:`` commits on a clean
    exit, rolls back on exception, and closes the connection — the same per-op
    lifecycle the old sqlite3 ``connect()`` had. Rows are dicts (`dict_row`).
    Use this directly when a single logical operation spans several statements
    in one transaction (e.g. inventory.upsert_from_run); use the helpers below
    for one-shot queries.

    ``autocommit=True`` commits each statement as it runs. Needed whenever one
    session must span many statements and NOT hold a single transaction open:
    a long batched backfill that should keep completed batches even if a later
    one fails, `LISTEN/NOTIFY` listeners, and `VACUUM`, which Postgres refuses
    to run inside a transaction block. Documented on the shared `core.db`
    surface (workspace CLAUDE.md §14) but missing here until 2026-08-28, so
    `connect(autocommit=True)` raised TypeError in this repo only.
    """
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=autocommit)


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    with connect() as conn:
        return conn.execute(sql, params or ()).fetchone()


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with connect() as conn:
        return conn.execute(sql, params or ()).fetchall()


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Run a single write statement; return the affected row count."""
    with connect() as conn:
        cur = conn.execute(sql, params or ())
        return cur.rowcount


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with connect() as conn:
        conn.cursor().executemany(sql, list(seq))

"""automation/db.py must reuse connections (psycopg_pool) instead of opening a
fresh TLS+SCRAM handshake per query — that handshake is the entire admin-load
cost (measured 2026-09-11: ~1.4 s per connect vs <1 ms per query)."""
from __future__ import annotations

import contextlib

import psycopg
import pytest

from automation import db


class FakeConn:
    def __init__(self, n: int, fail_first: bool = False):
        self.n = n
        self.fail_first = fail_first
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql, params=None):
        if self.fail_first:
            self.fail_first = False
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        self.executed.append(sql)
        return self

    def fetchone(self):
        return {"n": self.n}

    def fetchall(self):
        return [{"n": self.n}]

    def cursor(self):
        return self

    def executemany(self, sql, seq):
        self.executed.append(sql)

    rowcount = 1

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    """Hands out the SAME connection every checkout, like a real pool would
    for a single-threaded caller."""

    def __init__(self, conns: list[FakeConn]):
        self.conns = conns
        self.checkouts = 0
        self.closed = False

    @contextlib.contextmanager
    def connection(self):
        self.checkouts += 1
        conn = self.conns[0]
        try:
            yield conn
        finally:
            if getattr(conn, "broken", False):
                self.conns.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("BLACKWHOLE_DB_URL", "postgresql://u:p@h/db")
    monkeypatch.delenv("BLACKWHOLE_DB_POOL", raising=False)
    db.reset_pool()
    yield
    db.reset_pool()


def test_connect_reuses_pooled_connection(monkeypatch):
    pool = FakePool([FakeConn(1)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    direct_calls = []
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: direct_calls.append(1))

    with db.connect() as c1:
        c1.execute("select 1")
    with db.connect() as c2:
        c2.execute("select 2")

    assert c1 is c2
    assert pool.checkouts == 2
    assert direct_calls == [], "pooled path must never open a direct connection"


def test_fetch_helpers_go_through_pool(monkeypatch):
    pool = FakePool([FakeConn(7)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    assert db.fetch_one("select 7")["n"] == 7
    assert db.fetch_all("select 7") == [{"n": 7}]
    assert db.execute("update x") == 1
    assert pool.checkouts == 3


def test_autocommit_bypasses_pool(monkeypatch):
    pool = FakePool([FakeConn(1)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    direct = FakeConn(9)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: direct)
    with db.connect(autocommit=True) as c:
        assert c is direct
    assert pool.checkouts == 0


def test_dedicated_bypasses_pool(monkeypatch):
    """recorder/cli.py holds a session advisory lock on one connection for a
    whole run — that must be a private connection, never a pooled one."""
    pool = FakePool([FakeConn(1)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    direct = FakeConn(9)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: direct)
    assert db.connect(pooled=False) is direct
    assert pool.checkouts == 0


def test_env_kill_switch_disables_pool(monkeypatch):
    monkeypatch.setenv("BLACKWHOLE_DB_POOL", "0")
    pool = FakePool([FakeConn(1)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    direct = FakeConn(9)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: direct)
    with db.connect() as c:
        assert c is direct
    assert pool.checkouts == 0


def test_pool_is_lazy_and_singleton(monkeypatch):
    made = []

    def _mk():
        made.append(1)
        return FakePool([FakeConn(1)])

    monkeypatch.setattr(db, "_make_pool", _mk)
    assert made == []            # importing / not querying builds nothing
    db.fetch_one("select 1")
    db.fetch_one("select 1")
    assert made == [1]


def test_read_retries_once_on_stale_pooled_connection(monkeypatch):
    """A pooled connection the pooler silently dropped raises OperationalError
    on first use. Reads retry once on a fresh checkout; the caller never sees it."""
    stale = FakeConn(0, fail_first=True)
    stale.broken = True
    good = FakeConn(3)
    pool = FakePool([stale, good])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    assert db.fetch_one("select 3")["n"] == 3
    assert pool.checkouts == 2


def test_write_does_not_retry(monkeypatch):
    stale = FakeConn(0, fail_first=True)
    pool = FakePool([stale, FakeConn(3)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    with pytest.raises(psycopg.OperationalError):
        db.execute("insert into x values (1)")
    assert pool.checkouts == 1


def test_reset_pool_closes(monkeypatch):
    pool = FakePool([FakeConn(1)])
    monkeypatch.setattr(db, "_make_pool", lambda: pool)
    db.fetch_one("select 1")
    db.reset_pool()
    assert pool.closed


def test_pool_is_rebuilt_when_dsn_changes(monkeypatch):
    """A test (or a re-pointed .env) that swaps BLACKWHOLE_DB_URL must not keep
    using a pool built for the previous database."""
    built = []

    def _mk():
        built.append(1)
        return FakePool([FakeConn(len(built))])

    monkeypatch.setattr(db, "_make_pool", _mk)
    assert db.fetch_one("select")["n"] == 1
    monkeypatch.setenv("BLACKWHOLE_DB_URL", "postgresql://u:p@other/db")
    assert db.fetch_one("select")["n"] == 2
    assert db.fetch_one("select")["n"] == 2
    assert built == [1, 1]

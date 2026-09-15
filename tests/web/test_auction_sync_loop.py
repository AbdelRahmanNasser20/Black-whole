"""The auction-expiry sync runs as a loop inside the web process.

Two things are worth pinning: that the pass runs OFF the event loop (a
blocking psycopg pass on the loop freezes every other tab — the 2026-09-11
diagnosis behind `test_event_loop_hygiene.py`), and that a bad pass cannot kill
the loop. No database and no network here; `sync_once` is replaced.
"""
from __future__ import annotations

import asyncio
import sys
import threading

import pytest

# `automation.web` re-exports the FastAPI instance as `.app`, so the plain
# `from automation.web import app` binds the object, not the module.
import automation.web.app  # noqa: F401

app_mod = sys.modules["automation.web.app"]


@pytest.fixture
def fake_sync(monkeypatch):
    """Replace `auction_sync.sync_once`, recording the thread it ran on."""
    from automation import auction_sync

    calls: dict = {"n": 0, "thread": None, "kwargs": None}

    def _sync_once(**kw):
        calls["n"] += 1
        calls["thread"] = threading.get_ident()
        calls["kwargs"] = kw
        return {"checked": 1, "expired": 0, "relisted": 0, "unchanged": 1,
                "unresolved": 0, "errors": 0}

    monkeypatch.setattr(auction_sync, "sync_once", _sync_once)
    return calls


def test_the_pass_runs_in_a_worker_thread_not_on_the_event_loop(fake_sync):
    async def go():
        main = threading.get_ident()
        await app_mod._auction_sync_tick()
        return main

    main_thread = asyncio.run(go())
    assert fake_sync["n"] == 1
    assert fake_sync["thread"] != main_thread, "sync_once must go through asyncio.to_thread"


def test_a_failing_pass_does_not_propagate(monkeypatch):
    from automation import auction_sync

    def _boom(**kw):
        raise RuntimeError("maestro 500")

    monkeypatch.setattr(auction_sync, "sync_once", _boom)
    asyncio.run(app_mod._auction_sync_tick())   # must not raise — the loop survives


def test_the_loop_is_started_at_startup_and_cancelled_at_shutdown(monkeypatch, fake_sync):
    monkeypatch.delenv("BLACKWHOLE_DB_URL", raising=False)   # skip the pool pre-warm

    async def _idle():
        await asyncio.sleep(3600)

    # The other two schedulers would hit the DB; this test is about ours.
    monkeypatch.setattr(app_mod, "_alerts_loop", _idle)
    monkeypatch.setattr(app_mod, "_tracking_loop", _idle)
    monkeypatch.setattr(app_mod, "_AUCTION_SYNC_ENABLED", True)
    monkeypatch.setattr(app_mod, "_auction_sync_task", None)

    async def go():
        await app_mod._start_alerts_loop()
        started = app_mod._auction_sync_task
        assert started is not None and not started.done()
        await app_mod._stop_alerts_loop()
        return started

    task = asyncio.run(go())
    assert task.cancelled() or task.done()


def test_the_loop_can_be_switched_off_by_env(monkeypatch, fake_sync):
    monkeypatch.delenv("BLACKWHOLE_DB_URL", raising=False)

    async def _idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_mod, "_alerts_loop", _idle)
    monkeypatch.setattr(app_mod, "_tracking_loop", _idle)
    monkeypatch.setattr(app_mod, "_AUCTION_SYNC_ENABLED", False)
    monkeypatch.setattr(app_mod, "_auction_sync_task", None)

    async def go():
        await app_mod._start_alerts_loop()
        assert app_mod._auction_sync_task is None
        await app_mod._stop_alerts_loop()

    asyncio.run(go())


def test_the_default_interval_is_half_an_hour():
    # Auctions close on a clock, not a heartbeat; 30 min is the same cold
    # cadence the bid tracker uses for a lot that is days out.
    assert app_mod._AUCTION_SYNC_INTERVAL_SEC == 1800.0


def test_the_pass_is_quiet_by_default(fake_sync):
    # The loop passes a no-op logger: a 30-minute heartbeat must not spam the
    # server log with one line per lot. The tick prints only when something
    # changed or errored.
    asyncio.run(app_mod._auction_sync_tick())
    assert callable(fake_sync["kwargs"]["log"])
    assert fake_sync["kwargs"]["log"]("anything") is None

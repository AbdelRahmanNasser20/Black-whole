"""The startup hook must return immediately — nothing is served until it does.

FastAPI serves no request, `/api/health` included, until every `on_event("startup")`
coroutine has returned. Anything slow there is boot latency at best; anything that
can hang (pgeocode 0.5.0 opens the GeoNames URL with no timeout, so a blackholed
connection never returns) is a server that never comes up.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import pytest

app_mod = importlib.import_module("automation.web.app")


@pytest.fixture(autouse=True)
def _no_background_loops(monkeypatch):
    """Let the hook run without leaving the real pollers ticking behind the test."""
    monkeypatch.delenv("BLACKWHOLE_DB_URL", raising=False)
    monkeypatch.setattr(app_mod, "_alerts_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(app_mod, "_tracking_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(app_mod, "_alerts_task", None)
    monkeypatch.setattr(app_mod, "_tracking_task", None)
    monkeypatch.setattr(app_mod, "_geo_warm_task", None)
    yield
    for name in ("_alerts_task", "_tracking_task", "_geo_warm_task"):
        task = getattr(app_mod, name, None)
        if isinstance(task, asyncio.Task):
            task.cancel()


@pytest.mark.asyncio
async def test_startup_does_not_wait_on_the_pgeocode_prewarm(monkeypatch):
    from automation.alerts import geo

    started = asyncio.Event()

    def slow_pgeocode():
        started.set()
        time.sleep(2)               # a stand-in for the GeoNames download
        return "nominatim"

    monkeypatch.setattr(geo, "_pgeocode_us", slow_pgeocode)
    t0 = time.monotonic()
    await app_mod._start_alerts_loop()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"startup blocked {elapsed:.2f}s on the pgeocode pre-warm"
    # Fired, not skipped: the warm-up is running, just not in the hook's way.
    assert isinstance(app_mod._geo_warm_task, asyncio.Task)
    assert not app_mod._geo_warm_task.done()

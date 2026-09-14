"""automation/web/readcache: short-TTL memo for read-only admin JSON handlers.

Flipping between tabs re-fetches the same tiny tables; each fetch is a
pooler round trip. A 15 s memo makes a revisit instant, and every write
through the API (POST/PATCH/PUT/DELETE) drops the whole memo so the UI never
shows a stale row after its own edit."""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation.web import readcache


def setup_function():
    readcache.invalidate_all()


def test_hit_within_ttl_skips_the_function():
    calls = []

    @readcache.cached(ttl=30)
    def handler(status: str | None = None):
        calls.append(status)
        return {"items": [status]}

    assert handler(status="x") == {"items": ["x"]}
    assert handler(status="x") == {"items": ["x"]}
    assert calls == ["x"]


def test_key_includes_arguments_and_defaults():
    calls = []

    @readcache.cached(ttl=30)
    def handler(status: str | None = None, with_stats: int = 0):
        calls.append((status, with_stats))
        return {"k": (status, with_stats)}

    handler()
    handler(status=None)          # same as default → hit
    handler(status="a")
    handler(with_stats=1)
    assert calls == [(None, 0), ("a", 0), (None, 1)]


def test_expires_after_ttl(monkeypatch):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(readcache.time, "monotonic", lambda: now[0])

    @readcache.cached(ttl=10)
    def handler():
        calls.append(1)
        return {"n": len(calls)}

    handler(); now[0] += 9; handler()
    assert calls == [1]
    now[0] += 2; handler()
    assert calls == [1, 1]


def test_invalidate_all_forces_recompute():
    calls = []

    @readcache.cached(ttl=30)
    def handler():
        calls.append(1)
        return {"n": len(calls)}

    handler(); readcache.invalidate_all(); handler()
    assert calls == [1, 1]


def test_signature_is_preserved_for_fastapi():
    import inspect

    @readcache.cached(ttl=1)
    def handler(status: str | None = None, limit: int = 5):
        return {}

    assert [p.name for p in inspect.signature(handler).parameters.values()] == ["status", "limit"]


def test_middleware_invalidates_on_successful_writes_only():
    app = FastAPI()
    app.middleware("http")(readcache.invalidate_on_write_middleware)
    calls = []

    @app.get("/api/thing")
    @readcache.cached(ttl=60)
    def read():
        calls.append(1)
        return {"n": len(calls)}

    @app.post("/api/thing")
    def write():
        return {"ok": True}

    @app.post("/api/bad")
    def bad():
        from fastapi import HTTPException
        raise HTTPException(400, "nope")

    c = TestClient(app)
    assert c.get("/api/thing").json() == {"n": 1}
    assert c.get("/api/thing").json() == {"n": 1}          # memo hit
    c.post("/api/bad")                                      # failed write: keep memo
    assert c.get("/api/thing").json() == {"n": 1}
    c.post("/api/thing")                                    # successful write: drop memo
    assert c.get("/api/thing").json() == {"n": 2}


def test_static_assets_are_cacheable_when_versioned():
    from automation.web.app import app
    c = TestClient(app)
    r = c.get("/static/app.css?v=123")
    assert r.status_code == 200
    assert "immutable" in r.headers.get("cache-control", "")
    r = c.get("/static/app.css")
    assert "immutable" not in r.headers.get("cache-control", "")
    assert "max-age" in r.headers.get("cache-control", "")


def test_every_response_reports_server_timing():
    from automation.web.app import app
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.headers.get("server-timing", "").startswith("app;dur=")


def test_render_health_check_does_not_hit_the_db():
    """Render probes healthCheckPath constantly; `/` ran 5+ queries per probe
    (1.7 M calls on the inventory stats statements by 2026-09-11)."""
    import re
    from pathlib import Path
    y = (Path(__file__).resolve().parents[2] / "render.yaml").read_text()
    web = y[y.index("name: black-whole-web"):]
    m = re.search(r"healthCheckPath:\s*(\S+)", web)
    assert m and m.group(1) == "/api/health"

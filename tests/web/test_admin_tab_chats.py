"""Sales › Chats: the admin view of storefront chat sessions (static/admin/chats.js, /api/chats)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import site_chat_store
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

ADMIN = Path("automation/web/static/admin")
ROW_TWIN = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + "</div>"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    readcache.invalidate_all()
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="chats"[^>]*>(.*?)</section>', html, re.S)
    assert m, "chats pane missing"
    return m.group(1)


def test_chats_pane_ships_skeleton_filter_and_own_css():
    pane = _pane()
    assert "/static/admin/chats.css" in pane and 'class="controls' in pane
    assert re.search(r'<div[^>]*id="chat-list"[^>]*data-state="loading"', pane)
    assert pane.count(ROW_TWIN) >= 5 and "Loading" not in pane
    assert re.findall(r'class="seg-btn(?: is-active)?" data-value="([a-z]+)"', pane) == [
        "answered", "completed", "open", "all"]
    assert 'id="chat-stats" hidden' in pane
    assert "storage_note" not in pane


def test_chats_css_and_js_follow_the_admin_rules():
    c = TestClient(app)
    assert c.get("/static/admin/chats.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", (ADMIN / "chats.css").read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "chats.css: use a token"
    for legacy in ("--ink", "--line", "--bg-elev", "--ok)", "--err)"):
        assert legacy not in css, legacy
    js = (ADMIN / "chats.js").read_text()
    assert "fetch(" not in js and "apiFetch" not in js and "withButtonLoading" not in js
    assert "from '../ui/state.js'" in js and "./shell.js" not in js
    # every stored value goes through an escape helper before it reaches innerHTML
    assert "escapeHtml(e.q)" in js and "escapeHtml(e.a)" in js and "escapeHtml(c.name" in js


def test_chats_api_lists_sessions_and_stats(monkeypatch):
    now = datetime.now(timezone.utc)
    item = {"id": "s1", "status": "completed", "step": "done", "name": "Dana", "created_at": now,
            "completed_at": now + timedelta(seconds=95), "log": [{"q": "ZIP?", "a": "30301"}]}
    seen = {}
    monkeypatch.setattr(site_chat_store, "schema_ready", lambda: True)
    monkeypatch.setattr(site_chat_store, "list_sessions", lambda view="answered": seen.setdefault("view", view) and [item])
    monkeypatch.setattr(site_chat_store, "stats", lambda days=30: {"days": days, "opened": 4})
    c = TestClient(app)
    r = c.get("/api/chats?view=completed")
    assert r.status_code == 200 and r.json()["items"][0]["name"] == "Dana" and seen["view"] == "completed"
    assert c.get("/api/chats/stats").json() == {"days": 30, "opened": 4}
    assert c.get("/api/chats?view=bogus").status_code == 400


def test_chats_api_says_how_to_apply_the_migration(monkeypatch):
    monkeypatch.setattr(site_chat_store, "schema_ready", lambda: False)
    r = TestClient(app).get("/api/chats")
    assert r.status_code == 409 and "038_chat_sessions.sql" in r.json()["detail"]


def test_chats_api_is_behind_the_admin_wall(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    auth_svc.reset_caches()
    r = TestClient(app).get("/api/chats", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)

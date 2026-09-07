"""Workstream E-drafts contract (plan §10 E2–E10): the Drafts pane ships a skeleton twin inside
data-state="loading" (no "Loading…" text), its fetch goes through UI.load, and its CSS lives in
static/admin/drafts.css (tokens only) — not app.css."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
DRAFTS_JS = STATIC / "admin/drafts.js"
DRAFTS_CSS = STATIC / "admin/drafts.css"
APP_CSS = STATIC / "app.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _drafts_pane(html: str) -> str:
    m = re.search(r'<section class="panel" data-pane="drafts".*?</section>', html, flags=re.S)
    assert m, "drafts pane missing"
    return m.group(0)


def test_drafts_pane_ships_skeleton_twin_not_loading_text():
    html = TestClient(app).get("/admin").text
    pane = _drafts_pane(html)
    assert re.search(r'<div[^>]*id="drafts-grid"[^>]*data-state="loading"', pane)
    assert 'class="sk-card"' in pane, "server-side skeleton twin (skeleton_cards macro)"
    assert "Loading" in html  # other tabs still carry theirs until their branch lands …
    assert "Loading" not in pane  # … but this pane never writes "Loading…" text again
    assert "drafts-empty" not in pane
    assert "storage_note" not in pane


def test_drafts_css_is_linked_and_served():
    c = TestClient(app)
    html = c.get("/admin").text
    assert 'href="/static/admin/drafts.css' in html
    assert c.get("/static/admin/drafts.css").status_code == 200


def test_drafts_js_uses_ui_load_not_fetch():
    src = DRAFTS_JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert "load(" in src
    assert "drafts-empty" not in src, "empty state goes through UI.renderEmpty (load's isEmpty/empty)"
    assert "Loading" not in src and "Scanning" not in src


def test_drafts_css_moved_out_of_app_css_tokens_only():
    css = DRAFTS_CSS.read_text()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for cls in (".drafts-grid", ".draft ", ".draft-head", ".draft-grid-imgs", ".draft-meta", ".draft-actions"):
        assert cls in body, cls
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "use a token"
    assert not re.search(r"--(ink|line|bg-elev)\b", body), "new token names only (no legacy aliases)"
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.S)
    assert not re.search(r"^\.draft\b", app_css, flags=re.M), "Drafts card rules must leave app.css"
    assert not re.search(r"^\.drafts-grid\b", app_css, flags=re.M)
    assert re.search(r"^\.drafts-empty\b", app_css, flags=re.M), "other tabs still use .drafts-empty — keep it"

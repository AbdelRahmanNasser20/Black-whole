"""E-listings-db contract (plan §10 E2–E10, tab = listings-db): the pane ships a server-side skeleton twin inside
data-state="loading" (no "Loading…"/"querying…" text, no spinner), the table is the shared .table and the pager
is the shared "Load more (N remaining)" button, its CSS lives in static/admin/listings-db.css (tokens only, no
legacy aliases) and is gone from app.css, listings_db.js reads through UI.load / mutates through UI.pending, and
filter state (q, source, offset) goes through shell.js-style getParams/setParams. States only — no redesign."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/listings_db.js"
CSS = STATIC / "admin/listings-db.css"
APP_CSS = STATIC / "app.css"
TAB_RULES = (".src-pill", ".ldb-qty", ".ldb-title-main", ".ldb-asset", ".ldb-conf", ".ldb-end.expired",
             ".ldb-launch.queued", "#ldb-q", "#ldb-form select")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="listings-db"[^>]*>(.*?)</section>', html, re.S)
    assert m, "listings-db pane missing"
    return m.group(1)


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    assert re.search(r'<div[^>]*id="ldb-wrap"[^>]*data-state="loading"', pane)
    for bad in ("Loading", "querying", "spinner", "pulse", "drafts-empty"):
        assert bad not in pane, bad
    # the twin is byte-for-byte the state.js `row` skeleton so the JS swap does not reflow
    twin = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + '</div>'
    assert pane.count(twin) >= 10
    assert "storage_note" not in pane


def test_pane_uses_shared_table_pager_and_own_stylesheet():
    pane = _pane()
    assert "/static/admin/listings-db.css" in pane
    for gone in ("ldb-table", 'class="ldb-pager"', "ldb-page-info", 'id="ldb-prev"', 'id="ldb-next"'):
        assert gone not in pane, gone
    assert re.search(r'<div[^>]*class="[^"]*\bpager\b[^"]*"[^>]*id="ldb-pager"', pane)
    assert re.search(r'<button[^>]*id="ldb-more"[^>]*>Load more</button>', pane)
    for kept in ('id="ldb-q"', 'id="ldb-source"', 'id="ldb-status"', 'id="ldb-status-bar"', 'id="ldb-refresh"', 'id="ldb-reset"'):
        assert kept in pane, kept


def test_listings_db_css_is_served_and_moved_out_of_app_css():
    assert TestClient(app).get("/static/admin/listings-db.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    assert not re.search(r"--(ink|line|bg-elev|ok|err)\b", css), "new token names only (no legacy aliases)"
    assert "rgba(" not in css, "use a token"
    for sel in TAB_RULES:
        assert sel in css, sel
    for gone in (".ldb-table", ".ldb-pager", ".ldb-page-info"):
        assert gone not in css, f"{gone}: use the shared .table / .pager"
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.S)
    assert not re.search(r"^\.ldb-|^#ldb-|^\.src-pill", app_css, flags=re.M), "Listings DB rules must leave app.css"
    assert "Listings DB" not in app_css


def test_listings_db_js_uses_primitives_and_url_params():
    src = JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    # URL state has shell.js semantics but must NOT import shell.js (second module instance → TDZ crash)
    assert "from './shell.js'" not in src
    assert "getParams(" in src and "setParams(" in src and "history[replace ? 'replaceState' : 'pushState']" in src
    for key in ("q", "source", "offset"):
        assert f"setParams({{{key}:" in src, key
    assert "withButtonLoading" not in src
    assert "keepOld" in src
    assert "Load more (" in src and "remaining)" in src
    for bad in ("querying listings.db", "Loading…", "drafts-empty", "spinner", "storage_note"):
        assert bad not in src, bad

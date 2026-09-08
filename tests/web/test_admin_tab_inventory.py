"""E-inventory contract (plan §10 E2–E10, tab = inventory): the pane ships a server-side skeleton twin inside
data-state="loading" (no "Loading…" text), its CSS lives in static/admin/inventory.css (no hex outside tokens),
inventory.js reads through UI.load / mutates through UI.pending / marks inline-edited rows is-pending, and
filter state (status, q) goes through shell.js getParams/setParams. storage_note never renders."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/inventory.js"
CSS = STATIC / "admin/inventory.css"
APP_CSS = STATIC / "app.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="inventory"[^>]*>(.*?)</section>', html, re.S)
    assert m, "inventory pane missing"
    return m.group(1)


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    assert 'data-state="loading"' in pane
    assert "sk-row" in pane
    assert "Loading" not in pane and "Loading…" not in pane
    assert "drafts-empty" not in pane
    # the twin is byte-for-byte the state.js `row` skeleton so the JS swap does not reflow
    twin = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + '</div>'
    assert pane.count(twin) >= 10
    assert "storage_note" not in pane


def test_pane_uses_shared_table_and_own_stylesheet():
    pane = _pane()
    assert "/static/admin/inventory.css" in pane
    assert 'class="inv-table' not in pane and "inv-table-wrap" not in pane
    assert 'id="inv-q"' in pane and 'id="inv-status-filter"' in pane


def test_inventory_css_is_served_and_moved_out_of_app_css():
    assert TestClient(app).get("/static/admin/inventory.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    for sel in (".inv-strip", ".strip-num", ".plat-ok", ".inv-add-grid", ".inv-hero", ".inv-loc-input"):
        assert sel in css, sel
    app_css = APP_CSS.read_text()
    for sel in (".inv-strip", ".strip-num", ".plat-ok", ".inv-add-grid", ".inv-hero", ".inv-loc-input"):
        assert sel not in app_css, f"{sel} still in app.css"


def test_inventory_js_uses_primitives_and_url_params():
    src = JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    # URL state has shell.js semantics but must NOT import shell.js: the entry is `shell.js?v=…`, so `./shell.js`
    # is a second module instance that re-boots every tab mid-evaluation (TDZ crash seen in the smoke).
    assert "from './shell.js'" not in src
    # E-polish: getParams/setParams come from shared.js (no per-tab mirror)
    assert re.search(r"import \{[^}]*\bgetParams\b[^}]*\bsetParams\b[^}]*\} from '\./shared\.js'", src)
    assert "history[replace ? 'replaceState' : 'pushState']" not in src, "no local mirror of setParams"
    assert "setParams({status:" in src and "setParams({q:" in src
    assert "withButtonLoading" not in src
    assert "'is-pending'" in src, "inline edits mark the <tr> is-pending"
    assert "keepOld" in src
    assert "storage_note" not in src

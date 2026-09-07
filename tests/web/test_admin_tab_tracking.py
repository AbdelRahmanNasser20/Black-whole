"""Workstream E-tracking contract (plan §10 E2–E10): the Tracking pane ships a skeleton twin inside
data-state="loading" (no "Loading…" text), every read goes through UI.load and every mutation through
UI.pending, the `label` filter lives in the URL via shell.js, and the tab's CSS lives in
static/admin/tracking.css (tokens only, shared .table) — not app.css."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
TRACKING_JS = STATIC / "admin/tracking.js"
TRACKING_CSS = STATIC / "admin/tracking.css"
APP_CSS = STATIC / "app.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _tracking_pane(html: str) -> str:
    m = re.search(r'<section class="panel" data-pane="tracking".*?</section>', html, flags=re.S)
    assert m, "tracking pane missing"
    return m.group(0)


def test_tracking_pane_ships_skeleton_twin_not_loading_text():
    html = TestClient(app).get("/admin").text
    pane = _tracking_pane(html)
    assert re.search(r'<div[^>]*id="trk-list"[^>]*data-state="loading"', pane)
    # server-side twin of skeleton('row', 6): six .sk-row with five .sk-cell each, byte-identical to state.js SK.row
    twin = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + '</div>'
    assert pane.count(twin) == 6
    assert "Loading" not in pane          # this pane never writes "Loading…" text again
    assert "drafts-empty" not in pane
    assert "inv-table" not in pane and "trk-table" not in pane, "shared .table / .table-wrap only"
    assert 'class="seg-btn is-active"' in pane, "components.css seg state, not app.css .active"
    assert "storage_note" not in pane
    # the operator controls keep their ids (tracking.js binds by id)
    for el_id in ("trk-ref", "trk-label", "trk-label-options", "trk-add", "trk-sync", "trk-refresh",
                  "trk-label-filter", "trk-summary"):
        assert f'id="{el_id}"' in pane, el_id


def test_tracking_css_is_linked_inside_the_pane_and_served():
    c = TestClient(app)
    html = c.get("/admin").text
    assert 'href="/static/admin/tracking.css' in _tracking_pane(html)
    head = html.split("{% block content %}")[0] if "{% block content %}" in html else html.split("<main", 1)[0]
    assert "tracking.css" not in head, "the <link> rides inside the pane block, not <head>"
    assert c.get("/static/admin/tracking.css").status_code == 200


def test_tracking_js_uses_ui_load_pending_and_url_params():
    src = TRACKING_JS.read_text()
    assert "fetch(" not in src and "apiFetch" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bapi\b[^}]*\} from '\.\./ui/state\.js'", src)
    # URL state comes from shell.js (E1 contract). test_admin_modules.py forbids a static `from './shell.js'`
    # today, so a dynamic import of the same module instance is accepted too.
    assert re.search(r"(from|import\()\s*'\./shell\.js'", src)
    # every §2 site: list + history are reads (load), add/rename/remove/sync are mutations (pending)
    assert "'/api/tracking'" in src and "/history`" in src
    for verb in ("'DELETE'", "'PATCH'", "'POST'"):
        assert verb in src, verb
    assert "/api/tracking/sync" in src
    assert "drafts-empty" not in src, "empty state goes through UI.renderEmpty (load's isEmpty/empty)"
    assert "Loading" not in src
    assert "seg-btn is-active" in src or "is-active" in src
    assert "label:" in src and "setParams(" in src and "getParams(" in src


def test_tracking_css_moved_out_of_app_css_tokens_only():
    css = TRACKING_CSS.read_text()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for cls in (".trk-handle", ".trk-label-cell", ".trk-drawer", ".trk-drawer-grid", ".trk-obs", ".trk-bidder",
                ".trk-rival", ".trk-hot", ".trk-err"):
        assert cls in body, cls
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "use a token"
    assert not re.search(r"--(ink|line|bg-elev|ok|err)\b", body), "new token names only (no legacy aliases)"
    assert "border-radius" not in body, "radius 0 everywhere (plan §1.2)"
    assert ".trk-table" not in body, "shared .table replaces .trk-table"
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.M)
    app_css = re.sub(r"/\*.*?\*/", "", app_css, flags=re.S)
    assert not re.search(r"^\.trk-", app_css, flags=re.M), "Tracking rules must leave app.css"
    assert not re.search(r"^\.ac-grow\b", app_css, flags=re.M), ".ac-grow was tracking-only (deals has .deal-topbar .ac-grow)"

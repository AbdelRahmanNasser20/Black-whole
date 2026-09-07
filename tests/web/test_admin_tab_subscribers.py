"""E-subscribers contract (plan §10 E2–E10, tab = subscribers): the pane ships a server-side skeleton twin inside
data-state="loading" (no "Loading…" text), its CSS lives in static/admin/subscribers.css (tokens only) and no
longer borrows the Inquiries tab's .inq-* rules, subscribers.js reads through UI.load / mutates through
UI.pending, and the status filter lives in the URL (?status=) via shell.js-style getParams/setParams."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/subscribers.js"
CSS = STATIC / "admin/subscribers.css"
APP_CSS = STATIC / "app.css"

ROW_TWIN = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + '</div>'


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="subscribers"[^>]*>(.*?)</section>', html, re.S)
    assert m, "subscribers pane missing"
    return m.group(1)


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    assert 'data-state="loading"' in pane
    assert "Loading" not in pane and "Loading…" not in pane
    assert "drafts-empty" not in pane
    # byte-for-byte the state.js `row` skeleton so the JS swap does not reflow
    assert pane.count(ROW_TWIN) >= 5
    assert 'id="sub-list"' in pane and "storage_note" not in pane


def test_pane_uses_own_stylesheet_and_shared_controls():
    pane = _pane()
    assert "/static/admin/subscribers.css" in pane
    assert "inq-" not in pane, "subscribers must not borrow the Inquiries tab's classes"
    assert 'id="sub-status-filter"' in pane and 'id="sub-refresh"' in pane
    assert 'class="controls' in pane
    assert 'class="seg-btn is-active"' in pane and 'class="seg-btn active"' not in pane
    for status in ("new", "contacted", "matched", "unsubscribed"):
        assert f'data-value="{status}"' in pane, status


def test_subscribers_css_is_served_tokens_only_and_out_of_app_css():
    assert TestClient(app).get("/static/admin/subscribers.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    for legacy in ("--ink", "--line", "--bg-elev", "--ok", "--err"):
        assert not re.search(rf"var\({legacy}\b", css), f"{legacy}: use the tokens.css name"
    for sel in (".sub-list", ".card-sub", ".sub-status-new", ".sub-status-unsubscribed", ".sub-danger"):
        assert sel in css, sel
    app_css = APP_CSS.read_text()
    for sel in (".inq-matched", ".inq-unsubscribed", ".inq-status-matched", ".inq-status-unsubscribed"):
        assert sel not in app_css, f"{sel} (subscriber-only) still in app.css"


def test_subscribers_js_uses_primitives_and_url_params():
    src = JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    # URL state has shell.js semantics but must NOT import shell.js (second module instance → TDZ crash).
    assert "from './shell.js'" not in src
    # E-polish: getParams/setParams come from shared.js (no per-tab mirror)
    assert re.search(r"import \{[^}]*\bgetParams\b[^}]*\bsetParams\b[^}]*\} from '\./shared\.js'", src)
    assert "history[replace ? 'replaceState' : 'pushState']" not in src, "no local mirror of setParams"
    assert "setParams({status:" in src
    assert "withButtonLoading" not in src and "apiFetch" not in src
    assert "keepOld" in src
    assert "innerHTML = '<div class=\"drafts-empty\">" not in src and "Loading…" not in src
    for status in ("'new'", "'contacted'", "'matched'", "'unsubscribed'"):
        assert status in src, status


def test_api_subscribers_status_filter_is_validated_client_side():
    """The seg values are the only statuses the tab will put in ?status= — an unknown value falls back to all."""
    src = JS.read_text()
    assert "statusValues()" in src and ".includes(p.status" in src

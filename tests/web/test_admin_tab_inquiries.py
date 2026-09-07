"""E-inquiries contract (plan §10 E2–E10, tab = inquiries): the pane ships a server-side skeleton twin inside
data-state="loading" (no "Loading…" text), its CSS lives in static/admin/inquiries.css (no hex outside tokens,
`.inq-card` → `.card` + modifier), inquiries.js reads through UI.load / mutates through UI.pending, and the
status filter lives in the URL (`?status=`) with shell.js getParams/setParams semantics. storage_note never renders."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/inquiries.js"
CSS = STATIC / "admin/inquiries.css"
APP_CSS = STATIC / "app.css"
SK_CARD = ('<div class="sk-card" aria-hidden="true"><div class="sk sk-band"></div><div class="sk sk-line" style="--w:40%"></div>'
           '<div class="sk sk-line"></div><div class="sk sk-line" style="--w:70%"></div><div class="sk sk-line" style="--w:50%"></div></div>')


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="inquiries"[^>]*>(.*?)</section>', html, re.S)
    assert m, "inquiries pane missing"
    return m.group(1)


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    assert re.search(r'<div[^>]*id="inq-list"[^>]*data-state="loading"', pane)
    # byte-for-byte the state.js `card` skeleton (skeleton_cards macro) so the JS swap does not reflow
    assert pane.count(SK_CARD) >= 5
    assert "Loading" not in pane
    assert "drafts-empty" not in pane
    assert "storage_note" not in pane


def test_pane_uses_own_stylesheet_and_shared_controls():
    pane = _pane()
    assert "/static/admin/inquiries.css" in pane
    assert 'id="inq-status-filter"' in pane and 'id="inq-refresh"' in pane
    assert 'class="seg-btn is-active"' in pane, "shared .seg-btn.is-active (components.css), not the legacy .active"
    assert "inq-list" not in pane.replace('id="inq-list"', ""), "legacy .inq-list class must not be used"
    assert "inq-card" not in pane


def test_inquiries_css_is_served_tokens_only_and_out_of_app_css():
    assert TestClient(app).get("/static/admin/inquiries.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    assert not re.search(r"--(ink|line|bg-elev)\b", css), "new token names only (no legacy aliases)"
    for sel in (".inquiry-list", ".card--inquiry", ".inquiry-kind", ".inquiry-msg", ".inquiry-actions"):
        assert sel in css, sel
    assert ".inq-card" not in css, "the legacy card class stays out of the new stylesheet"
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.S)
    for sel in (".inquiry-list", ".card--inquiry", ".inquiry-msg"):
        assert sel not in app_css, f"{sel} belongs in inquiries.css"


def test_inquiries_js_uses_primitives_and_url_params():
    src = JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    # URL state has shell.js semantics but must NOT import shell.js: the entry is `shell.js?v=…`, so `./shell.js`
    # would be a second module instance that re-boots every tab mid-evaluation.
    assert "from './shell.js'" not in src
    # E-polish: getParams/setParams come from shared.js (no per-tab mirror)
    assert re.search(r"import \{[^}]*\bgetParams\b[^}]*\bsetParams\b[^}]*\} from '\./shared\.js'", src)
    assert "history[replace ? 'replaceState' : 'pushState']" not in src, "no local mirror of setParams"
    assert "setParams({status:" in src
    assert "withButtonLoading" not in src
    assert "keepOld" in src
    assert "'/api/inquiries'" in src and "method: 'PATCH'" in src and "method: 'DELETE'" in src
    assert "Loading" not in src and "drafts-empty" not in src
    assert "inq-card" not in src and "inq-list" not in src.replace("#inq-list", "")
    assert "storage_note" not in src

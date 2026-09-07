"""E-test-scrape contract (plan §10 E2–E10, tab = test-scrape; §12: states only, no redesign): the pane ships a
server-side skeleton twin inside data-state="loading" (no "Loading…" / spinner / drafts-empty), its CSS lives in
static/admin/test-scrape.css (tokens only, radius 0, out of app.css), and test_scrape.js reads through UI.load
(fetch row #42) and runs the button through UI.pending. No URL params (the tab's only "read" is a live scrape the
operator triggers by hand — nothing may fire on activation or popstate). storage_note never renders."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/test_scrape.js"
CSS = STATIC / "admin/test-scrape.css"
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
    m = re.search(r'<section class="panel" data-pane="test-scrape"[^>]*>(.*?)</section>', html, re.S)
    assert m, "test-scrape pane missing"
    return m.group(1)


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    assert re.search(r'<div[^>]*id="ts-grid"[^>]*data-state="loading"', pane)
    # byte-for-byte the state.js `card` skeleton (skeleton_cards macro) so the JS swap does not reflow
    assert pane.count(SK_CARD) >= 4
    assert "Loading" not in pane
    assert "drafts-empty" not in pane and "spinner" not in pane
    assert "storage_note" not in pane


def test_pane_uses_own_stylesheet_and_shared_controls():
    pane = _pane()
    assert "/static/admin/test-scrape.css" in pane
    for el_id in ("ts-source", "ts-q", "ts-pages", "ts-run", "ts-status"):
        assert f'id="{el_id}"' in pane, el_id
    assert 'class="seg-btn is-active"' in pane, "shared .seg-btn.is-active (components.css), not the legacy .active"
    assert 'class="seg-btn active"' not in pane


def test_test_scrape_css_is_served_tokens_only_and_out_of_app_css():
    assert TestClient(app).get("/static/admin/test-scrape.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "use a token"
    assert not re.search(r"--(ink|line|bg-elev|err|ok)\b", css), "new token names only (no legacy aliases)"
    assert not re.search(r"border-radius:\s*[1-9]", css), "radius 0 everywhere (§1.2)"
    for sel in (".ts-input", ".ts-source-pill", ".ts-miss", ".ts-miss-pill", ".ts-keyword-group"):
        assert sel in css, sel
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.S)
    for sel in (".ts-input", ".ts-source-pill", ".ts-miss", ".ts-miss-pill", ".ts-keyword-group"):
        assert sel not in app_css, f"{sel} belongs in test-scrape.css"


def test_test_scrape_js_uses_primitives_and_never_autoruns():
    src = JS.read_text()
    assert "fetch(" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert "from './shell.js'" not in src
    assert "withButtonLoading" not in src
    assert "'/api/test-scrape?" in src or "`/api/test-scrape?" in src
    assert "skeleton: 'card'" in src
    assert "renderEmpty" in src or "empty:" in src
    assert "Loading" not in src and "drafts-empty" not in src and "spinner" not in src
    assert "is-loading" not in src
    # the read is a live scrape: it must only ever start from the button / Enter / a CTA the operator clicks
    assert "export function load()" in src
    load_body = src.split("export function load()", 1)[1]
    assert "runTestScrape" not in load_body and "/api/test-scrape" not in load_body
    assert "storage_note" not in src

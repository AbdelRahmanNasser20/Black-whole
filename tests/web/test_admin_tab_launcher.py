"""Plan §10 E2 (ui/e-launcher): the Launcher pane ships a server-side skeleton twin inside data-state="loading",
its fetch sites go through UI.load / UI.pending, the SSE stream marks the phase grid stale, and its CSS lives in
static/admin/launcher.css (no hex outside tokens) instead of app.css."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
LAUNCHER_JS = STATIC / "admin/launcher.js"
LAUNCHER_CSS = STATIC / "admin/launcher.css"
APP_CSS = STATIC / "app.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _launcher_pane() -> str:
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="launcher">(.*?)</section>\s*\n\s*\n', html, re.S)
    assert m, "launcher pane missing"
    return m.group(0)


def test_pane_ships_skeleton_twin_inside_loading_state():
    pane = _launcher_pane()
    grid = re.search(r'<div class="phase-grid" id="phase-grid"([^>]*)>(.*?)</div>\s*\n\s*\n', pane, re.S)
    assert grid, "phase grid missing"
    attrs, body = grid.group(1), grid.group(2)
    assert 'data-state="loading"' in attrs
    assert 'data-phases="scrape,llm,download,dewatermark,facebook,ebay"' in attrs
    # one skeleton line per phase body — the twin the JS replaces without reflow
    assert body.count('<div class="phase-body" data-body>') == 6
    assert body.count('class="sk sk-line"') >= 6
    assert "Loading" not in pane and "spinner" not in pane


def test_pane_links_its_own_stylesheet_and_run_button_is_text_only():
    pane = _launcher_pane()
    assert 'href="/static/admin/launcher.css?v=' in pane
    # UI.pending swaps button.textContent, so the ▶ button must not carry child spans that would be flattened
    run_btn = re.search(r'<button[^>]*id="run-btn"[^>]*>(.*?)</button>', pane, re.S)
    assert run_btn and "<span" not in run_btn.group(1)
    assert "run-btn-label" not in pane
    assert "btn-glyph" not in pane


def test_launcher_css_is_served_and_app_css_lost_the_launcher_rules():
    assert TestClient(app).get("/static/admin/launcher.css").status_code == 200
    css = LAUNCHER_CSS.read_text()
    for sel in (".launcher {", ".url-field {", ".phase-grid {", ".phase {", ".price-prompt {", ".console {",
                ".queue-strip {", ".toggle-state {", ".opts-copy {"):
        assert sel in css, sel
    app_css = APP_CSS.read_text()
    for sel in (".launcher {", ".url-field {", ".phase-grid {", ".phase {", ".price-prompt {", ".console {",
                ".queue-strip {", ".toggle-state {", ".opts-copy {", ".btn-glyph {"):
        assert sel not in app_css, f"{sel} still in app.css"
    assert "border-radius: 4px" not in css and "rgba(255,255,255" not in css


def test_launcher_js_uses_the_primitives():
    src = LAUNCHER_JS.read_text()
    assert "fetch(" not in src
    assert "withButtonLoading" not in src
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    for endpoint in ("/api/runs/start", "/api/runs/cancel", "/api/runs/queue/clear", "/api/runs/stdin"):
        assert endpoint in src, endpoint
    assert src.count("pending(") >= 4, "the four mutations go through UI.pending"
    # the one read (/api/runs/state on tab activation) goes through UI.load with keepOld
    assert "uiLoad(" in src and "keepOld: true" in src and "/api/runs/state" in src
    # SSE: onerror → markStale, onopen → clearStale
    assert "new EventSource('/api/runs/stream')" in src
    assert "markStale(" in src and "clearStale(" in src


def test_no_storage_note_anywhere_in_the_pane():
    assert "storage_note" not in _launcher_pane()

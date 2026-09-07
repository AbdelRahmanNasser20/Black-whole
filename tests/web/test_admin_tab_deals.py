"""Admin Deals tab (plan §10 E-deals): the pane ships server-side skeletons inside data-state="loading",
never "Loading…" text; its CSS lives in static/admin/deals.css (tokens only); its JS never calls fetch() raw;
filter state lives in the URL (shell.js getParams/setParams), not localStorage."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _deals_pane() -> str:
    html = TestClient(app).get("/admin?tab=deals").text
    # the pane nests <section class="section"> boxes, so cut at the next pane instead of the first </section>
    m = re.search(r'<section class="panel" data-pane="deals".*?(?=<section class="panel" data-pane="listings-db")', html, flags=re.S)
    assert m, "deals pane missing"
    return m.group(0)


def test_deals_pane_ships_skeletons_not_loading_text():
    pane = _deals_pane()
    assert "Loading…" not in pane and "Loading deals" not in pane
    # tree + table body: state attribute + the byte-identical twin of skeleton('line'|'tr', n)
    assert 'id="deal-tree-nodes" data-state="loading"' in pane
    assert 'id="deal-rows" data-state="loading"' in pane
    assert pane.count('<div class="sk sk-line" aria-hidden="true"></div>') >= 6
    assert pane.count('<tr class="sk-tr" aria-hidden="true"><td colspan="99"><div class="sk sk-line"></div></td></tr>') == 12
    # shared primitives replace the per-tab prefixed ones (1:1 only)
    assert 'class="table-wrap"' in pane and 'class="table" id="deal-table"' in pane
    assert "inv-table" not in pane
    assert 'class="drawer" id="deal-drawer"' in pane
    assert "storage_note" not in pane


def test_deals_css_is_its_own_file_with_tokens_only():
    assert TestClient(app).get("/static/admin/deals.css").status_code == 200
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "admin/deals.css").read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "deals.css: use a token"
    assert not re.search(r"border-radius:\s*\d+px", css), "deals.css: radius is var(--radius) / 0"
    assert "/static/admin/deals.css" in _deals_pane()
    # the Deals section left app.css
    app_css = (STATIC / "app.css").read_text()
    for cls in (".deal-", ".dt-node", ".cat-pill", "#deal-"):
        assert cls not in app_css, f"{cls} still in app.css"


def test_deals_js_uses_the_loading_primitives_and_url_state():
    src = (STATIC / "admin/deals.js").read_text()
    assert "fetch(" not in src, "deals.js: use UI.load / UI.pending / apiFetch"
    assert "localStorage" not in src, "deals.js: ?map=1 replaces admin.dealMapOn"
    # shell.js's getParams/setParams contract — but never `import './shell.js'` (the `?v=` script URL makes that a
    # second shell instance that boots the admin twice); the helpers are used by name so shared.js can own them later
    assert "from './shell.js'" not in src
    assert "getParams" in src and "setParams" in src and "replaceState" in src
    assert "from '../ui/state.js'" in src
    for name in ("load(", "pending(", "markStale("):
        assert name in src, name
    assert "keepOld: true" in src

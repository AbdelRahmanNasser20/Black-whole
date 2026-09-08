"""E-auctions contract (plan §10 E2–E10, tab = auctions): the pane ships a server-side skeleton twin inside
data-state="loading" (no "Loading…" text), its CSS lives in static/admin/auctions.css (no hex), every read in
auctions.js goes through UI.load / UI.api and every mutation through UI.pending, and filter state lives in the
URL (source, q, profile, map) with shell.js's param semantics — no localStorage toggle."""
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
JS = STATIC / "admin/auctions.js"
CSS = STATIC / "admin/auctions.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane() -> str:
    html = TestClient(app).get("/admin").text
    after = html.split('data-pane="auctions"', 1)[1]
    return after.split('<section class="panel"', 1)[0]


def test_pane_ships_skeleton_twin_not_loading_text():
    pane = _pane()
    grid = re.search(r'<div class="grid grid-auction" id="auction-grid" data-state="loading"[^>]*>(.*?)</div>\s*</section>', pane, re.S)
    assert grid, "auction grid must carry data-state=loading"
    assert grid.group(1).count('class="sk-card"') == 8
    assert 'id="auc-profile" data-state="loading"' in pane and 'sk-pill' in pane
    assert 'id="auction-cache-stats" data-state="loading"' in pane
    assert 'id="auction-favorites-grid" data-state="loading"' in pane
    for bad in ("Loading", "loading…", "fetching listings", 'class="spinner"', "drafts-empty"):
        assert bad not in pane, bad
    assert "storage_note" not in pane


def test_pane_loads_its_own_css_and_it_is_served():
    pane = _pane()
    assert '/static/admin/auctions.css' in pane
    assert TestClient(app).get("/static/admin/auctions.css").status_code == 200
    body = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "use a token"
    assert not re.search(r"border-radius:\s*\d*[1-9]\d*px", body), "radius drift — use var(--radius)"


def test_auction_css_left_app_css():
    app_css = (STATIC / "app.css").read_text()
    for sel in (".auction-card {", ".fav-card {", ".fav-strip {", ".auction-star {", ".scrape-strip {",
                ".dropdown-menu {", ".staleness-banner {", ".auction-filter-summary {"):
        assert sel not in app_css, f"{sel} still in app.css — moved to admin/auctions.css"
    css = CSS.read_text()
    for sel in (".card-auction", ".card-fav", ".fav-strip", ".auction-star", ".scrape-strip", ".staleness-banner"):
        assert sel in css, sel


def test_js_uses_ui_primitives_and_url_params():
    src = JS.read_text()
    assert "fetch(" not in src, "reads go through UI.load / UI.api"
    assert "withButtonLoading" not in src, "mutations go through UI.pending"
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    # shell.js is loaded as shell.js?v=<asset_v>; importing './shell.js' would boot a second shell (see auctions.js)
    assert "from './shell.js'" not in src
    # E-polish: URL params come from shared.js — no local mirror
    assert re.search(r"import \{[^}]*\bgetParams\b[^}]*\bsetParams\b[^}]*\} from '\./shared\.js'", src)
    assert not re.search(r"function _?(get|set)Params\(", src)
    assert "markStale(" in src and "clearStale(" in src
    assert "localStorage.setItem" not in src, "?map= replaces localStorage.admin.aucMapOn"
    assert "'admin.aucMapOn'" in src and "localStorage.removeItem(" in src, "one-time migration of the old key"
    for key in ("source", "q", "profile", "map"):
        assert re.search(rf"setParams\(\{{[^}}]*\b{key}\b", src), f"URL key {key} not written"
    assert "30000" in src, "favorites poll stays 30 s"
    r = subprocess.run(["node", "--check", "--input-type=module"], input=src, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

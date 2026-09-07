"""Workstream F contract: /admin loads ES modules under static/admin/, app.js is gone,
every tab module exports mount()/load(), tabs only import shared.js / ../ui, and every module parses."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

ADMIN = Path("automation/web/static/admin")
TABS = ["launcher", "drafts", "auctions", "inventory", "inquiries", "subscribers",
        "listings_db", "test_scrape", "deals", "tracking"]


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_admin_shell_loads_modules_not_app_js():
    html = TestClient(app).get("/admin").text
    assert 'type="module" src="/static/admin/main.js' in html
    assert "/static/app.js" not in html
    assert not Path("automation/web/static/app.js").exists()


def test_admin_modules_are_served():
    c = TestClient(app)
    for name in ["main", "shared"] + TABS:
        assert c.get(f"/static/admin/{name}.js").status_code == 200, name


@pytest.mark.parametrize("tab", TABS)
def test_tab_module_exports_mount_and_load(tab):
    src = (ADMIN / f"{tab}.js").read_text()
    assert re.search(r"export (async )?function mount\(", src), tab
    assert re.search(r"export (async )?function load\(", src), tab
    assert not re.search(r"from '\./(?!shared)", src), f"{tab}: tabs import only shared.js / ../ui"


def test_main_mounts_every_tab_then_restores():
    src = (ADMIN / "main.js").read_text()
    for tab in TABS:
        assert f"from './{tab}.js'" in src, tab
    assert src.index("t.mount()") < src.index("restoreLastTab();\n"), "mount all tabs before restoring the saved tab"


@pytest.mark.parametrize("f", sorted(ADMIN.glob("*.js")) if ADMIN.exists() else [], ids=lambda f: f.name)
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_module_syntax(f):
    r = subprocess.run(["node", "--check", "--input-type=module"], input=f.read_text(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

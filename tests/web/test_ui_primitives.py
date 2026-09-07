"""Design-system contract: primitives exist, are served, and new modules never call fetch() directly."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
NEW_DIRS = ("ui", "deals", "admin", "site")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_ui_static_files_are_served():
    c = TestClient(app)
    for name in ("tokens.css", "components.css", "skeleton.css", "state.js", "card.js"):
        assert c.get(f"/static/ui/{name}").status_code == 200, name


def test_tokens_hold_the_operator_palette():
    css = (STATIC / "ui/tokens.css").read_text()
    for token, value in {"--bg": "#0c0c0e", "--surface": "#131316", "--border": "#25252c",
                         "--text": "#ece8dd", "--muted": "#9e988a", "--accent": "#ffb547",
                         "--danger": "#ff6c4d", "--success": "#6cd47e", "--radius": "0px"}.items():
        assert re.search(rf"{re.escape(token)}:\s*{re.escape(value)}", css), token


def test_no_hardcoded_hex_outside_tokens():
    for d in NEW_DIRS:
        for f in (STATIC / d).glob("*.css"):
            if f.name == "tokens.css":
                continue
            body = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), f"{f}: use a token"


def test_no_raw_fetch_in_new_modules():
    for d in NEW_DIRS:
        for f in (STATIC / d).glob("*.js"):
            if f == STATIC / "ui/state.js":
                continue
            assert "fetch(" not in f.read_text(), f"{f}: use UI.load / UI.pending / UI.api"


def test_preview_page_renders_every_state():
    html = TestClient(app).get("/admin/ui").text
    for state_id in ("ex-skeleton-card", "ex-skeleton-tr", "ex-empty", "ex-error", "ex-stale", "ex-pending", "ex-card"):
        assert f'id="{state_id}"' in html, state_id
    assert "storage_note" not in html


def test_state_js_exports_the_contract():
    src = (STATIC / "ui/state.js").read_text()
    for name in ("esc", "fmt", "toast", "api", "skeleton", "setBusy", "renderEmpty",
                 "renderError", "markStale", "clearStale", "pending", "load"):
        assert re.search(rf"export (async )?(function|const) {name}\b", src), name
    assert "window.UI" in src

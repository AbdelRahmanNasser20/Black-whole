"""Channels tab contract (multichannel master plan, Phase 1.6): a 12th rail tab whose pane ships
its skeleton twins inside data-state="loading", one switch per channel rendered SERVER-SIDE (so a
channel can never be missing from the admin), the FB Marketplace red "OFF — waiting for operator"
pill, an approval queue with Approve/Reject, the lot × channel matrix, and a `Sync now` button.
Every read goes through UI.load, every mutation through UI.pending, and the CSS lives in
static/admin/channels.css (tokens only, radius 0) — not app.css."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import channels as channels_pkg
from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
CH_JS = STATIC / "admin/channels.js"
CH_CSS = STATIC / "admin/channels.css"
APP_CSS = STATIC / "app.css"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _pane(html: str) -> str:
    m = re.search(r'<section class="panel" data-pane="channels".*?</section>', html, flags=re.S)
    assert m, "channels pane missing"
    return m.group(0)


def test_rail_has_a_channels_tab_with_a_pane():
    html = TestClient(app).get("/admin").text
    assert re.search(r'<a class="rail-tab" data-tab="channels" href="\?tab=channels"[^>]*>'
                     r'<span class="rail-tab-num">12</span><span class="rail-tab-label">Channels</span></a>', html)
    _pane(html)


def test_channels_pane_ships_switches_queue_matrix_and_sync_now():
    html = TestClient(app).get("/admin").text
    pane = _pane(html)
    # switches: master + one per channel, rendered by the server from automation.channels.CHANNELS
    assert 'data-key="channels_master_enabled"' in pane
    for c in channels_pkg.CHANNELS:
        assert f'data-key="channel_{c}_enabled"' in pane, c
    assert pane.count('<input type="checkbox" data-key="') == 1 + len(channels_pkg.CHANNELS)
    # FB Marketplace: the red pill the operator has to consciously turn off
    assert "OFF — waiting for operator" in pane
    assert re.search(r'id="ch-pill-fb_marketplace"', pane)
    # queue + matrix ship their skeleton twins (no "Loading…" text)
    for el_id in ("ch-queue", "ch-matrix"):
        assert re.search(rf'<div[^>]*id="{el_id}"[^>]*data-state="loading"', pane), el_id
    twin = '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>' * 5 + '</div>'
    assert pane.count(twin) >= 2
    assert "Loading" not in pane
    assert "storage_note" not in pane
    # the operator buttons keep their ids (channels.js binds by id)
    assert 'id="ch-sync"' in pane and "Sync now" in pane
    assert 'id="ch-refresh"' in pane


def test_channels_css_is_linked_inside_the_pane_and_served():
    c = TestClient(app)
    html = c.get("/admin").text
    assert 'href="/static/admin/channels.css' in _pane(html)
    head = html.split("<main", 1)[0]
    assert "channels.css" not in head, "the <link> rides inside the pane block, not <head>"
    assert c.get("/static/admin/channels.css").status_code == 200
    assert c.get("/static/admin/channels.js").status_code == 200


def test_channels_js_uses_ui_load_pending_and_the_channel_routes():
    src = CH_JS.read_text()
    assert "fetch(" not in src and "apiFetch" not in src
    assert re.search(r"import \{[^}]*\bload\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bpending\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert re.search(r"import \{[^}]*\bapi\b[^}]*\} from '\.\./ui/state\.js'", src)
    assert "shell.js'" not in src
    assert re.search(r"export (async )?function mount\(", src)
    assert re.search(r"export (async )?function load\(", src)
    # the four routes from Phase 1.5, and nothing outside /api/channels
    assert "'/api/channels'" in src
    assert "/api/channels/switches" in src and "'PATCH'" in src
    assert "/api/channels/queue/" in src and "/approve" in src and "/reject" in src
    assert "/api/channels/sync" in src
    for label in ("Sync now", "Approve", "Reject", "waiting for operator"):
        assert label in src, label
    # one CSS class per state so the matrix cell colour is data-driven
    for state in channels_pkg.STATES:
        assert f"ch-cell--{state}" in src, state
    assert "Loading" not in src
    assert "storage_note" not in src


def test_channels_css_tokens_only_and_out_of_app_css():
    css = CH_CSS.read_text()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for cls in (".ch-switch", ".ch-pill", ".ch-cell--off", ".ch-cell--queued", ".ch-cell--pending_approval",
                ".ch-cell--live", ".ch-cell--delisted", ".ch-cell--error"):
        assert cls in body, cls
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "use a token"
    assert not re.search(r"--(ink|line|bg-elev|ok|err)\b", body), "new token names only (no legacy aliases)"
    assert "border-radius" not in body, "radius 0 everywhere (plan §1.2)"
    app_css = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(), flags=re.S)
    assert not re.search(r"^\.ch-", app_css, flags=re.M), "Channels rules live in channels.css"

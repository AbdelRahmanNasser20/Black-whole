"""The A/B (compare) tab was removed 2026-09-04. Guard against it creeping back."""
import re

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_admin_has_no_compare_tab():
    html = TestClient(app).get("/admin").text
    assert 'data-tab="compare"' not in html
    assert 'data-pane="compare"' not in html
    # 12 rail tabs after the A/B removal, the Deposits addition and the Channels tab (Phase 1.6) (regex so `rail-tab-num`
    # spans don't count) — E1 rail markup. Every rail tab must have a pane and a shell module.
    tabs = re.findall(r'<a class="rail-tab(?: is-active)?" data-tab="([a-z-]+)"', html)
    assert len(tabs) == 12, tabs
    assert "compare" not in tabs
    for t in tabs:
        assert f'data-pane="{t}"' in html, t


def test_compare_api_is_gone():
    assert TestClient(app).get("/api/compare").status_code == 404

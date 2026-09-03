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
    # 10 tab buttons after removal (regex so `class="tabs"` / `tab-num` spans don't count)
    assert len(re.findall(r'<button class="tab(?: active)?" data-tab="', html)) == 10


def test_compare_api_is_gone():
    assert TestClient(app).get("/api/compare").status_code == 404

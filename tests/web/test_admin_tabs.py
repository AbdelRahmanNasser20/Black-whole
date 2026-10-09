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
    # 12 rail tabs (Chairs, 2026-10-09, is the 12th): the Sales group (2026-10-02) folded Inquiries, Subscribers and Deposits into one tab and added
    # Quotes; Archive is the 11th (regex so `rail-tab-num` spans don't count) — E1 rail markup.
    tabs = re.findall(r'<a class="rail-tab(?: is-active)?" data-tab="([a-z-]+)"', html)
    assert tabs == ["launcher", "drafts", "auctions", "inventory", "sales", "listings-db", "test-scrape",
                    "deals", "tracking", "channels", "archive", "chairs"], tabs
    assert "compare" not in tabs
    # Every rail tab has a pane and a shell module — except `sales`, which is a GROUP: it opens one of four views,
    # each of which is still its own pane (and its own `?tab=` value, so old links keep working).
    for t in tabs:
        if t != "sales":
            assert f'data-pane="{t}"' in html, t
    assert 'data-pane="sales"' not in html
    for view in SALES_VIEWS:
        assert f'data-pane="{view}"' in html, view


SALES_VIEWS = ["quotes", "inquiries", "deposits", "subscribers"]


def _admin() -> str:
    return TestClient(app).get("/admin").text


def test_sales_rail_tab_opens_quotes_and_carries_a_badge():
    html = _admin()
    m = re.search(r'<a class="rail-tab" data-tab="sales" href="\?tab=quotes"[^>]*>(.*?)</a>', html, re.S)
    assert m, "Sales rail tab missing"
    assert '<span class="rail-tab-num">05</span><span class="rail-tab-label">Sales</span>' in m.group(1)
    assert 'id="sales-badge" hidden' in m.group(1)       # empty until /api/sales/counts answers
    for gone in ("inquiries", "subscribers", "deposits"):
        assert f'data-tab="{gone}"' not in html, f"{gone} must not be a rail tab any more"


def test_sales_view_switch_lists_the_four_views_in_key_order():
    html = _admin()
    nav = re.search(r'<nav class="sales-nav" id="sales-nav"[^>]*hidden>(.*?)</nav>', html, re.S)
    assert nav, "Sales view switch missing (or not hidden by default)"
    views = re.findall(r'data-sales-view="([a-z]+)" href="\?tab=([a-z]+)"', nav.group(1))
    assert views == [(v, v) for v in SALES_VIEWS]
    assert re.findall(r"<kbd>([a-z])</kbd>", nav.group(1)) == ["q", "i", "d", "s"]


def test_quotes_pane_ships_skeleton_filter_and_banner():
    html = _admin()
    m = re.search(r'<section class="panel" data-pane="quotes"[^>]*>(.*?)</section>', html, re.S)
    assert m, "quotes pane missing"
    pane = m.group(1)
    assert "/static/admin/quotes.css" in pane
    assert re.search(r'<div[^>]*id="quo-list"[^>]*data-state="loading"', pane)
    assert pane.count('class="sk-card"') >= 5 and "Loading" not in pane
    assert re.findall(r'class="seg-btn(?: is-active)?" data-value="([a-z]+)"', pane) == [
        "open", "new", "answered", "won", "lost", "junk", "all"]
    assert 'id="quo-banner" role="status" hidden' in pane
    assert "storage_note" not in pane


def test_quotes_css_is_served_and_tokens_only():
    from pathlib import Path
    assert TestClient(app).get("/static/admin/quotes.css").status_code == 200
    for name in ("quotes.css", "shell.css"):
        css = re.sub(r"/\*.*?\*/", "", Path("automation/web/static/admin", name).read_text(), flags=re.S)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), f"{name}: use a token"


def test_shell_groups_the_sales_views_and_binds_the_keys():
    from pathlib import Path
    src = Path("automation/web/static/admin/shell.js").read_text()
    assert "const SALES_VIEWS = ['quotes', 'inquiries', 'deposits', 'subscribers'];" in src
    assert "const SALES_CODES = {KeyQ: 'quotes', KeyI: 'inquiries', KeyD: 'deposits', KeyS: 'subscribers'};" in src
    # the shortcuts never fire while typing, with a modifier held, or under the deal drawer / a dialog
    for needle in ("isTyping(e.target)", "e.metaKey || e.ctrlKey || e.altKey",
                   "#deal-drawer.is-open, dialog[open]",
                   "e.code === 'BracketLeft' || e.code === 'BracketRight'", "(?:Digit|Numpad)([0-9])"):
        assert needle in src, needle
    # physical keys, not characters: an Arabic layout types ج د ض and Arabic-Indic digits on the same keys
    assert "e.key ===" not in src and ".test(e.key)" not in src
    assert "'/api/sales/counts'" in src


def test_deep_link_script_lights_the_sales_tab_for_a_sales_view():
    """The pre-shell inline script must not flash Launcher on ?tab=deposits."""
    html = _admin()
    inline = html.split("show the ?tab= pane before shell.js runs")[1].split("</script>")[0]
    assert "['quotes', 'inquiries', 'deposits', 'subscribers']" in inline
    assert "getElementById('sales-nav')" in inline


def test_compare_api_is_gone():
    assert TestClient(app).get("/api/compare").status_code == 404

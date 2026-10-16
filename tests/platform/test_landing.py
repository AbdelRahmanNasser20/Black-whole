"""GET /platform — the landing sells the free beta honestly.

What this guards:
  · the signup CTA: prominent "Get free early access" links to /platform/signup,
    founding-member framing, and NO live billing UI (billing ships dark);
  · every number is real: the hero stats are platform_api.stats() (live DB counts)
    rendered server-side; the template itself carries no baked-in big number;
  · the safe fallback: stats unavailable (DB down / first view) -> the page still
    renders, with the source count only — never a 500, never an invented figure;
  · platform_api.stats() never blocks the request path: it answers from a memo
    and refreshes in a background thread; a failed refresh keeps the last good
    numbers and never raises.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import platform_api
from automation.web.app import app

TEMPLATE = Path("automation/web/templates/platform.html").read_text()
STATS = {"sold_outcomes": 219336, "open_now": 14478}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    from automation.web import auth as auth_svc
    auth_svc.reset_caches()
    platform_api.clear_cache()
    yield
    platform_api.clear_cache()
    auth_svc.reset_caches()


def _page(monkeypatch, stats):
    monkeypatch.setattr(platform_api, "stats", lambda: stats)
    r = TestClient(app, base_url="https://testserver").get("/platform")
    assert r.status_code == 200
    return r.text


# ───────────────────────── the CTA ─────────────────────────

def test_platform_page_has_signup_cta(monkeypatch):
    html = _page(monkeypatch, STATS)
    assert 'href="/platform/signup"' in html
    assert "Get free early access" in html
    assert "Founding members" in html
    # the CTA is page-wide: hero, pricing and the closing band all carry it
    assert html.count('href="/platform/signup"') >= 3
    # free-beta framing, no card
    assert "Free while the beta runs" in html and "No card" in html


def test_billing_stays_dark_on_the_landing(monkeypatch):
    html = _page(monkeypatch, STATS)
    assert "/platform/api/subscribe" not in html
    assert "stripe" not in html.lower()
    # the Pro tease names no number: no dollar amount anywhere in the pricing section
    pricing = html.split('id="pricing"', 1)[1].split("</section>", 1)[0]
    assert "Not priced yet" in pricing
    assert not re.search(r"\$\s*\d", pricing)
    assert "preferred pricing" in pricing and "first access" in pricing
    # the Pro tease names only real, free-tier-distinct advantages — never an
    # unbuilt feature and never a free feature moved behind a paywall
    assert "forecast" not in pricing.lower()
    assert "unlimited" not in pricing.lower()
    later = pricing.split('pf-plan--later', 1)[1]
    assert "max bid" not in later.lower() and "flip score" not in later.lower()


# ───────────────────────── real numbers only ─────────────────────────

def test_stats_render_server_side(monkeypatch):
    html = _page(monkeypatch, STATS)
    assert "219,336" in html and "14,478" in html
    assert 'data-countup="219336"' in html and 'data-countup="14478"' in html


def test_stats_absent_degrades_to_source_count_only(monkeypatch):
    html = _page(monkeypatch, None)
    assert "data-countup" not in html
    # the source counts need no database and still render, from code constants
    total = len(platform_api.SITES) + len(platform_api.PLANNED_SITES)
    assert f"of {total}" in html
    assert f"{len(platform_api.SITES)} sources tracked" in html


def test_template_bakes_in_no_big_number():
    # every thousands-grouped figure must come from the context, never the file
    assert not re.search(r"\d{1,3},\d{3}", TEMPLATE)
    # the "N of M sources" figures come from platform_api constants, not the file
    for literal in ('pf-fact-n">7', "of 13", "310"):
        assert literal not in TEMPLATE, literal


def test_planned_sources_are_named_as_roadmap(monkeypatch):
    html = _page(monkeypatch, STATS)
    product = html.split('id="product"', 1)[1].split("</section>", 1)[0]
    assert "On the roadmap" in product
    for name in ("HiBid", "PropertyRoom", "GovPlanet", "Bid4Assets", "BidSpotter", "iBid Illinois"):
        assert name in product, name


# ───────────────────────── stats(): never blocks, never raises ─────────────────────────

class InlineThread:
    """threading.Thread stand-in that runs the target synchronously on start()."""
    def __init__(self, target=None, **kw):
        self._target = target

    def start(self):
        self._target()


def test_stats_refresh_success_and_failure(monkeypatch):
    monkeypatch.setattr(platform_api.db, "fetch_one",
                        lambda sql, params=None: {"sold_outcomes": 5, "open_now": 2})
    platform_api._refresh_stats()
    assert platform_api.stats() == {"sold_outcomes": 5, "open_now": 2}

    def boom(sql, params=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(platform_api.db, "fetch_one", boom)
    platform_api._refresh_stats()          # must not raise …
    assert platform_api.stats() == {"sold_outcomes": 5, "open_now": 2}  # … and keeps the last good read


def test_stats_cold_start_refreshes_in_background(monkeypatch):
    monkeypatch.setattr(platform_api.threading, "Thread", InlineThread)
    monkeypatch.setattr(platform_api.db, "fetch_one",
                        lambda sql, params=None: {"sold_outcomes": 7, "open_now": 3})
    # cold: the call itself kicks the (inline) refresh and never raises
    assert platform_api.stats() in (None, {"sold_outcomes": 7, "open_now": 3})
    assert platform_api.stats() == {"sold_outcomes": 7, "open_now": 3}

    def boom(sql, params=None):
        raise AssertionError("fresh memo must not re-read")
    monkeypatch.setattr(platform_api.db, "fetch_one", boom)
    assert platform_api.stats() == {"sold_outcomes": 7, "open_now": 3}


def test_stats_failure_backs_off_between_page_views(monkeypatch):
    monkeypatch.setattr(platform_api.threading, "Thread", InlineThread)
    calls = []

    def boom(sql, params=None):
        calls.append(sql)
        raise RuntimeError("db down")
    monkeypatch.setattr(platform_api.db, "fetch_one", boom)
    for _ in range(3):                       # three page views during an outage …
        assert platform_api.stats() is None
    assert len(calls) == 1, "… must try the database once, then back off"
    # once the backoff lapses, the next view tries again and recovers
    with platform_api._stats_lock:
        platform_api._stats["fail_until"] = 0.0
    monkeypatch.setattr(platform_api.db, "fetch_one",
                        lambda sql, params=None: {"sold_outcomes": 9, "open_now": 4})
    assert platform_api.stats() == {"sold_outcomes": 9, "open_now": 4}

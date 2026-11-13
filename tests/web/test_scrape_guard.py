"""Scrape guard on the public auction feed: per-IP rate limit, trap link + ban,
anonymous depth cap, robots rules. DB-free."""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals as pd
from automation.web import public_sold as ps
from automation.web import rate_limit, scrape_guard as sg

app_mod = importlib.import_module("automation.web.app")
app = app_mod.app
PAGE = {"rows": [], "total": 0, "page": 1, "per_page": 25, "pages": 1}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    monkeypatch.delenv("SCRAPE_GUARD_ENABLED", raising=False)
    auth_svc.reset_caches()
    rate_limit.reset()
    sg.reset()
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: dict(PAGE))
    monkeypatch.setattr(pd, "fetch_facets", lambda: {"categories": [], "states": [], "stats": {}})
    monkeypatch.setattr(ps, "fetch_sold", lambda **kw: {
        "summary": {"sold": 0, "reserve_not_met": 0, "no_bid": 0, "sold_share": None, "median": None,
                    "p25": None, "p75": None, "since": None, "until": None},
        "categories": [], "rows": [], "total": 0, "page": 1, "per_page": 50, "pages": 1})
    monkeypatch.setattr(app_mod, "_platform_viewer", lambda request: None)
    yield
    rate_limit.reset()
    sg.reset()
    auth_svc.reset_caches()


def _c(ip="203.0.113.7"):
    return TestClient(app, headers={"cf-connecting-ip": ip})


# ── what is guarded ──────────────────────────────────────────────────────────

def test_guarded_paths():
    for p in ("/deals/api/lots", "/platform/api/auctions", "/map/api/points",
              "/distress/api/cases", "/deals/sold", "/deals/305/10340/1"):
        assert sg.is_guarded(p), p
    for p in ("/", "/deals", "/platform", "/api/deals", "/static/x.css", "/listings/abc",
              "/freight-estimate", "/api/health"):
        assert not sg.is_guarded(p), p


# ── rate limit ───────────────────────────────────────────────────────────────

def test_per_ip_limit_answers_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(sg, "PER_MIN", 3)
    c = _c()
    assert [c.get("/deals/api/facets").status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/deals/api/facets")
    assert r.status_code == 429 and r.json() == {"detail": "rate_limited"}
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # another visitor is not affected, and an unguarded page never is
    assert _c("198.51.100.9").get("/deals/api/facets").status_code == 200
    assert c.get("/api/health").status_code == 200


def test_operator_session_is_never_limited(monkeypatch):
    monkeypatch.setattr(sg, "PER_MIN", 1)
    c = _c()
    c.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    assert [c.get("/deals/api/facets").status_code for _ in range(4)] == [200] * 4


def test_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(sg, "PER_MIN", 1)
    monkeypatch.setenv("SCRAPE_GUARD_ENABLED", "0")
    c = _c()
    assert [c.get("/deals/api/facets").status_code for _ in range(3)] == [200] * 3


# ── trap ─────────────────────────────────────────────────────────────────────

def test_trap_hit_bans_the_ip_from_the_feed_only():
    c = _c("203.0.113.50")
    assert c.get("/deals/api/facets").status_code == 200
    assert c.get(sg.TRAP_PATH).status_code == 404          # looks like a dead link
    r = c.get("/deals/api/facets")
    assert r.status_code == 429 and "retry-after" in r.headers
    assert c.get("/deals/sold").status_code == 429
    assert c.get("/api/health").status_code == 200          # the storefront still works
    assert _c("198.51.100.9").get("/deals/api/facets").status_code == 200


def test_trap_never_bans_an_unknown_address_or_the_operator():
    sg.ban("unknown")
    assert not sg.is_banned("unknown")
    c = _c("203.0.113.51")
    c.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    c.get(sg.TRAP_PATH)
    assert not sg.is_banned("203.0.113.51")


def test_ban_expires(monkeypatch):
    sg.ban("203.0.113.52")
    assert sg.is_banned("203.0.113.52")
    real = sg.time.time
    monkeypatch.setattr(sg.time, "time", lambda: real() + sg.BAN_HOURS * 3600 + 1)
    assert not sg.is_banned("203.0.113.52")


def test_public_pages_carry_the_hidden_trap_link():
    for path in ("/deals", "/deals/sold"):
        html = _c("198.51.100.77").get(path).text
        assert f'href="{sg.TRAP_PATH}"' in html, path
        i = html.index(f'href="{sg.TRAP_PATH}"')
        tag = html[html.rindex("<a", 0, i):html.index(">", i)]
        assert 'rel="nofollow"' in tag and 'aria-hidden="true"' in tag and 'tabindex="-1"' in tag and "hidden" in tag


# ── depth cap ────────────────────────────────────────────────────────────────

def test_anonymous_depth_is_capped(monkeypatch):
    monkeypatch.setattr(sg, "ANON_MAX_ROWS", 200)
    c = _c()
    assert c.get("/deals/api/lots?page=2&per_page=100").status_code == 200
    r = c.get("/deals/api/lots?page=3&per_page=100")
    assert r.status_code == 401 and r.json() == {"detail": "sign_in_required"}
    assert c.get("/deals/api/sold?page=5").status_code == 401      # 5 × 50 rows
    assert c.get("/deals/sold?page=5").status_code == 401
    assert c.get("/platform/api/auctions?page=9&per_page=25").status_code == 401


def test_signed_in_viewer_reads_past_the_cap(monkeypatch):
    monkeypatch.setattr(sg, "ANON_MAX_ROWS", 200)
    monkeypatch.setattr(app_mod, "_platform_viewer", lambda request: {"id": 1, "onboarded_at": "x"})
    assert _c().get("/deals/api/lots?page=3&per_page=100").status_code == 200


def test_depth_reads_bad_numbers_as_the_default():
    assert sg.depth("/deals/api/lots", {"page": "abc", "per_page": "-5"}) == 25
    assert sg.depth("/deals/api/lots", {"page": "4", "per_page": "100"}) == 400
    assert sg.depth("/deals/api/sold", {"page": "3"}) == 150
    assert sg.depth("/deals/api/facets", {"page": "999"}) == 0
    # an oversized per_page is clamped by the endpoint, so it is counted clamped
    assert sg.depth("/platform/api/auctions", {"per_page": "5000"}) == 100
    assert sg.depth("/deals/api/sold", {"page": "2", "per_page": "9999"}) == 100


# ── crawler rules ────────────────────────────────────────────────────────────

def test_feed_json_is_marked_noindex_and_robots_disallows_it():
    assert _c().get("/deals/api/facets").headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    robots = _c().get("/robots.txt").text
    for line in ("Disallow: /deals", "Disallow: /platform/api/", "Disallow: /map/api/",
                 "Disallow: /distress/api/"):
        assert line in robots, line

"""GET /liquidators, /platform/bankruptcies, /platform/deals — the liquidator-facing pages built on the
/platform draft (PR #117).

What this guards:
  · every page renders with NO database (the handlers read nothing; the deals page fetches the existing
    public /platform/api/auctions + /platform/api/sites from the browser);
  · honest copy: no invented customers or metrics, every sample is labelled as sample in the server-rendered
    HTML, plain-English ("Ask AI") search is only ever "coming" — never claimed live;
  · the bankruptcy fixture is invented and stays that way: obviously fake debtor names, no contact details;
  · the deals page never renders an auction photo (policy: public_deals.py) and uses the policy-gated
    /platform/api/auctions, never deal_lots directly;
  · the pages are reachable by direct URL only — noindex, not in the storefront nav/footer or the sitemap;
  · the /liquidators contact form posts to the existing /contact with the lead tagged in `message`.
"""
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import auth as auth_svc
from automation.web import platform_api, public_deals
from automation.web.app import app

app_module = sys.modules["automation.web.app"]

WEB = Path("automation/web")
FIXTURE = WEB / "static/site/platform/bankruptcies.sample.json"
BK_JS = (WEB / "static/site/platform/bankruptcies.js").read_text()
DEALS_JS = (WEB / "static/site/platform/deals.js").read_text()
LQ_JS = (WEB / "static/site/liquidators.js").read_text()
CSS = (WEB / "static/site/platform-tools.css").read_text()
PAGES = ("/liquidators", "/platform/bankruptcies", "/platform/deals")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """Any DB read from a page handler is a failure, not a slow test."""
    def boom(*a, **k):
        raise AssertionError("these pages must not touch the database")
    monkeypatch.setattr(public_deals, "fetch_facets", boom)
    monkeypatch.setattr(public_deals, "fetch_page", boom)
    monkeypatch.setattr(platform_api, "auctions", boom)
    monkeypatch.setattr(inventory, "list_public", boom)
    monkeypatch.setattr(inventory, "get", boom)
    monkeypatch.setattr(app_module.db, "fetch_all", boom)
    monkeypatch.setattr(app_module.db, "fetch_one", boom)


def _client():
    return TestClient(app, base_url="https://testserver")


def _get(path: str) -> str:
    r = _client().get(path)
    assert r.status_code == 200, path
    return r.text


# ───────────────────────── all three pages ─────────────────────────

@pytest.mark.parametrize("path", PAGES)
def test_renders_without_a_database_and_is_noindex(path):
    html = _get(path)
    assert '<meta name="robots" content="noindex,nofollow">' in html
    assert 'data-theme="light"' in html                      # storefront light tokens, not a dark page
    assert "platform-tools.css" in html
    assert "storage_note" not in html


@pytest.mark.parametrize("path", PAGES)
def test_out_of_the_sitemap_nav_and_footer(path, monkeypatch):
    monkeypatch.setattr(inventory, "list_public", lambda: [])
    monkeypatch.setattr(inventory, "list_sold_showcase", lambda: [])
    assert path not in _client().get("/sitemap.xml").text
    assert path not in _client().get("/robots.txt").text     # a crawler must fetch the page to see noindex
    assert path not in (WEB / "templates/_public_base.html").read_text()
    assert f'href="{path}' not in _client().get("/sell").text


@pytest.mark.parametrize("path", PAGES)
def test_copy_makes_no_customer_or_metric_claims(path):
    html = _get(path).lower()
    for banned in ("testimonial", "trusted by", "per month", "/mo", "customers use", "free trial",
                   "join 1", "join 2", "join 3", "join 4", "join 5"):
        assert banned not in html, banned
    # no "N+ liquidators / customers / companies" style claim anywhere
    assert not re.search(r"\d[\d,]*\+?\s+(liquidators|customers|companies|clients|users)\b", html)
    # plain-English search is never claimed to be live
    for claim in ("ai search is live", "ai-powered search is live", "search powered by ai"):
        assert claim not in html


def test_css_uses_tokens_only_and_js_uses_the_shared_api():
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "platform-tools.css: use a token"
    for js in (BK_JS, DEALS_JS, LQ_JS):
        assert "fetch(" not in js
        assert "localStorage" not in js and "sessionStorage" not in js


# ───────────────────────── /liquidators ─────────────────────────

def test_liquidators_page_structure_and_copy():
    html = _get("/liquidators")
    for section in ("lq-hero", "lq-tracks", "lq-how", "lq-sample", "lq-access"):
        assert f'id="{section}"' in html, section
    low = html.lower()
    # the two sides of the pitch
    assert "before anyone else" in low
    assert "churches" in low and "venues" in low and "event compan" in low
    # the sample table is labelled as sample in the server-rendered HTML, and its rows come from the fixture
    assert "sample data, invented" in low
    sample = html.split('id="lq-sample"', 1)[1]
    assert "Sample Harbor Banquet Hall LLC" in sample
    # the links to the two demos
    assert 'href="/platform/bankruptcies"' in html and 'href="/platform/deals"' in html
    # AI search is "coming", nothing more
    assert "coming" in low


def test_liquidators_form_posts_to_contact_with_a_tag():
    html = _get("/liquidators")
    assert 'id="lq-access-form"' in html
    assert "api('/contact'" in LQ_JS
    assert "[LIQUIDATORS PAGE]" in LQ_JS
    # the lead tag leads the message so it survives truncation in the Telegram preview
    assert re.search(r"message:\s*`\$\{LEAD_TAG\}", LQ_JS)
    # client-side guard: no name / no email → no POST
    assert "Add your name" in LQ_JS and "email address" in LQ_JS


def test_liquidators_sample_rows_render_server_side_without_db():
    rows = app_module._bankruptcy_samples()
    assert len(rows) >= 5
    html = _get("/liquidators")
    for row in rows[:5]:
        assert row["debtor"] in html


# ───────────────────────── /platform/bankruptcies ─────────────────────────

def test_bankruptcies_page_controls_and_sample_banner():
    html = _get("/platform/bankruptcies")
    low = html.lower()
    assert "sample data, invented" in low
    assert "not a court record" in low
    for control in ("bk-q", "bk-state", "bk-chapter", "bk-industry", "bk-filed", "bk-assets", "bk-sort"):
        assert f'id="{control}"' in html, control
    assert 'id="bk-rows"' in html and 'id="bk-drawer"' in html
    # the Ask AI box is a form that never leaves the page
    assert 'id="bk-ask-form"' in html and 'id="bk-ask-result"' in html
    assert "coming soon" in low
    assert "/static/site/platform/bankruptcies.sample.json" in BK_JS


def test_bankruptcies_ask_ai_is_ui_only():
    # the ask handler neither fetches nor posts: it only shows the coming-soon state
    ask = BK_JS.split("function initAsk", 1)[1].split("\nfunction ", 1)[0]
    assert "api(" not in ask and "fetch(" not in ask
    assert "not built yet" in ask.lower() or "coming soon" in ask.lower()


def test_bankruptcy_fixture_is_obviously_invented():
    data = json.loads(FIXTURE.read_text())
    assert data["sample"] is True
    assert "INVENTED" in data["note"]
    rows = data["filings"]
    assert len(rows) >= 20
    keys = {"id", "case_no", "debtor", "chapter", "industry", "city", "state", "filed",
            "est_assets", "est_liabilities", "signal", "asset_classes", "court", "summary"}
    for row in rows:
        assert keys <= set(row), row["id"]
        assert re.search(r"\b(Sample|Example)\b", row["debtor"]), row["debtor"]
        assert "SAMPLE" in row["case_no"]
        assert row["court"].startswith("Sample Court")
        assert row["chapter"] in data["chapters"] and row["industry"] in data["industries"]
        assert row["signal"] in data["signals"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", row["filed"])
        assert isinstance(row["est_assets"], int) and isinstance(row["est_liabilities"], int)
    blob = FIXTURE.read_text().lower()
    for private in ("email", "phone", "@", "street", "ssn", "attorney", "trustee_name"):
        assert private not in blob, private
    assert len({r["id"] for r in rows}) == len(rows) and len({r["case_no"] for r in rows}) == len(rows)


# ───────────────────────── /platform/deals ─────────────────────────

def test_deals_page_structure_sources_and_no_photos():
    html = _get("/platform/deals")
    low = html.lower()
    for control in ("pd-q", "pd-site", "pd-category", "pd-state", "pd-status", "pd-sort"):
        assert f'id="{control}"' in html, control
    assert 'id="pd-rows"' in html and 'id="pd-sources"' in html and 'id="pd-drawer"' in html
    # every source the recorder knows or plans is named server-side, planned ones labelled as such
    for name in ("GovDeals", "AllSurplus", "Public Surplus", "HiBid"):
        assert name in html, name
    assert "planned" in low
    # photo policy: nothing here renders an auction image
    assert "<img" not in html.split('id="pd-feed"', 1)[1].split("</section>", 1)[0]
    assert "<img" not in DEALS_JS and "image" not in DEALS_JS.lower()
    # the feed reads the policy-gated public read model, never deal_lots
    assert "/platform/api/auctions" in DEALS_JS and "/platform/api/sites" in DEALS_JS
    assert "deal_lots" not in DEALS_JS
    # an empty feed is a direction, not a blank table
    assert "Show closed auctions" in DEALS_JS
    # the page does not bake a number into the template; counts come from the endpoint at runtime
    assert html.split('id="pd-count"', 1)[1].split("</span>", 1)[0].split(">", 1)[1].strip() == ""


def test_deals_page_sources_come_from_platform_api_tables():
    html = _get("/platform/deals")
    for key, (name, _kind) in {**platform_api.SITES, **platform_api.PLANNED_SITES}.items():
        assert f'data-site="{key}"' in html, key
        assert name in html

"""GET /platform — the software page with the read-only demo.

What this guards:
  · the page renders with NO database (the handler reads nothing; the Deal finder calls the existing
    public /deals/api/* endpoints from the browser);
  · the hero stays almost wordless, the sections keep their order, and the copy never claims "all" sites
    or a natural-language search that does not exist yet;
  · GET /platform/api/sources is public, read-only, memoised, and answers names only when the read fails;
  · the two sample tabs are labelled as sample data in the server-rendered HTML, not only by JS;
  · it is reachable by direct URL only — not in the storefront nav/footer, not in the sitemap, noindex;
  · the sample JSON is invented and stays that way: no contact details, no private fields, and the same
    vocabulary as the real ledger (statuses, channels, channel states);
  · the request-access form posts to the existing /contact with the lead tagged in `message`
    (`inquiries` has no source column — no schema change).
"""
import inspect
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import channels, inventory
from automation.web import auth as auth_svc
from automation.web import public_deals, readcache
from automation.web.app import app

app_module = sys.modules["automation.web.app"]

WEB = Path("automation/web")
SAMPLE_DIR = WEB / "static/site/platform"
JS = (WEB / "static/site/platform.js").read_text()
TEMPLATE = (WEB / "templates/platform.html").read_text()
LEAD_TAG = "[PLATFORM PAGE]"


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def no_db(monkeypatch):
    """Any DB read from the page handler is a failure, not a slow test."""
    def boom(*a, **k):
        raise AssertionError("GET /platform must not touch the database")
    monkeypatch.setattr(public_deals, "fetch_facets", boom)
    monkeypatch.setattr(public_deals, "fetch_page", boom)
    monkeypatch.setattr(inventory, "list_public", boom)
    monkeypatch.setattr(inventory, "get", boom)
    monkeypatch.setattr(app_module.db, "fetch_all", boom)
    monkeypatch.setattr(app_module.db, "fetch_one", boom)


def _client():
    return TestClient(app, base_url="https://testserver")


def _page(no_db=None) -> str:
    r = _client().get("/platform")
    assert r.status_code == 200
    return r.text


# ───────────────────────── the page ─────────────────────────

def test_platform_renders_without_a_database(no_db):
    html = _page()
    for tab in ("Deal finder", "Inventory &amp; listings", "Buyer CRM"):
        assert tab in html
    assert 'id="pf-pane-deals"' in html and 'id="pf-pane-inventory"' in html and 'id="pf-pane-crm"' in html
    assert "/static/site/platform.js" in html and "/static/site/platform.css" in html


def test_handler_is_plain_def_and_public():
    assert not inspect.iscoroutinefunction(app_module.public_platform)
    assert not auth_svc.PROTECTED_PREFIXES or not "/platform".startswith(auth_svc.PROTECTED_PREFIXES)


def test_sample_tabs_are_labelled_in_the_html(no_db):
    html = _page()
    # one live pane, two sample panes — marked in the markup, not left to JavaScript
    assert html.count('data-source="live"') == 1
    assert html.count('data-source="sample"') == 2
    for pane in ("inventory", "crm"):
        block = html.split(f'id="pf-pane-{pane}"', 1)[1].split('role="tabpanel"', 1)[0]
        assert 'data-source="sample"' in block.split(">", 1)[0]
        assert "<strong>Sample data.</strong>" in block
        assert "invented" in block
        tab = html.split(f'id="pf-tab-{pane}"', 1)[1].split("</button>", 1)[0]
        assert "Sample data" in tab
    deals_tab = html.split('id="pf-tab-deals"', 1)[1].split("</button>", 1)[0]
    assert "Sample data" not in deals_tab


def test_page_is_noindex_and_out_of_the_sitemap(no_db, monkeypatch):
    html = _page()
    assert '<meta name="robots" content="noindex,nofollow">' in html
    monkeypatch.setattr(inventory, "list_public", lambda: [])
    monkeypatch.setattr(inventory, "list_sold_showcase", lambda: [])
    assert "/platform" not in _client().get("/sitemap.xml").text
    # robots.txt must NOT disallow it: a crawler has to fetch the page to see the noindex.
    assert "/platform" not in _client().get("/robots.txt").text


def test_no_nav_or_footer_link_points_at_the_page(no_db):
    # the shared storefront chrome never mentions it …
    assert "/platform" not in (WEB / "templates/_public_base.html").read_text()
    # … so a buyer page carries no link to it …
    assert 'href="/platform' not in _client().get("/sell").text
    # … and the page does not link to itself from its own header or footer either.
    html = _page()
    assert 'href="/platform' not in html


def test_copy_makes_no_price_or_customer_claims(no_db):
    html = _page().lower()
    for banned in ("testimonial", "trusted by", "per month", "/mo", "customers use", "join ", "free trial"):
        assert banned not in html, banned
    assert "no price list" in html
    # No number about the feed is baked into the template: those come from /deals/api/facets at runtime.
    stats = TEMPLATE.split('id="pf-deals-stats"', 1)[1].split("</span>", 1)[0]
    assert not re.search(r"\d", stats)


# ───────────────────────── hero, order, words ─────────────────────────

def _words(text: str) -> list[str]:
    return re.sub(r"<[^>]+>", " ", text).split()


def test_hero_is_almost_wordless_and_jumps_to_the_map(no_db):
    html = _page()
    hero = html.split('class="pf-hero"', 1)[1].split("</section>", 1)[0]
    headline = hero.split('class="pf-headline">', 1)[1].split("</h1>", 1)[0]
    sub = hero.split('class="pf-hero-sub">', 1)[1].split("</p>", 1)[0]
    assert 1 <= len(_words(headline)) <= 6, headline
    assert 1 <= len(_words(sub)) <= 14, sub
    assert hero.count("<p") == 1, "one sub-line, no lede paragraphs"
    assert hero.count('class="pf-cta"') == 1 and '<a class="pf-cta" href="#map">' in hero
    # the hero must not depend on hero.jpg existing: a colour and gradients sit under it
    css = (WEB / "static/site/platform.css").read_text()
    rule = css.split(".pf-hero {", 1)[1].split("}", 1)[0]
    assert "background-color" in rule and "url(/static/site/platform/hero.jpg)" in rule
    assert "linear-gradient" in rule and "radial-gradient" in rule


def test_sections_come_in_the_agreed_order(no_db):
    html = _page()
    marks = ['class="pf-hero"', 'id="sources"', 'id="map"', 'id="demo"', 'id="why"', 'id="access"']
    at = [html.index(m) for m in marks]
    assert at == sorted(at), dict(zip(marks, at))
    # the map agent's three hooks stay in the template
    assert '/static/site/platform/map.css' in html and '/static/site/platform/map.js' in html
    assert '{% include "_platform_map.html" %}' in TEMPLATE
    for h2 in re.findall(r"<h2[^>]*>(.*?)</h2>", TEMPLATE, re.S):
        assert len(_words(h2)) <= 4, h2


def test_copy_stays_inside_the_honesty_limits(no_db):
    html = _page()
    text = " ".join(_words(html.split('<main', 1)[1].split("</main>", 1)[0])).lower()
    for banned in ("all surplus", "all sites", "all auction", "every site", "every auction", "every surplus"):
        assert banned not in text, banned
    # no count of sites is baked in: it comes from /platform/api/sources at runtime
    strip = TEMPLATE.split('id="sources"', 1)[1].split("</section>", 1)[0]
    assert not re.search(r"\d", " ".join(_words(re.sub(r"\{[#%].*?[#%]\}", "", strip, flags=re.S))))
    # natural-language search does not exist yet: only ever labelled as coming
    why = html.split('id="why"', 1)[1].split("</section>", 1)[0]
    assert re.search(r"Coming</span>\s*Plain-English search", why)
    assert "plain-english search" not in text.replace("coming plain-english search", "")


def test_why_block_is_three_short_rows(no_db):
    assert TEMPLATE.count("WHY LIQUIDATORS — COPY BLOCK") == 2, "start + end markers for the copy agent"
    why = _page().split('id="why"', 1)[1].split("</section>", 1)[0]
    pains = re.findall(r'class="pf-why-pain">(.*?)</span>', why)
    fixes = re.findall(r'class="pf-why-fix">(.*?)</span>', why)
    assert len(pains) == len(fixes) == 3
    assert 25 <= sum(len(_words(t)) for t in pains + fixes) <= 45


# ───────────────────────── the sources strip ─────────────────────────

@pytest.fixture
def fresh_cache():
    readcache.invalidate_all()
    yield
    readcache.invalidate_all()


def _snapshot_rows():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return [
        {"source": "govdeals", "lots": 5349, "last_seen": now - timedelta(minutes=9)},
        {"source": "purple_wave", "lots": 1119, "last_seen": now - timedelta(hours=23)},
        {"source": "municibid", "lots": 582, "last_seen": now - timedelta(days=30)},
    ]


def test_sources_strip_renders_names_without_a_database(no_db):
    strip = _page().split('id="sources"', 1)[1].split("</section>", 1)[0]
    for name in app_module._PLATFORM_SOURCE_NAMES.values():
        assert f'<span class="pf-source-name">{name}</span>' in strip


def test_sources_endpoint_is_public_plain_def_and_read_only(monkeypatch, fresh_cache):
    assert not inspect.iscoroutinefunction(app_module.public_platform_sources)
    assert not "/platform/api/sources".startswith(auth_svc.PROTECTED_PREFIXES)
    seen = []

    def fake(sql, params=None):
        seen.append(sql)
        return _snapshot_rows()

    monkeypatch.setattr(app_module.db, "fetch_all", fake)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")      # auth on: the route must still answer without a login
    auth_svc.reset_caches()
    r = _client().get("/platform/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["tracked"] == len(body["sources"]) == 7 and body["live"] == 2
    by = {s["key"]: s for s in body["sources"]}
    assert by["govdeals"] == {"key": "govdeals", "name": "GovDeals", "live": True, "lots": 5349,
                              "last_seen": by["govdeals"]["last_seen"]}
    assert by["purple_wave"]["live"] is True                      # inside 24 h
    assert by["municibid"]["live"] is False and by["municibid"]["lots"] == 582
    assert by["mibid"] == {"key": "mibid", "name": "MiBid", "live": False, "lots": 0, "last_seen": None}
    for s in body["sources"]:
        assert set(s) == {"key", "name", "live", "lots", "last_seen"}
    assert len(seen) == 1 and seen[0].lstrip().upper().startswith("SELECT") and "listing_snapshots" in seen[0]
    # memoised: page views do not each hit the DB
    for _ in range(3):
        assert _client().get("/platform/api/sources").json()["tracked"] == 7
    assert len(seen) == 1


def test_sources_endpoint_failure_gives_names_only_and_is_not_cached(monkeypatch, fresh_cache):
    calls = []

    def boom(sql, params=None):
        calls.append(sql)
        raise RuntimeError("connection to server at db.internal failed: password authentication failed")

    monkeypatch.setattr(app_module.db, "fetch_all", boom)
    r = _client().get("/platform/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and set(body) == {"ok", "sources"}
    assert [s["name"] for s in body["sources"]] == list(app_module._PLATFORM_SOURCE_NAMES.values())
    assert all(set(s) == {"key", "name"} for s in body["sources"])
    assert "password" not in r.text and "db.internal" not in r.text and "RuntimeError" not in r.text
    # a failed read is not memoised: the next call tries again and recovers
    monkeypatch.setattr(app_module.db, "fetch_all", lambda sql, params=None: _snapshot_rows())
    assert _client().get("/platform/api/sources").json()["ok"] is True
    assert len(calls) == 1


def test_script_fills_the_strip_from_the_public_endpoint_only():
    assert "api('/platform/api/sources')" in JS
    block = JS.split("async function initSources()", 1)[1].split("\n}\n", 1)[0]
    assert "catch { return; }" in block, "a failed read leaves the server-rendered names alone"
    assert "data.tracked" in block and "esc(s.name)" in block


# ───────────────────────── the sample data ─────────────────────────

@pytest.fixture(scope="module")
def inv_sample():
    return json.loads((SAMPLE_DIR / "inventory.sample.json").read_text())


@pytest.fixture(scope="module")
def crm_sample():
    return json.loads((SAMPLE_DIR / "crm.sample.json").read_text())


def test_sample_files_are_served():
    c = _client()
    for name in ("inventory.sample.json", "crm.sample.json"):
        r = c.get(f"/static/site/platform/{name}")
        assert r.status_code == 200, name
        assert r.json()["sample"] is True


def test_inventory_sample_uses_the_real_vocabulary(inv_sample):
    assert [c["key"] for c in inv_sample["channels"]] == list(channels.CHANNELS)
    assert inv_sample["lots"], "no sample lots"
    for lot in inv_sample["lots"]:
        assert lot["status"] in inventory.ALL_STATUSES, lot["lot_id"]
        assert set(lot["channels"]) == set(channels.CHANNELS), lot["lot_id"]
        for key, cell in lot["channels"].items():
            assert cell["state"] in channels.STATES, (lot["lot_id"], key)
        assert 0 <= lot["quantity_remaining"] <= lot["quantity_original"]
        # A lot the feed gate would not sell is never shown as live — same rule as channels/sync.py.
        sellable = lot["status"] in inventory.CATALOG_FEED_STATUSES and lot["quantity_remaining"] > 0
        if not sellable:
            assert all(c["state"] != "live" for c in lot["channels"].values()), lot["lot_id"]
        # Only the approval channel can sit in pending_approval.
        for key, cell in lot["channels"].items():
            if cell["state"] == "pending_approval":
                assert key in channels.APPROVAL_CHANNELS, (lot["lot_id"], key)


def test_inventory_sample_carries_no_private_fields(inv_sample):
    raw = (SAMPLE_DIR / "inventory.sample.json").read_text().lower()
    for banned in ("storage_note", "gate code", "cost", "hammer", "margin", "purchase", "paid", "govdeals", "gd-"):
        # the leading `_note` names the things that must never be here; nothing else may
        assert banned not in raw.replace(inv_sample["_note"].lower(), ""), banned
    for lot in inv_sample["lots"]:
        assert set(lot) <= {"lot_id", "title", "unit", "city", "state", "quantity_remaining",
                            "quantity_original", "price_per_unit", "status", "channels"}, lot["lot_id"]
        assert re.fullmatch(r"L-\d{4}", lot["lot_id"]), "sample ids must not look like real lot ids"


def test_crm_sample_has_no_contact_details(inv_sample, crm_sample):
    raw = (SAMPLE_DIR / "crm.sample.json").read_text()
    body = raw.replace(crm_sample["_note"], "")
    assert "@" not in body, "no email addresses in sample data"
    assert "http" not in body.lower(), "no links in sample data"
    assert not re.search(r"\d{3}[\s.\-)]\s?\d{3}[\s.\-]\d{4}", body), "no phone numbers in sample data"
    assert not re.search(r"\b\d{2,5}\s+\w+\s+(st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way)\b", body, re.I), \
        "no street addresses in sample data"
    lot_ids = {l["lot_id"] for l in inv_sample["lots"]}
    stages = {s["key"] for s in crm_sample["stages"]}
    chans = {c["key"] for c in crm_sample["channels"]}
    assert chans == {"fb_marketplace", "ebay", "craigslist", "email", "text"}
    for b in crm_sample["buyers"]:
        assert set(b) <= {"id", "name", "about", "channel", "lot_id", "wants", "quantity_wanted",
                          "stage", "next_action", "thread", "draft"}, b["id"]
        assert re.fullmatch(r"[A-Z][a-z]+ [A-Z]\.", b["name"]), "first name + initial only"
        assert b["stage"] in stages and b["channel"] in chans and b["lot_id"] in lot_ids, b["id"]
        assert b["thread"] and all(m["from"] in ("buyer", "seller") for m in b["thread"]), b["id"]


# ───────────────────────── the script ─────────────────────────

def test_script_reads_only_the_public_deals_endpoints():
    assert "'/deals/api/lots?'" in JS and "'/deals/api/facets'" in JS
    # never the auth-walled admin API, and never a second sellable-lots or inventory read
    assert not re.search(r"['\"`]/api/", JS)
    assert "/listings" not in JS and "/catalog" not in JS
    # the only write is the request-access POST
    assert JS.count("method: 'POST'") == 1 and "api('/contact'" in JS
    assert "/subscribe" not in JS
    for verb in ("PATCH", "PUT", "DELETE"):
        assert verb not in JS
    assert "localStorage" not in JS and "sessionStorage" not in JS


def test_script_never_claims_live_without_the_endpoint_saying_so():
    # The tab ships as "Real data"; only paintDealsTruth() may upgrade it, from an endpoint count.
    tag = TEMPLATE.split('id="pf-deals-tag"', 1)[1].split("</span>", 1)[0]
    assert "Real data" in tag
    assert "live ? 'Live data' : 'Real data'" in JS
    # the slow closed-archive query runs on a click, never on page load
    assert "onOpen.deals = () => { loadDealFacets(); loadDeals(); };" in JS
    assert "status: 'active'" in JS.split("const deals = ", 1)[1].split(";", 1)[0]


# ───────────────────────── the form ─────────────────────────

def test_access_form_posts_to_contact_with_the_lead_tagged(monkeypatch):
    assert f"export const LEAD_TAG = '{LEAD_TAG}';" in JS
    assert "kind: 'buy'" in JS          # the only two kinds `inquiries` accepts are buy | sell
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        if kw["kind"] not in ("buy", "sell"):
            raise ValueError("kind must be 'buy' or 'sell'")
        return {"id": 77, **kw}

    async def no_ping(row):
        seen["pinged"] = row["id"]

    monkeypatch.setattr(inventory, "create_inquiry", fake_create)
    monkeypatch.setattr(app_module, "_notify_new_inquiry", no_ping)
    # exactly the shape platform.js::accessPayload builds
    payload = {"kind": "buy", "name": "Test Person", "email": "test@example.com",
               "message": f"{LEAD_TAG} I am: liquidator | Company: Example Liquidators | Pallets of returns."}
    r = _client().post("/contact", json=payload)
    assert r.status_code == 200 and r.json() == {"ok": True, "id": 77}
    assert seen["kind"] == "buy" and seen["email"] == "test@example.com"
    assert seen["message"].startswith(LEAD_TAG)
    assert seen["lot_id"] is None and seen["quantity_interested"] is None


def test_access_form_markup(no_db):
    html = _page()
    form = html.split('id="pf-access-form"', 1)[1].split("</form>", 1)[0]
    for field in ('name="name"', 'name="email"', 'name="company"', 'name="role"', 'name="note"'):
        assert field in form
    assert "REQUEST ACCESS" in form
    assert 'name="phone"' not in form

"""GET /platform — the software page: one product window (read-only demo) over a dark backdrop.

What this guards:
  · the page renders with NO database (the handler reads nothing; the window's sections fetch from the
    browser: /platform/api/auctions with the public /deals/api/* as fallback, /platform/api/sites);
  · the hero stays almost wordless, the sections keep their order, and the copy never claims "all" sites
    or a natural-language search that does not exist yet;
  · the window has its five sections, the two sample ones are labelled as sample in the server-rendered
    HTML (not only by JS), and Favorites is the visitor's own stars in this browser — never the operator's;
  · GET /platform/api/sources is public, read-only, memoised, and answers names only when the read fails;
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
AUCTIONS_JS = (SAMPLE_DIR / "auctions.js").read_text()
SECTIONS = ("auctions", "favorites", "inventory", "buyers", "sites")
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
    assert html.count('id="pf-win"') == 1, "one product window"
    for name in SECTIONS:
        assert f'id="pf-tab-{name}"' in html and f'id="pf-pane-{name}"' in html, name
    # application chrome: a sidebar of sections and a top bar with one search field
    assert 'class="pf-side"' in html and 'class="pf-top"' in html and 'id="pf-q"' in html
    side = html.split('class="pf-side-nav"', 1)[1].split("</aside>", 1)[0]
    assert [m for m in re.findall(r'data-tab="(\w+)"', side)] == list(SECTIONS)
    # Auctions is the section on screen; every other pane starts hidden
    for name in SECTIONS:
        pane = html.split(f'id="pf-pane-{name}"', 1)[1].split(">", 1)[0]
        assert ("hidden" in pane) == (name != "auctions"), name
    assert "/static/site/platform.js" in html and "/static/site/platform.css" in html


def test_handler_is_plain_def_and_public():
    assert not inspect.iscoroutinefunction(app_module.public_platform)
    assert not auth_svc.PROTECTED_PREFIXES or not "/platform".startswith(auth_svc.PROTECTED_PREFIXES)


def test_sample_sections_are_labelled_in_the_html(no_db):
    html = _page()
    # two sample panes, one visitor pane (favorites) — marked in the markup, not left to JavaScript
    assert html.count('data-source="sample"') == 2
    assert html.count('data-source="visitor"') == 1
    for pane in ("inventory", "buyers"):
        block = html.split(f'id="pf-pane-{pane}"', 1)[1].split('role="tabpanel"', 1)[0]
        assert 'data-source="sample"' in block.split(">", 1)[0]
        head = block.split('class="pf-pane-head"', 1)[1].split("</div>", 1)[0]
        assert "Sample data" in head and "invented" in head
        tab = html.split(f'id="pf-tab-{pane}"', 1)[1].split("</button>", 1)[0]
        assert "Sample" in tab
    for pane in ("auctions", "favorites", "sites"):
        block = html.split(f'id="pf-pane-{pane}"', 1)[1].split('role="tabpanel"', 1)[0]
        assert "Sample" not in block.split('class="pf-pane-head"', 1)[1].split("</div>", 1)[0], pane
        assert "Sample" not in html.split(f'id="pf-tab-{pane}"', 1)[1].split("</button>", 1)[0], pane
    auctions = html.split('id="pf-pane-auctions"', 1)[1].split('role="tabpanel"', 1)[0]
    assert 'data-source="live"' in auctions.split(">", 1)[0] and "Real data" in auctions


def test_auctions_view_has_every_filter_a_list_and_a_map(no_db):
    html = _page()
    pane = html.split('id="pf-pane-auctions"', 1)[1].split('role="tabpanel"', 1)[0]
    for control in ("pf-au-status", "pf-au-deals", "pf-au-site", "pf-au-category", "pf-au-state",
                    "pf-au-ending", "pf-au-maxbid", "pf-au-nobids", "pf-au-sort"):
        assert f'id="{control}"' in pane, control
    assert 'data-status="open"' in pane and 'data-status="closed"' in pane
    assert 'value="24h"' in pane and 'value="7d"' in pane
    for sort in ("ending", "bid_low", "bids", "newest"):
        assert f'<option value="{sort}">' in pane, sort
    assert 'id="pf-au-list"' in pane and 'id="pf-au-map"' in pane
    # the text search is the window's one search field, in the top bar
    assert 'type="search"' not in pane


def test_favorites_are_the_visitors_own_stars():
    # kept in this browser, every storage call guarded, and nothing reads the operator's favorites
    assert "const STAR_KEY = 'pf.stars.v1';" in AUCTIONS_JS
    assert AUCTIONS_JS.count("window.localStorage.") == 2
    for call in ("window.localStorage.getItem(STAR_KEY)", "window.localStorage.setItem(STAR_KEY"):
        before = AUCTIONS_JS.split(call, 1)[0]
        assert before.rstrip().endswith("try {") or "try {" in before.rsplit("\n", 3)[-1] or "try {" in before[-120:], call
    for src in (JS, AUCTIONS_JS):
        assert "favorite" not in "".join(re.findall(r"api\([^)]*\)", src)).lower()
        assert "auction_favorites" not in src and "/favorites" not in src
    assert "localStorage" not in JS and "sessionStorage" not in JS + AUCTIONS_JS


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
    # No number about the feed is baked into the template: the counts come from the endpoints at runtime.
    for slot in ("pf-au-count", "pf-n-auctions", "pf-sites-count", "pf-n-sites"):
        assert TEMPLATE.split(f'id="{slot}"', 1)[1].split("</span>", 1)[0].split(">", 1)[1].strip() == "", slot


# ───────────────────────── hero, order, words ─────────────────────────

def _words(text: str) -> list[str]:
    return re.sub(r"<[^>]+>", " ", text).split()


def test_hero_is_almost_wordless_and_jumps_to_the_window(no_db):
    html = _page()
    hero = html.split('class="pf-hero"', 1)[1].split("</section>", 1)[0]
    headline = hero.split('class="pf-headline">', 1)[1].split("</h1>", 1)[0]
    sub = hero.split('class="pf-hero-sub">', 1)[1].split("</p>", 1)[0]
    assert 1 <= len(_words(headline)) <= 6, headline
    assert 1 <= len(_words(sub)) <= 14, sub
    assert hero.count("<p") == 1, "one sub-line, no lede paragraphs"
    assert hero.count('class="pf-cta"') == 1 and '<a class="pf-cta" href="#app">' in hero
    # the backdrop must not depend on hero.jpg existing: a colour and gradients sit under it
    css = (WEB / "static/site/platform.css").read_text()
    rule = css.split(".pf-backdrop {", 1)[1].split("}", 1)[0]
    assert "background-color" in rule and "url(/static/site/platform/hero.jpg)" in rule
    assert "linear-gradient" in rule and "radial-gradient" in rule


def test_sections_come_in_the_agreed_order(no_db):
    html = _page()
    marks = ['class="pf-hero"', 'id="app"', 'id="why"', 'id="access"']
    at = [html.index(m) for m in marks]
    assert at == sorted(at), dict(zip(marks, at))
    # the hero and the window share one dark backdrop; the window sits directly under the hero
    stage = html.split('class="pf-backdrop" data-theme="dark"', 1)[1].split('id="why"', 1)[0]
    assert 'class="pf-hero"' in stage and 'id="pf-win"' in stage
    # no separate map section or tabbed console any more: the window replaced both
    assert 'id="map"' not in html and 'id="demo"' not in html and "pf-console" not in html
    # the buyers map keeps its own partial + files
    assert '/static/site/platform/map.css' in html and '/static/site/platform/map.js' in html
    assert '{% include "_platform_map.html" %}' in TEMPLATE
    for h2 in re.findall(r"<h2[^>]*>(.*?)</h2>", TEMPLATE, re.S):
        assert len(_words(h2)) <= 4, h2


def test_copy_stays_inside_the_honesty_limits(no_db):
    html = _page()
    text = " ".join(_words(html.split('<main', 1)[1].split("</main>", 1)[0])).lower()
    for banned in ("all surplus", "all sites", "all auction", "every site", "every auction", "every surplus"):
        assert banned not in text, banned
    # no count of sites is baked in: it comes from the sites endpoint at runtime
    strip = TEMPLATE.split('id="pf-pane-sites"', 1)[1].split("</ul>", 1)[0]
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
    strip = _page().split('id="pf-sites-list"', 1)[1].split("</ul>", 1)[0]
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


def test_script_shows_each_site_with_its_true_status():
    block = JS.split("async function fetchSites()", 1)[1].split("\n}\n", 1)[0]
    # the new route first, the older one as fallback, names only when both fail
    assert block.index("api('/platform/api/sites')") < block.index("api('/platform/api/sources')")
    assert "return null;" in block
    assert "const SITE_STATUS = {live: 'Live', paused: 'Paused', planned: 'Planned'};" in JS
    render = JS.split("function renderSites()", 1)[1].split("\n}\n", 1)[0]
    assert "if (!sites.rows) return;" in render, "a failed read leaves the server-rendered names alone"
    # a planned site is never shown as scraped: no lot count, no last-seen
    assert "const scraped = status === 'live' || status === 'paused';" in render
    assert render.count("scraped &&") == 2 and "esc(s.name)" in render


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

def test_scripts_read_only_public_endpoints():
    assert "const PRIMARY_URL = '/platform/api/auctions';" in AUCTIONS_JS
    for url in ("'/deals/api/lots'", "'/deals/api/pins'", "'/deals/api/facets'"):
        assert url in AUCTIONS_JS, url
    for src in (JS, AUCTIONS_JS):
        # never the auth-walled admin API, and never a second sellable-lots or inventory read
        assert not re.search(r"['\"`]/api/", src)
        assert "/listings" not in src and "/catalog" not in src and "/subscribe" not in src
        for verb in ("PATCH", "PUT", "DELETE"):
            assert verb not in src
    # the only write is the request-access POST
    assert JS.count("method: 'POST'") == 1 and "api('/contact'" in JS
    assert "POST" not in AUCTIONS_JS
    assert "fetch(" not in AUCTIONS_JS.replace("fetchPrimary(", "").replace("fetchFallback(", ""), "go through ui/state.js api()"


def test_auctions_fall_back_when_the_new_route_is_missing():
    load = AUCTIONS_JS.split("async function load(", 1)[1].split("\n}\n", 1)[0]
    assert load.index("fetchPrimary(page, ac.signal)") < load.index("fetchFallback(page, ac.signal)")
    assert "view.mode = 'fallback';" in load
    # the fallback reads only columns the public endpoint returns
    used = set(re.findall(r"\br\.([a-z_]+)", AUCTIONS_JS.split("function fromDeal(r)", 1)[1].split("\n}\n", 1)[0]))
    cols = {c.strip() for c in public_deals.PUBLIC_COLS.split(",")} | {"govdeals_url"}
    assert used and used <= cols, used - cols
    # an auction with no coordinates is listed, just without a pin
    assert "filter((it) => it.lat != null && it.lng != null)" in AUCTIONS_JS
    # "great deals" is a preset over the server's own filters: no bids + ending within 24 hours
    assert "{ status: 'open', noBids: true, ending: '24h', sort: 'ending' }" in AUCTIONS_JS
    assert "p.set('no_bids', '1')" in AUCTIONS_JS and "p.set('max_bids', '0')" in AUCTIONS_JS


def test_script_never_claims_live_without_the_endpoint_saying_so():
    # The pane ships as "Real data"; only paintTruth() may upgrade it, once the endpoint reports open auctions.
    tag = TEMPLATE.split('id="pf-au-tag"', 1)[1].split("</span>", 1)[0]
    assert "Real data" in tag
    assert "live ? 'Live data' : 'Real data'" in AUCTIONS_JS
    assert "if (f.status === 'open' && res.total > 0) view.sawOpen = true;" in AUCTIONS_JS
    # the slow closed-archive query runs on a click, never on page load
    assert "status: 'open'" in AUCTIONS_JS.split("const f = ", 1)[1].split(";", 1)[0]


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

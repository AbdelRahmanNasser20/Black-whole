"""lot_pages: one public lot from three backings, public policy first, a sold
price only when the archive confirms it. DB-free (connection + memo faked)."""
import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import deals_sources as ds
from automation.web import lot_pages as lp
from automation.web import public_deals as pd
from automation.web import rate_limit, scrape_guard as sg

app_mod = importlib.import_module("automation.web.app")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DL = {"site": "govdeals", "native_id": "305/10340/1", "asset_id": 305, "account_id": 10340, "auction_id": 1,
      "title": "Lot of (30) Laptops", "description": "Thirty laptops. Call Bob.", "canonical_category": "computers_electronics",
      "native_category_name": "Computers", "city": "Houston", "state": "TX", "lat": 29.7, "lng": -95.3,
      "bid_count": 4, "current_bid": 300.0, "currency_code": "USD", "end_utc": NOW + timedelta(days=2),
      "outcome": None, "final_bid_count": None, "outcome_complete": False, "first_seen_at": NOW - timedelta(days=3),
      "hero_image_url": "https://webassets.lqdt1.com/assets/photos/10340/a.jpg", "public_ok": True}
SOLD = {"outcome": "sold", "final_price": 512.5, "currency": "USD", "closed_at": NOW - timedelta(days=1), "bid_count": 9}
SNAP = {"id": "municibid:86233903", "source": "municibid", "source_name": "Municibid", "source_lot_id": "86233903",
        "asset_id": None, "account_id": None, "auction_id": None, "title": "Zero-turn mower",
        "canonical_category": None, "native_category_name": None, "city": "Erie", "state": "PA", "lat": 42.1, "lng": -80.1,
        "bid_count": 2, "current_bid": 150.0, "currency_code": "USD", "end_utc": NOW + timedelta(hours=5),
        "outcome": None, "final_bid": None, "final_bid_count": None, "outcome_complete": False,
        "first_seen_at": NOW - timedelta(days=1), "url": "https://municibid.com/Listing/Details/86233903",
        "viewer_url": "/deals/lot/zero-turn-mower-pa-municibid-86233903",
        "photo_url": "https://cdn.municibid.com/p/1.jpg"}


class Conn:
    """Answers by table name; records every statement."""
    def __init__(self, deal_lot=None, archive=None):
        self.deal_lot, self.archive, self.seen = deal_lot, archive, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.seen.append((sql, params))
        row = self.archive if "FROM lot_archive" in sql else self.deal_lot
        return type("Cur", (), {"fetchone": lambda s: dict(row) if row else None})()


@pytest.fixture
def world(monkeypatch):
    state = {"conn": Conn(), "snap": [], "archive_only": None}
    monkeypatch.setattr(lp.db, "connect", lambda: state["conn"])
    monkeypatch.setattr(lp.db, "fetch_one", lambda sql, params=(): state["archive_only"])
    monkeypatch.setattr(ds, "lots", lambda status="active", now=None, seating=False: [dict(r) for r in state["snap"]])
    monkeypatch.setattr(lp.site_settings, "get_all", lambda: {"deal_lot_photos": state.get("photos", 2)})
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: {"rows": []})
    lp.clear_cache()
    yield state
    lp.clear_cache()


# ───────────────────────── resolve ─────────────────────────

def test_deal_lots_row_wins_and_carries_only_public_fields(world):
    world["conn"] = Conn(deal_lot=DL)
    lot = lp.resolve("govdeals", "305/10340/1", now=NOW)
    assert lot["backing"] == "deal_lots" and lot["source_name"] == "GovDeals"
    assert lot["path"] == "/deals/lot/lot-of-30-laptops-tx-govdeals-305.10340.1"
    assert lot["url"] == "https://www.govdeals.com/en/asset/305/10340"
    assert lot["photos"] == ["https://webassets.lqdt1.com/assets/photos/10340/a.jpg"]
    assert not lot["ended"] and lot["sold_price"] is None and lot["current_bid"] == 300.0
    sql, params = world["conn"].seen[0]
    assert "FROM tracked_lots" in sql and "auction_favorites" in sql and "deal_list_items" in sql
    assert "WHERE site = %s AND native_id = %s" in sql and params[-2:] == ("govdeals", "305/10340/1")
    for private in ("seller", "archived_hero_url", "gallery_urls", "high_bidder", "raw", "reserve_price", "zip"):
        assert private not in sql and private not in lot


def test_operator_pick_is_a_404_and_never_falls_through(world):
    world["conn"] = Conn(deal_lot=dict(DL, public_ok=False))
    world["archive_only"] = dict(SOLD, source="govdeals", lot_key="305/10340/1", title="x", city=None, state="TX",
                                 canonical_category=None)
    assert lp.resolve("govdeals", "305/10340/1", now=NOW) is None


def test_seating_lot_is_public_unless_the_switch_is_on(world, monkeypatch):
    world["conn"] = Conn(deal_lot=dict(DL, title="Lot of 200 Banquet Chairs", canonical_category="seating_furniture"))
    assert lp.resolve("govdeals", "305/10340/1", now=NOW)["title"] == "Lot of 200 Banquet Chairs"
    monkeypatch.setenv("PUBLIC_DEALS_HIDE_SEATING", "1")
    assert lp.resolve("govdeals", "305/10340/1", now=NOW) is None


def test_sold_only_when_the_archive_confirms_a_usd_sale(world):
    ended = dict(DL, outcome_complete=True, outcome="sold", end_utc=NOW - timedelta(days=1))
    world["conn"] = Conn(deal_lot=ended, archive=SOLD)
    lot = lp.resolve("govdeals", "305/10340/1", now=NOW)
    assert lot["ended"] and lot["sold_price"] == 512.5 and lp.robots(lot) == "index,follow"
    for archive in (None, dict(SOLD, outcome="reserve_not_met"), dict(SOLD, final_price=None), dict(SOLD, currency="EUR")):
        world["conn"] = Conn(deal_lot=ended, archive=archive)
        lot = lp.resolve("govdeals", "305/10340/1", now=NOW)
        assert lot["ended"] and lot["sold_price"] is None and lp.robots(lot) == "noindex,follow", archive


def test_a_lot_past_its_end_is_ended_even_before_the_tracker_closes_it(world):
    world["conn"] = Conn(deal_lot=dict(DL, end_utc=NOW - timedelta(minutes=5)))
    lot = lp.resolve("govdeals", "305/10340/1", now=NOW)
    assert lot["ended"] and lp.robots(lot) == "noindex,follow"


def test_snapshot_source_reads_the_memo_not_the_database(world):
    world["snap"] = [SNAP]
    lot = lp.resolve("municibid", "86233903", now=NOW)
    assert lot["backing"] == "snapshot" and lot["path"] == SNAP["viewer_url"]
    assert lot["photos"] == ["https://cdn.municibid.com/p/1.jpg"] and lot["url"] == SNAP["url"]
    assert world["conn"].seen == []                      # municibid has no deal_lots row and no archive
    assert lp.resolve("municibid", "999", now=NOW) is None


def test_archive_only_lot_is_a_sold_page_through_the_sold_policy(world):
    seen = {}

    def fetch_one(sql, params=()):
        seen["sql"], seen["params"] = sql, params
        return dict(SOLD, source="govdeals", lot_key="7/8/9", title="Vulcan fryer", canonical_category="food_service",
                    city="Austin", state="TX")
    lp.db.fetch_one = fetch_one
    lot = lp.resolve("govdeals", "7/8/9", now=NOW)
    assert lot["backing"] == "archive" and lot["sold_price"] == 512.5 and lot["ended"] and lot["photos"] == []
    assert lot["path"] == "/deals/lot/vulcan-fryer-tx-govdeals-7.8.9"
    assert "FROM tracked_lots" in seen["sql"] and "currency = %s" in seen["sql"] and seen["params"][-2:] == ("govdeals", "7/8/9")


def test_unknown_source_and_untitled_lot_resolve_to_nothing(world):
    assert lp.resolve("hibid", "1", now=NOW) is None
    world["snap"] = [dict(SNAP, title="   ")]
    assert lp.resolve("municibid", "86233903", now=NOW) is None


# ───────────────────────── photos ─────────────────────────

def test_photos_are_source_links_only_and_capped(world):
    world["conn"] = Conn(deal_lot=dict(DL, hero_image_url="http://plain.example/a.jpg"))
    assert lp.resolve("govdeals", "305/10340/1", now=NOW)["photos"] == []
    assert lp.MAX_PHOTOS == 3


@pytest.mark.parametrize("mode,signed_in,shown", [(2, False, True), (1, False, False), (1, True, True), (0, True, False)])
def test_photo_gate_follows_the_site_setting(world, mode, signed_in, shown):
    world["photos"] = mode
    world["conn"] = Conn(deal_lot=DL)
    ctx = lp.view(lp.resolve("govdeals", "305/10340/1", now=NOW), "https://x.test", signed_in=signed_in)
    assert ctx["show_photos"] is shown
    assert ctx["photos_need_sign_in"] is (mode == 1 and not signed_in)
    assert ("image" in ctx["jsonld"][0]) is shown


def test_default_photo_setting_is_everyone():
    from automation import site_settings
    assert site_settings.SPEC["deal_lot_photos"] == {"type": int, "min": 0, "max": 2, "default": 2}


# ───────────────────────── view model ─────────────────────────

def test_view_has_cost_meta_and_jsonld(world):
    world["conn"] = Conn(deal_lot=DL)
    ctx = lp.view(lp.resolve("govdeals", "305/10340/1", now=NOW), "https://x.test")
    assert ctx["cost"]["total"] == 337.5 and ctx["robots"] == "index,follow"
    assert ctx["meta"]["title"] == "Lot of (30) Laptops — Houston, TX | GovDeals auction"
    assert "Current bid $300" in ctx["meta"]["description"] and "4 bids" in ctx["meta"]["description"]
    product, trail = ctx["jsonld"]
    assert product["@type"] == "Product" and product["offers"]["price"] == "300.00"
    assert product["offers"]["availability"].endswith("/InStock")
    assert product["offers"]["priceValidUntil"] == "2026-11-17" and product["offers"]["priceCurrency"] == "USD"
    assert [c["name"] for c in trail["itemListElement"]] == ["Auction deals", "Computers Electronics", "Lot of (30) Laptops"]
    assert ctx["canonical_url"] == "https://x.test/deals/lot/lot-of-30-laptops-tx-govdeals-305.10340.1"


def test_sold_view_prices_the_confirmed_sale_and_drops_the_cost_total(world):
    world["conn"] = Conn(deal_lot=dict(DL, outcome_complete=True, end_utc=NOW - timedelta(days=1)), archive=SOLD)
    ctx = lp.view(lp.resolve("govdeals", "305/10340/1", now=NOW), "https://x.test")
    assert ctx["price"] == 512.5 and ctx["cost"]["total"] is None
    assert ctx["jsonld"][0]["offers"]["price"] == "512.50" and ctx["jsonld"][0]["offers"]["availability"].endswith("/SoldOut")
    assert "priceValidUntil" not in ctx["jsonld"][0]["offers"] and "Sold for $512" in ctx["meta"]["description"]


def test_similar_never_raises_and_skips_itself(world, monkeypatch):
    world["conn"] = Conn(deal_lot=DL)
    lot = lp.resolve("govdeals", "305/10340/1", now=NOW)
    rows = [{"title": f"L{i}", "viewer_url": f"/deals/lot/l{i}-tx-govdeals-{i}.1.1"} for i in range(9)] + \
           [{"title": "self", "viewer_url": lot["path"]}, {"title": "no page", "viewer_url": None}]
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: {"rows": rows})
    out = lp.similar(lot)
    assert len(out) == lp.SIMILAR and lot["path"] not in [r["viewer_url"] for r in out]
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    assert lp.similar(lot) == []


# ───────────────────────── routes ─────────────────────────

@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    rate_limit.reset()
    sg.reset()
    world["conn"] = Conn(deal_lot=DL)
    yield TestClient(app_mod.app, base_url="https://testserver", follow_redirects=False)
    auth_svc.reset_caches()
    rate_limit.reset()


PATH = "/deals/lot/lot-of-30-laptops-tx-govdeals-305.10340.1"


def test_page_renders_for_anyone_with_photo_cost_and_schema(client):
    r = client.get(PATH)
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60, s-maxage=300"
    html = r.text
    assert '<meta name="robots" content="index,follow">' in html
    assert f'<link rel="canonical" href="{app_mod.PUBLIC_BASE_URL}{PATH}">' in html
    assert 'src="https://webassets.lqdt1.com/assets/photos/10340/a.jpg"' in html
    assert 'loading="lazy"' in html and 'referrerpolicy="no-referrer"' in html
    assert 'id="lot-cost"' in html and "12.5 %" in html and "$337.50" in html and "(estimate)" in html
    assert "Sales tax is charged by the seller at checkout and is not included." in html
    assert '<script type="application/ld+json">' in html and '"@type": "Product"' in html
    assert "Bid on GovDeals" in html and 'href="https://www.govdeals.com/en/asset/305/10340"' in html
    assert "Call Bob" in html                                  # the listing text is public
    assert "storage_note" not in html and "cdn.example.com" not in html


def test_a_title_cannot_break_out_of_the_schema_script(client, world):
    world["conn"] = Conn(deal_lot=dict(DL, title='Desk</script><script>alert(1)</script>'))
    html = client.get(client.get("/deals/lot/x-tx-govdeals-305.10340.1").headers["location"]).text
    assert "<script>alert(1)" not in html


def test_wrong_text_part_301s_to_the_canonical_url(client):
    r = client.get("/deals/lot/old-title-zz-govdeals-305.10340.1")
    assert r.status_code == 301 and r.headers["location"] == PATH


def test_old_id_url_301s_for_the_public_and_stays_the_operator_view(client, monkeypatch):
    monkeypatch.setattr(app_mod.db, "fetch_one", lambda sql, params=(): dict(DL, seller="City", gallery_urls=None,
                        archived_hero_url=None, images_archived=False, opening_bid=1.0, has_reserve=False, final_bid=None))
    monkeypatch.setattr(pd, "is_operator_lot", lambda a, b, c: False)
    monkeypatch.setattr("deals.tracking_store.history", lambda a, b: [])
    r = client.get("/deals/305/10340/1")
    assert r.status_code == 301 and r.headers["location"] == PATH
    monkeypatch.setattr(pd, "is_operator_lot", lambda a, b, c: True)
    assert client.get("/deals/305/10340/1").status_code == 404           # an operator pick has no public page
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    r = client.get("/deals/305/10340/1")
    assert r.status_code == 200 and 'id="bid-rail"' in r.text and "<dd>City</dd>" in r.text


def test_unknown_lot_and_bad_url_are_404_and_a_failed_read_is_503(client, world, monkeypatch):
    world["conn"] = Conn()
    world["archive_only"] = None
    assert client.get("/deals/lot/x-tx-govdeals-1.2.3").status_code == 404
    assert client.get("/deals/lot/not-a-lot").status_code == 404
    monkeypatch.setattr(lp, "resolve", lambda s, k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert client.get(PATH).status_code == 503


def test_ended_unconfirmed_page_is_noindex_and_says_so(client, world):
    world["conn"] = Conn(deal_lot=dict(DL, outcome_complete=True, end_utc=NOW - timedelta(days=1)))
    html = client.get(PATH).text
    assert '<meta name="robots" content="noindex,follow">' in html
    assert "Auction ended — final price not confirmed." in html and 'id="lot-cost"' not in html
    world["conn"] = Conn(deal_lot=dict(DL, outcome_complete=True, end_utc=NOW - timedelta(days=1)), archive=SOLD)
    html = client.get(PATH).text
    assert "Sold for $512.50" in html and '<meta name="robots" content="index,follow">' in html
    assert "View on GovDeals" in html


def test_photos_off_and_signed_in_gate_on_the_page(client, world):
    world["photos"] = 0
    lp.clear_cache()
    html = client.get(PATH).text
    assert "webassets.lqdt1.com" not in html and "Sign in (free)" not in html
    world["photos"] = 1
    lp.clear_cache()
    r = client.get(PATH)
    assert "webassets.lqdt1.com" not in r.text and "Sign in (free)" in r.text
    assert r.headers["cache-control"] == "private, no-store"
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    assert "webassets.lqdt1.com" in client.get(PATH).text


def test_robots_opens_the_feed_pages_and_keeps_the_json_closed(client):
    txt = client.get("/robots.txt").text
    lines = txt.splitlines()
    assert "Disallow: /deals" not in lines                       # /deals, /deals/sold and lot pages are crawlable
    for closed in ("/deals/api/", "/platform/api/", "/map/api/", "/distress/api/", "/api/", "/admin"):
        assert f"Disallow: {closed}" in lines
    assert sg.TRAP_PATH.startswith("/deals/api/")
    assert f"Sitemap: {app_mod.PUBLIC_BASE_URL}/sitemap-index.xml" in lines


def test_lot_routes_are_plain_def_so_db_reads_stay_off_the_event_loop():
    import inspect
    from automation.web import lot_page_routes as routes
    for fn in (routes.deal_lot_page, routes.sitemap_index, routes.sitemap_deals_child):
        assert not inspect.iscoroutinefunction(fn), fn.__name__

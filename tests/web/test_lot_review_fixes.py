"""PR #174 review fixes: operator picks hide on every source, currency is never
guessed, similar lots are memoised, the snapshot path never loads in a request,
the sitemap warms off the request path, photos come from source hosts only.
DB-free: every read is faked."""
import importlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import deal_sitemap as dsm
from automation.web import deals_sources as ds
from automation.web import lot_pages as lp
from automation.web import public_deals as pd
from automation.web import rate_limit, scrape_guard as sg

app_mod = importlib.import_module("automation.web.app")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


# ───────────── 1. operator picks: one key helper, every source ─────────────

@pytest.mark.parametrize("site,native_id,asset,account,expected", [
    ("govdeals", "305/10340/1", 305, 10340, {"305/10340"}),
    ("allsurplus", "17/28505/4", 17, 28505, {"17/28505"}),
    # synthetic integer ids never appear in a star; the prefixed native key does
    ("txauction", "9001/445566", 123456789, -10, {"123456789/-10", "tx:445566"}),
    ("publicsurplus", "4020144", 987654, -2, {"987654/-2", "ps:4020144"}),
    ("bidspotter", "aaf53c9e-a9d1", 5, -3, {"5/-3", "bs:aaf53c9e-a9d1"}),
    (None, None, 305, 10340, {"305/10340"}),
])
def test_favorite_keys_cover_every_stored_format(site, native_id, asset, account, expected):
    assert set(pd.favorite_keys(site=site, native_id=native_id, asset_id=asset, account_id=account)) == expected


def test_exclusion_sql_matches_a_star_by_native_key_for_each_prefixed_source():
    where, args = pd.exclusion_where()
    # the three pick tables are all in the one fragment
    for table in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert f"FROM {table}" in where
    # favorites: the maestro pair AND the prefixed native key of each foreign site
    assert "deal_lots.asset_id::text || '/' || deal_lots.account_id::text" in where
    for site, prefix in pd.FAVORITE_PREFIX.items():
        assert f"WHEN '{site}' THEN '{prefix}:'" in where
    assert "regexp_replace(deal_lots.native_id, '^.*/', '')" in where
    # tracked / listed lots key on deal_lots' own integer ids (synthetic for foreign sites)
    assert "t.asset_id = deal_lots.asset_id AND t.account_id = deal_lots.account_id" in where
    assert "li.asset_id = deal_lots.asset_id AND li.account_id = deal_lots.account_id" in where
    assert where.count("%s") == len(args) == 0          # constants only while seating is public


@pytest.mark.parametrize("site,native_id,asset,account,star", [
    ("govdeals", "305/10340/1", 305, 10340, "305/10340"),
    ("allsurplus", "17/28505/4", 17, 28505, "17/28505"),
    ("txauction", "9001/445566", 123456789, -10, "tx:445566"),
    ("publicsurplus", "4020144", 987654, -2, "ps:4020144"),
])
def test_is_operator_lot_asks_favorites_for_every_key_of_the_row(monkeypatch, site, native_id, asset, account, star):
    seen = {}

    def fetch_one(sql, params):
        seen["sql"], seen["params"] = sql, params
        return {"hit": True}

    monkeypatch.setattr(pd.db, "fetch_one", fetch_one)
    assert pd.is_operator_lot(asset, account, 1, site=site, native_id=native_id) is True
    assert "auction_favorites WHERE asset_id = ANY(%s)" in seen["sql"]
    assert "FROM tracked_lots WHERE asset_id=%s AND account_id=%s" in seen["sql"]
    assert "FROM deal_list_items WHERE asset_id=%s AND account_id=%s AND auction_id=%s" in seen["sql"]
    keys = seen["params"][2]
    assert star in keys and seen["sql"].count("%s") == len(seen["params"])


@pytest.mark.parametrize("lot_id,star", [("4020144", "ps:4020144"), ("445566", "tx:445566"),
                                         ("17/28505", "17/28505"), ("aaf5-uuid", "bs:aaf5-uuid")])
def test_snapshot_half_hides_a_star_in_any_format(lot_id, star):
    picks = pd.OperatorPicks(ids={star, star.split(":", 1)[-1]},
                             pairs={(17, 28505)} if "/" in star else ())
    assert pd.is_operator_pick(picks, source_lot_id=lot_id)


def test_tracked_and_listed_lots_hide_on_the_snapshot_half_by_url_and_title():
    picks = pd.OperatorPicks(pairs={(305, 10340)}, urls={"publicsurplus.com/sms/auction/view?auc=4020144"},
                             titles={"stack chairs lot of 80"})
    assert pd.is_operator_pick(picks, source_lot_id="4020144",
                               url="https://www.publicsurplus.com/sms/auction/view?auc=4020144")
    assert pd.is_operator_pick(picks, source_lot_id="x", title="Stack Chairs - Lot of 80")
    assert pd.is_operator_pick(picks, source_lot_id="x", url="https://www.govdeals.com/en/asset/305/10340")


# ───────────── 3 + 4. currency is exact, never guessed ─────────────

def test_a_sale_with_unknown_or_foreign_currency_is_not_a_confirmed_sale():
    base = {"outcome": "sold", "final_price": 500.0, "closed_at": NOW}
    assert lp.confirmed_sale({**base, "currency": "USD"})[0] == 500.0
    assert lp.confirmed_sale({**base, "currency": None})[0] is None
    assert lp.confirmed_sale({**base, "currency": "CAD"})[0] is None
    assert lp.confirmed_sale({**base, "currency": "usd"})[0] is None


DL = {"site": "allsurplus", "native_id": "17/28505/4", "asset_id": 17, "account_id": 28505, "auction_id": 4,
      "title": "Forklift", "description": None, "canonical_category": "heavy_equipment",
      "native_category_name": "Forklifts", "city": "Toronto", "state": "ON", "lat": None, "lng": None,
      "bid_count": 4, "current_bid": 300.0, "currency_code": "CAD", "end_utc": NOW + timedelta(days=2),
      "outcome": None, "final_bid_count": None, "outcome_complete": False, "first_seen_at": NOW,
      "hero_image_url": "https://webassets.lqdt1.com/assets/photos/28505/a.jpg", "public_ok": True}


class Conn:
    def __init__(self, deal_lot=None, archive=None):
        self.deal_lot, self.archive = deal_lot, archive

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        row = self.archive if "FROM lot_archive" in sql else self.deal_lot
        return type("Cur", (), {"fetchone": lambda s: dict(row) if row else None})()


@pytest.fixture
def world(monkeypatch):
    state = {"conn": Conn()}
    monkeypatch.setattr(lp.db, "connect", lambda: state["conn"])
    monkeypatch.setattr(lp.db, "fetch_one", lambda sql, params=(): None)
    monkeypatch.setattr(lp.site_settings, "get_all", lambda: {"deal_lot_photos": 2})
    monkeypatch.setattr(lp, "similar", lambda lot: [])
    lp.clear_cache()
    yield state
    lp.clear_cache()


def test_live_non_usd_lot_shows_no_price_no_cost_and_no_offer(world):
    world["conn"] = Conn(deal_lot=DL)
    lot = lp.resolve("allsurplus", "17/28505/4", now=NOW)
    assert lot["current_bid"] is None and lot["price_on_source"] is True
    v = lp.view(lot, "https://black-whole.com")
    assert v["price"] is None and v["cost"]["total"] is None
    assert "offers" not in v["jsonld"][0] and "$" not in v["meta"]["description"]


def test_usd_lot_keeps_its_price(world):
    world["conn"] = Conn(deal_lot={**DL, "currency_code": "USD"})
    lot = lp.resolve("allsurplus", "17/28505/4", now=NOW)
    assert lot["current_bid"] == 300.0 and lot["price_on_source"] is False


def test_ended_unconfirmed_lot_has_no_offer_in_the_schema(world):
    world["conn"] = Conn(deal_lot={**DL, "currency_code": "USD", "outcome_complete": True, "outcome": "sold"})
    lot = lp.resolve("allsurplus", "17/28505/4", now=NOW)
    assert lot["ended"] and lot["sold_price"] is None
    assert "offers" not in lp.jsonld(lot, "https://black-whole.com", photos=False)[0]


def test_untitled_deal_lots_row_is_not_a_page(world):
    world["conn"] = Conn(deal_lot={**DL, "title": "  "})
    assert lp.resolve("allsurplus", "17/28505/4", now=NOW) is None


# ───────────── 2. similar lots: one memo per (category, state), no count ─────────────

def test_similar_is_memoised_per_category_and_state_and_never_counts(monkeypatch):
    calls = []

    def fake_rows(*, category, state, limit):
        calls.append((category, state))
        return [{"viewer_url": f"/deals/lot/x-{i}", "title": str(i)} for i in range(8)]

    monkeypatch.setattr(pd, "fetch_page", lambda **kw: pytest.fail("similar must not run the counting page query"))
    monkeypatch.setattr(pd, "similar_rows", fake_rows)
    pd.clear_cache()
    lot = {"category": "computers_electronics", "state": "TX", "path": "/deals/lot/x-0"}
    first = lp.similar(lot)
    again = lp.similar({**lot, "path": "/deals/lot/x-3"})
    assert calls == [("computers_electronics", "TX")]
    assert len(first) == lp.SIMILAR and all(r["viewer_url"] != "/deals/lot/x-0" for r in first)
    assert all(r["viewer_url"] != "/deals/lot/x-3" for r in again)
    pd.clear_cache()


def test_similar_rows_sql_has_no_count_and_keeps_the_policy(monkeypatch):
    seen = []

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            seen.append((sql, params))
            return type("Cur", (), {"fetchall": lambda s: []})()

    monkeypatch.setattr(pd.db, "connect", lambda: C())
    monkeypatch.setattr(ds, "peek", lambda status: [])
    pd.similar_rows(category="computers_electronics", state="TX", limit=12)
    assert len(seen) == 1
    sql, params = seen[0]
    assert "count(" not in sql.lower() and pd.exclusion_where()[0] in sql
    assert sql.count("%s") == len(params)


# ───────────── 7. snapshot path never loads inside a request ─────────────

def test_find_on_a_cold_memo_is_a_miss_and_starts_one_background_refresh(monkeypatch):
    ds.clear_cache()
    started = []
    monkeypatch.setattr(ds, "_read", lambda status: pytest.fail("a request must never run the snapshot read"))
    monkeypatch.setattr(ds, "_spawn_refresh", lambda status: started.append(status))
    assert ds.find("municibid", "86233903", now=NOW) is None
    assert sorted(started) == ["closed", "open"]
    ds.clear_cache()


def test_find_on_a_warm_memo_returns_the_one_row_without_copying_the_set(monkeypatch):
    ds.clear_cache()
    live = {"source": "municibid", "source_lot_id": "1", "end_utc": NOW + timedelta(hours=1), "title": "A"}
    gone = {"source": "municibid", "source_lot_id": "2", "end_utc": NOW - timedelta(hours=1), "title": "B"}
    done = {"source": "municibid", "source_lot_id": "3", "end_utc": NOW - timedelta(days=1), "title": "C"}
    ds._store("open", {"public": [live, gone], "seating": []})
    ds._store("closed", {"public": [done], "seating": []})
    monkeypatch.setattr(ds, "_read", lambda status: pytest.fail("warm memo: no read"))
    hit = ds.find("municibid", "1", now=NOW)
    assert hit == live and hit is not live                       # a copy of ONE row
    assert ds.find("municibid", "2", now=NOW) is None            # ended while the memo was warm
    assert ds.find("municibid", "3", now=NOW)["title"] == "C"
    assert ds.find("municibid", "nope", now=NOW) is None
    ds.clear_cache()


def test_lot_404_is_not_cacheable(monkeypatch):
    monkeypatch.setattr(lp, "resolve", lambda source, key: None)
    rate_limit.reset()
    sg.reset()
    r = TestClient(app_mod.app).get("/deals/lot/zero-turn-mower-pa-municibid-86233903")
    assert r.status_code == 404 and r.headers.get("cache-control") == "no-store"


# ───────────── 6. cold sitemap ─────────────

def test_cold_sitemap_is_valid_minimal_and_not_cacheable(monkeypatch):
    dsm.clear_cache()
    monkeypatch.setattr(dsm, "_spawn", lambda: None)
    c = TestClient(app_mod.app)
    idx = c.get("/sitemap-index.xml")
    assert idx.status_code == 200 and idx.headers["cache-control"] == "no-cache"
    assert idx.text.startswith("<?xml") and "<sitemapindex" in idx.text
    child = c.get("/sitemaps/deals-1.xml")
    assert child.status_code == 200 and child.headers["cache-control"] == "no-cache"
    assert "<urlset" in child.text
    dsm._state.update(at=__import__("time").monotonic(), built=True, urls=[("/deals/lot/a-tx-govdeals-1.2.3", None)])
    assert c.get("/sitemap-index.xml").headers["cache-control"] == "public, max-age=3600"
    dsm.clear_cache()


def test_warm_starts_one_daemon_build_and_never_blocks(monkeypatch):
    dsm.clear_cache()
    gate, ran = threading.Event(), []

    def slow_collect():
        gate.wait(2)
        ran.append(1)
        return [("/deals/lot/a-tx-govdeals-1.2.3", None)]

    monkeypatch.setattr(dsm, "collect", slow_collect)
    dsm.warm()
    dsm.warm()                                   # a second call while busy starts nothing
    assert not dsm.ready() and ran == []          # returned before the build finished
    gate.set()
    for _ in range(200):
        if dsm.ready():
            break
        __import__("time").sleep(0.01)
    assert dsm.ready() and ran == [1]
    dsm.clear_cache()


def test_startup_hook_warms_the_sitemap():
    import inspect
    assert "deal_sitemap.warm()" in inspect.getsource(app_mod._start_alerts_loop)


# ───────────── 5. /deals is indexed on its plain page only ─────────────

def test_deals_plain_page_is_indexable_and_variants_are_not(monkeypatch):
    monkeypatch.setattr(app_mod, "_platform_viewer", lambda request: None)
    c = TestClient(app_mod.app)
    plain = c.get("/deals").text
    assert '<meta name="robots" content="index,follow">' in plain
    assert f'<link rel="canonical" href="{app_mod.PUBLIC_BASE_URL}/deals">' in plain
    for q in ("?view=table", "?q=forklift", "?page=2", "?state=TX"):
        html = c.get("/deals" + q).text
        assert '<meta name="robots" content="noindex,follow">' in html
        assert f'<link rel="canonical" href="{app_mod.PUBLIC_BASE_URL}/deals">' in html
    assert "/deals" in dsm.STATIC_PATHS


# ───────────── photos: source hosts only ─────────────

@pytest.mark.parametrize("url", [
    "https://webassets.lqdt1.com/assets/photos/10340/a.jpg",
    "https://storagemunicibidpro.blob.core.windows.net/p/1.jpg",
    "https://d323w7klwy72q3.cloudfront.net/i/1.jpg",
    "https://www.gsaauctions.gov/img/1.jpg",
])
def test_source_photo_hosts_pass(url):
    assert ds.safe_photo_url(url) == url


@pytest.mark.parametrize("url", [
    "https://pub-abc123.r2.dev/p/secret.jpg",
    "https://abc.r2.cloudflarestorage.com/lot-archive/x.jpg",
    "https://nihgzltpjriekyqqucbd.supabase.co/storage/v1/object/public/x.jpg",
    "https://black-whole.com/api/deal-photos/key",
    "https://img.black-whole.com/p/abc.jpg",
    "https://evil.example/x.jpg",
    "https://lqdt1.com.evil.example/x.jpg",
    "http://webassets.lqdt1.com/assets/photos/1/a.jpg",
])
def test_private_and_unknown_hosts_never_pass(url):
    assert ds.safe_photo_url(url) is None


def test_env_cannot_allow_a_private_host(monkeypatch):
    monkeypatch.setenv("DEAL_LOT_PHOTO_HOSTS", "r2.dev,cdn.newsource.com")
    assert ds.safe_photo_url("https://pub-abc.r2.dev/x.jpg") is None
    assert ds.safe_photo_url("https://cdn.newsource.com/x.jpg") == "https://cdn.newsource.com/x.jpg"


# ───────────── Chairs feed: operator links never point at a public 404 ─────────────

def test_chairs_feed_rows_link_the_operator_view_or_nothing():
    from automation.web import chairs_feed as cf
    from deals.fees import fee_model_from_env
    gd = cf.shape_govdeals({"asset_id": 1, "account_id": 2, "auction_id": 3, "title": "50 chairs",
                            "current_bid": 10.0, "operator_pick": True}, fee_model_from_env())
    assert gd["viewer_url"] == "/deals/1/2/3"                      # the operator's own viewer
    snap = cf.shape_snapshot({"id": "municibid:9", "source": "municibid", "title": "40 chairs",
                              "viewer_url": "/deals/lot/40-chairs-pa-municibid-9", "operator_pick": True})
    assert snap["viewer_url"] is None

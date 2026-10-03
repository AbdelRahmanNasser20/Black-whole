"""Craigslist adapter — pure copy builder, strict city/ZIP resolution, registry
discovery, and the sync-callable entry points with the browser stubbed out.
No browser, no network, no DB: `db.execute` and `channels.store.upsert` are
monkeypatched to record what the adapter would write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from automation import channels, craigslist
from automation.channels import sync
from automation.publish import registry
from automation.publish.adapters import craigslist as cl_adapter
from automation.publish.models import STATUS_DRY_RUN, PublishRequest, ListingData

NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)


def lot(**over) -> dict:
    row = {
        "lot_id": "9006", "title": "Mauve Banquet Chairs", "status": "owned",
        "quantity_remaining": 442, "price_per_chair": 28, "city": "Phoenix", "state": "AZ",
        "zip_code": None, "chair_type": None, "subtitle": "Light-purple diamond seats.",
        "description": "Mauve event banquet chairs on black frames. Stackable.",
        "hero_image_url": "https://r2.example/p/x/h.jpg",
        "image_urls": ["https://r2.example/p/x/h.jpg"] + [f"https://r2.example/p/x/{i}.jpg" for i in range(10)],
        "fake_sold_out": False, "storage_note": "gate 1234",
    }
    row.update(over)
    return row


# ── city / ZIP resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize("city,state,sub", [
    ("Phoenix", "AZ", "phoenix"), ("Atlanta", "GA", "atlanta"), ("Pittsburgh", "PA", "pittsburgh"),
    ("Nashville", "TN", "nashville"), ("Stanton", "CA", "orangecounty"), ("Boise", "Idaho", "boise"),
    ("Las Vegas", "NV", "lasvegas"), ("Tampa", "FL", "tampa"), ("Orlando", "FL", "orlando"),
])
def test_resolve_lot_subdomain_known_cities(city, state, sub):
    assert craigslist.resolve_lot_subdomain(city, state) == sub


def test_resolve_lot_subdomain_unknown_city_raises():
    with pytest.raises(ValueError, match="CITY_SUBDOMAINS"):
        craigslist.resolve_lot_subdomain("Timbuktu", "ML")
    with pytest.raises(ValueError):
        craigslist.resolve_lot_subdomain("", "")


def test_zip_from_row_wins_then_city_fallback_then_raise():
    assert craigslist.build_lot_post(lot(zip_code="85254")).postal == "85254"
    assert craigslist.build_lot_post(lot()).postal == craigslist.CITY_ZIP["phoenix"]
    with pytest.raises(ValueError, match="ZIP"):
        craigslist.build_lot_post(lot(city="Dallas", state="TX"))   # subdomain known, no ZIP


def test_build_lot_post_never_touches_storage_note():
    post = craigslist.build_lot_post(lot())
    assert "gate 1234" not in post.title and "gate 1234" not in post.body


# ── title ───────────────────────────────────────────────────────────────────

def test_title_has_qty_and_location_within_70():
    post = craigslist.build_lot_post(lot())
    assert post.title == "Mauve Banquet Chairs — 442 Available, Stackable (Phoenix, AZ)"
    assert len(post.title) <= craigslist.CL_TITLE_MAX


def test_title_replaces_existing_location_and_fits():
    row = lot(lot_id="gd-56-9685", city="Pittsburgh", state="PA", quantity_remaining=2500,
              title="~2,500 Wire Frame Stacking Chairs — Chrome Frame, Dark Plum Pad, Linkable (Pittsburgh, PA)")
    t = craigslist.build_lot_post(row).title
    assert len(t) <= craigslist.CL_TITLE_MAX
    assert t.endswith("(Pittsburgh, PA)")
    assert t.count("(Pittsburgh, PA)") == 1
    assert t.startswith("~2,500 Wire Frame")


def test_title_falls_back_to_qty_chair_type():
    t = craigslist.build_lot_post(lot(title="", chair_type="Banquet Chairs")).title
    assert t.startswith("442 Banquet Chairs")


def test_title_without_description_uses_subtitle_in_body():
    post = craigslist.build_lot_post(lot(description=None))
    assert post.body.startswith("Light-purple diamond seats.")


# ── body ────────────────────────────────────────────────────────────────────

def test_body_lines_in_order_no_emoji_no_auction_words():
    post = craigslist.build_lot_post(lot())
    lines = post.body.splitlines()
    assert lines[0] == "Mauve event banquet chairs on black frames. Stackable."
    assert "Quantity available: 442" in lines
    assert "$28 per chair. Bulk discounts on the full lot." in lines
    assert "Local pickup in Phoenix. Delivery quotes available for larger orders." in lines
    assert lines[-1] == "Reply with how many you need and whether you want pickup or delivery."
    assert "black-whole.com" in post.body
    low = post.body.lower()
    for banned in ("bid", "auction", "govdeals"):
        assert banned not in low
    assert post.price == 28 and post.city == "Phoenix" and post.subarea_pref == "phx north"


def test_price_required():
    with pytest.raises(ValueError, match="price"):
        craigslist.build_lot_post(lot(price_per_chair=0))


def test_photo_urls_hero_first_capped():
    urls = craigslist.lot_photo_urls(lot())
    assert urls[0] == "https://r2.example/p/x/h.jpg"
    assert len(urls) == craigslist.CL_MAX_PHOTOS
    assert len(set(urls)) == len(urls)


def test_per_city_override_uses_city_zip_not_row_zip():
    post = craigslist.build_lot_post(lot(zip_code="85254"), city="Atlanta")
    assert post.subdomain == "atlanta" and post.postal == craigslist.CITY_ZIP["atlanta"]
    assert "(Atlanta)" in post.title and "AZ" not in post.title


# ── registry ────────────────────────────────────────────────────────────────

def test_registry_discovers_craigslist_with_alias():
    names = registry.load_builtin(force=True)
    assert "craigslist" in names
    assert registry.get("cl") is registry.get("craigslist")
    assert registry.get("craigslist").per_city is True


async def test_orchestrator_dry_run_never_opens_browser(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("post_listing called on a dry run")
    monkeypatch.setattr(craigslist, "post_listing", boom)
    data = ListingData(title="Mauve Banquet Chairs", city="Phoenix", state="AZ", quantity="442",
                       price_per_chair=28, lot_id="9006", description_text="desc")
    res = await cl_adapter.CraigslistAdapter().publish(object(), PublishRequest(data, city="atlanta", dry_run=True))
    assert res.status == STATUS_DRY_RUN and res.ok
    assert "atlanta.craigslist.org" in (res.detail or "")


# ── sync-callable entry points (browser stubbed) ────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    """Record the adapter's bookkeeping; stub the browser + photo download."""
    writes = {"sql": [], "store": [], "posts": [], "manage": []}

    monkeypatch.setattr(cl_adapter.db, "execute", lambda sql, params=None: writes["sql"].append((sql, params)) or 1)
    monkeypatch.setattr(cl_adapter.channel_store, "upsert",
                        lambda lot_id, channel, **kw: writes["store"].append((lot_id, channel, kw)) or {})
    monkeypatch.setattr(craigslist, "download_lot_photos", lambda row, log=print: [Path("/tmp/a.jpg")])

    async def fake_post(post, images):
        writes["posts"].append((post, images))
        return (f"https://{post.subdomain}.craigslist.org/nph/fuo/d/x/7977055260.html", "7977055260")

    async def fake_manage(external_id, action):
        writes["manage"].append((external_id, action))
        return True

    monkeypatch.setattr(cl_adapter, "_post_live", fake_post)
    monkeypatch.setattr(cl_adapter, "_manage_live", fake_manage)
    return writes


def test_publish_lot_posts_and_records(ledger):
    res = cl_adapter.CraigslistAdapter().publish_lot(sync._public_row(lot()))
    assert res == {"url": "https://phoenix.craigslist.org/nph/fuo/d/x/7977055260.html", "external_id": "7977055260"}
    (post, images), = ledger["posts"]
    assert post.subdomain == "phoenix" and images == [Path("/tmp/a.jpg")]
    (sql, params), = ledger["sql"]
    assert "craigslist_url" in sql and "%s" in sql and params == (res["url"], "9006")
    assert ledger["store"] == [("9006", "craigslist", {"state": "live", "url": res["url"], "external_id": "7977055260"})]


def test_publish_lot_bookkeeping_failure_does_not_break_the_post(ledger, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(cl_adapter.db, "execute", boom)
    monkeypatch.setattr(cl_adapter.channel_store, "upsert", boom)
    res = cl_adapter.CraigslistAdapter().publish_lot(lot())
    assert res["external_id"] == "7977055260"


def test_unpublish_uses_external_id_or_url_and_clears(ledger):
    ad = cl_adapter.CraigslistAdapter()
    ad.unpublish_lot(lot(), {"external_id": "123"})
    ad.unpublish_lot(lot(craigslist_url="https://phoenix.craigslist.org/nph/fuo/d/x/456.html"), None)
    assert ledger["manage"] == [("123", "delete"), ("456", "delete")]
    assert ledger["sql"][0][1] == (None, "9006")
    assert ledger["store"][0] == ("9006", "craigslist", {"state": "delisted"})
    with pytest.raises(RuntimeError, match="post id"):
        ad.unpublish_lot(lot(), None)


def test_update_lot_is_explicitly_unsupported(ledger):
    with pytest.raises(NotImplementedError, match="re-list"):
        cl_adapter.CraigslistAdapter().update_lot(lot(), {})


def test_run_sync_inside_a_running_loop():
    async def inner():
        async def coro():
            return 7
        return cl_adapter.run_sync(coro)
    import asyncio
    assert asyncio.run(inner()) == 7


# ── through the sync engine ─────────────────────────────────────────────────

def test_sync_loop_lists_a_lot_on_craigslist_via_the_adapter(ledger, monkeypatch):
    upserts, states = [], []
    monkeypatch.setattr(sync.inventory, "list_all", lambda: [lot()])
    monkeypatch.setattr(sync.store, "current_map", lambda: {})
    monkeypatch.setattr(sync.store, "recent_synced_at", lambda channel, since: [])
    monkeypatch.setattr(sync.store, "upsert", lambda lot_id, channel, *, state, **kw: upserts.append((lot_id, channel, state, kw)) or {})
    monkeypatch.setattr(sync.store, "set_state", lambda lot_id, channel, state, *, last_error=None: states.append((lot_id, channel, state)) or {})
    monkeypatch.setattr(sync.site_settings, "channel_enabled", lambda c: c == "craigslist")
    monkeypatch.setattr(sync.site_settings, "get_all", lambda: {"browser_channel_daily_cap": 4, "browser_channel_spacing_s": 1200})
    monkeypatch.setattr(sync.registry, "get", lambda key: cl_adapter.CraigslistAdapter() if key == "craigslist" else None)

    rep = sync.run_once(now=NOW)
    assert rep["errors"] == 0
    (post, _), = ledger["posts"]
    assert "gate 1234" not in post.body
    # `cl_adapter.channel_store` IS `sync.store`, so the adapter's own mirror write
    # and the engine's final write (the one with the hash) both land here.
    cl_writes = [u for u in upserts if u[1] == "craigslist"]
    lot_id, channel, state, kw = cl_writes[-1]
    assert (lot_id, state, kw["external_id"]) == ("9006", "live", "7977055260")
    assert kw["url"].endswith("/7977055260.html") and kw["payload_hash"]


def test_sync_loop_records_unknown_city_as_error_not_silence(ledger, monkeypatch):
    upserts = []
    monkeypatch.setattr(sync.inventory, "list_all", lambda: [lot(city="Timbuktu", state="ML")])
    monkeypatch.setattr(sync.store, "current_map", lambda: {})
    monkeypatch.setattr(sync.store, "recent_synced_at", lambda channel, since: [])
    monkeypatch.setattr(sync.store, "upsert", lambda lot_id, channel, *, state, **kw: upserts.append((channel, state, kw)) or {})
    monkeypatch.setattr(sync.store, "set_state", lambda *a, **kw: {})
    monkeypatch.setattr(sync.site_settings, "channel_enabled", lambda c: c == "craigslist")
    monkeypatch.setattr(sync.site_settings, "get_all", lambda: {"browser_channel_daily_cap": 4, "browser_channel_spacing_s": 1200})
    monkeypatch.setattr(sync.registry, "get", lambda key: cl_adapter.CraigslistAdapter() if key == "craigslist" else None)

    rep = sync.run_once(now=NOW)
    assert rep["errors"] == 1 and ledger["posts"] == []
    (channel, state, kw), = upserts
    assert channel == "craigslist" and state == "error" and "CITY_SUBDOMAINS" in kw["last_error"]

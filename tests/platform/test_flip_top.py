"""automation/web/flip_top.py — the /platform top deals.

Guards: the request path never reads the DB (memo + thread); no read, a failed
read or the flag off = no section, never a 500; only allow-listed keys leave;
vague, damaged, weak or scattered lots never reach the list; an ended lot drops.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import flip_top, platform_api
from automation.web.app import app
from deals.auction_comps import CompResult

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("FLIP_TOP_ENABLED", "1")
    flip_top.clear_cache()
    yield
    flip_top.clear_cache()


def _row(title, cat="vehicles", bid=1000.0, bids=9, hours=5, n=1):
    return {"site": "govdeals", "asset_id": n, "account_id": 2, "auction_id": 3, "native_id": None,
            "title": title, "canonical_category": cat, "city": "Mesa", "state": "AZ",
            "bid_count": bids, "current_bid": bid, "end_utc": NOW + timedelta(hours=hours),
            "hero_image_url": "https://webassets.lqdt1.com/assets/photos/1/a.jpg"}


def _res(value=9000.0, cost=1125.0, comps=20, score=98, damaged=False, p25=None, p75=None):
    return CompResult(comp_count=comps, comp_value=value, unit_value=value,
                      p25=p25 or value * 0.8, p75=p75 or value * 1.2, quantity=1,
                      damaged=damaged, score=score, cost=cost)


def test_pick_filters_weak_damaged_vague_and_scattered():
    live = [_row("2018 Ford Transit Cargo Van", n=1),
            _row("2017 Ford F-550 Dump Truck", n=2),
            _row("2016 Chevrolet Tahoe PPV", n=3),
            _row("2015 Dodge Charger Pursuit", n=4),
            _row("Dell Laptops", n=5),
            _row("2014 Toyota Tacoma Pickup", n=6),
            _row("2013 Honda Civic Sedan", n=7)]
    results = {0: _res(), 1: _res(comps=6), 2: _res(damaged=True), 3: _res(p25=1000, p75=9000),
               4: _res(), 5: _res(score=40, cost=9500), 6: _res(value=100, cost=20)}
    picked = flip_top.pick(live, results, now=NOW)
    assert [r["title"] for r, _ in picked] == ["2018 Ford Transit Cargo Van"]


def test_pick_dedupes_titles_and_caps_a_category():
    live = [_row("2018 Ford Transit Cargo Van", n=n) for n in range(3)]
    live += [_row(f"201{n} Ford F-{n}50 Dump Truck", n=10 + n) for n in range(5)]
    picked = flip_top.pick(live, {i: _res() for i in range(len(live))}, now=NOW)
    titles = [r["title"] for r, _ in picked]
    assert titles.count("2018 Ford Transit Cargo Van") == 1
    assert len(picked) == flip_top.PER_CATEGORY


def test_card_is_an_allow_list():
    card = flip_top._card(_row("2018 Ford Transit Cargo Van"), _res(), photos=True)
    assert set(card) == {"title", "url", "place", "bid", "bid_count", "ends", "score",
                         "comp_value", "comp_count", "pct_of_comp", "photo_url"}
    assert card["url"].startswith("/deals/lot/")
    assert card["pct_of_comp"] == 12
    assert flip_top._card(_row("x"), _res(), photos=False)["photo_url"] is None
    bad = _row("2018 Ford Transit Cargo Van")
    bad["hero_image_url"] = "https://pub-abc.r2.dev/private.jpg"
    assert flip_top._card(bad, _res(), photos=True)["photo_url"] is None


def test_live_read_goes_through_the_public_policy():
    assert "{policy}" in flip_top._LIVE_SQL
    assert "deal_verdicts" not in flip_top._LIVE_SQL + flip_top._SOLD_SQL


def _wait(pred, secs=3.0):
    end = time.time() + secs
    while time.time() < end and not pred():
        time.sleep(0.01)


def _data(hours=5.0):
    ends = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"lots": [{"title": "2018 Ford Transit Cargo Van", "url": "/deals/lot/x-az-govdeals-1.2.3",
                      "place": "Mesa, AZ", "bid": 1000.0, "bid_count": 9, "ends": ends, "score": 98,
                      "comp_value": 9000.0, "comp_count": 20, "pct_of_comp": 12, "photo_url": None}],
            "as_of": "x", "scanned": 1, "scored": 1}


def test_top_never_blocks_then_serves_the_memo(monkeypatch):
    calls = []
    monkeypatch.setattr(flip_top, "compute", lambda: calls.append(1) or _data())
    assert flip_top.top() is None                      # first view: nothing read yet
    _wait(lambda: flip_top._memo["data"] is not None)
    got = flip_top.top()
    assert got["lots"][0]["closes_in"] in ("4 h", "5 h")
    flip_top.top()
    assert len(calls) == 1


def test_failed_read_and_flag_off_give_none(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(flip_top, "compute", boom)
    assert flip_top.top() is None
    _wait(lambda: not flip_top._memo["refreshing"])
    assert flip_top.top() is None                      # backs off, no raise
    monkeypatch.setenv("FLIP_TOP_ENABLED", "0")
    monkeypatch.setattr(flip_top, "compute", lambda: pytest.fail("read while off"))
    assert flip_top.top() is None


def test_ended_lots_drop_out(monkeypatch):
    monkeypatch.setattr(flip_top, "compute", lambda: _data(hours=-1))
    flip_top.top()
    _wait(lambda: flip_top._memo["data"] is not None)
    assert flip_top.top() is None


def _page(monkeypatch, top):
    monkeypatch.setattr(platform_api, "stats", lambda: None)
    monkeypatch.setattr(flip_top, "top", lambda: top)
    r = TestClient(app, base_url="https://testserver").get("/platform")
    assert r.status_code == 200
    return r.text


def test_landing_renders_the_section_only_with_data(monkeypatch):
    assert 'id="top-deals"' not in _page(monkeypatch, None)
    lot = {**_data()["lots"][0], "closes_in": "5 h"}
    html = _page(monkeypatch, {"lots": [lot]})
    assert 'id="top-deals"' in html
    assert "Top 1 deals closing soon" in html
    assert 'href="/deals/lot/x-az-govdeals-1.2.3"' in html
    assert "Similar lots closed near $9,000 (20 sales)" in html
    assert "not bidding advice" in html


def test_thread_start_failure_never_raises_and_does_not_stick(monkeypatch):
    class Dead:
        def __init__(self, *a, **k): pass
        def start(self): raise RuntimeError("can't start new thread")
    monkeypatch.setattr(flip_top.threading, "Thread", Dead)
    assert flip_top.top() is None
    assert flip_top._memo["refreshing"] is False


def test_old_memo_is_dropped_when_reads_keep_failing(monkeypatch):
    flip_top._memo.update(data=_data(), at=time.monotonic() - flip_top.MAX_AGE - 1,
                          fail_until=time.monotonic() + 60)
    assert flip_top.top() is None


def test_pick_skips_a_lot_with_no_public_page(monkeypatch):
    live = [_row("2018 Ford Transit Cargo Van", n=1), _row("2017 Ford F-550 Dump Truck", n=2)]
    real = flip_top.deal_lot_urls.for_row
    monkeypatch.setattr(flip_top.deal_lot_urls, "for_row",
                        lambda row: None if row["asset_id"] == 1 else real(row))
    picked = flip_top.pick(live, {0: _res(), 1: _res()}, now=NOW)
    assert [r["asset_id"] for r, _ in picked] == [2]

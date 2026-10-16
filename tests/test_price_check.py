"""Unit tests for automation/price_check.py — the "what this costs new" strip.

The hard rules under test:
- never an invented number: anchors come from live price_anchors rows or the
  two hand-verified statics, nothing else;
- anchors at or below our price are dropped (never an anti-selling row);
- any DB failure degrades to the static anchors, never a 500;
- sold / unpriced lots get no strip at all.
"""
import pytest

from automation import db, price_check


@pytest.fixture(autouse=True)
def _fresh_cache():
    price_check.reset_cache()
    yield
    price_check.reset_cache()


def _lot(**over):
    row = {
        "price_per_chair": 25.0, "is_sold": False,
        "title": "Mauve Banquet Chairs", "subtitle": None,
        "chair_type": "Banquet Chairs", "location_labels": ["Atlanta, GA"],
    }
    row.update(over)
    return row


def _live_row(**over):
    row = {
        "title": "MTS Seating 594 Omega I Banquet Stacking Chair",
        "brand": "MTS Seating", "price_per_chair": 141.61,
        "listing_url": "https://www.todaysclassroom.com/mts-594/",
    }
    row.update(over)
    return row


# ── pick_anchors (pure) ──────────────────────────────────────────────────────

def test_live_brand_match_beats_the_statics():
    rows = [
        _live_row(),
        _live_row(title="Lancaster economy banquet chair", brand="Lancaster",
                  price_per_chair=31.49,
                  listing_url="https://www.webstaurantstore.com/lancaster/"),
    ]
    picks = price_check.pick_anchors(rows, ours=25.0, lot_text="mts omega banquet chairs")
    assert [p["price"] for p in picks] == [141.61, 31.49]
    assert picks[0]["source"] == "todaysclassroom.com"
    assert picks[0]["label"].endswith("— new")


def test_no_brand_match_takes_highest_plus_cheapest():
    rows = [
        _live_row(brand="Shelby Williams", price_per_chair=200.0,
                  listing_url="https://a.example/1"),
        _live_row(brand="Lancaster", price_per_chair=31.0,
                  listing_url="https://b.example/2"),
        _live_row(brand="NPS", price_per_chair=143.0,
                  listing_url="https://c.example/3"),
    ]
    picks = price_check.pick_anchors(rows, ours=25.0, lot_text="mauve banquet chairs")
    assert [p["price"] for p in picks] == [200.0, 31.0]


def test_anchors_at_or_below_our_price_are_dropped():
    rows = [_live_row(price_per_chair=24.0), _live_row(price_per_chair=25.0,
            listing_url="https://b.example/2")]
    picks = price_check.pick_anchors(rows, ours=25.0, lot_text="mts")
    # Both live rows are useless; the verified statics (169 / 47) fill in.
    assert [p["price"] for p in picks] == [169.0, 47.0]
    assert all(p["price"] > 25.0 for p in picks)


def test_nothing_above_our_price_means_no_strip():
    assert price_check.pick_anchors([], ours=500.0, lot_text="x") == []


def test_one_live_row_is_topped_up_from_the_statics():
    picks = price_check.pick_anchors([_live_row()], ours=25.0, lot_text="mts")
    assert len(picks) == 2
    assert picks[0]["price"] == 169.0          # static Omega II outprices the live row
    assert picks[1]["price"] == 141.61


def test_garbage_live_rows_never_crash_or_render():
    rows = [
        {"title": "No price", "listing_url": "https://x.example/a"},
        {"title": "Bad url", "price_per_chair": 90.0, "listing_url": "not a url"},
        {"price_per_chair": "nan?", "listing_url": "https://x.example/b"},
    ]
    picks = price_check.pick_anchors(rows, ours=25.0, lot_text="x")
    assert [p["price"] for p in picks] == [169.0, 47.0]


def test_long_titles_are_clipped_at_a_word():
    row = _live_row(title="MTS Seating 578 Omega II Banquet Stacking Chair "
                          "square back 18 inch seat height extra words")
    pick = price_check.pick_anchors([row], ours=25.0, lot_text="mts")[1]
    assert len(pick["label"]) <= price_check.LABEL_MAX + len("… — new")
    assert pick["label"].endswith("… — new")


# ── strip_for ────────────────────────────────────────────────────────────────

def test_sold_or_unpriced_lot_gets_no_strip(monkeypatch):
    monkeypatch.setattr(price_check, "live_anchors", lambda: [])
    assert price_check.strip_for(_lot(is_sold=True)) is None
    assert price_check.strip_for(_lot(price_per_chair=None)) is None
    assert price_check.strip_for(_lot(price_per_chair=0)) is None


def test_strip_carries_ours_and_the_first_location(monkeypatch):
    monkeypatch.setattr(price_check, "live_anchors", lambda: [])
    strip = price_check.strip_for(_lot())
    assert strip["ours"] == 25.0
    assert strip["where"] == "Atlanta, GA"
    assert [a["price"] for a in strip["anchors"]] == [169.0, 47.0]


def test_no_location_degrades_to_none(monkeypatch):
    monkeypatch.setattr(price_check, "live_anchors", lambda: [])
    assert price_check.strip_for(_lot(location_labels=[]))["where"] is None


# ── live_anchors degrade + memo ──────────────────────────────────────────────

def test_db_failure_falls_back_to_statics(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(db, "fetch_one", boom)
    assert price_check.live_anchors() == []
    strip = price_check.strip_for(_lot())
    assert [a["source"] for a in strip["anchors"]] == [
        "todaysclassroom.com", "webstaurantstore.com"]


def test_missing_table_means_empty_live_set(monkeypatch):
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)   # probe: no table
    monkeypatch.setattr(db, "fetch_all",
                        lambda *a, **k: pytest.fail("must not query a missing table"))
    assert price_check.live_anchors() == []


def test_table_probe_caches_only_a_positive_answer(monkeypatch):
    calls = {"probe": 0}

    def probe(*a, **k):
        calls["probe"] += 1
        return {"?column?": 1}
    monkeypatch.setattr(db, "fetch_one", probe)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
    t = {"v": 0.0}
    price_check.live_anchors(now=lambda: t["v"])
    t["v"] += price_check.TTL_SECONDS + 1
    price_check.live_anchors(now=lambda: t["v"])
    assert calls["probe"] == 1               # ready flag cached after the first yes


def test_rows_are_memoised_for_the_ttl(monkeypatch):
    calls = {"n": 0}

    def fetch_all(*a, **k):
        calls["n"] += 1
        return [_live_row()]
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: {"?column?": 1})
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    t = {"v": 100.0}
    now = lambda: t["v"]  # noqa: E731
    assert price_check.live_anchors(now=now)[0]["brand"] == "MTS Seating"
    t["v"] += 10
    price_check.live_anchors(now=now)
    assert calls["n"] == 1
    t["v"] += price_check.TTL_SECONDS
    price_check.live_anchors(now=now)
    assert calls["n"] == 2


def test_a_failure_is_not_memoised(monkeypatch):
    state = {"fail": True}

    def fetch_one(*a, **k):
        if state["fail"]:
            raise RuntimeError("blip")
        return {"?column?": 1}
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [_live_row()])
    assert price_check.live_anchors() == []
    state["fail"] = False
    assert price_check.live_anchors() != []   # recovers without waiting out a TTL

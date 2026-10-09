"""Offline tests for the Municibid adapter, against fixtures captured live
2026-10-09 from the redesigned (Next.js) municibid.com (see
recorder/sources/municibid.py's module docstring for the verified pages/
markup). No network calls — polite_get is monkeypatched.
"""
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recorder.models import Observation
from recorder.sources import municibid as mb
from recorder.sources.base import SourceFetchFailed

FIXTURES = Path(__file__).parent / "fixtures" / "municibid"

CHAIRS, DESKS_CHAIRS, FURNITURE, OFFICE = "11097697", "29720497", "160885", "169128"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class _FakeResponse:
    def __init__(self, text, status_code=200, url="https://municibid.com/fake"):
        self.text = text
        self.status_code = status_code
        self.url = url


@pytest.fixture
def chairs_html():
    return _load("browse_chairs_active.html")


@pytest.fixture
def desks_chairs_html():
    return _load("browse_desks_chairs_active.html")


@pytest.fixture
def furniture_pages():
    return [_load(f"browse_furniture_active_page{i}.html") for i in range(3)]


@pytest.fixture
def office_html():
    return _load("browse_office_supplies_active.html")


@pytest.fixture
def chairs_completed_html():
    return _load("browse_chairs_completed_page0.html")


@pytest.fixture
def empty_page_html():
    return _load("browse_empty_page.html")


@pytest.fixture
def detail_active_html():
    return _load("detail_active_86233903.html")


@pytest.fixture
def detail_closed_html():
    return _load("detail_closed_successful_82394410.html")


@pytest.fixture
def detail_unsold_html():
    return _load("detail_closed_unsuccessful_86003942.html")


@pytest.fixture
def detail_not_found_html():
    return _load("detail_not_found.html")


def _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html,
               completed_html=None):
    """A fake municibid routing the real 2026-10-09 captures by category/page."""
    calls: list[dict] = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        assert url == mb.BROWSE_URL
        calls.append(dict(params))
        page = int(params.get("page", 0))
        if params.get("status") == "completed":
            return _FakeResponse(completed_html if (completed_html and page == 0) else empty_page_html)
        cat = params["category"]
        if cat == CHAIRS:
            return _FakeResponse(chairs_html if page == 0 else empty_page_html)
        if cat == DESKS_CHAIRS:
            return _FakeResponse(desks_chairs_html if page == 0 else empty_page_html)
        if cat == FURNITURE:
            return _FakeResponse(furniture_pages[page] if page < 3 else empty_page_html)
        if cat == OFFICE:
            return _FakeResponse(_load("browse_office_supplies_active.html") if page == 0 else empty_page_html)
        raise AssertionError(f"unexpected category {cat}")

    monkeypatch.setattr(mb, "polite_get", fake_get)
    return calls


# --- discover() --------------------------------------------------------

def test_discover_parses_n_geq_5_observations(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    obs = mb.MunicibidSource().discover()
    assert len(obs) >= 5
    assert all(isinstance(o, Observation) for o in obs)


def test_discover_observations_have_required_shape(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    obs = mb.MunicibidSource().discover()
    for o in obs:
        assert o.source == "municibid"
        assert o.source_lot_id
        assert o.status == "active"
        assert isinstance(o.current_bid, Decimal)
        assert isinstance(o.bid_count, int)
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None


def test_discover_raw_roundtrips_source_listing_unmodified(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    obs = mb.MunicibidSource().discover()
    by_id = {str(c["listing"]["id"]): c["listing"] for c in mb._parse_browse_cards(chairs_html)}
    for o in obs:
        if o.source_lot_id in by_id:
            assert o.raw == by_id[o.source_lot_id]
    assert set(by_id) <= {o.source_lot_id for o in obs}
    # the card's own "listing" dict, verbatim — including fields we don't use
    o = next(o for o in obs if o.source_lot_id == "86226233")
    assert o.raw["primaryImageUrl"].startswith("https://")
    assert o.raw["sold"] is None


def test_discover_values_match_live_capture(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    by_id = {o.source_lot_id: o for o in mb.MunicibidSource().discover()}
    o = by_id["86226233"]  # "Blue Classroom Chairs", verified live 2026-10-09
    assert o.current_bid == Decimal("10")
    assert o.bid_count == 1
    assert o.end_date == datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def test_discover_sweeps_every_category_and_paginates_furniture(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    calls = _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    mb.MunicibidSource().discover()
    assert {c["category"] for c in calls} == {CHAIRS, DESKS_CHAIRS, FURNITURE, OFFICE}
    assert all("status" not in c for c in calls)  # active tab = no status param
    furniture_pages_fetched = sorted(c.get("page", 0) for c in calls if c["category"] == FURNITURE)
    assert furniture_pages_fetched == [0, 1, 2]  # 49 lots / 24 per page — the 1-card page 2 ends it
    assert all("page" not in c for c in calls if c["category"] in (CHAIRS, DESKS_CHAIRS, OFFICE))  # 5, 2 and 7 cards: one page each


def test_discover_keeps_all_seating_subcategory_lots_and_term_filters_furniture(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    ids = {o.source_lot_id for o in mb.MunicibidSource().discover()}
    assert {"86025161", "86226233", "86226711", "86233903", "86330735"} <= ids  # the 5 Chairs cards
    assert "86351268" in ids  # "Adjustable height school desks - lot of 300" — Desks and Chairs subcategory, kept as-is
    assert "86115761" not in ids  # "Grey Filing Cabinet (1)" — Furniture, no seating term
    assert "86338777" not in ids  # "Large Reception Desk" — Furniture page 2, no seating term
    assert "86025268" in ids  # "84x48 light oak hardwood table & chairs" — Furniture/Tables, matched on "chairs"
    assert "86045046" in ids  # "Office chairs" — Office Supplies category, matched on "chairs"
    assert "85959922" not in ids  # "Xerox Toner and Ink Cartridges" — Office Supplies, no seating term


def test_discover_dedupes_across_categories(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html)
    obs = mb.MunicibidSource().discover()
    ids = [o.source_lot_id for o in obs]
    assert len(ids) == len(set(ids))
    # every Chairs-subcategory lot also sits in the Furniture top category — stored once
    chairs_ids = {str(c["listing"]["id"]) for c in mb._parse_browse_cards(chairs_html)}
    furniture_ids = {str(c["listing"]["id"]) for p in furniture_pages for c in mb._parse_browse_cards(p)}
    assert chairs_ids & furniture_ids
    assert len(obs) == len(set(ids))


def test_discover_partial_category_failure_still_returns_healthy_categories(monkeypatch, chairs_html, empty_page_html, capsys):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if params["category"] == CHAIRS:
            return _FakeResponse(chairs_html)
        return _FakeResponse("", status_code=403)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().discover()
    assert len(obs) == 5
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_raises_fetch_failed_when_all_categories_fail(monkeypatch, capsys):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    with pytest.raises(SourceFetchFailed):
        mb.MunicibidSource().discover()
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_warns_loudly_on_healthy_but_empty_result(monkeypatch, empty_page_html, capsys):
    # fetched fine, matched nothing: a clean [] (breaker success), never SourceFetchFailed
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(empty_page_html))
    assert mb.MunicibidSource().discover() == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 active furniture listings" in out


def test_discover_raises_fetch_failed_on_unrecognized_page_shape(monkeypatch, capsys):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("<html>no flight payload here</html>"))
    with pytest.raises(SourceFetchFailed):
        mb.MunicibidSource().discover()
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_raises_fetch_failed_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise mb.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mb, "polite_get", raise_connection_error)
    with pytest.raises(SourceFetchFailed):
        mb.MunicibidSource().discover()
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


# --- pagination (_fetch_category) --------------------------------------------------------

def test_fetch_category_stops_on_short_page_without_an_extra_request(monkeypatch, furniture_pages, empty_page_html):
    calls = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls.append(params.get("page", 0))
        page = params.get("page", 0)
        return _FakeResponse(furniture_pages[page] if page < 3 else empty_page_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    cards = mb._fetch_category(FURNITURE, completed=False)
    assert len(cards) == 49
    assert calls == [0, 1, 2]  # page 2 has 1 card (< 24) — no page-3 probe needed


def test_fetch_category_page_zero_omits_page_param(monkeypatch, chairs_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["params"] = params
        return _FakeResponse(chairs_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    mb._fetch_category(CHAIRS, completed=False)
    assert "page" not in captured["params"]
    assert captured["params"] == {"category": CHAIRS}


def test_fetch_category_hits_cap_and_warns(monkeypatch, furniture_pages, capsys):
    # a source that keeps re-serving a full 24-card page forever must stop at
    # max_pages, not loop forever, and must say so loudly.
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(furniture_pages[0]))
    cards = mb._fetch_category(FURNITURE, completed=False, max_pages=3)
    assert len(cards) == 72
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "cap" in out.lower()


def test_fetch_category_returns_none_when_page_zero_fails(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    assert mb._fetch_category(FURNITURE, completed=False) is None


def test_fetch_category_keeps_partial_result_when_a_later_page_fails(monkeypatch, furniture_pages, capsys):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        page = params.get("page", 0)
        if page == 1:
            return _FakeResponse("", status_code=403)
        return _FakeResponse(furniture_pages[page])

    monkeypatch.setattr(mb, "polite_get", fake_get)
    cards = mb._fetch_category(FURNITURE, completed=False)
    assert len(cards) == 24  # page 0 preserved despite page 1 failing
    out = capsys.readouterr().out
    assert "WARNING" in out


def test_fetch_category_warns_when_header_count_disagrees_with_cards(monkeypatch, furniture_pages, capsys):
    # header says 49, but page 1 comes back short (result set shifted) -> 24+? cards, loud warning
    short_page1 = furniture_pages[2]  # the real 1-card page, served as page 1

    def fake_get(url, *, headers=None, params=None, timeout=30):
        page = params.get("page", 0)
        return _FakeResponse(furniture_pages[0] if page == 0 else short_page1)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    cards = mb._fetch_category(FURNITURE, completed=False)
    assert len(cards) == 25
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "49" in out


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_returns_closed_observations_within_lookback(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html, chairs_completed_html):
    calls = _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html, completed_html=chairs_completed_html)
    # the fixture's newest close is 2026-10-08T17:06Z; pin "now" so the 7-day window covers part of the page
    cards = mb._parse_browse_cards(chairs_completed_html)
    ends = [mb._parse_utc(c["listing"]["endsAtUtc"]) for c in cards]
    assert ends == sorted(ends, reverse=True)  # newest close first — the pagination stop rule relies on it
    cutoff = ends[10]
    monkeypatch.setattr(mb, "SOLD_SWEEP_LOOKBACK_DAYS", 0)

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff

    monkeypatch.setattr(mb, "datetime", _FixedNow)
    obs = mb.MunicibidSource().sold_sweep()
    assert all(o.status == "closed" for o in obs)
    assert all(isinstance(o.current_bid, Decimal) for o in obs)
    assert all(o.end_date >= cutoff for o in obs)
    assert len(obs) == 11  # ends[0..10] inclusive — the 13 older cards on the page were dropped
    assert all(c["status"] == "completed" for c in calls)
    assert {c["category"] for c in calls} == {CHAIRS, DESKS_CHAIRS}  # Furniture top is discover-only
    assert all("page" not in c for c in calls)  # window closed inside page 0 — no page-1 request


def test_sold_sweep_raw_carries_sold_flag_and_final_price(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html, chairs_completed_html):
    _live_site(monkeypatch, chairs_html, desks_chairs_html, furniture_pages, empty_page_html, completed_html=chairs_completed_html)
    monkeypatch.setattr(mb, "SOLD_SWEEP_LOOKBACK_DAYS", 36500)
    by_id = {o.source_lot_id: o for o in mb.MunicibidSource().sold_sweep()}
    o = by_id["86003942"]  # "Meeting Room Chairs" — ended 2026-10-08, reserve not met
    assert o.raw["sold"] is False
    assert o.bid_count == 0
    assert o.current_bid == Decimal("25")
    o = by_id["85978629"]  # "Mobile ergonomic ball chair" — sold
    assert o.raw["sold"] is True
    assert o.bid_count == 6
    assert o.current_bid == Decimal("8")
    assert o.end_date == datetime(2026, 10, 7, 20, 17, tzinfo=timezone.utc)


def test_sold_sweep_paginates_while_window_is_open(monkeypatch, chairs_completed_html, empty_page_html):
    calls = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls.append(params.get("page", 0))
        return _FakeResponse(chairs_completed_html if params.get("page", 0) == 0 else empty_page_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    monkeypatch.setattr(mb, "SOLD_SWEEP_LOOKBACK_DAYS", 36500)
    obs = mb.MunicibidSource().sold_sweep()
    assert len(obs) == 24
    assert calls == [0, 1, 0, 1]  # both seating categories: full page 0, then the empty page 1 ends it


def test_sold_sweep_returns_empty_when_all_categories_fail(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    assert mb.MunicibidSource().sold_sweep() == []


# --- poll() --------------------------------------------------------

def test_poll_active_lot_reparses_status_price_bids_enddate(monkeypatch, detail_active_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["url"] = url
        return _FakeResponse(detail_active_html, url="https://municibid.com/listing/86233903/x")

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().poll([{"source_lot_id": "86233903"}])
    assert captured["url"] == "https://municibid.com/listing/86233903/x"
    assert len(obs) == 1
    o = obs[0]
    assert o.source_lot_id == "86233903"
    assert o.status == "active"
    assert o.current_bid == Decimal("195")
    assert o.bid_count == 1
    assert o.end_date == datetime(2026, 10, 20, 12, 29, tzinfo=timezone.utc)
    # raw carries the untouched source payloads, so a future parser fix can
    # recompute current_bid/bid_count/end_date/status from raw alone.
    d = o.raw["detail_page"]
    assert d["url"] == "https://municibid.com/listing/86233903/x"
    assert d["listing_id"] == 86233903
    assert d["initial"]["status"] == "Active"
    assert d["initial"]["currentPrice"] == 195
    assert d["initial"]["bidCount"] == 1
    assert d["initial"]["endsAtUtc"] == "2026-10-20T12:29:00"
    assert d["initial"]["serverNowUtc"].startswith("2026-10-09T")
    assert d["summary"] == {"currentPrice": 195, "bidCount": 1, "ended": False}


def test_poll_closed_sold_lot_reparses_as_closed_with_final_bid(monkeypatch, detail_closed_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_closed_html))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "82394410"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "closed"
    assert o.current_bid == Decimal("21")  # same lot + same final as the 2026-07-31 fixture
    assert o.bid_count == 8
    assert o.end_date == datetime(2026, 5, 4, 12, 6, tzinfo=timezone.utc)
    assert o.raw["detail_page"]["initial"]["status"] == "Successful"
    assert o.raw["detail_page"]["summary"]["ended"] is True


def test_poll_closed_unsold_lot_is_closed_with_zero_bids(monkeypatch, detail_unsold_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_unsold_html))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "86003942"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "closed"
    assert o.bid_count == 0
    assert o.current_bid == Decimal("25")  # the unmet ask — sold_comps' bid_count > 0 gate drops it
    assert o.end_date == datetime(2026, 10, 8, 17, 6, tzinfo=timezone.utc)
    assert o.raw["detail_page"]["initial"]["status"] == "Unsuccessful"
    assert o.raw["detail_page"]["initial"]["reserveMet"] is False


def test_poll_never_hardcodes_active_status(monkeypatch, detail_closed_html):
    # status must be re-derived from the page, not assumed, even though the lot was "found".
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_closed_html))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "82394410"}])
    assert obs[0].status != "active"
    assert obs[0].status == "closed"


def test_poll_not_found_after_end_date_emits_gone(monkeypatch, detail_not_found_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_not_found_html))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = mb.MunicibidSource().poll([{"source_lot_id": "999999999", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 200  # the Next.js 404 boundary renders inside a 200
    assert obs[0].raw["recorder_probe"]["url"] == mb.DETAIL_URL_TMPL.format(id="999999999")


def test_poll_not_found_before_end_date_emits_nothing(monkeypatch, detail_not_found_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_not_found_html))
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    obs = mb.MunicibidSource().poll([{"source_lot_id": "999999999", "end_date": future_end}])
    assert obs == []


def test_poll_not_found_with_unknown_end_date_emits_nothing(monkeypatch, detail_not_found_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_not_found_html))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "999999999", "end_date": None}])
    assert obs == []


def test_poll_404_also_treated_as_not_found(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=404))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = mb.MunicibidSource().poll([{"source_lot_id": "1", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["http_status"] == 404


def test_poll_fetch_failure_for_one_lot_does_not_block_others(monkeypatch, detail_active_html, capsys):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if "bad-lot" in url:
            return _FakeResponse("", status_code=403)
        return _FakeResponse(detail_active_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().poll([
        {"source_lot_id": "bad-lot", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)},
        {"source_lot_id": "86233903"},
    ])
    assert len(obs) == 1
    assert obs[0].source_lot_id == "86233903"
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "bad-lot" in out


def test_poll_connection_exception_skips_lot_without_raising(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise mb.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mb, "polite_get", raise_connection_error)
    obs = mb.MunicibidSource().poll([{"source_lot_id": "1", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_get(*a, **k):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(mb, "polite_get", fail_get)
    assert mb.MunicibidSource().poll([]) == []


def test_poll_unrecognized_page_shape_is_a_fetch_failure_not_a_gone(monkeypatch, capsys):
    # a 200 with neither a bidbox nor the 404 marker (e.g. an interstitial)
    # must never become a 'gone' row — append-only, that mistake is permanent.
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("<title>Something Else</title>"))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "1", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "unrecognized page shape" in out


def test_poll_batch_aborts_after_consecutive_failures(monkeypatch, capsys):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    lots = [{"source_lot_id": str(i)} for i in range(15)]
    src = mb.MunicibidSource()
    assert src.poll(lots) == []
    assert src.last_poll_stats["aborted"] is True
    assert src.last_poll_stats["attempted"] == 10
    assert src.last_poll_stats["skipped"] == 5


# --- parsing helpers --------------------------------------------------------

def test_parse_money_handles_numbers_and_strings():
    assert mb._parse_money(195) == Decimal("195")
    assert mb._parse_money(24755.5) == Decimal("24755.5")
    assert mb._parse_money("1,200.00") == Decimal("1200.00")
    assert mb._parse_money(None) is None


def test_parse_utc_is_already_utc_no_conversion():
    # verified live 2026-10-09: "2026-10-20T12:29:00" renders as "8:29 AM ET" (EDT, UTC-4)
    assert mb._parse_utc("2026-10-20T12:29:00") == datetime(2026, 10, 20, 12, 29, tzinfo=timezone.utc)


def test_parse_utc_accepts_fractional_seconds():
    assert mb._parse_utc("2027-02-16T20:10:06.83") == datetime(2027, 2, 16, 20, 10, 6, 830000, tzinfo=timezone.utc)


def test_parse_utc_handles_missing_and_garbage():
    assert mb._parse_utc(None) is None
    assert mb._parse_utc("") is None
    assert mb._parse_utc("10/20/2026 8:29:00 AM") is None


def test_parse_header_count_reads_active_and_completed_headers(chairs_html, chairs_completed_html, empty_page_html, office_html):
    assert mb._parse_header_count(chairs_html) == 5
    assert mb._parse_header_count(office_html) == 7
    assert mb._parse_header_count(chairs_completed_html) == 3041
    assert mb._parse_header_count(empty_page_html) == 5  # page 5 of the 5-lot Chairs category


def test_parse_browse_cards_reads_24_cards_in_page_order(furniture_pages):
    cards = mb._parse_browse_cards(furniture_pages[0])
    assert len(cards) == 24
    assert cards[0]["completed"] is False
    assert all(set(c["listing"]) >= {"id", "title", "currentPrice", "bidCount", "endsAtUtc"} for c in cards)
    assert len(mb._parse_browse_cards(furniture_pages[2])) == 1


def test_empty_page_is_recognised(empty_page_html, chairs_html):
    assert mb._is_empty_page(empty_page_html)
    assert not mb._is_empty_page(chairs_html)
    assert mb._parse_browse_cards(empty_page_html) == []


def test_not_found_page_is_recognised(detail_not_found_html, detail_active_html):
    assert mb._is_not_found_page(detail_not_found_html)
    assert not mb._is_not_found_page(detail_active_html)


def test_not_found_template_on_a_live_page_is_not_a_not_found(detail_active_html):
    # every page's flight payload pre-renders the {"code":"404",...} not-found
    # template (verified live) — only the error digest line means not found.
    # Matching the template would turn every live lot into a false 'gone'.
    assert '"code":"404"' in mb._rsc_text(detail_active_html)
    assert not mb._is_not_found_page(detail_active_html)


@pytest.mark.parametrize("title,expected", [
    ("Blue Classroom Chairs", True),
    ("Mobile ergonomic ball chair", True),          # singular
    ("84x48 light oak hardwood table & chairs", True),
    ("Stackable Chairs lot of 40", True),
    ("Banquet tables", True),                        # "banquet"
    ("Office furniture - assorted", True),           # both words of "office furniture"
    ("Office desk", False),                          # "office" alone is not a term
    ("Grey Filing Cabinet (1)", False),
    ("Large Reception Desk", False),
    ("", False),
    (None, False),
])
def test_matches_furniture_terms(title, expected):
    assert mb._matches_furniture_terms(title) is expected

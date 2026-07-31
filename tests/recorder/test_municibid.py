"""Offline tests for the Municibid adapter, against fixtures captured live
2026-07-31 (see recorder/sources/municibid.py's module docstring for the
verified endpoints/markup). No network calls — polite_get is monkeypatched.
"""
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recorder.models import Observation
from recorder.sources import municibid as mb
from recorder.sources.base import FURNITURE_TERMS

FIXTURES = Path(__file__).parent / "fixtures" / "municibid"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class _FakeResponse:
    def __init__(self, text, status_code=200, url="https://municibid.com/fake"):
        self.text = text
        self.status_code = status_code
        self.url = url


@pytest.fixture
def discover_html():
    return _load("discover_active_chairs.html")


@pytest.fixture
def sold_html():
    return _load("sold_sweep_chairs.html")


@pytest.fixture
def sold_html_page1():
    return _load("sold_sweep_chairs_page1.html")


@pytest.fixture
def detail_active_html():
    return _load("detail_active_84516049.html")


@pytest.fixture
def detail_closed_html():
    return _load("detail_closed_82394410.html")


@pytest.fixture
def detail_not_found_html():
    return _load("detail_not_found.html")


# --- discover() --------------------------------------------------------

def test_discover_parses_n_geq_5_observations(monkeypatch, discover_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(discover_html))
    obs = mb.MunicibidSource().discover()
    assert len(obs) >= 5
    assert all(isinstance(o, Observation) for o in obs)


def test_discover_observations_have_required_shape(monkeypatch, discover_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(discover_html))
    obs = mb.MunicibidSource().discover()
    for o in obs:
        assert o.source == "municibid"
        assert o.source_lot_id
        assert o.status == "active"
        assert o.current_bid is None or isinstance(o.current_bid, Decimal)
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None


def test_discover_raw_roundtrips_source_item_unmodified(monkeypatch, discover_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(discover_html))
    obs = mb.MunicibidSource().discover()
    m = mb.MARKERS_RE.search(discover_html)
    items = json.loads(m.group(1))
    by_id = {str(it["id"]): it for it in items}
    for o in obs:
        assert o.raw == by_id[o.source_lot_id]


def test_discover_bid_counts_attached_from_cards(monkeypatch, discover_html):
    # fixture is a 34-item/34-card page (fits on page 0) — full bid_count coverage
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(discover_html))
    obs = mb.MunicibidSource().discover()
    assert obs
    assert all(o.bid_count is not None for o in obs)
    by_id = {o.source_lot_id: o for o in obs}
    assert by_id["84516049"].bid_count == 1  # "Banquet Chairs", verified live 2026-07-31


def test_discover_dedupes_across_furniture_terms(monkeypatch, discover_html):
    calls = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls.append(params)
        return _FakeResponse(discover_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().discover()
    assert len(calls) == len(FURNITURE_TERMS)
    ids = [o.source_lot_id for o in obs]
    assert len(ids) == len(set(ids))
    assert len(obs) == 34  # the fixture's real item count, not 6x


def test_discover_request_uses_active_only_status_filter(monkeypatch, discover_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured.setdefault("params", []).append(params)
        return _FakeResponse(discover_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    mb.MunicibidSource().discover()
    assert all(p["StatusFilter"] == "active_only" for p in captured["params"])
    assert {p["FullTextQuery"] for p in captured["params"]} == set(FURNITURE_TERMS)


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_returns_closed_observations(monkeypatch, sold_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(sold_html))
    obs = mb.MunicibidSource().sold_sweep()
    assert len(obs) == 70  # fixture's real completed-set size, deduped across 6 identical term calls
    assert all(o.status == "closed" for o in obs)
    assert all(isinstance(o.current_bid, Decimal) for o in obs)


def test_sold_sweep_achieves_full_bid_count_coverage_via_pagination(monkeypatch, sold_html, sold_html_page1):
    # fix round 1: 70 JSON items, only 40 cards on page 0 — but page 1 (real
    # capture) has the remaining 30, and _fetch_search_full now paginates to
    # get them, closing the gap that used to leave 30/70 with bid_count=None.
    def fake_get(url, *, headers=None, params=None, timeout=30):
        page = (params or {}).get("page", 0)
        return _FakeResponse(sold_html if not page else sold_html_page1)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().sold_sweep()
    assert len(obs) == 70
    assert all(o.bid_count is not None for o in obs)


def test_fetch_search_full_paginates_to_full_coverage_on_real_two_page_set(monkeypatch, sold_html, sold_html_page1):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        page = (params or {}).get("page", 0)
        return _FakeResponse(sold_html if not page else sold_html_page1)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    items, bid_counts = mb._fetch_search_full("chairs", "completed_only")
    ids = {it["id"] for it in items}
    assert len(ids) == 70
    assert set(bid_counts.keys()) == ids
    # converged after page 0 + page 1 — not the 10-page cap
    assert len(bid_counts) == 70


def test_fetch_search_full_stops_at_a_naturally_empty_page_but_warns_if_still_incomplete(monkeypatch, sold_html, capsys):
    # requesting a page past the last one returns HTTP 200 with an empty
    # card grid (verified live 2026-07-31, see module docstring) — pagination
    # must stop there rather than grinding to the max_pages cap. But (fix
    # round 1, live evidence in the report) a naturally-empty page doesn't
    # mean the id set from page 0 got fully covered — Municibid's live
    # result set can shift between throttled fetches — so this must still
    # warn loudly rather than silently swallowing the gap.
    calls = {"n": 0}
    empty_cards_html = (
        mb.MARKERS_RE.search(sold_html).group(0)
        + '<div class="srp-body"><div class="srp-split"><div class="srp-cards"></div></div></div>'
    )

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls["n"] += 1
        page = (params or {}).get("page", 0)
        return _FakeResponse(sold_html if not page else empty_cards_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    items, bid_counts = mb._fetch_search_full("chairs", "completed_only", max_pages=10)
    assert len(bid_counts) == 40  # only page 0's cards — page 1 was empty, so it stopped there
    assert calls["n"] == 2  # page 0 + one empty page 1, never reached the cap
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "30" in out  # 70 - 40 still missing bid_count


def test_fetch_search_full_hits_cap_and_warns_when_coverage_never_completes(monkeypatch, sold_html, capsys):
    # a source that keeps re-serving the same 40 cards forever (never empty,
    # never covers the remaining 30 ids) must stop at max_pages, not loop
    # forever, and must say so loudly.
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(sold_html))
    items, bid_counts = mb._fetch_search_full("chairs", "completed_only", max_pages=3)
    assert len(items) == 70
    assert len(bid_counts) == 40  # never grew past page-0's ids
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "cap" in out.lower()
    assert "30 closed lot(s)" in out or "30" in out


def test_fetch_search_full_returns_none_when_page_zero_fails(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    assert mb._fetch_search_full("chairs", "completed_only") is None


def test_fetch_search_full_keeps_partial_bid_counts_when_a_later_page_fails(monkeypatch, sold_html, capsys):
    calls = {"n": 0}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls["n"] += 1
        page = (params or {}).get("page", 0)
        if page == 1:
            return _FakeResponse("", status_code=403)
        return _FakeResponse(sold_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    items, bid_counts = mb._fetch_search_full("chairs", "completed_only")
    assert len(items) == 70
    assert len(bid_counts) == 40  # page 0's coverage preserved despite page 1 failing
    out = capsys.readouterr().out
    assert "WARNING" in out


def test_fetch_search_page_omits_page_param_for_page_zero(monkeypatch, discover_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["params"] = params
        return _FakeResponse(discover_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    mb._fetch_search_page("chairs", "active_only", page=0)
    assert "page" not in captured["params"]


def test_fetch_search_page_includes_page_param_when_nonzero(monkeypatch, discover_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["params"] = params
        return _FakeResponse(discover_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    mb._fetch_search_page("chairs", "active_only", page=2)
    assert captured["params"]["page"] == 2


def test_sold_sweep_request_uses_completed_only_status_filter(monkeypatch, sold_html):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured.setdefault("params", []).append(params)
        return _FakeResponse(sold_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    mb.MunicibidSource().sold_sweep()
    assert all(p["StatusFilter"] == "completed_only" for p in captured["params"])


def test_sold_sweep_returns_empty_when_all_terms_fail(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    assert mb.MunicibidSource().sold_sweep() == []


# --- poll() --------------------------------------------------------

def test_poll_active_lot_reparses_status_price_bids_enddate(monkeypatch, detail_active_html):
    monkeypatch.setattr(
        mb, "polite_get",
        lambda *a, **k: _FakeResponse(detail_active_html, url="https://municibid.com/Listing/Details/84516049/banquet-chairs"),
    )
    obs = mb.MunicibidSource().poll([{"source_lot_id": "84516049"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.source_lot_id == "84516049"
    assert o.status == "active"
    assert o.current_bid == Decimal("25.00")
    assert o.bid_count == 1
    assert o.end_date == datetime(2026, 8, 4, 12, 27, tzinfo=timezone.utc)
    # fix round 1: raw must carry the literal matched source substrings, not
    # just the already-derived values, so a future parser fix can recompute
    # current_bid/bid_count/end_date from raw alone without re-scraping.
    assert o.raw["detail_page"]["status_marker"] == "This Auction Ends in:"
    assert o.raw["detail_page"]["current_bid_raw"] == "25.00"
    assert o.raw["detail_page"]["bid_count_raw"] == "1"
    assert o.raw["detail_page"]["end_date_raw"] == "08/04/2026 08:27:00"
    assert o.raw["detail_page"]["url"] == "https://municibid.com/Listing/Details/84516049/banquet-chairs"


def test_poll_closed_lot_reparses_as_closed_with_final_bid(monkeypatch, detail_closed_html):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(detail_closed_html))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "82394410"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "closed"
    assert o.current_bid == Decimal("21.00")
    assert o.bid_count == 8
    assert o.end_date == datetime(2026, 5, 4, 12, 6, tzinfo=timezone.utc)
    assert o.raw["detail_page"]["status_marker"] == "Auction Ended:"
    assert o.raw["detail_page"]["current_bid_raw"] == "21.00"
    assert o.raw["detail_page"]["bid_count_raw"] == "8"
    assert o.raw["detail_page"]["end_date_raw"] == "05/04/2026 08:06:00"


def test_poll_never_hardcodes_active_status(monkeypatch, detail_closed_html):
    # regression guard for the GSA/PW "fix round 1" ruling — status must be
    # re-derived from the page, not assumed, even though the lot was "found".
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
    assert obs[0].raw["recorder_probe"]["http_status"] == 200
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
        {"source_lot_id": "84516049"},
    ])
    assert len(obs) == 1
    assert obs[0].source_lot_id == "84516049"
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


def test_poll_unrecognized_page_shape_is_a_fetch_failure(monkeypatch, capsys):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("<title>Something Else</title>"))
    obs = mb.MunicibidSource().poll([{"source_lot_id": "1", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


# --- HTTP / fetch-failure handling --------------------------------------------------------

def test_discover_returns_empty_when_all_terms_fail(monkeypatch, capsys):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    assert mb.MunicibidSource().discover() == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_partial_term_failure_still_returns_healthy_terms(monkeypatch, discover_html, capsys):
    calls = {"n": 0}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse("", status_code=403)
        return _FakeResponse(discover_html)

    monkeypatch.setattr(mb, "polite_get", fake_get)
    obs = mb.MunicibidSource().discover()
    assert len(obs) == 34  # remaining 5 terms all returned the same fixture, deduped
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_warns_loudly_on_healthy_but_empty_result(monkeypatch, capsys):
    empty_html = (
        '<script type="application/json" id="srp-markers-data">[]</script>'
        '<div class="srp-body"><div class="srp-split"></div></div>'
    )
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse(empty_html))
    assert mb.MunicibidSource().discover() == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 active furniture listings" in out


def test_discover_returns_empty_on_missing_markers_script(monkeypatch):
    monkeypatch.setattr(mb, "polite_get", lambda *a, **k: _FakeResponse("<html>no marker here</html>"))
    assert mb.MunicibidSource().discover() == []


def test_discover_returns_empty_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise mb.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mb, "polite_get", raise_connection_error)
    assert mb.MunicibidSource().discover() == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


# --- parsing helpers --------------------------------------------------------

def test_parse_money_strips_comma_thousands_separator():
    assert mb._parse_money("1,200.00") == Decimal("1200.00")
    assert mb._parse_money(25.0) == Decimal("25.0")
    assert mb._parse_money(None) is None


def test_parse_search_end_is_already_utc_no_conversion():
    # verified live 2026-07-31: this exact string, for this exact listing,
    # matched the detail page's ET data-action-time converted to UTC.
    dt = mb._parse_search_end("2026-08-04T12:27:00")
    assert dt == datetime(2026, 8, 4, 12, 27, 0, tzinfo=timezone.utc)


def test_parse_detail_end_converts_et_to_utc_edt():
    # verified live 2026-07-31 (EDT, UTC-4): "08/04/2026 08:27:00" ET ==
    # "2026-08-04T12:27:00" UTC (matches test above's search-JSON value).
    dt = mb._parse_detail_end("08/04/2026 08:27:00")
    assert dt == datetime(2026, 8, 4, 12, 27, 0, tzinfo=timezone.utc)


def test_parse_detail_end_handles_est_season_too():
    # winter (EST, UTC-5) — proves zoneinfo DST handling, not a hardcoded offset.
    dt = mb._parse_detail_end("01/15/2026 09:00:00")
    assert dt == datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_detail_end_handles_missing():
    assert mb._parse_detail_end(None) is None
    assert mb._parse_detail_end("") is None

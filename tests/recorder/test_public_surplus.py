"""Offline tests for the Public Surplus recorder adapter, against fixtures
captured live 2026-07-31 (see recorder/sources/public_surplus.py's module
docstring for the verified endpoints/markup, and the 401-login-wall finding
that supersedes the brief's "closed page for a while" assumption). No
network calls — polite_get is monkeypatched.
"""
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from recorder.models import Observation
from recorder.sources import public_surplus as ps
from recorder.sources.base import FURNITURE_TERMS

FIXTURES = Path(__file__).parent / "fixtures" / "public_surplus"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class _FakeResponse:
    def __init__(self, text, status_code=200, url="https://www.publicsurplus.com/fake"):
        self.text = text
        self.status_code = status_code
        self.url = url


@pytest.fixture
def search_html():
    return _load("search_chairs_page0.html")


@pytest.fixture
def detail_no_bids_html():
    return _load("detail_active_no_bids_4054005.html")


@pytest.fixture
def detail_with_bids_html():
    return _load("detail_active_with_bids_4053470.html")


@pytest.fixture
def login_wall_html():
    return _load("detail_login_wall_401.html")


# --- _parse_search_cards --------------------------------------------------------

def test_parse_search_cards_finds_real_cards(search_html):
    cards = ps._parse_search_cards(search_html, "https://www.publicsurplus.com/fake")
    assert len(cards) >= 5
    ids = {c["auc_id"] for c in cards}
    assert "4054005" in ids
    assert "4053470" in ids


def test_parse_search_cards_fields(search_html):
    cards = ps._parse_search_cards(search_html, "https://www.publicsurplus.com/fake")
    by_id = {c["auc_id"]: c for c in cards}
    c = by_id["4053470"]
    assert c["price_raw"] == "$10.50"
    assert c["end_epoch_ms_raw"] == "1785524400000"
    assert c["title"]
    assert c["link"] == "https://www.publicsurplus.com/sms/auction/view?auc=4053470"


def test_parse_search_cards_healthy_page_is_silent(search_html, capsys):
    ps._parse_search_cards(search_html, "https://www.publicsurplus.com/fake")
    out = capsys.readouterr().out
    assert out == ""  # real fixture has no drops/misses — must print nothing


# --- _parse_search_cards drop/miss aggregation (fix round 1, review finding #3) ---

_HEALTHY_TWO_CARD_HTML = """
<div class="auction-item" id="111searchGrid">
<a href="/sms/auction/view?auc=111" title="Test Lot 111">Test Lot 111</a>
<span id="val_111searchGrid">$5.00</span>
<script>updateTimeLeftSpan(timeLeftInfoMap, 111, "111searchGrid", 1000, 2000, 0, "", "", "searchList", timeLeftCallback);</script>
</div>
<div class="auction-item" id="222searchGrid">
<a href="/sms/auction/view?auc=222" title="Test Lot 222">Test Lot 222</a>
<span id="val_222searchGrid">$6.00</span>
<script>updateTimeLeftSpan(timeLeftInfoMap, 222, "222searchGrid", 1000, 3000, 0, "", "", "searchList", timeLeftCallback);</script>
</div>
"""

_DROPPED_AND_MISSING_FIELD_HTML = """
<div class="auction-item" id="333searchGrid">
<span id="val_333searchGrid">$7.00</span>
</div>
<div class="auction-item" id="444searchGrid">
<a href="/sms/auction/view?auc=444" title="Test Lot 444">Test Lot 444</a>
<script>updateTimeLeftSpan(timeLeftInfoMap, 444, "444searchGrid", 1000, 4000, 0, "", "", "searchList", timeLeftCallback);</script>
</div>
"""


def test_parse_search_cards_synthetic_healthy_page_is_silent(capsys):
    cards = ps._parse_search_cards(_HEALTHY_TWO_CARD_HTML, "https://www.publicsurplus.com/fake")
    assert len(cards) == 2
    out = capsys.readouterr().out
    assert out == ""


def test_parse_search_cards_counts_dropped_card_and_missing_price(capsys):
    # card 333 has no title/link -> dropped; card 444 has title/link but no
    # price span -> kept, price_raw=None (a miss).
    cards = ps._parse_search_cards(_DROPPED_AND_MISSING_FIELD_HTML, "https://www.publicsurplus.com/fake")
    assert len(cards) == 1
    assert cards[0]["auc_id"] == "444"
    assert cards[0]["price_raw"] is None
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "1 card(s) dropped" in out
    assert "1 price miss" in out


# --- discover() --------------------------------------------------------

def test_discover_parses_n_geq_5_observations(monkeypatch, search_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(search_html))
    obs = ps.PublicSurplusSource().discover()
    assert len(obs) >= 5
    assert all(isinstance(o, Observation) for o in obs)


def test_discover_observations_have_required_shape(monkeypatch, search_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(search_html))
    obs = ps.PublicSurplusSource().discover()
    for o in obs:
        assert o.source == "public_surplus"
        assert o.source_lot_id
        assert o.status == "active"
        assert o.current_bid is None or isinstance(o.current_bid, Decimal)
        assert o.bid_count is None  # never present on the search grid
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None


def test_discover_raw_roundtrips_parsed_card(monkeypatch, search_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(search_html))
    obs = ps.PublicSurplusSource().discover()
    by_id = {o.source_lot_id: o for o in obs}
    o = by_id["4053470"]
    assert o.raw["price_raw"] == "$10.50"
    assert o.raw["end_epoch_ms_raw"] == "1785524400000"
    assert o.current_bid == Decimal("10.50")


def test_discover_request_params(monkeypatch, search_html):
    captured = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured.append(params)
        return _FakeResponse(search_html)

    monkeypatch.setattr(ps, "polite_get", fake_get)
    ps.PublicSurplusSource().discover()
    assert captured  # at least one request made
    assert captured[0]["posting"] == "y"
    assert captured[0]["keyWord"] in FURNITURE_TERMS
    assert captured[0]["page"] == 0


def test_discover_dedupes_across_terms(monkeypatch, search_html):
    # every term sweep serves the SAME fixture — must still dedupe by auc_id
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(search_html))
    obs = ps.PublicSurplusSource().discover()
    ids = [o.source_lot_id for o in obs]
    assert len(ids) == len(set(ids))


def test_discover_all_terms_fail_returns_empty_and_prints_loud_error(monkeypatch, capsys):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    obs = ps.PublicSurplusSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "public_surplus" in out


def test_discover_returns_empty_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(ps, "polite_get", raise_connection_error)
    obs = ps.PublicSurplusSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_warns_loudly_on_healthy_but_empty_result(monkeypatch, capsys):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse("<html><body>no cards</body></html>"))
    obs = ps.PublicSurplusSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 active listings" in out


def test_discover_pagination_stops_on_short_page(monkeypatch, search_html):
    # fixture has < PS_PAGE_SIZE cards -> pagination must stop after page 0
    pages_fetched = []

    def fake_get(url, *, headers=None, params=None, timeout=30):
        pages_fetched.append(params["page"])
        return _FakeResponse(search_html)

    monkeypatch.setattr(ps, "polite_get", fake_get)
    ps.PublicSurplusSource().discover()
    # every term should only ever fetch page 0 (fixture has 6 cards < 25)
    assert set(pages_fetched) == {0}


# --- poll() --------------------------------------------------------

def test_poll_active_no_bids(monkeypatch, detail_no_bids_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(detail_no_bids_html))
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "4054005"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "active"
    assert o.current_bid == Decimal("20.00")
    assert o.bid_count == 0
    assert o.end_date is not None
    assert o.end_date.tzinfo is not None
    assert o.raw["detail_page"]["current_bid_raw"] == "$20.00"


def test_poll_active_with_bids(monkeypatch, detail_with_bids_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(detail_with_bids_html))
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "4053470"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "active"
    assert o.current_bid == Decimal("10.50")
    assert o.bid_count == 2
    assert o.raw["detail_page"]["bid_count_raw"] == "2"


def test_poll_returns_gone_for_401_login_wall_after_end_date(monkeypatch, login_wall_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(login_wall_html, status_code=401))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "4049641", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 401


def test_poll_returns_gone_for_404(monkeypatch):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse("not found", status_code=404))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "999999999", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["http_status"] == 404


def test_poll_401_before_end_date_emits_nothing(monkeypatch, login_wall_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(login_wall_html, status_code=401))
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "4049641", "end_date": future_end}])
    assert obs == []


def test_poll_401_with_unknown_end_date_emits_nothing(monkeypatch, login_wall_html):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse(login_wall_html, status_code=401))
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "4049641", "end_date": None}])
    assert obs == []


def test_poll_unrecognized_page_shape_is_fetch_failure_not_gone(monkeypatch, capsys):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse("<html><body>mystery page</body></html>"))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "123", "end_date": past_end}])
    assert obs == []  # must NOT be 'gone' — unrecognized shape is a fetch failure
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "unrecognized page shape" in out


def test_poll_blocked_403_emits_no_observation(monkeypatch, capsys):
    monkeypatch.setattr(ps, "polite_get", lambda *a, **k: _FakeResponse("", status_code=403))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "123", "end_date": past_end}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_connection_exception_emits_no_observation(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(ps, "polite_get", raise_connection_error)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = ps.PublicSurplusSource().poll([{"source_lot_id": "123", "end_date": past_end}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_get(*a, **k):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(ps, "polite_get", fail_get)
    assert ps.PublicSurplusSource().poll([]) == []


# --- poll() batch-level 401 block detection (fix round 1, review finding #1) ---

def test_poll_all_tracked_lots_401_suspected_block_zero_gone_loud_error(monkeypatch, login_wall_html, capsys):
    # a session-wide block would look identical, per lot, to N real closes —
    # this must NOT mass-mark every tracked lot 'gone' (append-only-unrecoverable).
    monkeypatch.setattr(ps, "polite_get", lambda url, **k: _FakeResponse(login_wall_html, status_code=401, url=url))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [{"source_lot_id": str(i), "end_date": past_end} for i in range(5)]
    obs = ps.PublicSurplusSource().poll(lots)
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "block" in out.lower()
    assert "5/5" in out


def test_poll_single_401_among_healthy_batch_still_gone(monkeypatch, detail_no_bids_html, detail_with_bids_html, login_wall_html):
    # a lone 401 in an otherwise-healthy batch is PS's normal, legitimate
    # close signal — must still become 'gone' exactly as before this fix.
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if "auc=4054005" in url:
            return _FakeResponse(detail_no_bids_html, url=url)
        if "auc=4053470" in url:
            return _FakeResponse(detail_with_bids_html, url=url)
        return _FakeResponse(login_wall_html, status_code=401, url=url)

    monkeypatch.setattr(ps, "polite_get", fake_get)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [
        {"source_lot_id": "4054005"},
        {"source_lot_id": "4054005"},
        {"source_lot_id": "4053470"},
        {"source_lot_id": "4053470"},
        {"source_lot_id": "9999999", "end_date": past_end},  # the lone 401
    ]
    obs = ps.PublicSurplusSource().poll(lots)
    statuses = [o.status for o in obs]
    assert statuses.count("gone") == 1
    assert statuses.count("active") == 4


def test_poll_below_min_count_401s_not_suspected(monkeypatch, login_wall_html):
    # 2 lots, both 401 (100% of the batch) — below BLOCK_SUSPECT_MIN_COUNT
    # (3), so NOT treated as a suspected block; both still become 'gone'.
    monkeypatch.setattr(ps, "polite_get", lambda url, **k: _FakeResponse(login_wall_html, status_code=401, url=url))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [{"source_lot_id": "1", "end_date": past_end}, {"source_lot_id": "2", "end_date": past_end}]
    obs = ps.PublicSurplusSource().poll(lots)
    assert len(obs) == 2
    assert all(o.status == "gone" for o in obs)


def test_poll_below_min_fraction_401s_not_suspected(monkeypatch, detail_no_bids_html, login_wall_html):
    # 3 401s out of 10 lots (30%) — meets the min COUNT but not the min
    # FRACTION (80%) — NOT suspected; the 3 still become 'gone' individually.
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if "auc=4054005" in url:
            return _FakeResponse(detail_no_bids_html, url=url)
        return _FakeResponse(login_wall_html, status_code=401, url=url)

    monkeypatch.setattr(ps, "polite_get", fake_get)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [{"source_lot_id": "4054005"} for _ in range(7)] + [
        {"source_lot_id": f"gone-{i}", "end_date": past_end} for i in range(3)
    ]
    obs = ps.PublicSurplusSource().poll(lots)
    statuses = [o.status for o in obs]
    assert statuses.count("gone") == 3
    assert statuses.count("active") == 7


def test_sold_sweep_returns_empty_list():
    assert ps.PublicSurplusSource().sold_sweep() == []


# --- parsing helpers --------------------------------------------------------

def test_parse_money_handles_dollar_sign_and_commas():
    assert ps._parse_money("$1,200.50") == Decimal("1200.50")
    assert ps._parse_money("$20.00") == Decimal("20.00")
    assert ps._parse_money(None) is None
    assert ps._parse_money("") is None


def test_parse_epoch_ms_converts_to_utc_datetime():
    dt = ps._parse_epoch_ms("1785524400000")
    assert dt.tzinfo is not None
    assert dt == datetime.fromtimestamp(1785524400, tz=timezone.utc)


def test_parse_epoch_ms_handles_missing():
    assert ps._parse_epoch_ms(None) is None
    assert ps._parse_epoch_ms("") is None


def test_parse_bid_count_digits():
    assert ps._parse_bid_count("2") == (2, "2")
    assert ps._parse_bid_count("  12  ") == (12, "12")


def test_parse_bid_count_no_bids():
    count, raw = ps._parse_bid_count('<span class="text-danger">No Bids</span>')
    assert count == 0
    assert "No Bids" in raw


def test_parse_bid_count_none():
    assert ps._parse_bid_count(None) == (None, None)

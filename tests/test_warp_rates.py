"""Offline tests for the Warp carrier-price client (`automation/warp_rates.py`).

No network: `urllib.request.urlopen` is replaced by a recorder that hands back
a canned body. The canned options are the real shape of a 2026-10-02 probe
(Atlanta 30318 → Decatur 30033, 4 pallets).
"""
import io
import json
import urllib.error
from datetime import date

import pytest

from automation import warp_rates

OPTIONS = [
    {"rank": 1, "carrier_name": "Averitt Express", "service_level": "STND",
     "price_usd": 589.512, "transit_days": 1, "is_warp": False},
    {"rank": 2, "carrier_name": "FedEx Economy", "service_level": "FEDEX_FREIGHT_ECONOMY",
     "price_usd": 612.36, "transit_days": 2, "is_warp": False},
    {"rank": 3, "carrier_name": "Saia LTL Freight", "service_level": "STANDARD",
     "price_usd": 625.512, "transit_days": 1, "is_warp": False},
]


class _Resp:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Capture the outbound request; answer with `state['payload']`."""
    state = {"payload": {"market_options": OPTIONS}, "requests": [], "raise": None}

    def fake_urlopen(req, timeout=None):
        state["requests"].append((req, timeout))
        if state["raise"]:
            raise state["raise"]
        return _Resp(state["payload"])

    monkeypatch.delenv("WARP_API_KEY", raising=False)
    monkeypatch.setattr(warp_rates.urllib.request, "urlopen", fake_urlopen)
    return state


def _call(**over):
    kw = dict(pallets=4, weight_lbs_per_pallet=560, height_in=80, pickup="2026-10-07")
    kw.update(over)
    return warp_rates.market_options("30318", "30033", **kw)


# ────────────────────────────────── request ─────────────────────────────────

def test_request_goes_to_market_options_with_the_documented_body(wire):
    _call()
    (req, timeout), = wire["requests"]
    assert req.full_url == "https://www.wearewarp.com/api/v1/ltl/market-options"
    assert req.get_method() == "POST"
    assert timeout == warp_rates.TIMEOUT_SEC
    assert json.loads(req.data) == {
        "origin_zip": "30318", "destination_zip": "30033",
        "pickup_date": "2026-10-07", "pallets": 4,
        "weight_lbs_per_pallet": 560,
        "length_in": 48, "width_in": 40, "height_in": 80,
        "accessorials": {"pickup": [], "delivery": ["liftgate-delivery", "residential-delivery"]},
    }


def test_never_calls_the_single_quote_endpoint(wire):
    _call()
    assert "/ltl/quote" not in wire["requests"][0][0].full_url


def test_no_key_means_no_authorization_header(wire):
    _call()
    assert wire["requests"][0][0].get_header("Authorization") is None


def test_env_key_is_sent_as_a_bearer_token(wire, monkeypatch):
    monkeypatch.setenv("WARP_API_KEY", "wak_live_abc")
    _call()
    assert wire["requests"][0][0].get_header("Authorization") == "Bearer wak_live_abc"


def test_height_is_clamped_to_the_ltl_limit(wire):
    _call(height_in=110)
    assert json.loads(wire["requests"][0][0].data)["height_in"] == 85


# ─────────────────────────────────── answers ────────────────────────────────

def test_returns_the_options_list(wire):
    assert [o["carrier_name"] for o in _call()] == [
        "Averitt Express", "FedEx Economy", "Saia LTL Freight",
    ]


def test_empty_list_is_a_normal_answer(wire):
    wire["payload"] = {"market_options": [], "note": "aggregator unavailable"}
    assert _call() == []


def test_http_error_is_warp_unavailable(wire):
    wire["raise"] = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
    with pytest.raises(warp_rates.WarpUnavailable):
        _call()


def test_timeout_is_warp_unavailable(wire):
    wire["raise"] = TimeoutError("timed out")
    with pytest.raises(warp_rates.WarpUnavailable):
        _call()


@pytest.mark.parametrize("payload", [[], {"quote": 1}, {"market_options": "nope"}])
def test_unexpected_shapes_are_warp_unavailable(wire, payload):
    wire["payload"] = payload
    with pytest.raises(warp_rates.WarpUnavailable):
        _call()


def test_an_oversized_response_is_refused(wire, monkeypatch):
    monkeypatch.setattr(warp_rates, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(warp_rates.WarpUnavailable) as err:
        _call()
    assert "too large" in str(err.value)


def test_response_level_substitution_drops_warps_own_option(wire):
    """A dedicated truck must never be read as an LTL price."""
    wire["payload"] = {
        "mode_substituted": {"requested": "ltl", "served": "ftl"},
        "market_options": OPTIONS + [
            {"carrier_name": "Warp Technology", "price_usd": 250.0, "is_warp": True},
        ],
    }
    names = [o["carrier_name"] for o in _call()]
    assert "Warp Technology" not in names and len(names) == 3


# ────────────────────────────────── summarize ───────────────────────────────

def test_summary_picks_the_cheapest_and_counts_all(wire):
    s = warp_rates.summarize(list(reversed(OPTIONS)))
    assert s["carrier_status"] == "ok"
    assert s["carrier_low"] == 589.51
    assert s["carrier_name"] == "Averitt Express"
    assert s["carrier_count"] == 3
    assert s["carrier_options"][0] == {
        "carrier": "Averitt Express", "price_usd": 589.51, "transit_days": 1,
    }


def test_summary_keeps_at_most_five_options():
    many = [{"carrier_name": f"C{i}", "price_usd": 100 + i} for i in range(18)]
    s = warp_rates.summarize(many)
    assert len(s["carrier_options"]) == 5
    assert s["carrier_count"] == 18


@pytest.mark.parametrize("bad", [
    {"carrier_name": "Truck", "price_usd": 250, "mode_substituted": {"served": "ftl"}},
    {"carrier_name": "Truck", "price_usd": 250, "mode": "ftl"},
    {"carrier_name": "Zero", "price_usd": 0},
    {"carrier_name": "Neg", "price_usd": -5},
    {"carrier_name": "Str", "price_usd": "589"},
    {"carrier_name": "Bool", "price_usd": True},
    {"carrier_name": "Missing"},
])
def test_summary_drops_substituted_and_unpriced_options(bad):
    s = warp_rates.summarize([bad] + OPTIONS)
    assert s["carrier_count"] == 3
    assert all(o["carrier"] != bad["carrier_name"] for o in s["carrier_options"])


def test_summary_of_nothing_is_status_none():
    assert warp_rates.summarize([]) == {
        "carrier_status": "none", "carrier_low": None, "carrier_name": None,
        "carrier_count": 0, "carrier_options": [],
    }


# ─────────────────────────────── pallets + dates ────────────────────────────

@pytest.mark.parametrize("qty, per, expected", [
    (160, 35, 5), (35, 35, 1), (36, 35, 2), (1, 35, 1), (0, 35, 0), (160, 0, 0),
])
def test_pallets_for(qty, per, expected):
    assert warp_rates.pallets_for(qty, per) == expected


def test_weight_per_pallet_includes_the_pallet():
    assert warp_rates.weight_per_pallet(160, 5, 13.0) == 416 + 40


@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 2), "2026-10-05"),    # Fri → Mon
    (date(2026, 9, 30), "2026-10-05"),    # Wed + 3 = Sat → Mon
    (date(2026, 10, 5), "2026-10-08"),    # Mon → Thu
])
def test_pickup_date_is_three_days_out_and_never_a_weekend(today, expected):
    assert warp_rates.pickup_date(today) == expected


# ────────────────────────────────── price check ─────────────────────────────

@pytest.mark.parametrize("low, high, carrier, verdict", [
    (570, 810, 590, "ok"),            # the Megan lane: estimator was right
    (1080, 1520, 1879, "ok"),         # 24 % over the top — inside tolerance
    (1080, 1520, 2309, "site_low"),   # real carriers far above what we showed
    (950, 1340, 500, "site_high"),
    (None, 810, 590, None),
    (570, 810, None, None),
    (0, 0, 590, None),
])
def test_price_check(low, high, carrier, verdict):
    assert warp_rates.price_check(low, high, carrier) == verdict


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("WARP_RATES_ENABLED", "0")
    assert warp_rates.enabled() is False
    monkeypatch.delenv("WARP_RATES_ENABLED")
    assert warp_rates.enabled() is True

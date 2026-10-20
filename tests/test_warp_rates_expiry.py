"""`automation/warp_rates.py` additions of 2026-10-20: the drop's accessorial
codes, the padded buyer price, the full (unlimited) summary, and the per-option
`expires_at` / `bookable` facts a live 04270 → 28206 call showed. Offline."""
import io
import json

import pytest

from automation import warp_rates

OPTIONS = [
    {"option_id": "o1", "carrier_name": "Averitt Express", "service_level": "STND",
     "price_usd": 589.512, "transit_days": 3, "is_warp": False, "bookable": False,
     "expires_at": "2026-10-23T18:00:00Z", "expires_in_seconds": 259200},
    {"option_id": "o2", "carrier_name": "Warp", "service_level": "WARP",
     "price_usd": 640.0, "transit_days": 2, "is_warp": True, "bookable": True,
     "expires_at": "2026-10-21T18:00:00+00:00"},
    {"option_id": "o3", "carrier_name": "XPO", "price_usd": 701.0, "transit_days": 4,
     "bookable": False, "expires_at": "garbage"},
]


@pytest.mark.parametrize("res, lift, codes", [
    (True, True, ("liftgate-delivery", "residential-delivery")),
    (True, False, ("residential-delivery",)),
    (False, True, ("liftgate-delivery",)),
    (False, False, ()),
])
def test_delivery_accessorials(res, lift, codes):
    assert warp_rates.delivery_accessorials(residential=res, liftgate=lift) == codes


def test_market_options_sends_the_given_drop(monkeypatch):
    sent = {}

    class _Resp:
        def __init__(self):
            self._buf = io.BytesIO(json.dumps({"market_options": OPTIONS}).encode())

        def read(self, n=-1):
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        sent["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(warp_rates.urllib.request, "urlopen", fake_open)
    out = warp_rates.market_options("04270", "28206", pallets=3, weight_lbs_per_pallet=473, height_in=80,
                                    timeout=9, delivery=("residential-delivery",))
    assert len(out) == 3 and sent["timeout"] == 9
    assert sent["body"]["accessorials"] == {"pickup": [], "delivery": ["residential-delivery"]}


@pytest.mark.parametrize("low, pct, price", [
    (589.51, 15, 678), (589.51, 0, 590), (100, 10, 110), (100.0, 33.3, 134), (1000, 100, 2000),
])
def test_padded_price_rounds_up_to_the_dollar(low, pct, price):
    assert warp_rates.padded_price(low, pct) == price


@pytest.mark.parametrize("low, pct", [(None, 15), (0, 15), (-5, 15), ("x", 15), (500, -1), (500, None)])
def test_padded_price_refuses_junk(low, pct):
    assert warp_rates.padded_price(low, pct) is None


def test_summary_keeps_expiry_bookable_and_service_level():
    s = warp_rates.summarize(OPTIONS, limit=None)
    assert [o["carrier"] for o in s["carrier_options"]] == ["Averitt Express", "Warp", "XPO"]
    averitt, warp, xpo = s["carrier_options"]
    assert averitt["expires_at"] == "2026-10-23T18:00:00Z" and averitt["bookable"] is False
    assert averitt["service_level"] == "STND"
    assert warp["bookable"] is True and warp["expires_at"] == "2026-10-21T18:00:00+00:00"
    assert xpo["expires_at"] is None            # unreadable ⇒ no expiry recorded
    # the summary as a whole goes stale with its EARLIEST kept option
    assert s["expires_at"] == "2026-10-21T18:00:00+00:00"
    assert s["bookable"] is True
    assert warp_rates.summarize([])["expires_at"] is None
    assert warp_rates.unavailable("error")["bookable"] is False


def test_summary_limit_none_keeps_every_usable_option():
    many = [{"carrier_name": f"C{i}", "price_usd": 500 + i, "transit_days": 2} for i in range(9)]
    assert len(warp_rates.summarize(many)["carrier_options"]) == warp_rates.OPTIONS_KEPT
    assert len(warp_rates.summarize(many, limit=None)["carrier_options"]) == 9


UTC_18 = 1792778400.0        # 2026-10-23T18:00:00Z


@pytest.mark.parametrize("value, epoch", [
    ("2026-10-23T18:00:00Z", UTC_18),
    ("2026-10-23T18:00:00+00:00", UTC_18),
    ("2026-10-23T14:00:00-04:00", UTC_18),
    ("2026-10-23T18:00:00", UTC_18),          # naive ⇒ UTC
    ("garbage", None), ("", None), (None, None), (12, None),
])
def test_expires_epoch(value, epoch):
    assert warp_rates.expires_epoch(value) == epoch


def test_expires_date_is_the_utc_day():
    assert warp_rates.expires_date("2026-10-23T18:00:00Z") == "2026-10-23"
    assert warp_rates.expires_date("2026-10-23T23:30:00-04:00") == "2026-10-24"
    assert warp_rates.expires_date(None) is None

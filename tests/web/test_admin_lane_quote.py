"""Admin lane tool behind Sales › Lane quote (`POST /api/freight/lane-quote`).

Offline: `warp_rates.market_options` is a recorder, `inventory.get` an
in-memory lot, `site_settings.get_all` a dict. Pinned: the route is
auth-walled, every carrier comes back UNPADDED with `expires_at` + `bookable`,
the padded "site would show" number and the estimator ride beside it, a lot
prefills origin + chair data, bad input is a 400 (never a padded ZIP), the
kill switch is a 409, the budget a 429, Warp down a 200 that says so, and the
padding setting round-trips through `/api/settings`.
"""
import sys

import pytest
from fastapi.testclient import TestClient

from automation import inventory, site_settings, warp_rates
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

app_module = sys.modules["automation.web.app"]

OPTIONS = [
    {"carrier_name": "Averitt Express", "service_level": "STND", "price_usd": 589.512, "transit_days": 3,
     "bookable": False, "expires_at": "2026-10-23T18:00:00Z"},
    {"carrier_name": "Warp", "price_usd": 640.0, "transit_days": 2, "is_warp": True, "bookable": True,
     "expires_at": "2026-10-21T18:00:00Z"},
    {"carrier_name": "XPO", "price_usd": 701.0, "transit_days": 4},
    {"carrier_name": "Truck", "price_usd": 6598.0, "transit_days": 2, "mode_substituted": True},
]
LOT = {"lot_id": "bs-10121-53", "title": "Maine chairs", "status": "active_bid", "zip_code": "04270",
       "state": "ME", "quantity_remaining": 360, "quantity_original": 400,
       "chair_weight_lb": 11, "chairs_per_pallet": 40, "pallet_height_in": 72}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("WARP_RATES_ENABLED", raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    app_module._carrier_reset()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()
    app_module._carrier_reset()


@pytest.fixture
def wire(monkeypatch):
    seen = {"calls": [], "options": OPTIONS, "raise": None, "settings": {"freight_padding_pct": 15.0}}

    def fake(origin, dest, **kw):
        seen["calls"].append((origin, dest, kw))
        if seen["raise"]:
            raise seen["raise"]
        return list(seen["options"])

    monkeypatch.setattr(warp_rates, "market_options", fake)
    monkeypatch.setattr(inventory, "get", lambda lot_id: dict(LOT) if lot_id == LOT["lot_id"] else None)
    monkeypatch.setattr(site_settings, "get_all", lambda: {**site_settings.defaults(), **seen["settings"]})
    return seen


def _client():
    return TestClient(app, base_url="https://testserver")


def _quote(client, **body):
    return client.post("/api/freight/lane-quote", json=body)


def test_lane_quote_lists_every_carrier_unpadded(wire):
    r = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=100)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["origin_zip"] == "04270" and out["dest_zip"] == "28206" and out["quantity"] == 100
    assert out["shipment"]["pallets"] == 3 and out["shipment"]["weight_lbs_per_pallet"] == 473
    assert out["shipment"]["height_in"] == 80 and out["shipment"]["lbs_per_chair_estimated"] is True
    assert [c["carrier"] for c in out["carriers"]] == ["Averitt Express", "Warp", "XPO"]   # substituted truck dropped
    assert out["carriers"][0]["price_usd"] == 589.51                 # unpadded
    assert out["carriers"][0]["expires_at"] == "2026-10-23T18:00:00Z" and out["carriers"][0]["bookable"] is False
    assert out["carriers"][1]["bookable"] is True
    assert out["carriers"][2]["expires_at"] is None
    assert out["carrier_count"] == 3 and out["carrier_status"] == "ok"
    assert out["cheapest"]["carrier"] == "Averitt Express"
    assert out["padded_price"] == 678 and out["padding_pct"] == 15.0
    assert out["estimator"]["ltl_low"] > 0 and out["estimator"]["miles"] > 500
    assert out["delivery_accessorials"] == ["liftgate-delivery", "residential-delivery"]
    assert out["residential"] is True and out["liftgate"] is True and out["lot_id"] is None
    ((origin, dest, kw),) = wire["calls"]
    assert (origin, dest) == ("04270", "28206")
    assert (kw["pallets"], kw["weight_lbs_per_pallet"], kw["height_in"]) == (3, 473, 80)


def test_lot_prefills_origin_quantity_and_chair_data(wire):
    out = _quote(_client(), lot_id="bs-10121-53", dest_zip="28206").json()
    assert out["origin_zip"] == "04270" and out["quantity"] == 360 and out["lot_id"] == "bs-10121-53"
    assert out["shipment"] == {"pallets": 9, "weight_lbs_per_pallet": 480, "height_in": 72,
                               "chairs_per_pallet": 40, "lbs_per_chair": 11, "lbs_per_chair_estimated": False,
                               "too_big": False}
    # explicit fields win over the lot
    app_module._carrier_reset()
    out = _quote(_client(), lot_id="bs-10121-53", dest_zip="28206", quantity=100, lbs_per_chair=13,
                 chairs_per_pallet="50", pallet_height_in=85).json()
    assert out["shipment"]["pallets"] == 2 and out["shipment"]["lbs_per_chair"] == 13
    assert out["shipment"]["height_in"] == 85


def test_unknown_lot_is_404(wire):
    assert _quote(_client(), lot_id="nope", dest_zip="28206").status_code == 404
    assert wire["calls"] == []


@pytest.mark.parametrize("residential, liftgate, codes", [
    (False, False, []), (True, False, ["residential-delivery"]), ("0", "1", ["liftgate-delivery"]),
])
def test_drop_flags_reach_warp(wire, residential, liftgate, codes):
    out = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=100,
                 residential=residential, liftgate=liftgate).json()
    assert out["delivery_accessorials"] == codes
    assert list(wire["calls"][0][2]["delivery"]) == codes


@pytest.mark.parametrize("body", [
    {"origin_zip": "4270", "dest_zip": "28206", "quantity": 100},      # never padded to 04270
    {"origin_zip": "04270", "dest_zip": "2820", "quantity": 100},
    {"origin_zip": "04270", "dest_zip": "K1A 0B1", "quantity": 100},
    {"origin_zip": "04270", "dest_zip": "96801", "quantity": 100},     # offshore: hand quote
    {"origin_zip": "04270", "dest_zip": "28206", "quantity": 0},
    {"origin_zip": "04270", "dest_zip": "28206", "quantity": "lots"},
    {"origin_zip": "04270", "dest_zip": "28206", "quantity": 100, "lbs_per_chair": -2},
    {"dest_zip": "28206", "quantity": 100},                            # no origin, no lot
])
def test_bad_input_is_a_400_without_a_call(wire, body):
    r = _quote(_client(), **body)
    assert r.status_code == 400, r.text
    assert wire["calls"] == []


def test_too_big_for_ltl_makes_no_call_and_says_so(wire):
    out = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=500).json()
    assert out["carrier_status"] == "too_big" and out["carriers"] == [] and out["padded_price"] is None
    assert "partial/full truck" in out["error"]
    assert out["estimator"] is not None and wire["calls"] == []


def test_warp_down_is_a_200_with_the_estimator(wire):
    wire["raise"] = warp_rates.WarpUnavailable("warp HTTP 503")
    out = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=100).json()
    assert out["carrier_status"] == "error" and out["error"] == "warp HTTP 503"
    assert out["carriers"] == [] and out["estimator"]["ltl_low"] > 0


def test_kill_switch_is_a_409(wire, monkeypatch):
    monkeypatch.setenv("WARP_RATES_ENABLED", "0")
    r = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=100)
    assert r.status_code == 409 and wire["calls"] == []


def test_budget_is_shared_and_a_429(wire, monkeypatch):
    monkeypatch.setattr(app_module, "CARRIER_CALLS_PER_HOUR", 1)
    client = _client()
    assert _quote(client, origin_zip="04270", dest_zip="28206", quantity=100).status_code == 200
    assert _quote(client, origin_zip="04270", dest_zip="28206", quantity=100).status_code == 200   # memo hit
    r = _quote(client, origin_zip="04270", dest_zip="30033", quantity=100)
    assert r.status_code == 429 and "next hour" in r.json()["detail"]
    assert len(wire["calls"]) == 1


def test_memo_drops_an_answer_past_its_expiry(wire, monkeypatch):
    expired = [dict(OPTIONS[0], expires_at="2020-01-01T00:00:00Z")]
    wire["options"] = expired
    client = _client()
    assert _quote(client, origin_zip="04270", dest_zip="28206", quantity=100).json()["carrier_count"] == 1
    assert _quote(client, origin_zip="04270", dest_zip="28206", quantity=100).json()["carrier_count"] == 1
    assert len(wire["calls"]) == 2            # nothing stale was served from the memo


def test_padding_setting_round_trips(monkeypatch):
    state = {}

    def set_many(values):
        for k, v in values.items():
            state[k] = site_settings._coerce(k, v)
        return {**site_settings.defaults(), **state}

    monkeypatch.setattr(site_settings, "get_all", lambda: {**site_settings.defaults(), **state})
    monkeypatch.setattr(site_settings, "set_many", set_many)
    client = _client()
    assert client.get("/api/settings").json()["freight_padding_pct"] == 15.0
    r = client.patch("/api/settings", json={"freight_padding_pct": 22.5})
    assert r.status_code == 200 and r.json()["freight_padding_pct"] == 22.5
    assert client.patch("/api/settings", json={"freight_padding_pct": 101}).status_code == 400
    assert client.patch("/api/settings", json={"freight_padding_pct": -1}).status_code == 400


def test_route_is_behind_the_admin_wall(wire, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    r = _quote(_client(), origin_zip="04270", dest_zip="28206", quantity=100)
    assert r.status_code == 401 and wire["calls"] == []


def test_lane_pane_and_module_are_served():
    client = TestClient(app)
    html = client.get("/admin").text
    assert 'data-pane="lane"' in html and 'id="lane-form"' in html and 'id="lane-padding"' in html
    assert client.get("/static/admin/lane.js").status_code == 200
    assert client.get("/static/admin/lane.css").status_code == 200

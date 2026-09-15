# tests/web/test_public_map.py
"""Public map read model: buckets, allow-list, multi-location pins, favorites redaction, nearby."""
import hashlib
import re

import pytest

from automation import favorites as fav_mod
from automation.web import public_map as pm

PRIVATE = ("storage_note", "govdeals_username", "govdeals_password", "contact_email",
           "contact_phone", "buyer_cert_path", "folder_path", "seller_id", "zip_code",
           "image_url", "link", "asset_id", "end_date_iso", "notes")

# Task 8 names the dewatermarked R2 object after the same opaque key this module
# uses for the pin id, so no public string ever spells the GovDeals asset id.
FAV_KEY = hashlib.sha256(b"9685/56").hexdigest()[:12]
FAV_HERO = f"https://r2/fav-{FAV_KEY}.jpg"


@pytest.fixture(autouse=True)
def _fixed_geo(monkeypatch):
    table = {("Boise", "ID"): (43.6, -116.2), ("Atlanta", "GA"): (33.7, -84.4),
             ("Pittsburgh", "PA"): (40.4, -80.0)}
    def fake(city, state, zip_code):
        hit = table.get((city, state))
        return (hit[0], hit[1], "city") if hit else (None, None, None)
    monkeypatch.setattr(pm.geo, "resolve_place", fake)
    pm.readcache.invalidate_all()


def _row(**kw):
    base = dict(lot_id="gd-1-2", title="500 banquet chairs", status="owned", quantity_remaining=500,
                quantity_original=500, price_per_chair=25, city="Boise", state="ID", zip_code="83702",
                storage_note="unit 12 gate 4321", hero_image_url="https://r2/x.jpg", image_urls=[],
                locations=None, fake_sold_out=False, chair_type="banquet")
    base.update(kw)
    return base


@pytest.mark.parametrize("row,expect", [
    (_row(status="owned"), "available"),
    (_row(status="listed"), "available"),
    (_row(status="won_pickup"), "incoming"),
    (_row(status="active_bid"), "incoming"),
    (_row(status="sold_out"), "sold"),
    (_row(status="lost_sold_out"), "sold"),
    (_row(status="owned", fake_sold_out=True), "sold"),
    (_row(status="hidden"), None),
    (_row(status="lost"), None),
    (_row(status="hidden", fake_sold_out=True), None),
])
def test_bucket(row, expect):
    assert pm.bucket(row) == expect


def test_points_allowlist_and_city_level():
    pts = pm.points_from_inventory([_row()])
    assert len(pts) == 1
    p = pts[0]
    assert set(p) <= pm.POINT_KEYS
    for k in PRIVATE:
        assert k not in p
    assert p["lat"] == 43.6 and p["precision"] == "city" and p["url"] == "/listings/gd-1-2"
    assert p["kind"] == "lot" and p["bucket"] == "available" and p["unit"]


def test_multi_location_lot_yields_one_pin_per_place():
    row = _row(locations=[{"city": "Boise", "state": "ID", "quantity": 300},
                          {"city": "Atlanta", "state": "GA", "quantity": 200}])
    pts = pm.points_from_inventory([row])
    assert [(p["city"], p["quantity"]) for p in pts] == [("Boise", 300), ("Atlanta", 200)]
    assert pts[0]["id"] != pts[1]["id"]


def test_free_lot_keeps_a_zero_price():
    p = pm.points_from_inventory([_row(price_per_chair=0)])[0]
    assert p["price_per_chair"] == 0.0


def test_unresolvable_place_is_dropped():
    assert pm.points_from_inventory([_row(city="Nowhere", state=None)]) == []


def _fav(**kw):
    base = dict(asset_id="9685/56", link="https://www.govdeals.com/en/asset/9685/56",
                title="Lot of 2,500 banquet chairs", quantity=2500, end_date_iso="2099-01-01T00:00:00+00:00",
                end_date_raw=None, image_url="https://cdn.govdeals.com/raw.jpg", location="Pittsburgh, PA",
                starred_at="2026-09-14", last_synced_at="2026-09-14", notes=None, sent_intervals=[],
                clean_hero_url=FAV_HERO, clean_image_urls=[])
    base.update(kw)
    return fav_mod.Favorite(**base)


def test_favorite_is_redacted_incoming():
    p = pm.points_from_favorites([_fav()])[0]
    assert set(p) <= pm.POINT_KEYS
    assert p["kind"] == "favorite" and p["bucket"] == "incoming"
    assert p["hero"] == FAV_HERO and p["url"] == "/#contact"
    flat = " ".join(str(v) for v in p.values())
    assert "govdeals" not in flat.lower() and "raw.jpg" not in flat
    # Nothing the public sees spells the asset id — not the pin id, not the
    # photo URL — or the GovDeals listing is reconstructable from the map.
    assert p["id"] == f"fav-{FAV_KEY}" and len(p["id"]) == 16
    for token in ("9685", "56"):
        assert not re.search(rf"(?<![0-9a-f]){token}(?![0-9a-f])", flat), token


def test_favorite_without_clean_photo_has_no_hero():
    p = pm.points_from_favorites([_fav(clean_hero_url=None)])[0]
    assert p["hero"] is None


def test_private_or_ended_favorites_are_skipped():
    assert pm.points_from_favorites([_fav(notes="#private")]) == []
    assert pm.points_from_favorites([_fav(end_date_iso="2000-01-01T00:00:00+00:00")]) == []


def test_favorite_is_hidden_once_the_lot_is_in_inventory(monkeypatch):
    """The PA lot we won shows once — as the lot, not twice with its favorite."""
    pm.readcache.invalidate_all()
    row = _row(lot_id="gd-56-9685", city="Pittsburgh", state="PA", status="active_bid",
               govdeals_url="https://www.govdeals.com/en/asset/9685/56")
    monkeypatch.setattr(pm.inventory, "list_public", lambda: [row])
    monkeypatch.setattr(pm.inventory, "list_sold_showcase", lambda: [])
    monkeypatch.setattr(pm.favorites_mod, "list_all", lambda: [_fav()])
    pts = pm.all_points()
    pitt = [p for p in pts if p["city"] == "Pittsburgh"]
    assert len(pitt) == 1
    assert pitt[0]["kind"] == "lot" and pitt[0]["lot_id"] == "gd-56-9685"
    pm.readcache.invalidate_all()


def test_points_from_favorites_exclude_skips_owned_assets():
    assert pm.points_from_favorites([_fav()], exclude={"9685/56"}) == []
    assert len(pm.points_from_favorites([_fav()], exclude={"1/2"})) == 1


def test_fetch_points_filters_and_near(monkeypatch):
    rows = [_row(), _row(lot_id="gd-3-4", city="Atlanta", state="GA", status="sold_out")]
    monkeypatch.setattr(pm, "all_points", lambda: pm.points_from_inventory(rows))
    monkeypatch.setattr(pm.geo, "parse_place", lambda t: ("Boise", "ID", None))
    out = pm.fetch_points(statuses={"available"}, near="Boise, ID", radius_mi=None)
    assert [p["lot_id"] for p in out["points"]] == ["gd-1-2"]
    assert out["counts"] == {"available": 1, "incoming": 0, "sold": 1}
    assert out["near"]["label"] == "Boise, ID" and out["points"][0]["distance_mi"] == 0.0
    far = pm.fetch_points(statuses=None, near="Boise, ID", radius_mi=100)
    assert [p["lot_id"] for p in far["points"]] == ["gd-1-2"]
    # The legend counts what is inside the radius, not what the checkboxes let through.
    assert far["counts"] == {"available": 1, "incoming": 0, "sold": 0}


def test_nearby_excludes_self_and_sold(monkeypatch):
    rows = [_row(), _row(lot_id="gd-3-4", city="Atlanta", state="GA"),
            _row(lot_id="gd-5-6", city="Atlanta", state="GA", status="sold_out")]
    monkeypatch.setattr(pm, "all_points", lambda: pm.points_from_inventory(rows))
    out = pm.nearby("gd-3-4", miles=200)
    assert out["origin"]["lat"] == 33.7
    assert [p["lot_id"] for p in out["items"]] == []          # Boise is > 200 mi, sold excluded
    out = pm.nearby("gd-3-4", miles=5000)
    assert [p["lot_id"] for p in out["items"]] == ["gd-1-2"]


def test_nearby_collapses_a_multi_location_lot_to_its_nearest_pin(monkeypatch):
    rows = [_row(),
            _row(lot_id="gd-3-4", city="Atlanta", state="GA",
                 locations=[{"city": "Atlanta", "state": "GA", "quantity": 100},
                            {"city": "Pittsburgh", "state": "PA", "quantity": 100}])]
    monkeypatch.setattr(pm, "all_points", lambda: pm.points_from_inventory(rows))
    out = pm.nearby("gd-1-2", miles=5000)
    assert [p["lot_id"] for p in out["items"]] == ["gd-3-4"]     # one slot, not two
    assert out["items"][0]["city"] == "Atlanta"                  # the nearer of its two pins


def test_all_points_dedupes_lots_merges_favorites_and_survives_a_favorites_outage(monkeypatch):
    pm.readcache.invalidate_all()
    sold = _row(lot_id="gd-9-9", city="Atlanta", state="GA", status="sold_out")
    monkeypatch.setattr(pm.inventory, "list_public", lambda: [_row(), sold])
    monkeypatch.setattr(pm.inventory, "list_sold_showcase", lambda: [sold])
    monkeypatch.setattr(pm.favorites_mod, "list_all", lambda: [_fav()])
    pts = pm.all_points()
    assert [p["lot_id"] for p in pts if p["kind"] == "lot"] == ["gd-1-2", "gd-9-9"]
    assert [p["kind"] for p in pts].count("favorite") == 1

    def boom():
        raise RuntimeError("favorites table gone")

    pm.readcache.invalidate_all()
    monkeypatch.setattr(pm.favorites_mod, "list_all", boom)
    pts = pm.all_points()
    assert [p["lot_id"] for p in pts] == ["gd-1-2", "gd-9-9"]
    pm.readcache.invalidate_all()

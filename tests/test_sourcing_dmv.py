"""DMV sourcing alerts — BLACKWHOLE-19.

All DB-free: the matcher/digest are pure Python and every test injects `lots=`,
so no database or network is touched. Overarching contract: SEND-DISABLED-BY-
DEFAULT — with no config the job composes a digest and sends nothing.
"""
from __future__ import annotations

import pytest

from automation.sourcing import dmv
from automation.sourcing.alerts import (
    dmv_match,
    filter_dmv_lots,
    resolve_lot_latlon,
)
from automation.sourcing import digest as digest_mod
from automation.sourcing.digest import (
    format_sourcing_digest,
    run_dmv_sourcing_alert,
)


# ── lot fixtures (deal_lots shape: lat/lng/zip/state/canonical_category) ──────

def _lot(**kw):
    base = {
        "asset_id": 100, "account_id": 7, "auction_id": 1,
        "title": "200 Stackable Banquet Chairs", "canonical_category": "chairs",
        "city": "Arlington", "state": "VA", "zip": "22201",
        "lat": 38.8799, "lng": -77.1068,  # Arlington, ~5 mi from DC
        "current_bid": 150.0, "bid_count": 0,
    }
    base.update(kw)
    return base


DC_LOT = _lot()  # chairs, ~5 mi from DC


# ── region config sanity ─────────────────────────────────────────────────────

def test_saved_searches_cover_dmv_states_both_sources():
    states = {s.params["state"] for s in dmv.SAVED_SEARCHES}
    sources = {s.source for s in dmv.SAVED_SEARCHES}
    assert states == {"DC", "MD", "VA"}
    assert sources == {"govdeals", "publicsurplus"}
    # every params dict uses only keys the deals saved-search runner whitelists
    allowed = {"q", "category", "native", "state", "max_bids", "ending_within",
               "status", "min_margin", "list_id", "tag"}
    for s in dmv.SAVED_SEARCHES:
        assert set(s.params) <= allowed


def test_follow_through_names_dc_baltimore_nova_richmond():
    metros = " ".join(p.metro for p in dmv.FOLLOW_THROUGH).lower()
    for needle in ("dc", "baltimore", "virginia", "richmond"):
        assert needle in metros


# ── chair-type gate ──────────────────────────────────────────────────────────

def test_is_chair_text_by_category_and_title():
    assert dmv.is_chair_text("Random Lot", "chairs") is True
    assert dmv.is_chair_text("500 Folding Chairs, grey", None) is True
    assert dmv.is_chair_text("Office Desks and Filing Cabinets", "furniture") is False


def test_non_chair_lot_skipped():
    lot = _lot(title="Pallet of Laptops", canonical_category="electronics")
    matches, skips = filter_dmv_lots([lot])
    assert matches == []
    assert skips[0].reason == "not_chairs"


# ── geo: within 100 mi of DC ─────────────────────────────────────────────────

def test_lot_uses_own_latlng_exact_precision():
    lat, lon, prec = resolve_lot_latlon(DC_LOT)
    assert prec == "exact"
    assert 38 < lat < 39


def test_dc_area_lot_matches_within_radius():
    passed, reason = dmv_match(DC_LOT)
    assert passed is True
    assert reason["distance_miles"] < 100
    assert reason["geo_precision"] == "exact"


def test_far_chair_lot_out_of_region():
    # Atlanta chairs (~640 mi south) — chairs, but nowhere near DC and not DMV.
    lot = _lot(city="Atlanta", state="GA", zip="30301", lat=33.749, lng=-84.388)
    matches, skips = filter_dmv_lots([lot])
    assert matches == []
    assert skips[0].reason == "out_of_region"


def test_state_only_dmv_lot_passes_by_region():
    # No coords → falls to state membership; MD is DMV so it passes.
    lot = _lot(state="MD", lat=None, lng=None, zip=None)
    passed, reason = dmv_match(lot)
    assert passed is True
    assert reason["in_dmv_state"] is True


def test_state_only_non_dmv_lot_out_of_region_skips():
    # CA resolves to a state centroid (>100 mi from DC) and isn't a DMV state.
    lot = _lot(state="CA", lat=None, lng=None, zip=None)
    passed, reason = dmv_match(lot)
    assert passed is False
    assert reason["in_dmv_state"] is False


def test_unknown_state_unresolved_skips():
    # A state we can't place at all → truly unresolved, dropped.
    lot = _lot(state="ZZ", lat=None, lng=None, zip=None)
    passed, reason = dmv_match(lot)
    assert passed is False
    assert reason["geo_precision"] == "unresolved"


def test_state_centroid_overshoot_degrades_to_region_pass():
    # Western VA via state centroid is >100 mi from DC, but it's a DMV state, so
    # the honest same-region degrade keeps it rather than dropping on centroid noise.
    lot = _lot(state="VA", lat=None, lng=None, zip=None)
    passed, reason = dmv_match(lot)
    assert passed is True
    assert reason.get("same_region_pass") or reason.get("in_dmv_state")


# ── "only NEW lots" (since filter) ───────────────────────────────────────────

def test_since_drops_already_seen_lots():
    old = _lot(asset_id=1, first_seen_at=5)
    new = _lot(asset_id=2, first_seen_at=15)
    matches, skips = filter_dmv_lots([old, new], since=10)
    assert [m.lot["asset_id"] for m in matches] == [2]
    assert any(s.reason == "not_new" for s in skips)


# ── digest formatting ────────────────────────────────────────────────────────

def test_digest_lists_lot_with_govdeals_url():
    matches, _ = filter_dmv_lots([DC_LOT])
    text = format_sourcing_digest(matches)
    assert "Banquet Chairs" in text
    assert "govdeals.com/en/asset/100/7" in text
    assert "VA" in text


def test_digest_empty_when_no_matches():
    assert "no new chair lots" in format_sourcing_digest([])


def test_digest_caps_at_twenty():
    lots = [_lot(asset_id=i, title=f"chairs lot {i}") for i in range(25)]
    matches, _ = filter_dmv_lots(lots)
    text = format_sourcing_digest(matches)
    assert "+5 more" in text


# ── orchestration: dry-run touches no DB, sends nothing ──────────────────────

def test_run_dry_run_sends_nothing_and_touches_no_db(monkeypatch):
    # Any DB access must blow up — proving dry-run reads nothing when lots given.
    import automation.db as db
    for fn in ("fetch_one", "fetch_all", "execute"):
        monkeypatch.setattr(db, fn, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("DB accessed during dry-run")))

    report = run_dmv_sourcing_alert(lots=[DC_LOT])
    assert report.dry_run is True
    assert report.matched == 1
    assert report.sent is False
    assert any("DRY-RUN" in n for n in report.notes)


def test_send_enabled_without_config_stays_dry_run(monkeypatch):
    # send_enabled=True but Telegram not configured → degrades to dry-run.
    import automation.telegram_alerts as tg
    monkeypatch.setattr(tg, "is_configured", lambda: False)
    report = run_dmv_sourcing_alert(lots=[DC_LOT], send_enabled=True)
    assert report.dry_run is True
    assert report.sent is False


def test_live_send_uses_injected_sender_no_telegram():
    # Inject a fake sender (no real Telegram) — proves the live path sends the
    # digest exactly once and records the result.
    calls = []

    def fake_send(text):
        calls.append(text)
        return True, None

    report = run_dmv_sourcing_alert(
        lots=[DC_LOT], send_enabled=True, sender=fake_send
    )
    assert report.dry_run is False
    assert report.sent is True
    assert len(calls) == 1 and "govdeals.com" in calls[0]


def test_live_send_skipped_when_no_matches():
    calls = []
    report = run_dmv_sourcing_alert(
        lots=[_lot(title="Laptops", canonical_category="electronics")],
        send_enabled=True, sender=lambda t: calls.append(t) or (True, None),
    )
    assert report.matched == 0
    assert report.sent is False
    assert calls == []


def test_send_failure_recorded(monkeypatch):
    report = run_dmv_sourcing_alert(
        lots=[DC_LOT], send_enabled=True,
        sender=lambda t: (False, "http_500"),
    )
    assert report.sent is False
    assert report.error == "http_500"
    assert any("send failed" in n for n in report.notes)


def test_loader_invoked_only_when_lots_none(monkeypatch):
    # When lots is None, the DB loader is called — patched to return fixtures so
    # no real DB is hit.
    monkeypatch.setattr(digest_mod, "load_dmv_lots", lambda **k: [DC_LOT])
    report = run_dmv_sourcing_alert()
    assert report.total_lots == 1
    assert report.matched == 1
